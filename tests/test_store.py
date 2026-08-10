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


def _session(store, started_at: float, frames: int = 3) -> int:
    """A session with `frames` frames one second apart, left open."""
    sid = store.create_session(started_at, CAR)
    store.add_frames(sid, [(started_at + i, bytes(324)) for i in range(frames)])
    return sid


def test_startup_cleanup_keeps_everything_it_should(tmp_path):
    """The one destructive path that runs before the user sees anything, on
    every single launch (issue #61 - it had no test at all). It must drop only
    the sessions that never produced a timed lap, and repair what a crash left
    half-written rather than deleting it."""
    store = Store(str(tmp_path / "c.db"))
    try:
        timed = _session(store, 1000.0)
        lap = store.add_lap(timed, 0, 1000.0, 0.0)
        store.complete_lap(lap, 1002.0, 2.0)
        store.end_session(timed, 1002.0, 3)

        untimed = _session(store, 2000.0)  # free-roam cruise: laps, no times
        store.add_lap(untimed, 0, 2000.0, 0.0)
        store.end_session(untimed, 2002.0, 3)

        kept = _session(store, 3000.0)  # an import, or a reprocess that found 0
        store.mark_session_kept(kept)
        store.end_session(kept, 3002.0, 3)

        crashed = _session(store, 4000.0)  # killed mid-lap: both rows still open
        store.add_lap(crashed, 0, 4000.0, 0.0)  # never completed
        done = store.add_lap(crashed, 1, 4001.0, 0.0)  # ...after one full lap
        store.complete_lap(done, 4002.0, 1.0)
        gone = store.create_group("doomed", 1, 100, [untimed])

        removed = store.cleanup_sessions()

        assert removed == 1
        assert sorted(s["id"] for s in store.list_sessions()) == [timed, kept, crashed]
        # the crash left no ended_at and an open lap; both take the last frame
        assert store.get_session(crashed)["ended_at"] == 4002.0
        rows = {row["lap_number"]: row for row in store.session_laps(crashed)}
        assert rows[0]["ended_t"] == 4002.0
        assert rows[0]["lap_time"] is None  # repaired, not invented
        assert rows[1]["lap_time"] == 1.0   # the finished one is untouched
        assert store.get_group(gone) is None  # emptied by the delete, so pruned
    finally:
        store.close()


def test_startup_cleanup_leaves_a_healthy_database_alone(tmp_path):
    """It runs on every launch, so the second run must be a no-op: nothing is
    deleted twice and no ended_t drifts to a later frame."""
    store = Store(str(tmp_path / "c2.db"))
    try:
        sid = _session(store, 1000.0)
        lap = store.add_lap(sid, 0, 1000.0, 0.0)
        store.complete_lap(lap, 1001.0, 1.0)
        store.end_session(sid, 1002.0, 3)

        assert store.cleanup_sessions() == 0
        assert store.cleanup_sessions() == 0
        assert [s["id"] for s in store.list_sessions()] == [sid]
        assert store.get_session(sid)["ended_at"] == 1002.0
        assert store.session_laps(sid)[0]["ended_t"] == 1001.0
    finally:
        store.close()


def _one_lap_then_an_open_one(store) -> tuple[int, list[dict]]:
    """What killing the process mid-session leaves behind: a finished lap, and
    an open one starting on the very frame the first ended."""
    sid = _session(store, 1000.0, frames=0)
    store.add_frames(sid, [(1000.0 + i, bytes(324)) for i in range(201)])
    done = store.add_lap(sid, 0, 1000.0, 0.0)
    store.complete_lap(done, 1100.0, 100.0)
    store.add_lap(sid, 1, 1100.0, 0.0)  # started_t == the previous ended_t
    return sid, store.session_laps(sid)


def test_excluding_an_open_lap_leaves_the_previous_lap_alone(tmp_path):
    """Issue #62: an open lap's anchor is its started_t, which is byte-identical
    to the previous lap's ended_t. With both ends of the span inclusive the
    anchor landed inside both laps, so tidying away the untimed trailing lap
    silently excluded the timed one before it - and with it the session best."""
    store = Store(str(tmp_path / "open.db"))
    try:
        sid, laps = _one_lap_then_an_open_one(store)
        assert laps[1]["ended_t"] is None
        anchor = store_mod.lap_anchor(laps[1])
        assert anchor == laps[0]["ended_t"]  # the collision this test is about

        store.add_edit(sid, "exclude_lap", anchor)
        rows = store.session_laps(sid)
        assert not rows[0]["excluded"], "the finished lap must survive"
        assert rows[1]["excluded"]
        # the session's own count uses a second, SQL copy of the same rule
        session = store.get_session(sid)
        assert session["lap_count"] == 1 and session["best_lap"] == 100.0
    finally:
        store.close()


def test_cleanup_closes_the_open_lap_so_the_anchors_separate(tmp_path):
    """The other half of #62: the orphaned open lap is repaired at startup, so
    its anchor becomes a real midpoint instead of sitting on the boundary."""
    store = Store(str(tmp_path / "open2.db"))
    try:
        sid, laps = _one_lap_then_an_open_one(store)
        store.mark_session_kept(sid)
        store.cleanup_sessions()

        rows = store.session_laps(sid)
        assert rows[1]["ended_t"] == 1200.0
        assert store_mod.lap_anchor(rows[1]) == 1150.0  # clear of lap 1's end
        store.add_edit(sid, "exclude_lap", store_mod.lap_anchor(rows[1]))
        rows = store.session_laps(sid)
        assert not rows[0]["excluded"] and rows[1]["excluded"]
    finally:
        store.close()


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


def test_a_half_written_frame_batch_never_survives_the_retry(tmp_path):
    """The tracker retries the same buffer after a failed write (issue #64),
    so a batch that died half-way must not still be sitting in the open
    transaction: the next write's commit would carry it along and store
    those frames twice."""
    class DiesHalfWay:
        """Frames that raise part-way through, like a disk filling up mid-write."""

        def __init__(self, frames, after):
            self.frames, self.after = frames, after

        def __iter__(self):
            for i, frame in enumerate(self.frames):
                if i == self.after:
                    raise sqlite3.OperationalError("database or disk is full")
                yield frame

    store = Store(str(tmp_path / "t.db"))
    try:
        sid = store.create_session(1000.0, CAR)
        frames = [(1000.0 + i / 60, bytes(324)) for i in range(10)]
        with pytest.raises(sqlite3.OperationalError):
            store.add_frames(sid, DiesHalfWay(frames, 5))
        store.add_frames(sid, frames)  # the retry, exactly as the tracker does it
        assert store.db.execute("SELECT COUNT(*) FROM frames").fetchone()[0] == 10
    finally:
        store.close()
