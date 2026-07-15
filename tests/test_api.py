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

from harness import completed_laps, flags_of, run, sessions
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


def test_collisions_tag_landings_but_keep_wall_hits(tmp_path):
    """/laps/{id}/data classifies each collision burst: jump landings carry
    landing=true (drawn amber, not counted as contact), wall hits
    landing=false. --dirty --jumps has both on lap 2 and only landings on
    the other laps."""
    from app.api.routes import lap_data

    def scenario(sim):
        sim.event(180, "dirty with jumps", dirty=True)
        sim.race_off()

    store = run(scenario, tmp_path, jumps=True)
    laps = completed_laps(store, sessions(store)[0]["id"])
    by_number = {lap["lap_number"]: lap for lap in laps}

    wall_lap = lap_data(by_number[1]["id"], _request_for(store), "speed_kmh", 500)
    kinds = {h["landing"] for h in wall_lap["collisions"]}
    assert kinds == {True, False}  # the wall hit and two jump landings

    clean_lap = lap_data(by_number[0]["id"], _request_for(store), "speed_kmh", 500)
    assert clean_lap["collisions"]  # the jumps did register...
    assert all(h["landing"] for h in clean_lap["collisions"])  # ...as landings


def test_lap_data_reports_jump_segments(tmp_path):
    """/laps/{id}/data returns each flight as a takeoff -> touchdown segment:
    the simulator's --jumps course launches the car twice per lap, and its
    touchdown jolt (well past IMPACT_ACCEL) must mark the segment hard."""
    from app.api.routes import lap_data

    def scenario(sim):
        sim.event(120, "jumps")
        sim.race_off()

    store = run(scenario, tmp_path, jumps=True)
    lap = completed_laps(store, sessions(store)[0]["id"])[0]
    data = lap_data(lap["id"], _request_for(store), "speed_kmh", 500)

    assert len(data["jumps"]) == 2  # two bumps on the loop
    for j in data["jumps"]:
        assert j["dist1"] > j["dist0"] >= 0   # lands after it takes off
        assert j["air_s"] >= 0.12             # a real flight, not a crest
        assert j["hard"] and j["g"] > 4.0     # the touchdown jolt registered
    # the hard landings are still classified as landings, never contact
    assert data["collisions"] and all(h["landing"] for h in data["collisions"])


def test_lap_data_no_jumps_on_a_flat_lap(tmp_path):
    """A plain circuit lap never leaves the ground: jumps must be empty."""
    from app.api.routes import lap_data

    def scenario(sim):
        sim.event(120, "flat")
        sim.race_off()

    store = run(scenario, tmp_path)
    lap = completed_laps(store, sessions(store)[0]["id"])[0]
    data = lap_data(lap["id"], _request_for(store), "speed_kmh", 500)
    assert data["jumps"] == []


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


def test_track_type_patch_overrides_auto_and_clears(tmp_path):
    """The dropdown always wins over the auto-suggested type (the suggestion
    is written with COALESCE at session close, a PATCH overwrites), and ""
    clears back to untagged."""
    from app.api.routes import SessionPatch, patch_session

    def scenario(sim):
        sim.event(120, "event")
        sim.race_off()

    store = run(scenario, tmp_path)
    sid = sessions(store)[0]["id"]
    assert store.get_session(sid)["track_type"] == "road"  # auto-filled
    req = _request_for(store)

    patch_session(sid, SessionPatch(track_type="street"), req)
    assert store.get_session(sid)["track_type"] == "street"

    patch_session(sid, SessionPatch(track_type=""), req)
    assert store.get_session(sid)["track_type"] is None


def test_route_patch_retags_every_session_on_the_route(tmp_path):
    """PATCH /routes/{id} with track_type retags all sessions of that route
    (the "apply to all sessions on this route?" prompt), rejects unknown
    types and routes, and renaming still works through the same endpoint."""
    from app.api.routes import RoutePatch, patch_route

    def scenario(sim):
        for i in range(2):
            sim.event(75, f"event {i + 1}")
        sim.race_off()

    store = run(scenario, tmp_path)
    ss = sessions(store)
    rid = ss[0]["route_id"]
    assert len(ss) == 2 and all(s["track_type"] == "road" for s in ss)
    req = _request_for(store)

    patch_route(rid, RoutePatch(track_type="touge"), req)
    assert all(s["track_type"] == "touge" for s in sessions(store))

    patch_route(rid, RoutePatch(name="Bandai Azuma"), req)
    assert sessions(store)[0]["route_name"] == "Bandai Azuma"

    with pytest.raises(HTTPException) as exc:
        patch_route(rid, RoutePatch(track_type="gravel"), req)
    assert exc.value.status_code == 400

    with pytest.raises(HTTPException) as exc:
        patch_route(rid + 99, RoutePatch(track_type="road"), req)
    assert exc.value.status_code == 404


def test_suggestions_are_valid_track_types():
    """Cross-file invariant: everything the classifier can suggest must be a
    member of the API's TRACK_TYPES (= TRACK_META = #track-select)."""
    from app.api.routes import TRACK_TYPES
    assert {"road", "dirt", "cross", "wtc"} <= TRACK_TYPES


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


def test_sessions_expose_car_known(tmp_path, monkeypatch):
    """car_known tells the UI to show the "unknown car — help name it"
    affordance: false when the ordinal is missing from the community list,
    true again once the user names it locally (DB override)."""
    from app import cars
    from app.api.routes import NameBody, sessions as sessions_ep, set_car_name

    def scenario(sim):
        sim.event(120, "event")
        sim.race_off()

    store = run(scenario, tmp_path)
    req = _request_for(store)

    out = sessions_ep(req)[0]  # simulator drives ordinal 269 (bundled list)
    assert out["car_known"] is True and out["car_name"] == "1987 Porsche 959"

    monkeypatch.delitem(cars.CAR_NAMES, 269)  # simulate a newer-than-list car
    out = sessions_ep(req)[0]
    assert out["car_known"] is False and out["car_name"] == "Car #269"

    set_car_name(269, NameBody(name="Porsche 959"), req)
    out = sessions_ep(req)[0]
    assert out["car_known"] is True and out["car_name"] == "Porsche 959"


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


# ------------------------- manual session edits (issue #26) -------------------------
# Stored in the `edits` table keyed by frame time, applied at read time; raw
# frames and the recorder's lap rows are never rewritten.


def _dirty_store(tmp_path):
    """3-lap race with a wall contact on lap 2 and a rewind on lap 3 (the
    --dirty scenario asserted in test_scenarios)."""
    def scenario(sim):
        sim.event(180, "dirty", dirty=True)
        sim.race_off()

    return run(scenario, tmp_path)


def test_dismiss_contact_clears_marker_and_lifts_the_flag(tmp_path):
    """Right-click "not a contact": the marker comes back tagged dismissed
    from /laps/{id}/data (not dropped - the data stays inspectable), and once
    no real contact remains the lap's contact flag is lifted via a flags
    override while flags_auto keeps what the recorder detected."""
    from app.api.routes import DismissBody, dismiss_contact, lap_data, session_laps

    store = _dirty_store(tmp_path)
    sid = sessions(store)[0]["id"]
    req = _request_for(store)
    lap = next(lap for lap in completed_laps(store, sid) if "contact" in flags_of(lap))

    data = lap_data(lap["id"], req, "speed_kmh", 500)
    hits = [c for c in data["collisions"] if not c["landing"]]
    assert hits and all(not c["dismissed"] for c in data["collisions"])

    for c in hits:
        out = dismiss_contact(lap["id"], DismissBody(t=c["t"]), req)
    assert out["remaining_contacts"] == 0
    assert not out["flags"] or "contact" not in out["flags"]

    data = lap_data(lap["id"], req, "speed_kmh", 500)
    assert all(c["dismissed"] for c in data["collisions"] if not c["landing"])

    row = next(r for r in session_laps(sid, req)["laps"] if r["id"] == lap["id"])
    assert "contact" not in (row["flags"] or "")
    assert "contact" in row["flags_auto"]


def test_dismiss_contact_rejects_a_time_with_no_marker(tmp_path):
    """A dismissal must anchor to a real collision peak: a t that matches
    nothing is a 404, not a silently stored dangling edit."""
    from app.api.routes import DismissBody, dismiss_contact

    store = _dirty_store(tmp_path)
    sid = sessions(store)[0]["id"]
    req = _request_for(store)
    lap = next(lap for lap in completed_laps(store, sid) if "contact" in flags_of(lap))

    with pytest.raises(HTTPException) as exc:
        dismiss_contact(lap["id"], DismissBody(t=-999.0), req)
    assert exc.value.status_code == 404
    assert store.session_edits(sid) == []
    assert "contact" in flags_of(store.session_laps(sid)[lap["lap_number"]])


def test_lap_flags_override_set_revert_and_validate(tmp_path):
    """PATCH /laps/{id} flags: "" clears every marker (effective flags None,
    detected CSV preserved in flags_auto); writing back exactly the detected
    value removes the override instead of storing a no-op edit; unknown
    tokens are a 400."""
    from app.api.routes import LapPatch, patch_lap

    store = _dirty_store(tmp_path)
    sid = sessions(store)[0]["id"]
    req = _request_for(store)
    lap = next(lap for lap in completed_laps(store, sid) if "rewind" in flags_of(lap))

    patch_lap(lap["id"], LapPatch(flags=""), req)
    row = next(r for r in store.session_laps(sid) if r["id"] == lap["id"])
    assert row["flags"] is None and row["flags_auto"] == flags_of(lap)

    patch_lap(lap["id"], LapPatch(flags=flags_of(lap)), req)  # = detected: revert
    assert store.session_edits(sid) == []
    row = next(r for r in store.session_laps(sid) if r["id"] == lap["id"])
    assert row["flags"] == flags_of(lap)

    with pytest.raises(HTTPException) as exc:
        patch_lap(lap["id"], LapPatch(flags="rewind,banana"), req)
    assert exc.value.status_code == 400


def test_exclude_lap_recomputes_bests_and_counts(tmp_path):
    """Excluding the best lap: it stays listed (excluded=true, never is_best,
    no gap) while the next-fastest becomes the best, and the session list's
    lap_count / best_lap aggregates drop it too. Restore brings it all back."""
    from app.api.routes import LapPatch, patch_lap, session_laps

    def scenario(sim):
        sim.event(180, "race")
        sim.race_off()

    store = run(scenario, tmp_path)
    sid = sessions(store)[0]["id"]
    req = _request_for(store)
    before = session_laps(sid, req)
    assert before["session"]["edit_count"] == 0
    best = next(lap for lap in before["laps"] if lap["is_best"])
    n_timed = sum(1 for lap in before["laps"] if lap["lap_time"])
    assert n_timed >= 2

    patch_lap(best["id"], LapPatch(excluded=True), req)
    after = session_laps(sid, req)
    assert after["session"]["edit_count"] == 1
    row = next(r for r in after["laps"] if r["id"] == best["id"])
    assert row["excluded"] and not row["is_best"] and row["gap_to_best"] is None
    new_best = next(r for r in after["laps"] if r["is_best"])
    assert new_best["id"] != best["id"]
    listed = sessions(store)[0]
    assert listed["lap_count"] == n_timed - 1
    assert listed["best_lap"] == new_best["lap_time"]

    patch_lap(best["id"], LapPatch(excluded=False), req)
    assert sessions(store)[0]["lap_count"] == n_timed
    restored = next(r for r in session_laps(sid, req)["laps"] if r["id"] == best["id"])
    assert restored["is_best"] and not restored["excluded"]


def test_edits_survive_reprocess_and_reset_reverts(tmp_path):
    """The point of time-keyed edits: reprocess deletes and recreates every
    lap row, yet dismissals, flag overrides and exclusions re-apply to the
    rebuilt laps. DELETE /sessions/{id}/edits is the explicit way back to
    exactly what the recorder detected."""
    from app.api.routes import (DismissBody, LapPatch, dismiss_contact, lap_data,
                                patch_lap, reset_edits)
    from app.recorder.reprocess import reprocess_session

    store = _dirty_store(tmp_path)
    sid = sessions(store)[0]["id"]
    req = _request_for(store)
    laps = completed_laps(store, sid)
    contact_lap = next(lap for lap in laps if "contact" in flags_of(lap))
    rewind_lap = next(lap for lap in laps if "rewind" in flags_of(lap))
    clean_lap = laps[0]

    data = lap_data(contact_lap["id"], req, "speed_kmh", 500)
    for c in data["collisions"]:
        if not c["landing"]:
            dismiss_contact(contact_lap["id"], DismissBody(t=c["t"]), req)
    patch_lap(rewind_lap["id"], LapPatch(flags=""), req)
    patch_lap(clean_lap["id"], LapPatch(excluded=True), req)

    store2 = Store(store.db_path)  # replay writes via the event-loop connection
    reprocess_session(store2, sid)
    store2.close()

    rows = {r["lap_number"]: r for r in store.session_laps(sid)}
    redone = rows[contact_lap["lap_number"]]
    # flags_auto == the replay re-detected the contact on the rebuilt row;
    # the override (keyed by time, not by the recycled lap id) still lifts it
    assert "contact" in redone["flags_auto"] and "contact" not in (redone["flags"] or "")
    assert rows[rewind_lap["lap_number"]]["flags"] is None
    assert rows[clean_lap["lap_number"]]["excluded"]
    data = lap_data(redone["id"], req, "speed_kmh", 500)
    assert all(c["dismissed"] for c in data["collisions"] if not c["landing"])

    out = reset_edits(sid, req)
    assert out["removed"] >= 3
    for row in store.session_laps(sid):
        assert row["flags"] == row["flags_auto"] and not row["excluded"]
    data = lap_data(redone["id"], req, "speed_kmh", 500)
    assert not any(c["dismissed"] for c in data["collisions"])
