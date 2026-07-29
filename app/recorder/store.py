"""SQLite persistence: raw telemetry frames plus session/lap/route index tables.

Writes happen only on the event-loop thread through the single `Store`
connection. API request handlers run in FastAPI's threadpool and must use
short-lived read connections from `Store.reader()` (safe under WAL; small
writes like renames are also fine there).
"""

from __future__ import annotations

import math
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id              INTEGER PRIMARY KEY,
    started_at      REAL NOT NULL,
    ended_at        REAL,
    name            TEXT,
    car_ordinal     INTEGER,
    car_class       INTEGER,
    car_pi          INTEGER,
    drivetrain_type INTEGER,
    frame_count     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS frames (
    id         INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    t          REAL NOT NULL,
    raw        BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_frames_session_t ON frames(session_id, t);
CREATE TABLE IF NOT EXISTS laps (
    id             INTEGER PRIMARY KEY,
    session_id     INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    lap_number     INTEGER NOT NULL,
    lap_time       REAL,
    started_t      REAL NOT NULL,
    ended_t        REAL,
    start_distance REAL
);
CREATE INDEX IF NOT EXISTS idx_laps_session ON laps(session_id);
CREATE TABLE IF NOT EXISTS routes (
    id         INTEGER PRIMARY KEY,
    name       TEXT,
    start_x    REAL NOT NULL,
    start_z    REAL NOT NULL,
    lap_length REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS car_names (
    ordinal INTEGER PRIMARY KEY,
    name    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS session_groups (
    id          INTEGER PRIMARY KEY,
    name        TEXT,
    route_id    INTEGER,
    car_ordinal INTEGER,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS edits (
    id         INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,
    anchor_t   REAL NOT NULL,
    value      TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_edits_session ON edits(session_id);
"""

# Manual session edits (analysis page), applied at read time - raw frames and
# the recorder's lap rows are never rewritten. Keyed by frame timestamps, not
# lap ids, so a reprocess (which deletes and recreates lap rows) keeps them:
#   dismiss_contact  anchor_t = the collision burst's peak frame t
#   flags            anchor_t inside a lap's [started_t, ended_t] span;
#                    value = the full flags CSV ("" = no flags)
#   exclude_lap      anchor_t inside a lap's span; lap drops out of bests/counts

# added after v1; applied to existing databases on startup
MIGRATIONS = (
    "ALTER TABLE sessions ADD COLUMN conditions TEXT",
    "ALTER TABLE sessions ADD COLUMN route_id INTEGER",
    "ALTER TABLE laps ADD COLUMN flags TEXT",  # "rewind,contact" etc.
    "ALTER TABLE sessions ADD COLUMN track_type TEXT",  # road/street/dirt/cross/drag
    # kept=1 exempts a session from the no-completed-laps cleanup
    # (LS_KEEP_DISCARDED captures, reprocessed sessions)
    "ALTER TABLE sessions ADD COLUMN kept INTEGER NOT NULL DEFAULT 0",
    # the lap's bounding-box dimensions (see the fingerprint note below);
    # NULL on rows recorded before the span term existed
    "ALTER TABLE routes ADD COLUMN span_x REAL",
    "ALTER TABLE routes ADD COLUMN span_z REAL",
    # does one visit produce laps, or a single run? (see ROUTE_KINDS below)
    "ALTER TABLE routes ADD COLUMN kind TEXT",       # recorder + backfill guess
    "ALTER TABLE routes ADD COLUMN kind_user TEXT",  # manual override; wins
    # several attempts at one sprint, browsed as one card (session_groups)
    "ALTER TABLE sessions ADD COLUMN group_id INTEGER",
)

# Merged runs. A point-to-point route can only be attempted by restarting the
# event, so a grind is N one-run sessions that the user wants to read - and
# score - as one thing. A group is purely an index: sessions, frames, laps and
# edits are untouched, so ungrouping is free and a reprocess of any member
# still behaves exactly as it did before.
#
# `route_id` / `car_ordinal` are pinned at creation rather than derived from
# the members, so the group keeps its identity as members are removed and
# add-validation is a column compare. Membership is validated on write only:
# a reprocess can legitimately re-fingerprint a session onto another route,
# and refusing to *read* a group the user can no longer repair would be worse
# than reporting it as mixed.

# Route shape. A Horizon sprint is point-to-point: one visit produces exactly
# one timed run, so calling it a "lap" is wrong everywhere in the UI. The
# packet says nothing about it, so the recorder infers it from which lap
# machinery fired (laps.py `_route_kind`) and stores it on the route - it's a
# property of the course, not of a session.
#
# Two columns for the same reason `laps.flags` has an `edits` overlay: `kind`
# is the machine's guess (recorder, plus a one-off backfill for routes driven
# before this existed) and `kind_user` is the user's correction. Effective
# value is COALESCE(kind_user, kind), so a wrong guess is repairable by the
# next real lap and a manual override is never in the blast radius.
ROUTE_KINDS = ("circuit", "sprint")

# Route fingerprint: same start point within this radius, a lap length within
# this fraction, and matching bounding-box dimensions = the same route.
#
# The span term carries the fingerprint. Horizon events routinely share a
# start line, and lap_length can't tell them apart: DistanceTraveled is
# normalized per route, reading ~5950 at the end of every completed route
# whatever the true driven distance (3.0 km and 6.9 km courses both report
# it). Start radius plus length alone therefore collapsed different courses
# launching from one spot onto a single row.
#
# Bounding-box dimensions hold up because they measure the ground covered,
# not the line driven - an off-track excursion or a rewind adds distance but
# barely moves the extents. Measured over 100 recorded sessions: laps of one
# course agree to within 3.5%, while different courses off a shared start
# line differ by 30% or more. The floor keeps small courses (some are only
# ~270 m across) from tripping on the tolerance alone.
# The radius is generous because the span term now backstops it. At 80 m it
# was splitting single courses in two whenever the grid moved the start a
# little: in a real database routes 9/24 (81.5 m apart) and 37/59 (102 m)
# are each one course on two rows. Scanned over every route pair in that
# database, 120 m merges exactly those two pairs and nothing else - the
# nearest genuinely different pair sits at 121 m and disagrees on span
# anyway.
ROUTE_START_RADIUS_M = 120.0
ROUTE_LENGTH_TOLERANCE = 0.05
ROUTE_SPAN_TOLERANCE = 0.15
ROUTE_SPAN_FLOOR_M = 50.0


def _spans_match(ax: float, az: float, bx: float, bz: float) -> bool:
    """Do two laps cover the same ground? Both bbox dimensions must agree."""
    return all(
        abs(a - b) <= max(ROUTE_SPAN_TOLERANCE * max(a, b), ROUTE_SPAN_FLOOR_M)
        for a, b in ((ax, bx), (az, bz))
    )

# One projection for every session read - the list, a single session, and a
# group's members. It used to be two hand-written joins that had to be kept in
# step by hand, and a column added to only one of them went missing wherever
# the other one fed. Callers append their own WHERE, then _SESSION_GROUP_BY.
_SESSION_SELECT = """
SELECT s.*, r.name AS route_name, cn.name AS car_name_override,
       COALESCE(r.kind_user, r.kind) AS route_kind, r.kind AS route_kind_auto,
       g.name AS group_name,
       COUNT(l.lap_time) AS lap_count, MIN(l.lap_time) AS best_lap
FROM sessions s
LEFT JOIN routes r ON r.id = s.route_id
LEFT JOIN car_names cn ON cn.ordinal = s.car_ordinal
LEFT JOIN session_groups g ON g.id = s.group_id
-- excluded laps (manual edit) don't count: same span-match as session_laps
LEFT JOIN laps l ON l.session_id = s.id AND NOT EXISTS
  (SELECT 1 FROM edits e WHERE e.session_id = l.session_id
   AND e.kind = 'exclude_lap' AND e.anchor_t >= l.started_t
   AND e.anchor_t <= COALESCE(l.ended_t, 1e18))
"""
_SESSION_GROUP_BY = " GROUP BY s.id"


def lap_span(lap: dict) -> tuple[float, float]:
    """A lap's frame-time span; an open lap (NULL ended_t - only ever the
    last one) extends to infinity so an anchor inside it still matches."""
    end = lap["ended_t"] if lap["ended_t"] is not None else float("1e18")
    return lap["started_t"], end


def lap_anchor(lap: dict) -> float:
    """Frame-time anchor identifying a lap across reprocesses: the midpoint
    of its span (started_t for an open lap). Midpoints avoid boundary ties
    with adjacent laps and still land inside the corresponding lap after a
    reprocess re-segments the session."""
    end = lap["ended_t"] if lap["ended_t"] is not None else lap["started_t"]
    return (lap["started_t"] + end) / 2


def _merge_edits(laps: list[dict], edits: list[dict]) -> list[dict]:
    """Apply the read-time edit overlay to lap rows. Shared by `session_laps`
    and `group_laps` so a merged group scores by exactly the same rules as
    the session it came from - an excluded lap has to stay excluded."""
    by_session: dict[int, list[dict]] = {}
    for e in edits:
        by_session.setdefault(e["session_id"], []).append(e)
    for lap in laps:
        lap["flags_auto"] = lap["flags"]
        lap["excluded"] = False
        t0, t1 = lap_span(lap)
        for e in by_session.get(lap["session_id"], ()):
            if not t0 <= e["anchor_t"] <= t1:
                continue
            if e["kind"] == "flags":
                lap["flags"] = e["value"] or None
            elif e["kind"] == "exclude_lap":
                lap["excluded"] = True
    return laps


class Store:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(db_path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        for stmt in MIGRATIONS:
            try:
                self.db.execute(stmt)
            except sqlite3.OperationalError:
                pass  # column already exists
        self.db.commit()
        self.backfill_route_kinds()
        # session ids must never be reused: discarding a session deletes the
        # max rowid, which plain INTEGER PRIMARY KEY would hand out again -
        # and the live dashboard detects "new event" by the id changing
        self._next_session_id = self.db.execute(
            "SELECT COALESCE(MAX(id), 0) + 1 FROM sessions").fetchone()[0]

    def backfill_route_kinds(self) -> int:
        """Classify routes driven before `kind` existed, from evidence already
        in the index tables - no frame reads, so the size of `frames` doesn't
        matter. Idempotent (NULL rows only), so it belongs in __init__ rather
        than the lifespan or a tools script: the packaged Windows exe has no
        shell step, and without this every pre-existing route would stay NULL
        forever and read as "lap".

        A route that ever produced more than one timed lap, or that carries a
        World Time Attack tag, is a circuit; anything else only ever produced
        single runs. Routes with no sessions left (their captures were
        deleted) stay NULL rather than get a fabricated guess."""
        if not self.db.execute(
                "SELECT 1 FROM routes WHERE kind IS NULL LIMIT 1").fetchone():
            return 0
        cur = self.db.execute("""
            UPDATE routes SET kind = (
                SELECT CASE
                    WHEN MAX(s.track_type = 'wtc') = 1 THEN 'circuit'
                    WHEN MAX(COALESCE(n.c, 0)) > 1     THEN 'circuit'
                    ELSE 'sprint' END
                FROM sessions s
                LEFT JOIN (SELECT session_id, COUNT(*) c FROM laps
                           WHERE lap_time IS NOT NULL GROUP BY session_id) n
                  ON n.session_id = s.id
                WHERE s.route_id = routes.id)
            WHERE kind IS NULL
              AND EXISTS (SELECT 1 FROM sessions WHERE route_id = routes.id)
        """)
        self.db.commit()
        return cur.rowcount

    def close(self) -> None:
        self.db.commit()
        self.db.close()

    # -- writes (event-loop thread only) ------------------------------------

    def cleanup_sessions(self) -> int:
        """Startup pass: close crashed sessions, drop those without a single
        completed lap (free-roam cruising, menu blips)."""
        self.db.execute(
            "UPDATE sessions SET ended_at ="
            " (SELECT MAX(t) FROM frames WHERE frames.session_id = sessions.id)"
            " WHERE ended_at IS NULL"
        )
        cur = self.db.execute(
            "DELETE FROM sessions WHERE id NOT IN"
            " (SELECT DISTINCT session_id FROM laps WHERE lap_time IS NOT NULL)"
            " AND COALESCE(kept, 0) = 0"
        )
        self.db.commit()
        self.prune_empty_groups()
        return cur.rowcount

    def create_session(self, started_at: float, frame: dict) -> int:
        sid = self._next_session_id
        self._next_session_id += 1
        self.db.execute(
            "INSERT INTO sessions (id, started_at, car_ordinal, car_class, car_pi,"
            " drivetrain_type) VALUES (?, ?, ?, ?, ?, ?)",
            (sid, started_at, frame["car_ordinal"], frame["car_class"],
             frame["car_pi"], frame["drivetrain_type"]),
        )
        self.db.commit()
        return sid

    def end_session(self, session_id: int, ended_at: float, frame_count: int,
                    conditions: str | None = None,
                    track_type: str | None = None) -> None:
        # COALESCE: auto-detected tags (wet, suggested track type) never
        # overwrite a value the user already set
        self.db.execute(
            "UPDATE sessions SET ended_at = ?, frame_count = ?,"
            " conditions = COALESCE(conditions, ?),"
            " track_type = COALESCE(track_type, ?) WHERE id = ?",
            (ended_at, frame_count, conditions, track_type, session_id),
        )
        self.db.commit()

    def auto_tag_session(self, session_id: int, conditions: str | None,
                         track_type: str | None) -> None:
        """Fill auto-detected tags without touching anything else (replays
        use this - the session row's timing must stay untouched); COALESCE
        keeps whatever the user already set."""
        self.db.execute(
            "UPDATE sessions SET conditions = COALESCE(conditions, ?),"
            " track_type = COALESCE(track_type, ?) WHERE id = ?",
            (conditions, track_type, session_id),
        )
        self.db.commit()

    def route_track_type(self, route_id: int,
                         exclude_session_id: int | None = None) -> str | None:
        """Latest track type any other session on this route carries (manual
        or auto). Event-loop connection: the tracker asks at session close."""
        row = self.db.execute(
            "SELECT track_type FROM sessions WHERE route_id = ?"
            " AND track_type IS NOT NULL AND id != ?"
            " ORDER BY started_at DESC LIMIT 1",
            (route_id,
             exclude_session_id if exclude_session_id is not None else -1),
        ).fetchone()
        return row[0] if row else None

    def discard_session(self, session_id: int) -> None:
        """Delete a just-ended session that produced no completed laps."""
        self.db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        self.db.commit()

    def add_frames(self, session_id: int, frames: list[tuple[float, bytes]]) -> None:
        self.db.executemany(
            "INSERT INTO frames (session_id, t, raw) VALUES (?, ?, ?)",
            ((session_id, t, raw) for t, raw in frames),
        )
        self.db.commit()

    def add_lap(self, session_id: int, lap_number: int, started_t: float,
                start_distance: float) -> int:
        cur = self.db.execute(
            "INSERT INTO laps (session_id, lap_number, started_t, start_distance)"
            " VALUES (?, ?, ?, ?)",
            (session_id, lap_number, started_t, start_distance),
        )
        self.db.commit()
        return cur.lastrowid

    def restart_lap(self, lap_id: int, started_t: float, start_distance: float) -> None:
        """Re-anchor an open lap to a later start (free-roam timer start)."""
        self.db.execute(
            "UPDATE laps SET started_t = ?, start_distance = ? WHERE id = ?",
            (started_t, start_distance, lap_id),
        )
        self.db.commit()

    def complete_lap(self, lap_id: int, ended_t: float, lap_time: float | None,
                     flags: str | None = None) -> None:
        self.db.execute(
            "UPDATE laps SET ended_t = ?, lap_time = ?, flags = ? WHERE id = ?",
            (ended_t, lap_time, flags, lap_id),
        )
        self.db.commit()

    def delete_lap(self, lap_id: int) -> None:
        """Drop an open lap that turned out not to be one (post-finish coast)."""
        self.db.execute("DELETE FROM laps WHERE id = ?", (lap_id,))
        self.db.commit()

    def delete_session_laps(self, session_id: int) -> None:
        self.db.execute("DELETE FROM laps WHERE session_id = ?", (session_id,))
        self.db.commit()

    def mark_session_kept(self, session_id: int) -> None:
        """Exempt from the no-completed-laps cleanup at startup."""
        self.db.execute("UPDATE sessions SET kept = 1 WHERE id = ?", (session_id,))
        self.db.commit()

    def match_or_create_route(self, start_x: float, start_z: float,
                              lap_length: float, span_x: float,
                              span_z: float, kind: str | None = None) -> int:
        for rid, rx, rz, rlen, rsx, rsz in self.db.execute(
                "SELECT id, start_x, start_z, lap_length, span_x, span_z FROM routes"):
            if (math.hypot(start_x - rx, start_z - rz) > ROUTE_START_RADIUS_M
                    or abs(lap_length - rlen) > ROUTE_LENGTH_TOLERANCE * rlen):
                continue
            if rsx is None:
                # recorded before the span term existed: adopt this lap's
                # shape, so the next course off this start line splits off
                # instead of collapsing onto the row. Reprocessing the
                # sessions of an already-collapsed route is what unpicks it.
                self.db.execute(
                    "UPDATE routes SET span_x = ?, span_z = ? WHERE id = ?",
                    (span_x, span_z, rid))
                self.db.commit()
                self.set_route_kind(rid, kind)
                return rid
            if _spans_match(span_x, span_z, rsx, rsz):
                self.set_route_kind(rid, kind)
                return rid
        cur = self.db.execute(
            "INSERT INTO routes (start_x, start_z, lap_length, span_x, span_z, kind)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (start_x, start_z, lap_length, span_x, span_z, kind),
        )
        self.db.commit()
        return cur.lastrowid

    def set_route_kind(self, route_id: int, kind: str | None) -> None:
        """Recorder write. Circuit evidence is strong and sprint evidence is
        weak: a LapNumber increment or a geometric loop closure can only
        happen on a circuit, while "only ever produced one timed run" is also
        what a single-lap circuit race looks like. So NULL -> anything and
        sprint -> circuit, never circuit -> sprint. Event-loop connection."""
        if kind is None:
            return
        self.db.execute(
            "UPDATE routes SET kind = ? WHERE id = ?"
            " AND (kind IS NULL OR (kind = 'sprint' AND ? = 'circuit'))",
            (kind, route_id, kind))
        self.db.commit()

    def set_session_route(self, session_id: int, route_id: int) -> None:
        self.db.execute("UPDATE sessions SET route_id = ? WHERE id = ?",
                        (route_id, session_id))
        self.db.commit()

    # -- reads / small writes (any thread; short-lived connection) -----------

    @contextmanager
    def reader(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def list_sessions(self) -> list[dict]:
        with self.reader() as conn:
            rows = conn.execute(
                _SESSION_SELECT + _SESSION_GROUP_BY
                + " ORDER BY s.started_at DESC").fetchall()
        return [dict(r) for r in rows]

    def get_session(self, session_id: int) -> dict | None:
        with self.reader() as conn:
            row = conn.execute(
                _SESSION_SELECT + " WHERE s.id = ?" + _SESSION_GROUP_BY,
                (session_id,)).fetchone()
        return dict(row) if row else None

    def rename_session(self, session_id: int, name: str | None) -> None:
        with self.reader() as conn:
            conn.execute("UPDATE sessions SET name = ? WHERE id = ?", (name, session_id))
            conn.commit()

    def set_session_conditions(self, session_id: int, conditions: str | None) -> None:
        with self.reader() as conn:
            conn.execute("UPDATE sessions SET conditions = ? WHERE id = ?",
                         (conditions, session_id))
            conn.commit()

    def set_session_track_type(self, session_id: int, track_type: str | None) -> None:
        with self.reader() as conn:
            conn.execute("UPDATE sessions SET track_type = ? WHERE id = ?",
                         (track_type, session_id))
            conn.commit()

    def rename_route(self, route_id: int, name: str) -> bool:
        with self.reader() as conn:
            cur = conn.execute("UPDATE routes SET name = ? WHERE id = ?", (name, route_id))
            conn.commit()
            return cur.rowcount > 0

    def set_route_kind_user(self, route_id: int, kind: str | None) -> None:
        """Manual override from the analysis page; None clears it so the
        recorder's own value shows again. Unconditional - the whole point is
        that the user outranks the guess."""
        with self.reader() as conn:
            conn.execute("UPDATE routes SET kind_user = ? WHERE id = ?",
                         (kind, route_id))
            conn.commit()

    def route_exists(self, route_id: int) -> bool:
        with self.reader() as conn:
            return conn.execute("SELECT 1 FROM routes WHERE id = ?",
                                (route_id,)).fetchone() is not None

    def set_route_sessions_track_type(self, route_id: int,
                                      track_type: str | None) -> int:
        """Retag every session on a route at once (the analysis page's
        "apply to all sessions on this route?" prompt). Overwrites existing
        tags on purpose - the user just confirmed exactly that. Returns the
        number of sessions updated."""
        with self.reader() as conn:
            cur = conn.execute(
                "UPDATE sessions SET track_type = ? WHERE route_id = ?",
                (track_type, route_id))
            conn.commit()
            return cur.rowcount

    def set_car_name(self, ordinal: int, name: str) -> None:
        with self.reader() as conn:
            conn.execute(
                "INSERT INTO car_names (ordinal, name) VALUES (?, ?)"
                " ON CONFLICT(ordinal) DO UPDATE SET name = excluded.name",
                (ordinal, name),
            )
            conn.commit()

    def clear_car_name(self, ordinal: int) -> None:
        with self.reader() as conn:
            conn.execute("DELETE FROM car_names WHERE ordinal = ?", (ordinal,))
            conn.commit()

    def get_car_override(self, ordinal: int) -> str | None:
        with self.reader() as conn:
            row = conn.execute("SELECT name FROM car_names WHERE ordinal = ?",
                               (ordinal,)).fetchone()
        return row["name"] if row else None

    def delete_session(self, session_id: int) -> None:
        with self.reader() as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            conn.commit()
        self.prune_empty_groups()  # it may have been a group's last member

    def session_laps(self, session_id: int) -> list[dict]:
        """Lap rows with manual edits applied at read time: `flags` is the
        effective value (a 'flags' override wins over what the recorder
        detected, which stays in `flags_auto`), `excluded` marks laps the
        user dropped from bests/counts."""
        with self.reader() as conn:
            rows = conn.execute(
                "SELECT * FROM laps WHERE session_id = ? ORDER BY started_t", (session_id,)
            ).fetchall()
        return _merge_edits([dict(r) for r in rows],
                            self.session_edits(session_id))

    # -- merged run groups ---------------------------------------------------

    def create_group(self, name: str | None, route_id: int, car_ordinal: int,
                     session_ids: list[int]) -> int:
        with self.reader() as conn:
            cur = conn.execute(
                "INSERT INTO session_groups (name, route_id, car_ordinal, created_at)"
                " VALUES (?, ?, ?, ?)", (name, route_id, car_ordinal, time.time()))
            gid = cur.lastrowid
            conn.executemany("UPDATE sessions SET group_id = ? WHERE id = ?",
                             [(gid, sid) for sid in session_ids])
            conn.commit()
        return gid

    def get_group(self, group_id: int) -> dict | None:
        with self.reader() as conn:
            row = conn.execute("SELECT * FROM session_groups WHERE id = ?",
                               (group_id,)).fetchone()
        return dict(row) if row else None

    def group_sessions(self, group_id: int) -> list[dict]:
        """Members, oldest first - the order runs are numbered in."""
        with self.reader() as conn:
            rows = conn.execute(
                _SESSION_SELECT + " WHERE s.group_id = ?" + _SESSION_GROUP_BY
                + " ORDER BY s.started_at", (group_id,)).fetchall()
        return [dict(r) for r in rows]

    def group_laps(self, group_id: int) -> list[dict]:
        """Every member's laps as one list, oldest first, with the same
        read-time edit overlay `session_laps` applies. One connection for the
        whole group rather than two per member."""
        with self.reader() as conn:
            ids = [r["id"] for r in conn.execute(
                "SELECT id FROM sessions WHERE group_id = ?", (group_id,))]
            if not ids:
                return []
            marks = ",".join("?" * len(ids))
            laps = [dict(r) for r in conn.execute(
                f"SELECT * FROM laps WHERE session_id IN ({marks})"
                " ORDER BY started_t", ids)]
            edits = [dict(r) for r in conn.execute(
                f"SELECT * FROM edits WHERE session_id IN ({marks})", ids)]
        return _merge_edits(laps, edits)

    def set_group_name(self, group_id: int, name: str | None) -> None:
        with self.reader() as conn:
            conn.execute("UPDATE session_groups SET name = ? WHERE id = ?",
                         (name, group_id))
            conn.commit()

    def set_session_group(self, session_id: int, group_id: int | None) -> None:
        with self.reader() as conn:
            conn.execute("UPDATE sessions SET group_id = ? WHERE id = ?",
                         (group_id, session_id))
            conn.commit()

    def delete_group(self, group_id: int) -> None:
        """Ungroup: the members survive, only the index goes. Explicitly two
        statements in one transaction - `reader()` doesn't enable foreign
        keys, so an ON DELETE SET NULL here would silently leave dangling
        group_ids behind."""
        with self.reader() as conn:
            conn.execute("UPDATE sessions SET group_id = NULL WHERE group_id = ?",
                         (group_id,))
            conn.execute("DELETE FROM session_groups WHERE id = ?", (group_id,))
            conn.commit()

    def prune_empty_groups(self) -> int:
        """Drop groups whose last member was deleted."""
        with self.reader() as conn:
            cur = conn.execute(
                "DELETE FROM session_groups WHERE id NOT IN"
                " (SELECT group_id FROM sessions WHERE group_id IS NOT NULL)")
            conn.commit()
        return cur.rowcount

    def session_edits(self, session_id: int) -> list[dict]:
        with self.reader() as conn:
            rows = conn.execute(
                "SELECT * FROM edits WHERE session_id = ? ORDER BY created_at",
                (session_id,)).fetchall()
        return [dict(r) for r in rows]

    def add_edit(self, session_id: int, kind: str, anchor_t: float,
                 value: str | None = None) -> None:
        with self.reader() as conn:
            conn.execute(
                "INSERT INTO edits (session_id, kind, anchor_t, value, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (session_id, kind, anchor_t, value, time.time()))
            conn.commit()

    def remove_edits(self, session_id: int, kind: str, t0: float, t1: float) -> int:
        """Drop edits of one kind anchored inside [t0, t1] (a lap's span)."""
        with self.reader() as conn:
            cur = conn.execute(
                "DELETE FROM edits WHERE session_id = ? AND kind = ?"
                " AND anchor_t >= ? AND anchor_t <= ?",
                (session_id, kind, t0, t1))
            conn.commit()
            return cur.rowcount

    def clear_edits(self, session_id: int) -> int:
        """The "Reset edits" escape hatch: drop every manual edit at once."""
        with self.reader() as conn:
            cur = conn.execute("DELETE FROM edits WHERE session_id = ?", (session_id,))
            conn.commit()
            return cur.rowcount

    def get_lap(self, lap_id: int) -> dict | None:
        with self.reader() as conn:
            row = conn.execute("SELECT * FROM laps WHERE id = ?", (lap_id,)).fetchone()
        return dict(row) if row else None

    def session_frames(self, session_id: int) -> list[tuple[float, bytes]]:
        with self.reader() as conn:
            rows = conn.execute(
                "SELECT t, raw FROM frames WHERE session_id = ? ORDER BY t",
                (session_id,)).fetchall()
        return [(r["t"], r["raw"]) for r in rows]

    def lap_frames(self, lap: dict) -> list[tuple[float, bytes]]:
        end = lap["ended_t"] if lap["ended_t"] is not None else float("1e18")
        with self.reader() as conn:
            rows = conn.execute(
                "SELECT t, raw FROM frames WHERE session_id = ? AND t >= ? AND t <= ? ORDER BY t",
                (lap["session_id"], lap["started_t"], end),
            ).fetchall()
        return [(r["t"], r["raw"]) for r in rows]
