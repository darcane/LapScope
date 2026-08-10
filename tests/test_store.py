"""Store-level checks that don't need a recorded session: opening an older
database, and the disk the recordings occupy.

The upgrade test is the one every existing user runs for real on the day they
install a new build — nothing else in the suite opens a database that predates
the current column set.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.recorder import store as store_mod
from app.recorder.store import SCHEMA, SCHEMA_VERSION, Store

CAR = {"car_ordinal": 100, "car_class": 4, "car_pi": 800, "drivetrain_type": 2}


def _user_version(path) -> int:
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def _pre_migration_db(path) -> None:
    """A database in the shape LapScope shipped before any column was added:
    SCHEMA only, no MIGRATIONS, and therefore user_version 0."""
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO sessions (id, started_at, ended_at, frame_count,"
                 " car_ordinal, car_class, car_pi, drivetrain_type)"
                 " VALUES (7, 1000.0, 1100.0, 3, 100, 4, 800, 2)")
    conn.execute("INSERT INTO routes (id, name, start_x, start_z, lap_length)"
                 " VALUES (1, 'The Goliath', 0.0, 0.0, 5950.0)")
    conn.execute("INSERT INTO laps (session_id, lap_number, lap_time, started_t,"
                 " ended_t, start_distance) VALUES (7, 0, 91.5, 1000.0, 1091.5, 0.0)")
    conn.commit()
    conn.close()


def test_opens_a_pre_migration_database(tmp_path):
    """Upgrade day: a database written before the added columns existed must
    migrate in place, keep its rows, and read back through the normal
    projection with every current column present."""
    db = tmp_path / "v0.db"
    _pre_migration_db(db)
    assert _user_version(db) == 0

    store = Store(str(db))
    try:
        rows = store.list_sessions()
        assert [r["id"] for r in rows] == [7]
        row = rows[0]
        for col in ("conditions", "route_id", "track_type", "kept", "group_id"):
            assert col in row, f"{col} missing after migration"
        assert row["lap_count"] == 1 and row["best_lap"] == 91.5
        assert store.session_laps(7)[0]["flags"] is None  # laps.flags added too
        route = store.get_route(1)
        assert route["name"] == "The Goliath"
        for col in ("span_x", "span_z", "kind", "kind_user", "outline",
                    "catalog_key"):
            assert col in route, f"routes.{col} missing after migration"
        # ids are never reused: the counter picks up past the existing row
        assert store.create_session(1200.0, CAR) == 8
    finally:
        store.close()

    assert _user_version(db) == SCHEMA_VERSION


def test_stamps_and_reopens(tmp_path):
    """A fresh database carries the schema version, and opening an existing
    one again is a no-op - the ALTERs all raise "duplicate column" the second
    time and that is the one error the runner is allowed to swallow."""
    db = tmp_path / "fresh.db"
    Store(str(db)).close()
    assert _user_version(db) == SCHEMA_VERSION

    store = Store(str(db))
    store.close()
    assert _user_version(db) == SCHEMA_VERSION


def test_a_migration_failure_is_not_swallowed(tmp_path, monkeypatch):
    """Only "column already exists" is expected. A blanket except also hides
    "database is locked" / "disk I/O error", which silently no-ops every ALTER
    and leaves the app half-migrated - it then dies later on a missing column,
    an error that says nothing about what actually went wrong."""
    monkeypatch.setattr(store_mod, "MIGRATIONS",
                        store_mod.MIGRATIONS + ("ALTER TABLE nope ADD COLUMN x",))
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        Store(str(tmp_path / "broken.db"))


def _recorded(store, frames: int = 20_000) -> int:
    sid = store.create_session(1000.0, CAR)
    store.add_frames(sid, [(1000.0 + i / 60, bytes(324)) for i in range(frames)])
    store.end_session(sid, 1000.0 + frames / 60, frames)
    return sid


def test_deleting_frees_pages_but_only_compacting_returns_the_space(tmp_path):
    """The README used to promise that deleting sessions reclaims disk space.
    It doesn't: the freed pages go on the freelist for reuse and the file
    never shrinks on its own (issue #59)."""
    store = Store(str(tmp_path / "t.db"))
    try:
        keep = _recorded(store)
        drop = _recorded(store)
        full = store.storage_stats()
        assert full["db_bytes"] > 4 * 1024 * 1024
        assert full["sessions"] == 2

        store.delete_session(drop)
        after_delete = store.storage_stats()
        assert after_delete["db_bytes"] >= full["db_bytes"]  # not one byte back
        assert after_delete["free_bytes"] > 0                # just reusable

        out = store.vacuum()
        assert out["reclaimed_bytes"] > 0
        assert out["after_bytes"] < out["before_bytes"]
        compacted = store.storage_stats()
        assert compacted["db_bytes"] == out["after_bytes"]
        assert compacted["free_bytes"] == 0

        # and the session that was kept survived the rebuild intact
        assert [s["id"] for s in store.list_sessions()] == [keep]
        assert len(store.lap_frames({"session_id": keep, "started_t": 0.0,
                                     "ended_t": 1e18})) == 20_000
    finally:
        store.close()
