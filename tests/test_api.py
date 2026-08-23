"""API-level checks: the real endpoint functions run against a store the
harness produced. The handlers are plain functions reading
``request.app.state``, so a stub request object is enough - no HTTP server,
no httpx, same zero-dependency footprint as the rest of the tests.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
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


def test_raw_channels_cover_every_packet_field(tmp_path):
    """The generated raw_* channels expose every packet field verbatim (wheel
    groups as _fl/_fr/_rl/_rr) without disturbing the curated channels'
    scaling. RAW_FIELDS in common.js mirrors the same list for the frontend."""
    from app.api.routes import CHANNELS, lap_data
    from app.telemetry.packet import FIELDS

    for name, count in FIELDS:
        if count == 1:
            assert f"raw_{name}" in CHANNELS
        else:
            for w in ("fl", "fr", "rl", "rr"):
                assert f"raw_{name}_{w}" in CHANNELS

    def scenario(sim):
        sim.event(120, "event")
        sim.race_off()

    store = run(scenario, tmp_path)
    lap = completed_laps(store, sessions(store)[0]["id"])[0]
    data = lap_data(lap["id"], _request_for(store),
                    "speed_kmh,steer,raw_speed,raw_steer,raw_tire_temp_fl", 500)
    ch = data["channels"]

    assert any(v > 1.0 for v in ch["raw_speed"])  # m/s, actually moving
    for kmh, mps in zip(ch["speed_kmh"], ch["raw_speed"]):
        assert abs(kmh - mps * 3.6) < 1e-3  # raw is the unscaled packet value
    for pct, raw in zip(ch["steer"], ch["raw_steer"]):
        assert abs(pct - raw / 1.27) < 1e-3  # curated ±100 %, raw ±127
    assert len(ch["raw_tire_temp_fl"]) == len(ch["raw_speed"])


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


def test_mid_flight_spike_marks_its_own_jump_hard():
    """A spike burst that starts AND ends while airborne (clipping something
    mid-flight) belongs to the flight it happened in - not to the previous
    jump, which is the last *emitted* segment at that moment (issue #41).
    Two flights, the second with a mid-air burst that subsides before
    touchdown: only the second may be hard."""
    from app.api.routes import _LapScan
    from app.telemetry.packet import empty_fields, pack

    def frame(d, *, air=False, gx=0.0):
        f = empty_fields()
        f["is_race_on"] = 1
        f["distance_traveled"] = d
        f["pos_x"] = d
        f["norm_susp_travel"] = [0.05 if air else 0.5] * 4
        f["tire_combined_slip"] = [0.01 if air else 0.3] * 4
        f["accel_x"] = gx
        return pack(f)

    rows: list[tuple[float, bytes]] = []
    t, d = 0.0, 0.0

    def add(n, **kw):
        nonlocal t, d
        for _ in range(n):
            rows.append((t, frame(d, **kw)))
            t += 0.05
            d += 2.0

    add(10)                  # grounded run-up
    add(8, air=True)         # flight 1: clean (0.4 s, past AIRBORNE_MIN_S)
    add(10)                  # grounded stretch between the flights
    add(4, air=True)         # flight 2 begins (0.2 s in: "flying")
    add(2, air=True, gx=60)  # mid-air spike burst...
    add(4, air=True)         # ...subsides while still airborne
    add(10)                  # touchdown + rollout

    collisions, jumps = _LapScan(rows, 0.0).events()
    assert len(jumps) == 2
    assert not jumps[0]["hard"] and jumps[0]["g"] is None  # first flight clean
    assert jumps[1]["hard"] and jumps[1]["g"] > 4.0        # the spike is its
    assert collisions and all(c["landing"] for c in collisions)


def test_aero_cornering_bursts_are_dropped_impacts_kept():
    """Issue #49: a downforce car's cornering load crosses the contact
    threshold and used to draw a marker per fast corner. Only a burst that
    looks like an impulse survives - here one long aero corner (dropped) and
    one wall hit (kept), so exactly one marker comes back."""
    from app.api.routes import _LapScan
    from app.telemetry.packet import empty_fields, pack

    def frame(d, gx):
        f = empty_fields()
        f["is_race_on"] = 1
        f["distance_traveled"] = d
        f["pos_x"] = d
        f["norm_susp_travel"] = [0.5] * 4     # grounded: not a jump landing
        f["tire_combined_slip"] = [0.3] * 4
        f["accel_x"] = gx
        return pack(f)

    rows: list[tuple[float, bytes]] = []
    t, d = 0.0, 0.0

    def add(gs):
        nonlocal t, d
        for gx in gs:
            rows.append((t, frame(d, gx)))
            t += 1 / 60
            d += 0.7

    add([0.0] * 10)
    add([2.0 * i for i in range(31)])   # aero builds to 60 m/s^2 over 0.5 s...
    add([60.0] * 30)                    # ...holds through the corner...
    add([60.0 - 2.0 * i for i in range(31)])  # ...and unwinds. No impulse.
    add([0.0] * 10)
    aero_only = len(rows)
    assert _LapScan(rows, 0.0).events()[0] == []          # not one marker so far

    add([0.0, 70.0, 65.0, 50.0])        # a wall: 0 -> 70 m/s^2 in one frame
    add([0.0] * 10)

    collisions, jumps = _LapScan(rows, 0.0).events()
    assert jumps == []
    assert len(collisions) == 1                    # the wall, not the corner
    hit = collisions[0]
    assert not hit["landing"] and hit["g"] > 7.0   # 70 m/s^2 peak, ~7.1 g
    # and it is the late burst, not anything from the aero stretch
    assert hit["t"] > rows[aero_only - 1][0]


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


def test_route_patch_sets_and_clears_the_shape_override(tmp_path):
    """PATCH /routes/{id} with kind is the manual correction for the
    recorder's guess (it decides whether the UI says "lap" or "run").
    "" clears it back to the detected shape."""
    from app.api.routes import RoutePatch, patch_route

    def scenario(sim):
        sim.event(180, "race", race_laps=3)
        sim.race_off()

    store = run(scenario, tmp_path)
    sid = sessions(store)[0]["id"]
    rid = sessions(store)[0]["route_id"]
    req = _request_for(store)
    assert sessions(store)[0]["route_kind"] == "circuit"  # detected

    patch_route(rid, RoutePatch(kind="sprint"), req)
    s = sessions(store)[0]
    assert (s["route_kind"], s["route_kind_auto"]) == ("sprint", "circuit")

    patch_route(rid, RoutePatch(kind=""), req)
    assert sessions(store)[0]["route_kind"] == "circuit"

    # a shape-only edit must not disturb the name (the dialog sends both)
    patch_route(rid, RoutePatch(name="Bandai Azuma"), req)
    patch_route(rid, RoutePatch(kind="sprint"), req)
    assert sessions(store)[0]["route_name"] == "Bandai Azuma"

    with pytest.raises(HTTPException) as exc:
        patch_route(rid, RoutePatch(kind="rallycross"), req)
    assert exc.value.status_code == 400
    assert sid is not None


def test_route_outline_is_drawn_once_and_then_cached(tmp_path):
    """GET /routes/{id}/outline is what the browse bar's thumbnails read.
    It costs a lap's worth of frame parsing, so the first call fills
    routes.outline and every call after that answers from the column."""
    from app.api.routes import route_outline
    from app.recorder.store import ROUTE_OUTLINE_BOX

    def scenario(sim):
        sim.event(120, "event")
        sim.race_off()

    store = run(scenario, tmp_path)
    rid = sessions(store)[0]["route_id"]
    req = _request_for(store)
    assert store.get_route(rid)["outline"] is None

    out = route_outline(rid, req)
    pts = out["outline"]
    assert out["box"] == ROUTE_OUTLINE_BOX
    assert len(pts) >= 8 and len(pts) % 2 == 0        # flat (x, y) pairs
    assert all(isinstance(v, int) for v in pts)       # JSON-compact
    assert all(0 <= v <= ROUTE_OUTLINE_BOX for v in pts)
    # normalized on the longest axis, so one of the two must reach the box
    assert (max(pts[0::2]) == ROUTE_OUTLINE_BOX
            or max(pts[1::2]) == ROUTE_OUTLINE_BOX)

    assert json.loads(store.get_route(rid)["outline"]) == pts
    assert route_outline(rid, req)["outline"] == pts   # served from the cache

    with pytest.raises(HTTPException) as exc:
        route_outline(rid + 99, req)
    assert exc.value.status_code == 404


def test_route_outline_miss_is_not_cached(tmp_path):
    """A route whose captures were all deleted has nothing to draw today but
    may be driven again tomorrow, so a miss must leave the column NULL rather
    than pin an empty outline on it forever."""
    from app.api.routes import route_outline

    store = Store(str(tmp_path / "outline.db"))
    rid = store.match_or_create_route(0.0, 0.0, 1000.0, 100.0, 100.0)
    req = _request_for(store)

    assert route_outline(rid, req)["outline"] is None
    assert store.get_route(rid)["outline"] is None
    store.close()


def test_route_kind_reaches_both_session_payloads(tmp_path):
    """list_sessions and _SESSION_SELECT are separate hand-written joins:
    the sidebar cards come from one and the detail view from the other, so a
    field added to only one of them silently goes missing on the other."""
    from app.api.routes import session_laps, sessions as sessions_ep

    def scenario(sim):
        sim.sprint(60)
        sim.race_off()

    store = run(scenario, tmp_path)
    req = _request_for(store)

    card = sessions_ep(req)[0]
    detail = session_laps(card["id"], req)["session"]
    assert card["route_kind"] == "sprint"
    assert detail["route_kind"] == "sprint"
    assert "route_kind_auto" in card and "route_kind_auto" in detail


def test_suggestions_are_valid_track_types():
    """Cross-file invariant: everything the classifier can suggest must be a
    member of the API's TRACK_TYPES (= TRACK_META = #track-select)."""
    from app.api.routes import TRACK_TYPES
    assert {"road", "dirt", "cross", "wtc"} <= TRACK_TYPES


def test_route_kinds_invariant():
    """Cross-file invariant: ROUTE_KINDS (store.py, re-exported by the API)
    = the shape picker's options (analysis.js renameRoute) = the branches of
    lapWord / lapLabel (common.js). Adding a third shape means touching all
    three."""
    from app.api.routes import ROUTE_KINDS
    assert ROUTE_KINDS == {"circuit", "sprint"}


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


def test_sessions_payload_carries_every_browse_facet(tmp_path):
    """The analysis browse bar filters client-side over this payload - there
    are no query params by design (app/static/js/browse.js). Every field a
    facet, the search box or a sort option reads must be present, or the
    facet silently empties with nothing to point at."""
    from app.api.routes import sessions as sessions_ep

    def scenario(sim):
        sim.event(120, "event")
        sim.race_off()

    store = run(scenario, tmp_path)
    out = sessions_ep(_request_for(store))[0]

    for field in ("id", "started_at", "name", "display_name",     # search + sort
                  "route_id", "route_name",                       # Route facet
                  "car_class_letter", "car_pi",                   # Class facet
                  "car_ordinal", "car_name", "car_known",         # Car facet
                  "track_type", "conditions", "drivetrain",       # Type / More
                  "lap_count", "best_lap"):                       # card + sort
        assert field in out, f"browse bar reads {field}"


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


def test_dismiss_contact_rejects_landings_and_duplicate_dismissals(tmp_path):
    """API hardening (issue #42): a landing spike never counted as contact,
    so "dismissing" it is a 404 like any non-marker t (it could only strip a
    contact flag the user set by hand), and re-dismissing an
    already-dismissed marker is an idempotent no-op, not a duplicate edit
    row inflating edit_count."""
    from app.api.routes import DismissBody, dismiss_contact, lap_data

    def scenario(sim):
        sim.event(180, "dirty with jumps", dirty=True)
        sim.race_off()

    store = run(scenario, tmp_path, jumps=True)
    sid = sessions(store)[0]["id"]
    req = _request_for(store)
    lap = next(lap for lap in completed_laps(store, sid) if "contact" in flags_of(lap))
    data = lap_data(lap["id"], req, "speed_kmh", 500)
    walls = [c for c in data["collisions"] if not c["landing"]]
    # a landing far enough from every wall hit that ±0.5 s can't match one
    landing = next(c for c in data["collisions"] if c["landing"]
                   and all(abs(c["t"] - w["t"]) > 0.6 for w in walls))

    with pytest.raises(HTTPException) as exc:
        dismiss_contact(lap["id"], DismissBody(t=landing["t"]), req)
    assert exc.value.status_code == 404
    assert store.session_edits(sid) == []  # nothing stored for the rejection

    dismiss_contact(lap["id"], DismissBody(t=walls[0]["t"]), req)
    n_edits = len(store.session_edits(sid))
    out = dismiss_contact(lap["id"], DismissBody(t=walls[0]["t"]), req)
    assert out["ok"]
    assert len(store.session_edits(sid)) == n_edits  # no duplicate edit row


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


# ------------------------- CSV export (issue #29) -------------------------
# Full-rate telemetry out of the app: /data's decimation is for charts, an
# export must carry every kept frame and honor the manual edits above.


def _csv_text(resp):
    """A StreamingResponse body as text (Starlette wraps the sync generator
    into an async iterator, hence the event loop)."""
    async def collect():
        return "".join([chunk async for chunk in resp.body_iterator])
    return asyncio.run(collect())


def _csv_rows(resp):
    return list(csv.reader(io.StringIO(_csv_text(resp))))


def test_export_lap_csv_is_full_rate_with_stable_header(tmp_path):
    """/laps/{id}/export.csv: the documented header, one row per kept frame
    (a clean lap keeps everything, so rows == raw frame count), canonical
    km/h values, monotonic time, and a download disposition."""
    from app.api.routes import _EXPORT_HEADER, export_lap_csv, lap_data

    store = _dirty_store(tmp_path)
    sid = sessions(store)[0]["id"]
    req = _request_for(store)
    lap = next(lap for lap in completed_laps(store, sid) if not flags_of(lap))

    resp = export_lap_csv(lap["id"], req)
    assert resp.headers["content-type"].startswith("text/csv")
    disp = resp.headers["content-disposition"]
    assert disp.startswith('attachment; filename="lapscope_') and disp.endswith('.csv"')

    header, *body = _csv_rows(resp)
    assert header == _EXPORT_HEADER
    chart = lap_data(lap["id"], req, "speed_kmh", 50)
    assert len(body) == chart["n_frames"]  # full rate, nothing decimated away
    assert len(body) > len(chart["dist"])  # far denser than a chart fetch
    assert {r[0] for r in body} == {str(lap["lap_number"] + 1)}
    speeds = [float(r[header.index("speed_kmh")]) for r in body]
    assert max(speeds) > 50.0  # km/h scale, not raw m/s
    ts = [float(r[header.index("t_s")]) for r in body]
    assert ts == sorted(ts)


def test_export_lap_csv_respects_rewind_trim(tmp_path):
    """The rewound-over stretch never reaches an export - the CSV carries the
    same kept trace the charts and the map draw, not the raw frame rows."""
    from app.api.routes import export_lap_csv, lap_data

    store = _dirty_store(tmp_path)
    sid = sessions(store)[0]["id"]
    req = _request_for(store)
    lap = next(lap for lap in completed_laps(store, sid) if "rewind" in flags_of(lap))

    body = _csv_rows(export_lap_csv(lap["id"], req))[1:]
    raw = lap_data(lap["id"], req, "speed_kmh", 50)["n_frames"]
    assert 0 < len(body) < raw


def test_export_session_csv_skips_excluded_and_untimed_laps(tmp_path):
    """/sessions/{id}/export.csv concatenates exactly the timed laps (told
    apart by the lap column): exclusions are honored the way bests/counts do,
    and the untimed post-finish coast never gets a lap column a re-import
    would mint a time for - while either kind stays exportable through its
    own per-lap URL (explicit ask wins)."""
    from app.api.routes import (LapPatch, export_lap_csv, export_session_csv,
                                patch_lap)

    store = _dirty_store(tmp_path)
    sid = sessions(store)[0]["id"]
    req = _request_for(store)
    laps = completed_laps(store, sid)
    assert len(laps) < len(store.session_laps(sid))  # the coast lap exists...

    nums = {r[0] for r in _csv_rows(export_session_csv(sid, req))[1:]}
    assert nums == {str(lap["lap_number"] + 1) for lap in laps}  # ...and is skipped

    victim = laps[1]
    patch_lap(victim["id"], LapPatch(excluded=True), req)
    after = {r[0] for r in _csv_rows(export_session_csv(sid, req))[1:]}
    assert after == nums - {str(victim["lap_number"] + 1)}

    solo = _csv_rows(export_lap_csv(victim["id"], req))[1:]
    assert solo and {r[0] for r in solo} == {str(victim["lap_number"] + 1)}


def test_export_csv_unknown_ids_are_404(tmp_path):
    from app.api.routes import export_lap_csv, export_session_csv

    store = Store(tmp_path / "empty.db")
    req = _request_for(store)
    for handler in (export_lap_csv, export_session_csv):
        with pytest.raises(HTTPException) as exc:
            handler(12345, req)
        assert exc.value.status_code == 404
    store.close()


def test_export_filename_is_windows_and_header_safe():
    """Session names end up inside Content-Disposition and on the user's
    disk: anything outside the safe ASCII set (slashes, quotes, colons,
    unicode) flattens to underscores, Windows-hostile trailing dots/spaces
    are trimmed, and a name reduced to nothing falls back to "export"."""
    from app.api.routes import _export_filename, _safe_filename

    assert _safe_filename('Hökübu / "WTA" <run>: 2.') == "H_k_bu _ _WTA_ _run_ 2"
    assert _safe_filename("...") == "export"

    lap = {"lap_number": 1, "lap_time": 83.456}
    assert _export_filename("My Race", lap) == "lapscope_My Race_lap2_1-23.456.csv"
    assert _export_filename("My Race") == "lapscope_My Race_session.csv"
    untimed = {"lap_number": 2, "lap_time": None}
    assert _export_filename("My Race", untimed) == "lapscope_My Race_lap3.csv"

    # the all-fields variant is a different file, not the same one with more
    # in it - exporting both of a lap must not silently overwrite (issue #90)
    assert _export_filename("My Race", lap, raw=True)         == "lapscope_My Race_lap2_1-23.456_raw.csv"
    assert _export_filename("My Race", raw=True) == "lapscope_My Race_session_raw.csv"


# ------------------- the all-fields export (issue #90) -------------------
# The raw panels could show every packet field, one frame at a time, and the
# export could show a whole lap of nineteen columns. Neither could do both.


def test_export_lap_csv_raw_carries_every_packet_field(tmp_path):
    """raw=1: the curated header, unchanged, followed by one column per
    packet value (wheel groups split FL/FR/RL/RR). Same rows, same trace -
    only wider - and the raw columns really carry the packet, not zeros."""
    from app.api.routes import _EXPORT_HEADER, export_lap_csv
    from app.telemetry.packet import FIELDS

    store = _dirty_store(tmp_path)
    sid = sessions(store)[0]["id"]
    req = _request_for(store)
    lap = next(lap for lap in completed_laps(store, sid) if not flags_of(lap))

    plain_header, *plain_body = _csv_rows(export_lap_csv(lap["id"], req))
    header, *body = _csv_rows(export_lap_csv(lap["id"], req, raw=True))

    assert header[:len(_EXPORT_HEADER)] == _EXPORT_HEADER   # a superset...
    assert plain_body == [r[:len(_EXPORT_HEADER)] for r in body]  # ...to the value

    expected = []
    for name, count in FIELDS:
        expected += ([f"raw_{name}"] if count == 1 else
                     [f"raw_{name}_{w}" for w in ("fl", "fr", "rl", "rr")])
    assert header[len(_EXPORT_HEADER):] == expected
    assert len(header) == 107   # the number the export dialog prints

    col = {c: i for i, c in enumerate(header)}
    row = body[len(body) // 2]   # mid-lap: moving, on the throttle
    # raw_* is the packet verbatim, the curated columns are converted
    assert float(row[col["raw_speed"]]) * 3.6 == pytest.approx(
        float(row[col["speed_kmh"]]), abs=1e-3)
    assert float(row[col["raw_accel"]]) / 2.55 == pytest.approx(
        float(row[col["throttle_pct"]]), abs=1e-3)
    # the tuner's actual ask: per-wheel temps, in the packet's own Fahrenheit
    for w in ("fl", "fr", "rl", "rr"):
        assert float(row[col[f"raw_tire_temp_{w}"]]) > 100.0


def test_export_session_csv_raw_is_one_wide_document(tmp_path):
    """The session variant widens the same way: one header, the same laps,
    every row the full width (a per-lap header would break re-import)."""
    from app.api.routes import export_session_csv

    store = _dirty_store(tmp_path)
    sid = sessions(store)[0]["id"]
    req = _request_for(store)

    plain = _csv_rows(export_session_csv(sid, req))
    raw = _csv_rows(export_session_csv(sid, req, raw=True))

    assert len(raw) == len(plain)
    assert {r[0] for r in raw[1:]} == {r[0] for r in plain[1:]}   # same laps
    assert {len(r) for r in raw} == {107}


# ------------------------- CSV import (the reverse trip) -------------------------


def _import_request(store, text: str, recording: bool = False, *,
                    content_type: str = "text/csv", declared: str | None = None,
                    chunk: int = 1 << 20):
    """Stub request for import_csv: the raw body is the file, streamed the
    way Starlette streams it, and the tracker gate needs an answerable
    session_id. `declared` overrides Content-Length to stage a lying header."""
    req = _request_for(store, tracker=SimpleNamespace(
        session_id=7 if recording else None))
    raw = text.encode()
    req.headers = {"content-type": content_type,
                   "content-length": str(len(raw) if declared is None else declared)}

    async def stream():
        for i in range(0, max(len(raw), 1), chunk):
            yield raw[i:i + chunk]
    req.stream = stream
    return req


def test_import_csv_round_trips_a_session_export(tmp_path):
    """Export the dirty session, import the file back: the timed laps come
    back with their lap times, the telemetry channels survive (the wall-hit
    collision re-detects from the round-tripped G spike), and the session is
    browsable like any recording - just with no car metadata."""
    from app.api.routes import (export_session_csv, import_csv, lap_data,
                                session_laps)
    from app.api.routes import sessions as sessions_ep

    store = _dirty_store(tmp_path)
    sid = sessions(store)[0]["id"]
    req = _request_for(store)
    source = completed_laps(store, sid)

    text = _csv_text(export_session_csv(sid, req))
    # the harness hands back a closed store (reads run on short-lived
    # connections); import writes through the event-loop connection, so it
    # needs the store reopened - exactly as it is in a running app
    store = Store(store.db_path)
    out = asyncio.run(import_csv(_import_request(store, text), name="Round trip"))
    assert out["ok"] and out["laps"] == len(source)

    imported = session_laps(out["session_id"], _request_for(store))["laps"]
    assert len(imported) == len(source)
    for src, imp in zip(source, imported):
        assert imp["lap_number"] == src["lap_number"]
        # the exported clock's last sample sits within a frame of the lap time
        assert abs(imp["lap_time"] - src["lap_time"]) < 0.05

    # the wall hit on lap 2 re-detects from the reconstructed acceleration
    hit_lap = next(lap for lap in imported if lap["lap_number"] == 1)
    data = lap_data(hit_lap["id"], _request_for(store), "speed_kmh,pos_x", 500)
    assert any(not c["landing"] for c in data["collisions"])
    assert data["n_frames"] > 500  # full-rate frames were written

    card = next(s for s in sessions_ep(_request_for(store))
                if s["id"] == out["session_id"])
    assert card["display_name"] == "Round trip"
    assert card["car_name"] == "Unknown car"
    assert card["car_known"] is True  # no ordinal -> nothing to report/name
    assert card["lap_count"] == len(source)
    store.close()


def test_import_csv_accepts_an_all_fields_export(tmp_path):
    """The wide export is still a LapScope export: import reads the curated
    columns it knows and ignores the rest, so a raw file re-opens as the same
    session a curated one would (the raw values are not replayed - the
    synthesized frames stay neutral filler, see _synth_frame)."""
    from app.api.routes import export_session_csv, import_csv, session_laps

    store = _dirty_store(tmp_path)
    sid = sessions(store)[0]["id"]
    req = _request_for(store)
    source = completed_laps(store, sid)

    text = _csv_text(export_session_csv(sid, req, raw=True))
    store = Store(store.db_path)   # imports write, see the round-trip test
    out = asyncio.run(import_csv(_import_request(store, text), name="Raw trip"))
    assert out["ok"] and out["laps"] == len(source)

    imported = session_laps(out["session_id"], _request_for(store))["laps"]
    for src, imp in zip(source, imported):
        assert imp["lap_number"] == src["lap_number"]
        assert abs(imp["lap_time"] - src["lap_time"]) < 0.05
    store.close()


def test_import_csv_accepts_a_minimal_lap_file(tmp_path):
    """A hand-trimmed CSV with only the required columns is a valid import:
    one lap group, timed by the clock's last sample."""
    from app.api.routes import import_csv

    store = Store(tmp_path / "imp.db")
    text = ("lap,t_s,dist_m,speed_kmh,lap_time_s,pos_x_m,pos_z_m\n"
            "1,0.0,0.0,100.0,0.017,0.0,0.0\n"
            "1,1.0,27.8,100.0,1.017,27.8,0.0\n")
    out = asyncio.run(import_csv(_import_request(store, text), name=""))
    assert out["laps"] == 1 and out["frames"] == 2

    laps = store.session_laps(out["session_id"])
    assert len(laps) == 1 and abs(laps[0]["lap_time"] - 1.017) < 1e-6
    session = store.get_session(out["session_id"])
    assert session["name"] == "Imported session"  # fallback name
    store.close()


def test_import_csv_rejects_garbage(tmp_path):
    """Nothing is written on a bad file: wrong header, malformed numbers,
    and empty bodies are 400s that name the problem (and the line), and the
    reprocess-style 409 guards a live recording."""
    from app.api.routes import import_csv

    store = Store(tmp_path / "imp.db")
    ok_header = "lap,t_s,dist_m,speed_kmh,lap_time_s,pos_x_m,pos_z_m\n"

    with pytest.raises(HTTPException) as exc:
        asyncio.run(import_csv(_import_request(store, "a,b\n1,2\n"), name=""))
    assert exc.value.status_code == 400 and "missing columns" in exc.value.detail

    with pytest.raises(HTTPException) as exc:
        asyncio.run(import_csv(
            _import_request(store, ok_header + "1,zero,0,0,0,0,0\n"), name=""))
    assert exc.value.status_code == 400 and "line 2" in exc.value.detail

    with pytest.raises(HTTPException) as exc:
        asyncio.run(import_csv(_import_request(store, ok_header), name=""))
    assert exc.value.status_code == 400  # header only, no data rows

    with pytest.raises(HTTPException) as exc:
        asyncio.run(import_csv(
            _import_request(store, ok_header, recording=True), name=""))
    assert exc.value.status_code == 409

    assert store.list_sessions() == []  # every rejection left the DB alone
    store.close()


def test_import_csv_survives_reprocess(tmp_path):
    """Reprocessing an imported session must keep its lap times (issue #39):
    each synthesized lap group's final frame carries the group's lap time as
    LastLap, so the replay re-times every lap through the LastLap-change
    finish - including a session of identical consecutive lap times, which
    the LapNumber-increment path (LastLap on the next group's frames) could
    not tell apart. Before the fix this replay found 0 completed laps and
    the times were gone until a re-import."""
    from app.api.routes import import_csv
    from app.recorder.reprocess import reprocess_session

    store = Store(tmp_path / "imp.db")
    rows = ["lap,t_s,dist_m,speed_kmh,lap_time_s,pos_x_m,pos_z_m\n"]
    for lap in (1, 2):  # two identical 12 s laps (> the 5 s lap-age gate)
        for i in range(121):
            t = i * 0.1
            rows.append(f"{lap},{t:.1f},{t * 30:.1f},108.0,"
                        f"{t + 0.017:.3f},{t * 30:.1f},0.0\n")
    out = asyncio.run(import_csv(_import_request(store, "".join(rows)), name=""))
    sid = out["session_id"]
    before = [lap["lap_time"] for lap in store.session_laps(sid)]
    assert len(before) == 2 and all(t == pytest.approx(12.017) for t in before)

    found = reprocess_session(store, sid)
    after = [lap["lap_time"] for lap in store.session_laps(sid)]
    session = store.get_session(sid)
    store.close()
    assert found == 2
    assert all(t == pytest.approx(12.017, abs=0.05) for t in after)
    # the synthesized suspension is flat, not evidence of tarmac: a replay
    # must not auto-tag imported sessions with a track type
    assert session["track_type"] is None


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


def test_a_crash_mid_replay_leaves_the_original_laps_intact(tmp_path, monkeypatch):
    """Issue #60: reprocess deletes the session's laps before rebuilding them,
    so anything that raises mid-replay used to leave the session with no lap
    times at all - permanently, because the same frames crash the same way on
    every retry. The rollback has to put back every lap, and the endpoint has
    to say the data survived instead of a bare 500."""
    from app.api.routes import reprocess
    from app.recorder import reprocess as reprocess_mod

    def scenario(sim):
        sim.event(120, "event")
        sim.race_off()

    store = run(scenario, tmp_path)
    sid = sessions(store)[0]["id"]
    before = [(lap["lap_number"], lap["lap_time"]) for lap in store.session_laps(sid)]
    assert [t for _, t in before if t]  # the session really has timed laps

    real = reprocess_mod.SessionTracker

    class ExplodingTracker(real):
        """Dies a few hundred frames in - past the first lap boundary, so the
        replay has already written rows of its own by the time it fails."""
        seen = 0

        def on_frame(self, t, raw, frame):
            ExplodingTracker.seen += 1
            if ExplodingTracker.seen > 400:
                raise RuntimeError("detection blew up")
            return super().on_frame(t, raw, frame)

    monkeypatch.setattr(reprocess_mod, "SessionTracker", ExplodingTracker)
    store2 = Store(store.db_path)  # replay writes via the event-loop connection
    req = _request_for(store2, SimpleNamespace(session_id=None))
    with pytest.raises(HTTPException) as err:
        asyncio.run(reprocess(sid, req))
    store2.close()

    assert err.value.status_code == 500
    assert "left unchanged" in err.value.detail
    after = [(lap["lap_number"], lap["lap_time"]) for lap in store.session_laps(sid)]
    assert after == before


# ------------------------- merged run groups -------------------------
# A group is an index over sessions, never a rewrite of them: creating,
# ungrouping and removing members must all leave laps, frames and edits alone.


def _two_sessions_on_one_route(tmp_path):
    """Two events on the stadium loop: same route, same car - the shape a
    real sprint grind has, minus the wall-clock wait."""
    def scenario(sim):
        for i in range(2):
            sim.event(75, f"event {i + 1}")
        sim.race_off()

    store = run(scenario, tmp_path)
    ss = sorted(sessions(store), key=lambda s: s["started_at"])
    return store, [s["id"] for s in ss]


def test_group_create_validates_route_and_car(tmp_path):
    """Same route AND same car, both known, and nothing already grouped -
    anything else and the run table's best/gap column compares laps of
    different courses."""
    from app.api.routes import GroupCreate, create_group

    store, ids = _two_sessions_on_one_route(tmp_path)
    req = _request_for(store)

    out = create_group(GroupCreate(name="  Rivals grind  ", session_ids=ids), req)
    assert out["group"]["name"] == "Rivals grind"
    assert out["group"]["session_count"] == 2 and out["group"]["mixed"] is False
    gid = out["group"]["id"]
    assert all(s["group_id"] == gid for s in sessions(store))

    with pytest.raises(HTTPException) as exc:  # already grouped
        create_group(GroupCreate(session_ids=ids), req)
    assert exc.value.status_code == 409

    with pytest.raises(HTTPException) as exc:
        create_group(GroupCreate(session_ids=[max(ids) + 99]), req)
    assert exc.value.status_code == 404

    with pytest.raises(HTTPException) as exc:
        create_group(GroupCreate(session_ids=[]), req)
    assert exc.value.status_code == 400

    # a different route on either side is refused, as is an unidentified one
    with store.reader() as conn:
        conn.execute("UPDATE sessions SET group_id = NULL")
        conn.execute("UPDATE sessions SET route_id = 9999 WHERE id = ?", (ids[1],))
        conn.commit()
    with pytest.raises(HTTPException) as exc:
        create_group(GroupCreate(session_ids=ids), req)
    assert exc.value.status_code == 400 and "same route" in exc.value.detail

    with store.reader() as conn:
        conn.execute("UPDATE sessions SET route_id = NULL WHERE id = ?", (ids[1],))
        conn.commit()
    with pytest.raises(HTTPException) as exc:
        create_group(GroupCreate(session_ids=ids), req)
    assert exc.value.status_code == 400 and "identified route" in exc.value.detail


def test_group_laps_scores_across_the_whole_group(tmp_path):
    """The point of merging: one best over every attempt, gaps measured
    against it, and runs numbered in the order they were driven (each sprint
    member's own lap_number is 0, so run_index has to come from the group)."""
    from app.api.routes import GroupCreate, create_group, group_laps

    store, ids = _two_sessions_on_one_route(tmp_path)
    req = _request_for(store)
    gid = create_group(GroupCreate(session_ids=ids), req)["group"]["id"]

    payload = group_laps(gid, req)
    laps = payload["laps"]
    assert [s["id"] for s in payload["sessions"]] == ids  # oldest first
    # every row each session would list on its own, incomplete laps included
    assert len(laps) == sum(len(store.session_laps(i)) for i in ids)
    assert [lap["id"] for lap in laps] == sorted(
        (lap["id"] for lap in laps), key=lambda i: i)  # driven order
    assert [lap["run_index"] for lap in laps] == list(range(1, len(laps) + 1))
    assert sum(lap["is_best"] for lap in laps) == 1  # one best over the group

    best = min(lap["lap_time"] for lap in laps if lap["lap_time"])
    for lap in laps:
        if lap["lap_time"]:
            assert lap["gap_to_best"] == pytest.approx(lap["lap_time"] - best)
    assert payload["group"]["best_lap"] == pytest.approx(best)
    # the best is genuinely cross-session, not each session's own
    assert len({lap["session_id"] for lap in laps}) == 2


def test_group_laps_honors_excluded_laps(tmp_path):
    """group_laps must run the same read-time edit overlay session_laps does,
    or an excluded lap would come back and win the group."""
    from app.api.routes import (GroupCreate, LapPatch, create_group, group_laps,
                                patch_lap)

    store, ids = _two_sessions_on_one_route(tmp_path)
    req = _request_for(store)
    gid = create_group(GroupCreate(session_ids=ids), req)["group"]["id"]

    was_best = next(lap for lap in group_laps(gid, req)["laps"] if lap["is_best"])
    patch_lap(was_best["id"], LapPatch(excluded=True), req)

    laps = group_laps(gid, req)["laps"]
    now = {lap["id"]: lap for lap in laps}
    assert now[was_best["id"]]["excluded"] is True
    assert now[was_best["id"]]["is_best"] is False
    assert sum(lap["is_best"] for lap in laps) == 1
    assert group_laps(gid, req)["group"]["edit_count"] == 1


def test_ungroup_clears_group_id_on_every_member(tmp_path):
    """DELETE /groups/{id} is two statements in one transaction on purpose:
    reader() doesn't enable foreign keys, so an ON DELETE SET NULL would
    leave every member pointing at a row that no longer exists."""
    from app.api.routes import GroupCreate, create_group, delete_group

    store, ids = _two_sessions_on_one_route(tmp_path)
    req = _request_for(store)
    gid = create_group(GroupCreate(session_ids=ids), req)["group"]["id"]

    delete_group(gid, req)
    assert store.get_group(gid) is None
    assert all(s["group_id"] is None for s in sessions(store))
    assert all(completed_laps(store, i) for i in ids)  # nothing was rewritten


def test_deleting_a_member_keeps_the_group_and_prunes_when_empty(tmp_path):
    """A group outlives one member being deleted, and goes away with the
    last one rather than lingering as an orphan."""
    from app.api.routes import GroupCreate, create_group, delete_session

    store, ids = _two_sessions_on_one_route(tmp_path)
    req = _request_for(store, tracker=SimpleNamespace(session_id=None))
    gid = create_group(GroupCreate(session_ids=ids), req)["group"]["id"]

    delete_session(ids[0], req)
    assert store.get_group(gid) is not None
    assert [s["id"] for s in store.group_sessions(gid)] == [ids[1]]

    delete_session(ids[1], req)
    assert store.get_group(gid) is None


def test_group_membership_edits_and_mixed_reporting(tmp_path):
    """Add and remove a member through the endpoints, and check that a group
    whose member later moved route reads as mixed with a 200 - never a 409
    the user cannot get out of."""
    from app.api.routes import (GroupCreate, GroupMember, add_group_session,
                                create_group, group_laps, remove_group_session)

    store, ids = _two_sessions_on_one_route(tmp_path)
    req = _request_for(store)
    gid = create_group(GroupCreate(session_ids=[ids[0]]), req)["group"]["id"]

    add_group_session(gid, GroupMember(session_id=ids[1]), req)
    assert group_laps(gid, req)["group"]["session_count"] == 2

    out = remove_group_session(gid, ids[1], req)
    assert out["pruned"] is False
    assert group_laps(gid, req)["group"]["session_count"] == 1

    add_group_session(gid, GroupMember(session_id=ids[1]), req)
    with store.reader() as conn:  # as a reprocess re-fingerprinting it would
        conn.execute("UPDATE sessions SET route_id = 9999 WHERE id = ?", (ids[1],))
        conn.commit()
    assert group_laps(gid, req)["group"]["mixed"] is True


def test_removing_a_session_from_the_wrong_group_changes_nothing(tmp_path):
    """Issue #63: the endpoint checked that the *group* existed and then cleared
    whatever session id it was handed. Naming group A while passing a member of
    group B ungrouped that session and pruned B as newly empty - a request about
    A silently deleting B."""
    from app.api.routes import GroupCreate, create_group, remove_group_session

    def scenario(sim):
        for i in range(3):
            sim.event(75, f"event {i + 1}")
        sim.race_off()

    store = run(scenario, tmp_path)
    ids = [s["id"] for s in sorted(sessions(store), key=lambda s: s["started_at"])]
    req = _request_for(store)
    a = create_group(GroupCreate(name="A", session_ids=ids[:2]), req)["group"]["id"]
    b = create_group(GroupCreate(name="B", session_ids=ids[2:]), req)["group"]["id"]

    with pytest.raises(HTTPException) as err:
        remove_group_session(a, ids[2], req)  # a member of B, named through A
    assert err.value.status_code == 404
    assert store.get_group(b) is not None
    assert [s["id"] for s in store.group_sessions(b)] == [ids[2]]
    assert [s["id"] for s in store.group_sessions(a)] == ids[:2]

    # ...and a genuine removal still works, pruning included
    assert remove_group_session(b, ids[2], req)["pruned"] is True
    assert store.get_group(b) is None


def test_list_sessions_aggregates_survive_the_group_join(tmp_path):
    """list_sessions grew a LEFT JOIN for group_name; it is 1:1, so the
    lap_count / best_lap aggregates beside it must not change."""
    from app.api.routes import GroupCreate, create_group

    store, ids = _two_sessions_on_one_route(tmp_path)
    before = {s["id"]: (s["lap_count"], s["best_lap"]) for s in sessions(store)}
    create_group(GroupCreate(name="grind", session_ids=ids), _request_for(store))
    after = sessions(store)
    assert {s["id"]: (s["lap_count"], s["best_lap"]) for s in after} == before
    assert all(s["group_name"] == "grind" for s in after)


def test_compact_reports_bytes_and_waits_for_the_recorder(tmp_path):
    """The Settings action behind issue #59: it must refuse while a session is
    recording (VACUUM holds an exclusive lock for the whole rebuild, on the
    event-loop connection), and otherwise report what it actually gave back."""
    from app.api.routes import compact_storage, delete_session, storage

    def scenario(sim):
        sim.event(120, "event")
        sim.race_off()

    store = run(scenario, tmp_path)
    sid = sessions(store)[0]["id"]

    before = storage(_request_for(store))
    assert before["db_bytes"] > 0 and before["sessions"] == 1

    recording = _request_for(store, SimpleNamespace(session_id=sid))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(compact_storage(recording))
    assert exc.value.status_code == 409

    # run() closed the event-loop connection the rebuild goes through
    store2 = Store(store.db_path)
    idle = _request_for(store2, SimpleNamespace(session_id=None))
    delete_session(sid, idle)
    out = asyncio.run(compact_storage(idle))
    assert out["ok"] and out["reclaimed_bytes"] > 0
    assert out["after_bytes"] < out["before_bytes"]
    assert storage(idle)["db_bytes"] == out["after_bytes"]
    store2.close()


def test_the_chart_point_budget_is_actually_respected(tmp_path):
    """Issue #65: the stride was len(kept) // max_points, which is 1 for
    anything under twice the budget - so a 2562-frame lap answered a request
    for 1500 points with all 2562 of them, and the client paid for the extra
    in interpolation and chart rebuilds."""
    from app.api.routes import lap_data

    store = _dirty_store(tmp_path)
    req = _request_for(store)
    lap = next(lap for lap in completed_laps(store, sessions(store)[0]["id"])
               if not flags_of(lap))
    n = lap_data(lap["id"], req, "speed_kmh", 20000)["n_frames"]

    budget = n // 2 + 1  # the worst case: the old stride rounded down to 1
    data = lap_data(lap["id"], req, "speed_kmh", budget)
    assert n > budget                      # there is something to decimate
    assert len(data["dist"]) <= budget     # and the answer stays inside the ask
    assert len(data["dist"]) > budget / 2  # without throwing away the budget
    assert len(data["channels"]["speed_kmh"]) == len(data["dist"]) == len(data["t"])


def _flat_rows(n: int, *, rewind_at: int | None = None, rewind_by: int = 0):
    """n grounded frames, one meter apart, optionally rewinding the odometer."""
    from app.telemetry.packet import empty_fields, pack

    rows = []
    for i in range(n):
        d = float(i)
        if rewind_at is not None and i >= rewind_at:
            d = float(i - rewind_by)
        f = empty_fields()
        f.update(is_race_on=1, distance_traveled=d, pos_x=d,
                 norm_susp_travel=[0.5] * 4, tire_combined_slip=[0.3] * 4)
        rows.append((i / 60, pack(f)))
    return rows


def test_a_rewind_is_trimmed_the_same_whatever_the_window(monkeypatch):
    """The scan streams now, so a frame can only be rewound away while it is
    still inside REWIND_WINDOW. Any window wider than the rewind itself must
    give the identical trace - the window bounds memory, not behavior."""
    from app.api import routes

    rows = _flat_rows(400, rewind_at=300, rewind_by=120)
    full = [d for _, d, _ in routes._LapScan(rows, 0.0)]
    assert len(full) < len(rows)  # the rewound-over stretch really is dropped

    monkeypatch.setattr(routes, "REWIND_WINDOW", 200)  # still wider than the rewind
    assert [d for _, d, _ in routes._LapScan(rows, 0.0)] == full


def test_the_lap_scan_holds_a_window_not_the_whole_lap():
    """Issue #65: it used to keep every parsed frame - 506 MB measured on a
    real 119k-frame lap, per request, in the threadpool. Peak allocation must
    now be flat in the length of the lap instead of linear in it."""
    import tracemalloc

    from app.api.routes import _LapScan

    def peak_for(n: int) -> int:
        rows = _flat_rows(n)  # built before tracing: only the scan is measured
        tracemalloc.start()
        _LapScan(rows, 0.0).events()
        high = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        return high

    short, long = peak_for(2_000), peak_for(20_000)
    assert long < short * 1.5   # 10x the lap, same working set
    assert long < 20_000 * 500  # and nowhere near the ~4.2 KB per frame it kept


def test_status_reports_a_recorder_that_cannot_write():
    """Issue #64: packets keep arriving and every gauge keeps moving while a
    write failure quietly drops the recording, so /api/status has to be the
    one place that says so - it is what the dashboard banner reads."""
    from app.api.routes import status
    from app.telemetry.hub import Hub

    tracker = SimpleNamespace(session_id=3, best_lap_time=None,
                              write_error=None, frames_dropped=0)
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        hub=Hub(), tracker=tracker, udp_port=9999, udp_error=None)))
    assert asyncio.run(status(req))["write_error"] is None

    tracker.write_error, tracker.frames_dropped = "database or disk is full", 240
    out = asyncio.run(status(req))
    assert out["write_error"] == "database or disk is full"
    assert out["frames_dropped"] == 240
    assert out["session_active"] is True  # still "recording", which is the trap


def test_offline_mode_refuses_the_two_outbound_refreshes(monkeypatch):
    """Issue #76: LapScope's pitch is "no cloud, your data stays on your
    machine", so an install has to be able to opt out of the network entirely.
    LS_OFFLINE is the install-wide switch (the Settings toggle only covers one
    browser) and it has to stop the refresh *before* urllib is reached - a 403
    naming the switch, not a 502 that reads like the download failed."""
    from app.api import routes

    monkeypatch.setattr(routes, "OFFLINE", True)
    # would raise on any network attempt: the guard has to come first
    monkeypatch.setattr(routes.cars, "refresh",
                        lambda: pytest.fail("reached the network with LS_OFFLINE set"))
    monkeypatch.setattr(routes.tracks, "refresh",
                        lambda: pytest.fail("reached the network with LS_OFFLINE set"))

    for call in (lambda: routes.refresh_cars(),
                 lambda: routes.refresh_tracks(_request_for(None))):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(call())
        assert exc.value.status_code == 403
        assert "LS_OFFLINE" in exc.value.detail

    # and the page is told, so it never makes the GitHub call it owns itself
    assert routes.version()["offline"] is True
    monkeypatch.setattr(routes, "OFFLINE", False)
    assert routes.version()["offline"] is False


def test_import_csv_rejects_an_out_of_range_lap_number(tmp_path):
    """Issue #66: the lap column went unchecked into a uint16 packet field.
    70000 raised struct.error - an uncaught 500 from an endpoint that
    promises 400s carrying the line - and 0 was the quiet one: it stored
    lap_number -1, which re-exported as lap 0 and re-imported as -2."""
    from app.api.routes import IMPORT_MAX_LAP, import_csv

    store = Store(tmp_path / "imp.db")
    header = "lap,t_s,dist_m,speed_kmh,lap_time_s,pos_x_m,pos_z_m\n"
    for lap_no in (70000, 0, -1, IMPORT_MAX_LAP + 1):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(import_csv(_import_request(
                store, f"{header}{lap_no},0.0,0.0,100.0,0.017,0.0,0.0\n"), name=""))
        assert exc.value.status_code == 400
        assert "line 2" in exc.value.detail and "out of range" in exc.value.detail

    # the boundary itself is a valid lap and still imports
    out = asyncio.run(import_csv(_import_request(
        store, f"{header}{IMPORT_MAX_LAP},0.0,0.0,100.0,0.017,0.0,0.0\n"
               f"{IMPORT_MAX_LAP},1.0,27.8,100.0,1.017,27.8,0.0\n"), name=""))
    assert store.session_laps(out["session_id"])[0]["lap_number"] == IMPORT_MAX_LAP - 1
    store.close()


def test_import_csv_bounds_the_body_and_demands_a_csv(tmp_path, monkeypatch):
    """Issue #66/#67: the body was read whole, on the event loop, with no cap
    and no content-type check - so a huge upload froze the live dashboard
    (the kernel drops UDP meanwhile), and being a CORS-simple request meant
    any page the user visited could POST sessions into their database."""
    from app.api.routes import IMPORT_MAX_BYTES, import_csv

    store = Store(tmp_path / "imp.db")
    text = ("lap,t_s,dist_m,speed_kmh,lap_time_s,pos_x_m,pos_z_m\n"
            "1,0.0,0.0,100.0,0.017,0.0,0.0\n")

    for content_type in ("", "text/plain", "application/x-www-form-urlencoded"):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(import_csv(
                _import_request(store, text, content_type=content_type), name=""))
        assert exc.value.status_code == 415
    # charset parameters are part of a normal browser upload, not a mismatch
    asyncio.run(import_csv(
        _import_request(store, text, content_type="text/csv; charset=utf-8"), name=""))

    # refused on the declared size, before a byte of it is transferred...
    with pytest.raises(HTTPException) as exc:
        asyncio.run(import_csv(_import_request(
            store, text, declared=str(IMPORT_MAX_BYTES + 1)), name=""))
    assert exc.value.status_code == 413

    # ...and refused again while streaming, for a chunked upload that
    # declared nothing at all (a tiny cap keeps the test cheap)
    from app.api import routes

    monkeypatch.setattr(routes, "IMPORT_MAX_BYTES", 1024)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(import_csv(_import_request(
            store, "x" * 4096, declared="", chunk=256), name=""))
    assert exc.value.status_code == 413

    assert len(store.list_sessions()) == 1  # only the well-formed one landed
    store.close()


# ------------------------- host / origin guards (issue #67) -------------------------
# "Self-hosted on localhost" is not a boundary against the user's own browser:
# DNS rebinding reaches the whole API, and WebSocket handshakes skip CORS.


def test_the_host_guard_answers_addresses_but_not_borrowed_names():
    """A page the user visits can point its own domain at 127.0.0.1 and then
    talk to LapScope as if it were same-origin - DELETE /api/sessions/{id}
    included. That always arrives with the attacker's name in Host, never a
    bare address, which is exactly the line this draws."""
    from app.main import host_allowed

    for header in ("localhost:8000", "127.0.0.1:8000", "127.0.0.1", "[::1]:8000",
                   "192.168.1.20:8000", "nas.local:8000", "lapscope.localhost"):
        assert host_allowed(header), header
    for header in ("evil.example.com", "evil.example.com:8000", "", "   ",
                   "lapscope.evil.com"):
        assert not host_allowed(header), header


def test_the_host_guard_is_wired_in_and_extensible(monkeypatch):
    """It runs as the outermost middleware (nothing reaches a handler first),
    and an operator can name their own hostname instead of being locked out."""
    from app import main

    assert main.app.user_middleware[0].kwargs["dispatch"] is main.check_host

    async def call_next(_request):
        return "the handler ran"

    def call(host):
        req = SimpleNamespace(headers={"host": host})
        return asyncio.run(main.check_host(req, call_next))

    assert call("127.0.0.1:8000") == "the handler ran"
    assert call("lapscope.example.com").status_code == 400
    monkeypatch.setattr(main, "ALLOWED_HOSTS", {"lapscope.example.com"})
    assert call("lapscope.example.com") == "the handler ran"


class FakeWS:
    """Enough of a Starlette WebSocket for ws_live: a handshake, a send, and a
    receive that stays pending until something is queued on `incoming` - which
    is what the real one does between frames."""

    def __init__(self, origin):
        from app.telemetry.hub import Hub

        self.headers = {"origin": origin, "host": "localhost:8000"}
        self.app = SimpleNamespace(state=SimpleNamespace(hub=Hub()))
        self.accepted, self.close_code = False, None
        self.incoming: list[dict] = []
        self.sent: list[dict] = []

    async def accept(self):
        self.accepted = True

    async def close(self, code=1000):
        self.close_code = code

    async def send_json(self, msg):
        self.sent.append(msg)

    async def receive(self):
        while not self.incoming:
            await asyncio.sleep(0.001)  # nothing arrives unless a test queues it
        return self.incoming.pop(0)


def test_ws_live_only_talks_to_the_page_it_served():
    """/ws/live streams live position, speed and car. WebSockets are exempt
    from CORS, so without an Origin check any page the user has open can
    subscribe to it while they drive."""
    from app.main import origin_allowed, ws_live

    assert origin_allowed("http://localhost:8000", "localhost:8000")
    assert origin_allowed(None, "localhost:8000")  # a script, not a browser
    assert not origin_allowed("http://evil.example.com", "localhost:8000")
    assert not origin_allowed("null", "localhost:8000")  # file:// / sandboxed
    assert not origin_allowed("http://localhost:8001", "localhost:8000")

    ws = FakeWS("http://evil.example.com")
    asyncio.run(ws_live(ws))
    assert ws.close_code == 1008 and not ws.accepted

    ws = FakeWS("http://localhost:8000")  # the dashboard itself: accepted,
    with pytest.raises(asyncio.TimeoutError):  # then waits for frames forever
        asyncio.run(asyncio.wait_for(ws_live(ws), 0.05))
    assert ws.accepted and ws.close_code is None


def test_ws_live_lets_go_as_soon_as_the_socket_closes():
    """The handler has nothing to send between frames, and FH6 stops sending
    the moment you pause - so it cannot wait on the queue alone. A shutting-down
    server tells the connection to close, and if that goes unnoticed the stop
    waits out its whole grace period and logs the cancellation as an error."""
    from app.main import ws_live

    ws = FakeWS("http://localhost:8000")
    ws.incoming.append({"type": "websocket.disconnect", "code": 1001})
    hub = ws.app.state.hub
    # No timeout guard on purpose: if the handler ever stops noticing the
    # disconnect, this test hangs, which is the symptom being guarded against.
    asyncio.run(ws_live(ws))
    assert ws.accepted
    assert not hub._subscribers, "the queue outlived the socket"
