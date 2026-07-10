"""The AGENTS.md event-detection test matrix, run headlessly.

Each test drives the simulator scenario straight through a real
SessionTracker (see harness.py) and asserts the recorder's decisions —
session boundaries, lap counts, finish detection, dirty-lap flags — that the
manual `python tools/simulator.py ...` matrix checks by hand. No game, no
container, no real-time wait.
"""

from __future__ import annotations

from harness import completed_laps, flags_of, run, sessions


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
