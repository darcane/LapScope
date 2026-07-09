"""API-level checks: the real endpoint functions run against a store the
harness produced. The handlers are plain functions reading
``request.app.state``, so a stub request object is enough - no HTTP server,
no httpx, same zero-dependency footprint as the rest of the tests.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from harness import completed_laps, run, sessions
from app.recorder.store import Store


def _request_for(store, tracker=None):
    return SimpleNamespace(app=SimpleNamespace(
        state=SimpleNamespace(store=store, tracker=tracker)))


def test_lap_time_channel_falls_back_when_lap_clock_dead(tmp_path):
    """World Time Attack broadcasts CurrentLap 0 for the whole event; the
    lap_time channel must fall back to time-since-lap-start so the A/B
    delta-time chart isn't a flat zero line."""
    from app.api.routes import lap_data

    def scenario(sim):
        sim.wta(2)
        sim.race_off()

    store = run(scenario, tmp_path)
    lap = completed_laps(store, sessions(store)[0]["id"])[0]
    data = lap_data(lap["id"], _request_for(store), "lap_time,speed_kmh", 500)

    lt = data["channels"]["lap_time"]
    assert lt == data["t"]  # substituted with time since the lap's first frame
    assert lt[-1] > 10.0    # and it actually counts across the lap
    assert any(v > 1.0 for v in data["channels"]["speed_kmh"])  # others untouched


def test_lap_time_channel_kept_when_lap_clock_alive(tmp_path):
    """A circuit lap's CurrentLap is real telemetry and must pass through
    (alive channel, no zeroing, no substitution surprises)."""
    from app.api.routes import lap_data

    def scenario(sim):
        sim.event(120, "event")
        sim.race_off()

    store = run(scenario, tmp_path)
    lap = completed_laps(store, sessions(store)[0]["id"])[0]
    data = lap_data(lap["id"], _request_for(store), "lap_time", 500)
    assert any(v > 0.5 for v in data["channels"]["lap_time"])


def test_session_name_patch_sets_and_clears(tmp_path):
    """``name: ""`` must clear the custom name back to NULL so display_name
    falls back to route/date, and a PATCH without name must leave it alone.
    Regression: "" used to be silently ignored, so a name could never be
    cleared (issue #11)."""
    from app.api.routes import SessionPatch, patch_session

    def scenario(sim):
        sim.event(120, "event")
        sim.race_off()

    store = run(scenario, tmp_path)
    sid = sessions(store)[0]["id"]
    req = _request_for(store)

    patch_session(sid, SessionPatch(name="  Sunset Sprint PB  "), req)
    assert store.get_session(sid)["name"] == "Sunset Sprint PB"

    patch_session(sid, SessionPatch(conditions="wet"), req)  # name omitted
    assert store.get_session(sid)["name"] == "Sunset Sprint PB"

    patch_session(sid, SessionPatch(name=""), req)
    assert store.get_session(sid)["name"] is None


def test_reprocess_blocked_while_any_session_records(tmp_path):
    """The replay runs synchronously on the event loop, so reprocess must 409
    while ANY session is recording - not only when the target session is the
    live one (a long replay would freeze live telemetry mid-race, issue #11).
    With the tracker idle it must still run and rebuild the same laps."""
    from app.api.routes import reprocess

    def scenario(sim):
        sim.event(120, "event")
        sim.race_off()

    store = run(scenario, tmp_path)
    session = sessions(store)[0]
    sid = session["id"]

    recording_other = _request_for(store, SimpleNamespace(session_id=sid + 1))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(reprocess(sid, recording_other))
    assert exc.value.status_code == 409

    # run() closed the event-loop connection the replay writes through
    store2 = Store(store.db_path)
    idle = _request_for(store2, SimpleNamespace(session_id=None))
    out = asyncio.run(reprocess(sid, idle))
    store2.close()
    assert out["ok"] and out["laps"] == session["lap_count"]


def test_car_override_set_and_clear(tmp_path):
    """``name: ""`` on PATCH /cars deletes the override so the bundled name
    (or "Car #<ordinal>") shows again (issue #11, optional revert path)."""
    from app.api.routes import NameBody, car_name, set_car_name

    store = Store(str(tmp_path / "cars.db"))
    req = _request_for(store)

    set_car_name(999999, NameBody(name="  Kebab GT  "), req)
    assert car_name(999999, req) == {"ordinal": 999999, "name": "Kebab GT",
                                     "known": True}

    set_car_name(999999, NameBody(name="   "), req)
    out = car_name(999999, req)
    store.close()
    assert out["known"] is False and out["name"] == "Car #999999"
