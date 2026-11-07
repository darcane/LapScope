"""The AGENTS.md event-detection test matrix, run headlessly.

Each test drives the simulator scenario straight through a real
SessionTracker (see harness.py) and asserts the recorder's decisions —
session boundaries, lap counts, finish detection, dirty-lap flags — that the
manual `python tools/simulator.py ...` matrix checks by hand. No game, no
container, no real-time wait.
"""

from __future__ import annotations

from harness import completed_laps, flags_of, route_of, run, sessions


def test_freeroam_then_two_events_wet_same_route(tmp_path):
    """--freeroam 20 --events 2 --wet: free-roam discarded, 2 wet sessions on
    the same route."""
    def scenario(sim):
        sim.freeroam(20)
        for i in range(2):
            sim.event(75, f"event {i + 1}")
        sim.race_off()

    store = run(scenario, tmp_path, wet=True)
    ss = sessions(store)

    assert len(ss) == 2  # the free-roam session had no laps and was discarded
    for s in ss:
        assert s["lap_count"] >= 1
        assert s["conditions"] == "wet"
    assert ss[0]["route_id"] is not None
    assert ss[0]["route_id"] == ss[1]["route_id"]  # same stadium loop
    # the fingerprint's shape term, measured by the tracker over a full lap:
    # the loop is 840 x 240 m (2 x 600 m straights, 120 m radius ends)
    with store.reader() as conn:
        route = conn.execute("SELECT * FROM routes WHERE id = ?",
                             (ss[0]["route_id"],)).fetchone()
    assert 830 < route["span_x"] < 845
    assert 232 < route["span_z"] < 245


def test_dirty_flags_contact_on_lap2_rewind_on_lap3(tmp_path):
    """--duration 180 --dirty: injected wall contact lands on lap 2, the
    rewind on lap 3 (0-indexed lap_number 1 and 2)."""
    def scenario(sim):
        sim.event(180, "dirty", dirty=True)
        sim.race_off()

    store = run(scenario, tmp_path)
    ss = sessions(store)

    assert len(ss) == 1
    by_number = {lap["lap_number"]: flags_of(lap)
                 for lap in completed_laps(store, ss[0]["id"])}
    assert "contact" in by_number.get(1, "")
    assert "rewind" in by_number.get(2, "")


def test_race_three_laps_all_timed_no_phantom(tmp_path):
    """--race 3: exactly 3 timed laps (the last recovered from the finish, not
    a LapNumber increment) and no leftover open lap."""
    def scenario(sim):
        sim.event(200, "race (3 laps)", race_laps=3)
        sim.race_off()

    store = run(scenario, tmp_path)
    ss = sessions(store)

    assert len(ss) == 1
    all_laps = store.session_laps(ss[0]["id"])
    timed = [lap for lap in all_laps if lap["lap_time"] is not None]
    assert len(timed) == 3
    assert len(all_laps) == 3  # no phantom open lap left behind


def test_sprint_single_run_with_route(tmp_path):
    """--sprint 75: one point-to-point run of ~75 s, finished cleanly by the
    frozen race clock (no cutoff flag), route assigned."""
    def scenario(sim):
        sim.sprint(75)
        sim.race_off()

    store = run(scenario, tmp_path)
    ss = sessions(store)

    assert len(ss) == 1
    laps = completed_laps(store, ss[0]["id"])
    assert len(laps) == 1
    assert 67.0 < laps[0]["lap_time"] < 83.0
    assert ss[0]["route_id"] is not None
    assert flags_of(laps[0]) == ""  # confirmed finish, not an inferred cutoff


def test_dirt_sprint_distance_reset_finish(tmp_path):
    """--dirt 40: real dirt-sprint (CurrentLap counts) finished by the
    DistanceTraveled hard-reset handback; single ~40 s run, no phantom coast
    lap, not flagged cutoff."""
    def scenario(sim):
        sim.dirt_sprint(40)
        sim.race_off()

    store = run(scenario, tmp_path)
    ss = sessions(store)

    assert len(ss) == 1
    all_laps = store.session_laps(ss[0]["id"])
    laps = [lap for lap in all_laps if lap["lap_time"] is not None]
    assert len(laps) == 1
    assert len(all_laps) == 1  # the post-finish coast is not a lap
    assert 33.0 < laps[0]["lap_time"] < 47.0
    assert "cutoff" not in flags_of(laps[0])
    assert ss[0]["route_id"] is not None


def test_touge_cut_dead_at_line(tmp_path):
    """--dirt 40 --cut: touge point-to-point whose stream cuts dead at the
    line at speed; single ~40 s run recovered and flagged cutoff."""
    def scenario(sim):
        sim.dirt_sprint(40, cut=True)  # no race_off: the stream just stops

    store = run(scenario, tmp_path)
    ss = sessions(store)

    assert len(ss) == 1
    laps = completed_laps(store, ss[0]["id"])
    assert len(laps) == 1
    assert 33.0 < laps[0]["lap_time"] < 47.0
    assert "cutoff" in flags_of(laps[0])
    assert ss[0]["route_id"] is not None


def test_sprint_cut_dead_at_line(tmp_path):
    """--sprint 60 --cut: bare point-to-point (no lap fields) cut at the line;
    single ~60 s run recovered and flagged cutoff."""
    def scenario(sim):
        sim.sprint(60, cut=True)

    store = run(scenario, tmp_path)
    ss = sessions(store)

    assert len(ss) == 1
    laps = completed_laps(store, ss[0]["id"])
    assert len(laps) == 1
    assert 52.0 < laps[0]["lap_time"] < 68.0
    assert "cutoff" in flags_of(laps[0])


def test_world_time_attack_three_geometric_laps(tmp_path):
    """--wta 3: no lap fields at all; 3 laps found geometrically and the
    post-finish coast lap deleted at the distance-reset finish."""
    def scenario(sim):
        sim.wta(3)
        sim.race_off()

    store = run(scenario, tmp_path)
    ss = sessions(store)

    assert len(ss) == 1
    all_laps = store.session_laps(ss[0]["id"])
    timed = [lap for lap in all_laps if lap["lap_time"] is not None]
    assert len(timed) == 3
    assert len(all_laps) == 3  # no phantom post-finish lap


def test_wta_cut_dead_at_line(tmp_path):
    """--wta 3 --cut: the stream dies right at the final geometric crossing
    (inside the crossing circle, never exited); the pending crossing is
    finalized at session end and the last lap flagged cutoff."""
    def scenario(sim):
        sim.wta(3, cut=True)  # no race_off: the stream just stops

    store = run(scenario, tmp_path)
    ss = sessions(store)

    assert len(ss) == 1
    all_laps = store.session_laps(ss[0]["id"])
    timed = [lap for lap in all_laps if lap["lap_time"] is not None]
    assert len(timed) == 3
    assert len(all_laps) == 3  # no phantom open lap left behind
    assert "cutoff" in flags_of(timed[-1])  # time inferred at the cut
    assert flags_of(timed[0]) == "" and flags_of(timed[1]) == ""


def test_track_type_circuit_suggests_road(tmp_path):
    """A tarmac circuit (smooth suspension, no jumps) is auto-tagged road at
    session close."""
    def scenario(sim):
        sim.event(150, "event")
        sim.race_off()

    store = run(scenario, tmp_path)
    assert sessions(store)[0]["track_type"] == "road"


def test_track_type_sprint_suggests_road(tmp_path):
    """--sprint 75: a tarmac point-to-point run reads road too."""
    def scenario(sim):
        sim.sprint(75)
        sim.race_off()

    store = run(scenario, tmp_path)
    assert sessions(store)[0]["track_type"] == "road"


def test_track_type_dirt_sprint_suggests_dirt(tmp_path):
    """--dirt 60: washboard suspension at a low jump rate = dirt."""
    def scenario(sim):
        sim.dirt_sprint(60)
        sim.race_off()

    store = run(scenario, tmp_path)
    assert sessions(store)[0]["track_type"] == "dirt"


def test_track_type_cross_country(tmp_path):
    """--dirt 60 --jumps: the same dirt surface flying a crest every few
    hundred meters = cross-country."""
    def scenario(sim):
        sim.dirt_sprint(60)
        sim.race_off()

    store = run(scenario, tmp_path, jumps=True)
    assert sessions(store)[0]["track_type"] == "cross"


def test_track_type_wta_suggests_wtc(tmp_path):
    """--wta 3: geometric laps (loop closure back to the launch anchor) =
    wtc, whatever the surface evidence says."""
    def scenario(sim):
        sim.wta(3)
        sim.race_off()

    store = run(scenario, tmp_path)
    assert sessions(store)[0]["track_type"] == "wtc"


def test_track_type_route_inheritance_beats_telemetry(tmp_path):
    """A tag already on the route wins over the telemetry verdict: correct a
    course once (e.g. to touge) and every later session on it inherits."""
    def scenario(sim):
        sim.event(75, "event")
        sim.race_off()

    store = run(scenario, tmp_path)
    first = sessions(store)[0]
    assert first["track_type"] == "road"                # the telemetry verdict
    store.set_session_track_type(first["id"], "touge")  # the user corrects it

    store = run(scenario, tmp_path)                     # same DB -> same route
    ss = sessions(store)
    assert len(ss) == 2
    # ids are monotonic; started_at can tie under the harness's restarted clock
    newest = max(ss, key=lambda s: s["id"])
    assert newest["id"] != first["id"]
    assert newest["track_type"] == "touge"              # inherited, not re-derived


def test_jumps_do_not_break_a_point_to_point_run(tmp_path):
    """--sprint 75 --jumps: cross-country-style elevation spikes still record
    a single clean run (3D-map scaling is a frontend concern). The jumps go
    airborne and land with spikes past IMPACT_ACCEL - the landing classifier
    must keep the run free of the contact flag."""
    def scenario(sim):
        sim.sprint(75)
        sim.race_off()

    store = run(scenario, tmp_path, jumps=True)
    ss = sessions(store)

    assert len(ss) == 1
    laps = completed_laps(store, ss[0]["id"])
    assert len(laps) == 1
    assert "contact" not in flags_of(laps[0])


def test_jump_landings_clean_but_wall_contact_still_flags(tmp_path):
    """--duration 180 --dirty --jumps: every lap flies two bumps and lands
    hard (ground-plane spikes past the contact threshold) - those must NOT
    flag contact; the wall hit injected on lap 2 still must."""
    def scenario(sim):
        sim.event(180, "dirty with jumps", dirty=True)
        sim.race_off()

    store = run(scenario, tmp_path, jumps=True)
    ss = sessions(store)

    assert len(ss) == 1
    by_number = {lap["lap_number"]: flags_of(lap)
                 for lap in completed_laps(store, ss[0]["id"])}
    assert len(by_number) >= 3
    assert "contact" in by_number.get(1, "")  # the real wall hit still flags
    for n, fl in by_number.items():
        if n != 1:  # every other lap only landed jumps - clean
            assert "contact" not in fl, f"lap {n} flagged by a jump landing: {fl}"


# ------------------------- route shape (routes.kind) -------------------------
# Decides whether the UI says "lap" or "run". The tracker derives it from
# which lap machinery fired (`_route_kind`), so these cover all three route
# assignment sites plus the geometric branch, using the scenarios above.


def test_route_kind_circuit_when_laps_repeat(tmp_path):
    """A course the car comes back to the start of is a circuit: LapNumber
    increments, so _assign_route tags it that way."""
    def scenario(sim):
        sim.event(180, "race", race_laps=3)
        sim.race_off()

    store = run(scenario, tmp_path)
    assert route_of(store, sessions(store)[0]["id"])["kind"] == "circuit"


def test_route_kind_sprint_for_point_to_point(tmp_path):
    """Point-to-point finishes: the DistanceTraveled reset (sprint / dirt
    sprint) and the cut-dead-at-the-line recovery (touge) all produce one
    run, never a LapNumber increment."""
    for i, drive in enumerate((lambda sim: sim.sprint(60),
                               lambda sim: sim.dirt_sprint(60),
                               lambda sim: sim.sprint(60, cut=True))):
        def scenario(sim, drive=drive):
            drive(sim)
            sim.race_off()

        store = run(scenario, tmp_path / f"ptp{i}")
        assert route_of(store, sessions(store)[0]["id"])["kind"] == "sprint"


def test_route_kind_circuit_for_geometric_laps(tmp_path):
    """World Time Attack sends no lap fields at all - laps are detected
    geometrically, which is still evidence the course loops."""
    def scenario(sim):
        sim.wta(3)
        sim.race_off()

    store = run(scenario, tmp_path)
    assert route_of(store, sessions(store)[0]["id"])["kind"] == "circuit"


# -- live route recognition (issue #108) --------------------------------------
# The guess is a WS extra, never stored: `store.route_guesses` is every change
# of it the harness saw, as (session_id, route_guess, DistanceTraveled).

def _event(sim):
    sim.event(75, "event")
    sim.race_off()


def test_live_route_guess_on_lap_one(tmp_path):
    """Second visit to a route: recognized early in lap 1, and the guess is
    the route the fingerprint files the session under at the line."""
    first = run(_event, tmp_path)
    route = sessions(first)[0]["route_id"]
    # no history on lap 1; lap 2 is recognized from this session's own lap 1
    assert [(g, d > 1954) for _, g, d in first.route_guesses] == [(route, True)]

    store = run(_event, tmp_path)
    sid = max(s["id"] for s in sessions(store))
    assert sessions(store)[0]["route_id"] == route
    guesses = [(s, g, d) for s, g, d in store.route_guesses if g is not None]
    assert guesses and guesses[0][:2] == (sid, route)
    assert guesses[0][2] < 0.1 * 1954           # within the first ~200 m
    # held for the rest of the session - no flapping at lap boundaries
    assert [g for s, g, _ in store.route_guesses if s == sid] == [route]


def test_live_route_guess_names_its_reference_lap(tmp_path):
    """route_ref_lap is the stored lap the guess was matched against - the
    line the Live page draws ahead of the car. It is the route's fastest
    lap, and /laps/{id}/data hands its world positions over in one request."""
    from types import SimpleNamespace

    from app.api.routes import lap_data

    first = run(_event, tmp_path)
    first_sid = sessions(first)[0]["id"]
    fastest = min(completed_laps(first, first_sid), key=lambda lap: lap["lap_time"])

    store = run(_event, tmp_path)
    assert store.guess_ref_laps == [fastest["id"]]   # one fetch for the session
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(store=store)))
    d = lap_data(fastest["id"], req, channels="pos_x,pos_z", max_points=1500)
    xs, zs = d["channels"]["pos_x"], d["channels"]["pos_z"]
    assert 50 <= len(xs) <= 1500
    assert 830 < max(xs) - min(xs) < 850      # the whole 840 x 240 m loop
    assert 230 < max(zs) - min(zs) < 250


def test_live_route_guess_shared_start_line(tmp_path):
    """--course a and --course b grid on the same line and share 600 m of
    straight. With both on record, a lap of b is not guessed until the two
    part, and then as b - never as a."""
    a = sessions(run(_event, tmp_path, course="a"))[0]["route_id"]
    store = run(_event, tmp_path, course="b")
    b = max(sessions(store), key=lambda s: s["id"])["route_id"]
    assert a != b                               # two routes on record

    store = run(_event, tmp_path, course="b")
    guesses = [(g, d) for _, g, d in store.route_guesses if g is not None]
    assert guesses and guesses[0][0] == b
    assert guesses[0][1] > 600                  # only once they have parted
    assert all(g == b for g, _ in guesses)


def test_live_route_guess_withdrawn_on_an_unrecorded_course(tmp_path):
    """Only course a on record, then a lap of b: indistinguishable from a
    down the shared straight, so a is guessed - and withdrawn once the car
    goes straight on where a turns. Not re-guessed as a on the next lap -
    which is recognized as b instead, from this session's own first lap."""
    a = sessions(run(_event, tmp_path, course="a"))[0]["route_id"]
    store = run(_event, tmp_path, course="b")
    sid = max(s["id"] for s in sessions(store))
    seen = [(g, d) for s, g, d in store.route_guesses if s == sid]
    assert seen[0][0] == a and seen[0][1] < 600  # the honest early guess
    assert seen[1][0] is None and seen[1][1] > 600  # withdrawn after the split
    b = max(sessions(store), key=lambda s: s["id"])["route_id"]
    assert b != a
    assert seen[2:] == [(b, seen[2][1])] and seen[2][1] > 2554  # lap 2, as b


def test_live_route_guess_never_in_free_roam(tmp_path):
    """Free roam has no route: cruising the very loop on record guesses
    nothing (race_mode gates the matcher)."""
    run(_event, tmp_path)

    def cruise(sim):
        sim.freeroam(60)
        sim.race_off()

    store = run(cruise, tmp_path)
    assert store.route_guesses == []


def test_live_route_guess_point_to_point_and_wta(tmp_path):
    """The other event shapes recognize their route on the second visit:
    a dirt sprint (lap opens on the grid, CurrentLap counting) and a World
    Time Attack (lap re-based at the launch)."""
    for name, drive in (("dirt", lambda sim: sim.dirt_sprint(40)),
                        ("wta", lambda sim: sim.wta(2))):
        def scenario(sim, drive=drive):
            drive(sim)
            sim.race_off()

        run(scenario, tmp_path / name)
        store = run(scenario, tmp_path / name)
        newest = max(sessions(store), key=lambda s: s["id"])
        guesses = [g for s, g, _ in store.route_guesses if s == newest["id"]]
        assert guesses == [newest["route_id"]], name


# -- the live delta from lap 1 (issue #108, phase 3) ---------------------------

def _watch_delta():
    """observe() for run(): every frame's (session, lap, dist, delta, ref, ref lap)."""
    seen = []

    def observe(frame, extras):
        seen.append((extras["session_id"], frame["lap_number"], frame["distance_traveled"],
                     extras["delta"], extras["delta_ref"], extras["delta_ref_lap"]))
    return seen, observe


def _in_car(ordinal, car_class):
    def scenario(sim):
        sim.f.update(car_ordinal=ordinal, car_class=car_class)
        sim.event(75, "event")
        sim.race_off()
    return scenario


def test_live_delta_from_lap_one_against_this_cars_best(tmp_path):
    """Second visit in the same car: lap 1 gets a delta, measured against
    the best stored lap on the route (pb). Once a lap of this session
    completes, the faster of the two is the reference."""
    first_seen, observe1 = _watch_delta()
    first = run(_event, tmp_path, observe=observe1)
    sid1 = sessions(first)[0]["id"]
    best1 = min(completed_laps(first, sid1), key=lambda lap: lap["lap_time"])
    # the first visit has nothing stored to chase: lap 1 has no delta, and
    # lap 2 chases this session's own lap 1 (recognizing the route from it
    # brings no stored reference - this session's laps are excluded)
    assert all(dl is None for _, ln, _, dl, _, _ in first_seen if ln == 0)
    assert {r for _, ln, _, dl, r, _ in first_seen if ln == 1 and dl is not None} == {"session"}

    seen, observe = _watch_delta()
    store = run(_event, tmp_path, seed=99, observe=observe)
    sid = max(s["id"] for s in sessions(store))
    lap1 = [x for x in seen if x[0] == sid and x[1] == 0]
    timed = [x for x in lap1 if x[3] is not None]
    assert timed, "lap 1 should have a delta"
    assert timed[0][2] < 0.1 * 1954                 # from early in lap 1
    assert {(x[4], x[5]) for x in timed} == {("pb", best1["id"])}
    assert all(abs(x[3]) < 3.0 for x in timed)      # same line, similar pace

    mine = min((lap for lap in completed_laps(store, sid) if lap["lap_number"] == 0),
               key=lambda lap: lap["lap_time"])
    faster = mine if mine["lap_time"] < best1["lap_time"] else best1
    lap2 = [x for x in seen if x[0] == sid and x[1] == 1 and x[3] is not None]
    assert lap2 and {(x[4], x[5]) for x in lap2} == {("pb", faster["id"])}


def test_live_delta_class_best_but_never_another_class(tmp_path):
    """No lap in this car on the route: the class best stands in. A car of
    another class gets no stored reference at all - lap 1 has no delta."""
    run(_in_car(269, 6), tmp_path)

    seen, observe = _watch_delta()
    run(_in_car(1000, 6), tmp_path, observe=observe)   # another car, same class
    sid = max(x[0] for x in seen if x[0] is not None)
    refs = {x[4] for x in seen if x[0] == sid and x[1] == 0 and x[3] is not None}
    assert refs == {"class"}

    seen, observe = _watch_delta()
    store = run(_in_car(1001, 3), tmp_path, observe=observe)  # another class
    sid = max(s["id"] for s in sessions(store))
    assert all(x[3] is None for x in seen if x[0] == sid and x[1] == 0)
    assert {x[4] for x in seen if x[0] == sid and x[1] == 1 and x[3] is not None} == {"session"}


def test_live_delta_reference_goes_with_a_withdrawn_guess(tmp_path):
    """Course a on record, a lap of course b: a is guessed (and chased) down
    the shared straight, then withdrawn - and the delta goes with it."""
    run(_event, tmp_path, course="a")
    seen, observe = _watch_delta()
    run(_event, tmp_path, course="b", observe=observe)
    sid = max(x[0] for x in seen if x[0] is not None)
    lap1 = [x for x in seen if x[0] == sid and x[1] == 0]
    chased = [x for x in lap1 if x[4] == "pb"]
    assert chased and max(x[2] for x in chased) < 900      # only before the split
    assert all(x[3] is None for x in lap1 if x[2] > 900)   # nothing after it


def test_live_ghost_lap_is_on_disk_when_it_is_named(tmp_path):
    """The Live page fetches the ghost lap (delta_ref_lap) the moment it
    changes. When it changes to a lap this session just completed, that
    lap's frames have to be in the database already - not up to a second
    of them still buffered, or the ghost stops short of the line."""
    import sqlite3
    db = tmp_path / "telemetry.db"
    checked = []

    def observe(frame, extras):
        lap = extras.get("delta_ref_lap")
        if extras.get("delta_ref") != "session" or lap in {c[0] for c in checked}:
            return
        con = sqlite3.connect(db)
        try:
            sid, ended = con.execute(
                "SELECT session_id, ended_t FROM laps WHERE id = ?", (lap,)).fetchone()
            last = con.execute("SELECT max(t) FROM frames WHERE session_id = ?",
                               (sid,)).fetchone()[0]
        finally:
            con.close()
        checked.append((lap, ended, last))

    run(_event, tmp_path, observe=observe)
    assert checked, "a session best should have become the reference"
    for lap, ended, last in checked:
        assert last is not None and last >= ended, f"lap {lap} not flushed"
