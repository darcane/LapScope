"""SQLite persistence: raw telemetry frames plus session/lap/route index tables.

Writes happen only on the event-loop thread through the single `Store`
connection. API request handlers run in FastAPI's threadpool and must use
short-lived read connections from `Store.reader()` (safe under WAL; small
writes like renames are also fine there).
"""

from __future__ import annotations

import math
import sqlite3
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
"""

# added after v1; applied to existing databases on startup
MIGRATIONS = (
    "ALTER TABLE sessions ADD COLUMN conditions TEXT",
    "ALTER TABLE sessions ADD COLUMN route_id INTEGER",
    "ALTER TABLE laps ADD COLUMN flags TEXT",  # "rewind,contact" etc.
    "ALTER TABLE sessions ADD COLUMN track_type TEXT",  # road/street/dirt/cross/drag
    # kept=1 exempts a session from the no-completed-laps cleanup
    # (FC_KEEP_DISCARDED captures, reprocessed sessions)
    "ALTER TABLE sessions ADD COLUMN kept INTEGER NOT NULL DEFAULT 0",
)

# route fingerprint tolerances: same start point within this radius and a
# lap length within this fraction = same route
ROUTE_START_RADIUS_M = 80.0
ROUTE_LENGTH_TOLERANCE = 0.05

_SESSION_SELECT = """
SELECT s.*, r.name AS route_name, cn.name AS car_name_override
FROM sessions s
LEFT JOIN routes r ON r.id = s.route_id
LEFT JOIN car_names cn ON cn.ordinal = s.car_ordinal
"""


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
        # session ids must never be reused: discarding a session deletes the
        # max rowid, which plain INTEGER PRIMARY KEY would hand out again -
        # and the live dashboard detects "new event" by the id changing
        self._next_session_id = self.db.execute(
            "SELECT COALESCE(MAX(id), 0) + 1 FROM sessions").fetchone()[0]

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
                    conditions: str | None = None) -> None:
        self.db.execute(
            "UPDATE sessions SET ended_at = ?, frame_count = ?,"
            " conditions = COALESCE(conditions, ?) WHERE id = ?",
            (ended_at, frame_count, conditions, session_id),
        )
        self.db.commit()

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
                              lap_length: float) -> int:
        for rid, rx, rz, rlen in self.db.execute(
                "SELECT id, start_x, start_z, lap_length FROM routes"):
            if (math.hypot(start_x - rx, start_z - rz) <= ROUTE_START_RADIUS_M
                    and abs(lap_length - rlen) <= ROUTE_LENGTH_TOLERANCE * rlen):
                return rid
        cur = self.db.execute(
            "INSERT INTO routes (start_x, start_z, lap_length) VALUES (?, ?, ?)",
            (start_x, start_z, lap_length),
        )
        self.db.commit()
        return cur.lastrowid

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
                "SELECT s.*, r.name AS route_name, cn.name AS car_name_override,"
                " COUNT(l.lap_time) AS lap_count, MIN(l.lap_time) AS best_lap"
                " FROM sessions s"
                " LEFT JOIN routes r ON r.id = s.route_id"
                " LEFT JOIN car_names cn ON cn.ordinal = s.car_ordinal"
                " LEFT JOIN laps l ON l.session_id = s.id"
                " GROUP BY s.id ORDER BY s.started_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_session(self, session_id: int) -> dict | None:
        with self.reader() as conn:
            row = conn.execute(_SESSION_SELECT + " WHERE s.id = ?",
                               (session_id,)).fetchone()
        return dict(row) if row else None

    def rename_session(self, session_id: int, name: str) -> None:
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

    def set_car_name(self, ordinal: int, name: str) -> None:
        with self.reader() as conn:
            conn.execute(
                "INSERT INTO car_names (ordinal, name) VALUES (?, ?)"
                " ON CONFLICT(ordinal) DO UPDATE SET name = excluded.name",
                (ordinal, name),
            )
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

    def session_laps(self, session_id: int) -> list[dict]:
        with self.reader() as conn:
            rows = conn.execute(
                "SELECT * FROM laps WHERE session_id = ? ORDER BY started_t", (session_id,)
            ).fetchall()
        return [dict(r) for r in rows]

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
