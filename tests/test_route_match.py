"""Live route recognition (issue #108): the reference traces, the per-lap
matcher, and the tracker's failure fence around it. The end-to-end
scenarios (recognized on lap 1, shared start line, unrecorded course, free
roam) live with the rest of the matrix in test_scenarios.py."""

from __future__ import annotations

import logging

import pytest
from harness import run, sessions

from app.recorder import route_match as rm
from app.recorder.route_match import RouteMatcher, RouteRef, RouteRefs, build_ref, lap_origin
from app.telemetry.packet import empty_fields, pack


def ref(route_id, pts, name=None):
    """A reference from [(progress, x, z), ...]."""
    d, x, z = zip(*pts)
    return RouteRef(route_id, name, 1, list(d), list(x), list(z))


def straight(route_id, length=1000.0, heading=(1.0, 0.0)):
    """A straight course from the origin: progress p sits at p * heading."""
    hx, hz = heading
    return ref(route_id, [(p, p * hx, p * hz) for p in range(0, int(length) + 1, 10)])


def drive(m, course, upto, step=5.0, dt=0.1, off=None):
    """Feed `m` a car following `course` (a RouteRef) up to progress `upto`.
    off(p) -> (dx, dz) displaces the car from the course at progress p."""
    t, p = 0.0, step
    while p <= upto:
        x, z = course.pos_at(p)
        if off is not None:
            dx, dz = off(p)
            x, z = x + dx, z + dz
        m.feed(t, p, x, z)
        t += dt
        p += step
    return m


# -- reference traces ------------------------------------------------------

def test_pos_at_interpolates_and_ends():
    r = ref(7, [(0, 0, 0), (10, 10, 0), (20, 10, 10)])
    assert r.pos_at(5) == (5, 0)
    assert r.pos_at(15) == (10, 5)
    assert r.pos_at(0) == (0, 0)
    assert r.pos_at(20.5) is None   # past the end
    assert r.length == 20


def frames(progress_and_pos):
    out = []
    for i, (dist, x, z) in enumerate(progress_and_pos):
        f = empty_fields()
        f.update(distance_traveled=dist, pos_x=x, pos_z=z, is_race_on=1)
        out.append((float(i), pack(f)))
    return out


def test_build_ref_skips_the_grid_and_splices_a_rewind():
    lap = {"id": 3, "start_distance": 0.0}
    seq = [(0, 0, 0)] * 5                                      # grid hold
    seq += [(p, p, 0) for p in range(1, 301)]                  # drive to 300
    seq += [(p, p, 0) for p in range(299, 199, -1)]            # rewind to 200
    seq += [(p, p, 50) for p in range(201, 501)]               # re-driven, offset
    r = build_ref(9, "x", lap, frames(seq))
    assert r.d[0] == 1 and r.length >= 490
    assert all(b > a for a, b in zip(r.d, r.d[1:]))            # strictly increasing
    assert r.pos_at(150)[1] == 0                               # before the rewind point
    assert r.pos_at(400)[1] == 50                              # the re-driven stretch


def test_build_ref_stops_at_the_finish_odometer_reset():
    """A dirt sprint's handback resets DistanceTraveled to 0 while parked at
    the line: the end of the run, not a 6000-unit rewind."""
    seq = [(p, p, 0) for p in range(1, 3001, 5)] + [(0, 3000, 0)] * 20
    r = build_ref(9, None, {"id": 3, "start_distance": 0.0}, frames(seq))
    assert r is not None and r.length > 2900


def test_build_ref_rejects_a_lap_joined_mid_way():
    seq = [(p, p, 0) for p in range(800, 3000, 5)]
    assert build_ref(9, None, {"id": 3, "start_distance": 0.0}, frames(seq)) is None


def test_lap_origin_counts_from_the_line_when_the_grid_is_behind_it():
    """Real captures open lap 1 at DistanceTraveled -71 / -132 (grid behind
    the line); progress still has to be 0 AT the line."""
    assert lap_origin(-71.4) == 0.0
    assert lap_origin(None) == 0.0
    assert lap_origin(5951.0) == 5951.0   # lap 2 opens at the line
    seq = [(p, p + 71, 0) for p in range(-71, 2000, 5)]
    r = build_ref(9, None, {"id": 3, "start_distance": -71.0}, frames(seq))
    assert r.pos_at(500)[0] == pytest.approx(571, abs=1)


# -- the matcher -----------------------------------------------------------

def test_single_candidate_commits_after_the_commit_fraction():
    a = straight(1)
    m = RouteMatcher()
    m.start([a])
    drive(m, a, 0.8 * rm.MATCH_COMMIT_FRAC * a.length)
    assert m.guess is None                       # too early to be sure
    drive(m, a, 1.5 * rm.MATCH_COMMIT_FRAC * a.length)
    assert m.guess is a


def test_two_candidates_wait_for_the_split():
    """Same start, same first 300 units, then apart: no guess while the
    car could be on either; the one it follows once they part."""
    a = straight(1)
    bend = ref(2, [(p, p, 0) if p <= 300 else (p, 300, p - 300) for p in range(0, 1001, 10)])
    m = RouteMatcher()
    m.start([a, bend])
    drive(m, bend, 300)
    assert m.guess is None and len(m.cands) == 2
    drive(m, bend, 700)
    assert m.guess is bend
    assert 1 in m.ruled_out


def test_an_excursion_does_not_withdraw_a_guess():
    """A crash into the scenery: the car is far off the course, but progress
    stalls while it is - so it never piles up MATCH_SUSTAIN of it."""
    a = straight(1)
    m = RouteMatcher()
    m.start([a])
    drive(m, a, 400)
    assert m.guess is a
    t = 100.0
    for k in range(200):                        # 20 s stuck 150 m off, progress crawling
        m.feed(t + k * 0.1, 400 + k * 0.05, 400, 150)
    drive(m, a, 900)
    assert m.guess is a


def test_a_guess_is_withdrawn_once_the_car_keeps_progressing_off_it():
    a = straight(1)
    m = RouteMatcher()
    m.start([a])
    drive(m, a, 900, off=lambda p: (0, 0) if p < 300 else (0, (p - 300) * 0.8))
    assert m.guess is None and m.done
    m.start([a])                                 # next lap: not guessed again
    assert m.done and not m.cands
    m.reset()                                    # next session: fair game
    m.start([a])
    assert len(m.cands) == 1


def test_a_longer_course_overruns_the_reference():
    short = straight(1, length=500)
    long_ = straight(2, length=1000)
    m = RouteMatcher()
    m.start([short, long_])
    drive(m, long_, 600)
    assert m.guess is long_
    assert 1 in m.ruled_out


def test_past_the_line_proves_nothing():
    a = straight(1, length=500)
    m = RouteMatcher()
    m.start([a])
    drive(m, a, 500)
    for k in range(100):                          # coasting off sideways after the line
        m.feed(100 + k * 0.1, 505 + k, 500, 10 * k)
    assert m.guess is a


def test_nothing_to_match_is_done_at_once():
    m = RouteMatcher()
    m.start([])
    assert m.done
    m.feed(0.0, 100.0, 0.0, 0.0)
    assert m.guess is None


# -- the tracker's failure fence -------------------------------------------

def test_a_failing_matcher_never_touches_the_recording(tmp_path, monkeypatch, caplog):
    def boom(self, x, z):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(RouteRefs, "_load_near", boom)

    def scenario(sim):
        sim.event(75, "event")
        sim.race_off()

    with caplog.at_level(logging.ERROR, logger="lapscope.recorder"):
        store = run(scenario, tmp_path)
    s = sessions(store)
    assert len(s) == 1 and s[0]["lap_count"] >= 1 and s[0]["route_id"] is not None
    assert store.route_guesses == []
    # logged once, then off until the session ends (not once per packet)
    assert sum("live route matching failed" in r.message for r in caplog.records) == 1


# -- the live delta's stored reference (phase 3) ---------------------------

def test_build_delta_ref_reads_the_recorders_lap_clock():
    """CurrentLap while it runs; race time since the lap opened when it is
    dead (WTA, bare sprints) - the same clock the live lap is timed on."""
    from app.recorder.route_match import build_delta_ref

    def frames_of(rows):
        out = []
        for i, (dist, cur, rt) in enumerate(rows):
            f = empty_fields()
            f.update(distance_traveled=dist, current_lap=cur, current_race_time=rt,
                     is_race_on=1)
            out.append((float(i), pack(f)))
        return out

    lap = {"id": 5, "lap_time": 100.0, "start_distance": 0.0}
    live = [(p, p / 10, 50 + p / 10) for p in range(0, 1001, 2)]
    r = build_delta_ref("pb", lap, frames_of(live), 1000.0)
    assert r.t[r.d.index(500)] == pytest.approx(50.0)
    dead = [(p, 0.0, 50 + p / 10) for p in range(0, 1001, 2)]
    r = build_delta_ref("pb", lap, frames_of(dead), 1000.0)
    assert r.t[r.d.index(500)] == pytest.approx(50.0)      # 100 s - 50 s at open
    short = [(p, p / 10, p / 10) for p in range(0, 501, 2)]
    assert build_delta_ref("pb", lap, frames_of(short), 1000.0) is None  # half a route


def test_delta_ref_skips_excluded_and_rewound_laps(tmp_path):
    """The stored best is chosen like a best on the Analysis page: a lap the
    user excluded doesn't count, and neither does a rewound one."""
    from app.recorder.store import lap_anchor

    def scenario(sim):
        sim.event(220, "dirty", dirty=True)   # lap 2 contact, lap 3 rewind
        sim.race_off()

    store = run(scenario, tmp_path)
    sid = sessions(store)[0]["id"]
    rid = sessions(store)[0]["route_id"]
    timed = [lap for lap in store.session_laps(sid) if lap["lap_time"] is not None]
    rewound = {lap["id"] for lap in timed if "rewind" in (lap["flags"] or "")}
    assert rewound
    refs = RouteRefs(store)
    eligible = sorted((lap for lap in timed if lap["id"] not in rewound),
                      key=lambda lap: lap["lap_time"])
    got = refs._load_delta(rid, 269, 6, None)
    assert got.kind == "pb" and got.lap_id == eligible[0]["id"]

    store.add_edit(sid, "exclude_lap", lap_anchor(eligible[0]))
    assert refs._load_delta(rid, 269, 6, None).lap_id == eligible[1]["id"]
    assert refs._load_delta(rid, 269, 6, sid) is None    # its own session excluded
    assert refs._load_delta(rid, 1, 6, None).kind == "class"
    assert refs._load_delta(rid, 1, 3, None) is None


def test_delta_measures_progress_from_the_start_line():
    """A lap gridded behind the line opens at DistanceTraveled -71: its
    lap_dist runs 71 ahead of its progress. The delta has to compare
    progress, or a lap at exactly the reference's pace reads ~7 s off."""
    from app.recorder.laps import SessionTracker
    from app.recorder.route_match import DeltaRef

    tr = SessionTracker(store=None)
    tr._route_dref = DeltaRef("pb", 1, 100.0, [float(p) for p in range(2, 1001, 2)],
                              [p / 10 for p in range(2, 1001, 2)])
    tr._pick_ref()
    tr._lap_start_dist = -71.0                 # this lap opened behind the line
    assert tr._delta(500 + 71, 50.0) == pytest.approx(0.0)   # at progress 500, on pace
    assert tr._delta(30.0, 1.0) is None        # still behind the line

    # and the session-best reference carries its own offset: a lap that
    # opened behind the line, chased from a lap that opened on it
    tr._route_dref = None
    tr._sess_ref = ([float(p + 71) for p in range(2, 1001, 2)],
                    [p / 10 for p in range(2, 1001, 2)], 71.0, 100.0, 7)
    tr._pick_ref()
    assert tr.delta_ref == "session" and tr.delta_ref_lap == 7
    tr._lap_start_dist = 5951.0                # lap 2, opened at the line
    assert tr._delta(500.0, 50.0) == pytest.approx(0.0)
