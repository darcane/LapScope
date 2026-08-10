"""SQLite persistence: raw telemetry frames plus session/lap/route index tables.

Writes happen only on the event-loop thread through the single `Store`
connection. API request handlers run in FastAPI's threadpool and must use
short-lived read connections from `Store.reader()` (safe under WAL; small
writes like renames are also fine there).
"""

from __future__ import annotations

import logging
import math
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger("lapscope.store")

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

# Stamped into `PRAGMA user_version` once SCHEMA + MIGRATIONS have run, so a
# database can always say which shape it is without probing table_info. It
# costs nothing today and is the only way a future migration can tell a v1.0
# database from a v0.8 one; every DB written before this existed reads 0.
# Bump it when a change can't be expressed as an idempotent ADD COLUMN.
SCHEMA_VERSION = 1

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
    # the course drawn as a thumbnail (see ROUTE_OUTLINE_* below)
    "ALTER TABLE routes ADD COLUMN outline TEXT",
    # the catalogue entry that supplied this row's name (app/tracks.py);
    # NULL = named by the user, or not named at all
    "ALTER TABLE routes ADD COLUMN catalog_key TEXT",
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

# Route outline: the course drawn as a thumbnail, so a route is recognizable
# by its shape and not only by a name someone remembered to type. Deliberately
# NOT called "shape" anywhere user-facing - that word already means
# circuit-vs-sprint in the route dialog.
#
# Derived, cached, and cheap to lose: a flattened [x0, y0, x1, y1, ...] JSON
# list of integers in a 0..OUTLINE_BOX box, longest axis scaled to fit, y
# already flipped into screen space so a client can drop it straight into an
# SVG viewBox. Points are spaced along the driven line rather than by frame -
# a lap that starts with the car sitting on the grid would otherwise spend a
# tenth of its points on one spot and cut the corners where it was quick.
# DETAIL is that spacing as a fraction of the bounding box, so the point
# count follows how much the course wanders (2-4x DETAIL in practice).
#
# Filled lazily on first request (one lap's frames, tens of milliseconds)
# rather than by the recorder: the outline only matters once someone opens
# the browse bar, and a route driven before this existed has to be filled
# from stored frames anyway.
ROUTE_OUTLINE_BOX = 1000
ROUTE_OUTLINE_DETAIL = 150

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
#
# The same three terms also identify a course against the shipped catalogue of
# official routes (app/tracks.py), which is how a route gets a name without the
# user typing one: the packet never says where you are, but "this fingerprint
# is The Goliath" is knowledge that transfers between installs.
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
-- excluded laps (manual edit) don't count: same half-open span-match as
-- session_laps, so a session's lap_count agrees with the laps it lists
LEFT JOIN laps l ON l.session_id = s.id AND NOT EXISTS
  (SELECT 1 FROM edits e WHERE e.session_id = l.session_id
   AND e.kind = 'exclude_lap' AND e.anchor_t >= l.started_t
   AND e.anchor_t < COALESCE(l.ended_t, 1e18))
"""
_SESSION_GROUP_BY = " GROUP BY s.id"


def lap_span(lap: dict) -> tuple[float, float]:
    """A lap's frame-time span, HALF-OPEN: `t0 <= anchor < t1`. An open lap
    (NULL ended_t - only ever the last one) extends to infinity so an anchor
    inside it still matches.

    Half-open because consecutive laps share a frame: a lap's ended_t and the
    next lap's started_t are written from the same `t` (laps.py). A closed
    span made an anchor on that boundary belong to both laps, and excluding
    one silently excluded its neighbour (issue #62). Every place that matches
    an anchor against a lap has to use the same rule - `_merge_edits`, the
    `_SESSION_SELECT` overlay, `remove_edits`, and the flags lookup in the
    API - or a lap's count and its own view of itself disagree."""
    end = lap["ended_t"] if lap["ended_t"] is not None else float("1e18")
    return lap["started_t"], end


def lap_anchor(lap: dict) -> float:
    """Frame-time anchor identifying a lap across reprocesses: the midpoint
    of its span (started_t for an open lap, whose end isn't known yet).
    Midpoints avoid boundary ties with adjacent laps and still land inside the
    corresponding lap after a reprocess re-segments the session. An open lap's
    anchor sits exactly on the shared boundary, which only resolves to the
    right lap because the span above is half-open."""
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
            if not t0 <= e["anchor_t"] < t1:  # half-open: see lap_span
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
        self._in_transaction = False
        self.db = sqlite3.connect(db_path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        was = self.db.execute("PRAGMA user_version").fetchone()[0]
        if was > SCHEMA_VERSION:
            # a newer LapScope wrote this file; we can still read it (every
            # change so far is an added column), but say so rather than
            # silently stamping it back down to our own version
            log.warning("%s was written by a newer LapScope (schema v%d, this "
                        "build understands v%d)", db_path, was, SCHEMA_VERSION)
        for stmt in MIGRATIONS:
            try:
                self.db.execute(stmt)
            except sqlite3.OperationalError as exc:
                # ONLY "column already exists" is expected here. A blanket pass
                # also swallows "database is locked" / "disk I/O error" /
                # "readonly database", which no-ops every ALTER and leaves the
                # app half-migrated - it then dies further along on a missing
                # column, an error that says nothing about the real cause.
                if "duplicate column" not in str(exc).lower():
                    raise
        if was < SCHEMA_VERSION:  # never lower a newer build's stamp
            self.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION:d}")
        self.db.commit()
        self.backfill_route_kinds()
        self.backfill_route_names()
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
        self._commit()
        return cur.rowcount

    def close(self) -> None:
        self.db.commit()
        self.db.close()

    # -- writes (event-loop thread only) ------------------------------------

    def _commit(self) -> None:
        """Every write method commits through this. Inside `transaction()` it
        does nothing, so the caller decides when the batch becomes visible."""
        if not self._in_transaction:
            self.db.commit()

    @contextmanager
    def transaction(self):
        """Make a batch of writes all-or-nothing: on any exception the whole
        batch rolls back, including whatever ran before the failing statement.

        Reprocess is why this exists. It deletes a session's laps and rebuilds
        them from the frames; with each write committing on its own, a replay
        that raised halfway left the session with its lap times deleted and
        only a partial rebuild - permanently, since a crash on given frames
        repeats on every retry.

        Only the event-loop connection is grouped. Methods that write through
        `reader()` open their own connection and commit independently, and a
        read through `reader()` inside a transaction sees the *old* data, so
        do neither in here. Not reentrant: SQLite has no nested transactions,
        and a savepoint would only make the rollback look stronger than it is.
        """
        if self._in_transaction:
            raise RuntimeError("Store.transaction() does not nest")
        self._in_transaction = True
        try:
            yield
        except BaseException:
            self.db.rollback()
            raise
        else:
            self.db.commit()
        finally:
            self._in_transaction = False

    def cleanup_sessions(self) -> int:
        """Startup pass: close crashed sessions, drop those without a single
        completed lap (free-roam cruising, menu blips).

        Safe to run only before recording starts - it closes every open row it
        finds, and a live session's are open on purpose."""
        self.db.execute(
            "UPDATE sessions SET ended_at ="
            " (SELECT MAX(t) FROM frames WHERE frames.session_id = sessions.id)"
            " WHERE ended_at IS NULL"
        )
        # ...and the lap the crash caught mid-flight, which used to stay open
        # forever. An open lap's span has no end, so its edit anchor collapses
        # onto the previous lap's last frame and excluding it excluded that one
        # too (issue #62). lap_time stays NULL: it never crossed the line.
        # The `started_t <` guard keeps the span non-empty - a lap whose only
        # frame is the last one would get ended_t == started_t, and a half-open
        # [t, t) can never be pointed at, so it could never be edited at all.
        self.db.execute(
            "UPDATE laps SET ended_t ="
            " (SELECT MAX(t) FROM frames WHERE frames.session_id = laps.session_id)"
            " WHERE ended_t IS NULL AND started_t <"
            " (SELECT MAX(t) FROM frames WHERE frames.session_id = laps.session_id)"
        )
        cur = self.db.execute(
            "DELETE FROM sessions WHERE id NOT IN"
            " (SELECT DISTINCT session_id FROM laps WHERE lap_time IS NOT NULL)"
            " AND COALESCE(kept, 0) = 0"
        )
        self._commit()
        self.prune_empty_groups()
        return cur.rowcount

    def _disk_bytes(self) -> int:
        """What the database costs on disk: the file plus its WAL sidecars,
        because a checkpoint that hasn't happened yet is still your space."""
        total = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                total += Path(self.db_path + suffix).stat().st_size
            except OSError:  # -wal/-shm only exist while a connection is open
                pass
        return total

    def storage_stats(self) -> dict:
        """How much disk the recordings hold, and how much of it is already
        dead weight. `free_bytes` is the freelist: pages that deleting a
        session released *for reuse* but that the file never hands back on
        its own - SQLite only shrinks on VACUUM, and turning on auto_vacuum
        now would do nothing for a database whose tables already exist."""
        with self.reader() as conn:
            page_size = conn.execute("PRAGMA page_size").fetchone()[0]
            free_pages = conn.execute("PRAGMA freelist_count").fetchone()[0]
            sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        return {"db_bytes": self._disk_bytes(),
                "free_bytes": page_size * free_pages,
                "sessions": sessions}

    def vacuum(self) -> dict:
        """Rebuild the file so freed pages leave the disk, and report what
        that gave back.

        Event-loop connection only - VACUUM takes an exclusive lock for the
        whole rebuild, so it must never race the recorder's own writes (the
        API handler is `async def` for exactly this reason, same rule as
        reprocess). The truncating checkpoint afterwards is what makes the
        number honest: in WAL mode the rebuild lands in the -wal file first,
        so measuring without it would report a saving that is still on disk."""
        before = self._disk_bytes()
        self.db.commit()  # never deferred: VACUUM can't run inside a transaction
        self.db.execute("VACUUM")
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        after = self._disk_bytes()
        return {"before_bytes": before, "after_bytes": after,
                "reclaimed_bytes": max(0, before - after)}

    def create_session(self, started_at: float, frame: dict) -> int:
        sid = self._next_session_id
        self._next_session_id += 1
        self.db.execute(
            "INSERT INTO sessions (id, started_at, car_ordinal, car_class, car_pi,"
            " drivetrain_type) VALUES (?, ?, ?, ?, ?, ?)",
            (sid, started_at, frame["car_ordinal"], frame["car_class"],
             frame["car_pi"], frame["drivetrain_type"]),
        )
        self._commit()
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
        self._commit()

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
        self._commit()

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
        self._commit()

    def add_frames(self, session_id: int, frames: list[tuple[float, bytes]]) -> None:
        try:
            self.db.executemany(
                "INSERT INTO frames (session_id, t, raw) VALUES (?, ?, ?)",
                ((session_id, t, raw) for t, raw in frames),
            )
        except Exception:
            # the tracker retries the same buffer (issue #64), so a half
            # applied batch must not survive: the next write's commit would
            # otherwise carry it along and duplicate the re-inserted frames.
            # Inside transaction() the batch is already doomed - leave the
            # rollback to it, which is the only thing that may end one.
            if not self._in_transaction:
                self.db.rollback()
            raise
        self._commit()

    def add_lap(self, session_id: int, lap_number: int, started_t: float,
                start_distance: float) -> int:
        cur = self.db.execute(
            "INSERT INTO laps (session_id, lap_number, started_t, start_distance)"
            " VALUES (?, ?, ?, ?)",
            (session_id, lap_number, started_t, start_distance),
        )
        self._commit()
        return cur.lastrowid

    def restart_lap(self, lap_id: int, started_t: float, start_distance: float) -> None:
        """Re-anchor an open lap to a later start (free-roam timer start)."""
        self.db.execute(
            "UPDATE laps SET started_t = ?, start_distance = ? WHERE id = ?",
            (started_t, start_distance, lap_id),
        )
        self._commit()

    def complete_lap(self, lap_id: int, ended_t: float, lap_time: float | None,
                     flags: str | None = None) -> None:
        self.db.execute(
            "UPDATE laps SET ended_t = ?, lap_time = ?, flags = ? WHERE id = ?",
            (ended_t, lap_time, flags, lap_id),
        )
        self._commit()

    def delete_lap(self, lap_id: int) -> None:
        """Drop an open lap that turned out not to be one (post-finish coast)."""
        self.db.execute("DELETE FROM laps WHERE id = ?", (lap_id,))
        self._commit()

    def delete_session_laps(self, session_id: int) -> None:
        self.db.execute("DELETE FROM laps WHERE session_id = ?", (session_id,))
        self._commit()

    def mark_session_kept(self, session_id: int) -> None:
        """Exempt from the no-completed-laps cleanup at startup."""
        self.db.execute("UPDATE sessions SET kept = 1 WHERE id = ?", (session_id,))
        self._commit()

    def match_or_create_route(self, start_x: float, start_z: float,
                              lap_length: float, span_x: float,
                              span_z: float, kind: str | None = None) -> int:
        route_id = None
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
                self._commit()
                route_id = rid
                break
            if _spans_match(span_x, span_z, rsx, rsz):
                route_id = rid
                break
        if route_id is None:
            cur = self.db.execute(
                "INSERT INTO routes (start_x, start_z, lap_length, span_x, span_z, kind)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (start_x, start_z, lap_length, span_x, span_z, kind),
            )
            self._commit()
            route_id = cur.lastrowid
        else:
            self.set_route_kind(route_id, kind)
        # Single exit so this runs on every path: a brand-new row, and equally
        # a legacy row that only just adopted a shape, may be identifiable as
        # an official course now when it wasn't a moment ago.
        self._name_from_catalog(route_id, start_x, start_z, lap_length,
                                span_x, span_z)
        return route_id

    def _name_from_catalog(self, route_id: int, start_x: float, start_z: float,
                           lap_length: float, span_x: float,
                           span_z: float) -> str | None:
        """Give a route the official name of the course it fingerprints as,
        and return that name if it was applied.

        Guarded on the name being empty, so this is free to run on every lap
        close and can never overwrite something the user typed. `rename_route`
        clears `catalog_key` in the same spirit: once a human has had an
        opinion about a route's name, later catalogue updates leave it alone.

        The catalogue's shape goes through `set_route_kind`, so it can promote
        the recorder's guess but never downgrade it, and `kind_user` - the
        user's own override - is never touched.

        `tracks` is imported here rather than at module scope because it
        imports this module's fingerprint tolerances; keeping the import
        inside the call leaves that a one-way edge."""
        from .. import tracks
        entry = tracks.match(start_x, start_z, lap_length, span_x, span_z)
        if entry is None:
            return None
        cur = self.db.execute(
            "UPDATE routes SET name = ?, catalog_key = ? WHERE id = ?"
            " AND (name IS NULL OR TRIM(name) = '')",
            (entry["name"], entry["key"], route_id))
        self._commit()
        if not cur.rowcount:
            return None
        self.set_route_kind(route_id, entry["kind"])
        return entry["name"]

    def backfill_route_names(self) -> int:
        """Name routes the catalogue can identify, and return how many names
        changed. Two passes, both idempotent:

        - routes with no name yet. Covers a database recorded before the
          catalogue existed, and equally a catalogue that has since learned
          about a course the user drove last week. A route with no span can't
          be fingerprinted at all; reprocessing one of its sessions is what
          gives it one.
        - names this catalogue supplied that it has since corrected (a typo,
          a re-spelling). Rows the user renamed have `catalog_key` cleared, so
          they are invisible to this pass.

        Called from __init__ for the same reason as `backfill_route_kinds`:
        the packaged Windows exe has no shell step, so a pass that has to run
        once per install has to run itself. Also called after a catalogue
        refresh, which is what makes new tracks land without a restart."""
        from .. import tracks
        changed = 0
        for rid, sx, sz, rlen, spx, spz in self.db.execute(
                "SELECT id, start_x, start_z, lap_length, span_x, span_z FROM routes"
                " WHERE (name IS NULL OR TRIM(name) = '') AND span_x IS NOT NULL"
        ).fetchall():
            if self._name_from_catalog(rid, sx, sz, rlen, spx, spz):
                changed += 1
        for rid, name, key in self.db.execute(
                "SELECT id, name, catalog_key FROM routes"
                " WHERE catalog_key IS NOT NULL").fetchall():
            entry = tracks.TRACKS.get(key)
            if entry is not None and entry["name"] != name:
                self.db.execute("UPDATE routes SET name = ? WHERE id = ?",
                                (entry["name"], rid))
                changed += 1
        if changed:
            self._commit()
        return changed

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
        self._commit()

    def set_session_route(self, session_id: int, route_id: int) -> None:
        self.db.execute("UPDATE sessions SET route_id = ? WHERE id = ?",
                        (route_id, session_id))
        self._commit()

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
        """Clearing `catalog_key` is the point of the second column: a name the
        user typed outranks the shipped catalogue permanently, so a later
        catalogue update can't quietly undo their correction."""
        with self.reader() as conn:
            cur = conn.execute(
                "UPDATE routes SET name = ?, catalog_key = NULL WHERE id = ?",
                (name, route_id))
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

    def get_route(self, route_id: int) -> dict | None:
        with self.reader() as conn:
            row = conn.execute("SELECT * FROM routes WHERE id = ?",
                               (route_id,)).fetchone()
        return dict(row) if row else None

    def route_outline_lap(self, route_id: int) -> dict | None:
        """The lap an outline is drawn from: the fastest completed lap anyone
        has recorded on the route, which is also the cleanest line on it."""
        with self.reader() as conn:
            row = conn.execute(
                "SELECT l.* FROM laps l JOIN sessions s ON s.id = l.session_id"
                " WHERE s.route_id = ? AND l.lap_time IS NOT NULL"
                " ORDER BY l.lap_time LIMIT 1", (route_id,)).fetchone()
        return dict(row) if row else None

    def set_route_outline(self, route_id: int, outline: str) -> None:
        with self.reader() as conn:
            conn.execute("UPDATE routes SET outline = ? WHERE id = ?",
                         (outline, route_id))
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

    def remove_session_from_group(self, session_id: int, group_id: int) -> bool:
        """Take a session out of one specific group; False if it wasn't in it.

        The membership is part of the WHERE on purpose. Clearing group_id
        without checking meant a request naming group A could ungroup a
        session belonging to group B - and then prune B as newly empty
        (issue #63)."""
        with self.reader() as conn:
            cur = conn.execute(
                "UPDATE sessions SET group_id = NULL"
                " WHERE id = ? AND group_id = ?", (session_id, group_id))
            conn.commit()
        return cur.rowcount > 0

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
        """Drop edits of one kind anchored inside [t0, t1) - a lap's span, and
        half-open for the same reason it is everywhere else (see `lap_span`):
        with both ends inclusive, clearing one lap's override also cleared the
        next lap's, whose anchor can sit exactly on t1."""
        with self.reader() as conn:
            cur = conn.execute(
                "DELETE FROM edits WHERE session_id = ? AND kind = ?"
                " AND anchor_t >= ? AND anchor_t < ?",
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
