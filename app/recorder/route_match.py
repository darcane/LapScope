"""Live route recognition (issue #108): which stored route is being driven,
told while the lap is still running.

The completed-lap fingerprint (store.py) needs the lap's length and
bounding box, so it can only answer at the line. This answers mid-lap by
comparing the car against each candidate route's *reference trace*: where a
stored lap on that route was at the same lap progress.

That comparison is cheap and sharp because of one FH6 quirk (AGENTS.md):
`DistanceTraveled` is normalized route progress, 0 -> ~5950 over a full
route whatever its real length. So "progress 1200" is the same spot on the
course for every lap of one route, whatever the line or the car - and a
different spot on any other route, even one that leaves from the same start
line on the same road, because the two are normalized over different
lengths. One binary search per candidate per frame; no nearest-point search.

The pieces, kept apart so each can be tested without the others:

- `RouteRef` + `build_ref`: one route's reference trace, downsampled by
  progress, built from a stored lap's frames.
- `RouteRefs`: finds the routes whose start is near a point and builds their
  references - OFF the event loop (a reference is one lap's frames read and
  parsed, tens of ms), cached per route, invalidated when a session closes
  on that route.
- `RouteMatcher`: the per-lap decision. Pure arithmetic, no I/O.
- `DeltaRef` + `build_delta_ref` + `RouteRefs.delta_ref`: once the route is
  known, the lap the live delta is measured against from lap 1 - the best
  lap on the route in this car, else in this class (never another class).

The guess is display-only: it is never written to the database. The
completed-lap fingerprint stays the authority on which route a session was.
"""

from __future__ import annotations

import bisect
import logging
import math
import threading
from concurrent.futures import Executor, Future
from dataclasses import dataclass

from ..telemetry.packet import parse
from .store import ROUTE_START_RADIUS_M

log = logging.getLogger("lapscope.route_match")

REF_STEP = 10.0          # progress units between reference samples (~600 a route)
REF_RESET_DROP = 500.0   # progress falling this much in one frame = the run's
                         # odometer reset (finish handback / WTA collapse),
                         # not a rewind (rewinds scrub back gradually)
REF_START_MAX = 60.0     # the reference lap must start this close to progress
                         # 0, or the server joined it mid-lap
REF_MIN_COVER = 0.9      # ... and cover this much of the route's length
REF_CANDIDATE_LAPS = 3   # fastest laps tried per route for a usable trace

DELTA_STEP = 2.0         # progress units between delta-reference samples: the
                         # delta interpolates between them, so finer than REF_STEP
DELTA_KINDS = ("pb", "class")  # same car first, then same class - in that order

MATCH_TAU_S = 1.0        # smoothing time constant of the position error
MATCH_ELIM_M = 60.0      # smoothed error above this drops a candidate
MATCH_OK_M = 30.0        # the survivor must be at least this close to commit
MATCH_COMMIT_FRAC = 0.05 # ... and have been followed this far into the lap
MATCH_RETRACT_M = 60.0   # a committed guess drifting this far is withdrawn
MATCH_SUSTAIN = 0.015    # ... over this fraction of the route's progress: an
                         # off-course excursion (a crash, a wrong turn into
                         # scenery) stalls progress while the car is away, so
                         # it never accumulates this much; a genuinely
                         # different course keeps progressing
MATCH_OVERRUN = 0.03     # progress past the reference's end by this fraction
                         # = a longer route than this one


@dataclass
class RouteRef:
    route_id: int
    name: str | None
    lap_id: int
    d: list[float]   # lap progress, strictly increasing
    x: list[float]
    z: list[float]

    @property
    def length(self) -> float:
        return self.d[-1]

    def pos_at(self, prog: float) -> tuple[float, float] | None:
        """The reference's world position at lap progress `prog`, or None
        past its end."""
        d = self.d
        if prog > d[-1]:
            return None
        i = bisect.bisect_left(d, prog)
        if i == 0:
            return self.x[0], self.z[0]
        f = (prog - d[i - 1]) / (d[i] - d[i - 1])
        return (self.x[i - 1] + f * (self.x[i] - self.x[i - 1]),
                self.z[i - 1] + f * (self.z[i] - self.z[i - 1]))


def lap_origin(start_distance: float | None) -> float:
    """The progress origin of a lap that opened at `start_distance`.

    An event's start line is progress 0, and the grid can sit BEHIND it:
    some captures open lap 1 at DistanceTraveled -71 or -132, counting up
    to 0 at the line (sessions 35, 718). Measured from where the lap opened,
    those laps read ~80 m off every other lap of their route the whole way
    round. A later lap opens at the line itself, where the odometer is
    already positive, so the origin is wherever the lap opened."""
    return max(start_distance or 0.0, 0.0)


def build_ref(route_id: int, name: str | None, lap: dict,
              frames: list[tuple[float, bytes]]) -> RouteRef | None:
    """One lap's frames -> a progress-indexed reference trace, or None when
    the lap is no use as one (too short, or joined mid-lap).

    A rewind is handled like the recorder handles it: the stretch past the
    rewound-to point is dropped and the re-driven one replaces it."""
    start = lap_origin(lap["start_distance"])
    d: list[float] = []
    x: list[float] = []
    z: list[float] = []
    prev = None
    for _t, raw in frames:
        p = parse(raw)
        prog = p["distance_traveled"] - start
        if prev is not None and prev - prog > REF_RESET_DROP:
            break  # the run is over: odometer reset at the finish
        prev = prog
        if prog <= 0.0:
            continue  # grid hold / countdown
        while d and d[-1] >= prog:  # rewound: drop the undone stretch
            d.pop()
            x.pop()
            z.pop()
        if not d or prog >= d[-1] + REF_STEP:
            d.append(prog)
            x.append(p["pos_x"])
            z.append(p["pos_z"])
    if len(d) < 2 or d[0] > REF_START_MAX:
        return None
    return RouteRef(route_id, name, lap["id"], d, x, z)


@dataclass
class DeltaRef:
    """A stored lap as the live delta's reference: elapsed lap time `t` at
    lap progress `d` (counted from `lap_origin`, like the matcher's)."""
    kind: str        # "pb" (this car's best on the route) or "class"
    lap_id: int
    lap_time: float
    d: list[float]
    t: list[float]


def lap_elapsed(frame: dict, open_rt: float) -> float:
    """The lap clock the recorder runs on (laps.py `lap_elapsed` in
    `_lap_logic`): CurrentLap while it runs, else race time since the lap
    opened - so a stored lap's trace reads exactly like the live one."""
    cur = frame["current_lap"]
    if cur > 0.001:
        return cur
    rt = frame["current_race_time"]
    return rt - open_rt if rt > open_rt + 0.05 else 0.0


def build_delta_ref(kind: str, lap: dict, frames: list[tuple[float, bytes]],
                    route_length: float) -> DeltaRef | None:
    """One stored lap -> (progress, elapsed) samples, or None when it can't
    serve: joined mid-lap, or not covering the route."""
    start = lap_origin(lap["start_distance"])
    d: list[float] = []
    t: list[float] = []
    open_rt = None
    prev = None
    for _t, raw in frames:
        p = parse(raw)
        if open_rt is None:
            open_rt = p["current_race_time"]
        prog = p["distance_traveled"] - start
        if prev is not None and prev - prog > REF_RESET_DROP:
            break  # the run is over: odometer reset at the finish
        prev = prog
        el = lap_elapsed(p, open_rt)
        if prog <= 0.0 or el <= 0.0:
            continue
        while d and d[-1] >= prog:  # rewound: drop the undone stretch
            d.pop()
            t.pop()
        if not d or prog >= d[-1] + DELTA_STEP:
            d.append(prog)
            t.append(el)
    if len(d) < 2 or d[0] > REF_START_MAX or d[-1] < REF_MIN_COVER * route_length:
        return None
    return DeltaRef(kind, lap["id"], lap["lap_time"], d, t)


class RouteRefs:
    """Reference traces for the routes near a start point, loaded in the
    background and cached per route.

    `executor` None loads inline (tests, tools); production passes a
    single-worker pool so the UDP callback never waits on the database.
    Every database access goes through `store.reader()`, which is safe off
    the event loop (AGENTS.md: SQLite threading rule)."""

    def __init__(self, store, executor: Executor | None = None) -> None:
        self.store = store
        self.executor = executor
        # successes only: a route with no usable lap yet may get one at any
        # lap boundary (its own session's first lap), and is retried then
        self._cache: dict[int, RouteRef] = {}
        self._lock = threading.Lock()

    def near(self, x: float, z: float) -> Future:
        """A future of the RouteRefs whose route starts within the
        fingerprint's start radius of (x, z) and has a usable reference."""
        if self.executor is None:
            fut: Future = Future()
            try:
                fut.set_result(self._load_near(x, z))
            except Exception as e:  # surfaced by the caller, like a pool's
                fut.set_exception(e)
            return fut
        return self.executor.submit(self._load_near, x, z)

    def delta_ref(self, route_id: int, car_ordinal: int, car_class: int,
                  exclude_session: int | None) -> Future:
        """A future of the DeltaRef for a car on a route, or None if the
        route has no usable lap in this car or this class. Not cached: it is
        asked once per session, and a session that just closed may have set
        the best lap."""
        args = (route_id, car_ordinal, car_class, exclude_session)
        if self.executor is None:
            fut: Future = Future()
            try:
                fut.set_result(self._load_delta(*args))
            except Exception as e:
                fut.set_exception(e)
            return fut
        return self.executor.submit(self._load_delta, *args)

    def _load_delta(self, route_id: int, car_ordinal: int, car_class: int,
                    exclude_session: int | None) -> DeltaRef | None:
        """The fastest eligible lap: same car, else same class. Eligible =
        timed, not excluded by the user, not rewound - read through
        `session_laps`, so manual edits count exactly as they do for bests
        on the Analysis page. A rewound lap is a time the game let you take
        twice; it is no target to chase."""
        route = self.store.get_route(route_id)
        if route is None:
            return None
        laps: dict[str, list[dict]] = {k: [] for k in DELTA_KINDS}
        for s in self.store.route_sessions(route_id):
            if s["id"] == exclude_session:
                continue  # this session's own laps are the session best's job
            kind = ("pb" if s["car_ordinal"] == car_ordinal
                    else "class" if s["car_class"] == car_class else None)
            if kind is None:
                continue
            for lap in self.store.session_laps(s["id"]):
                if (lap["lap_time"] is None or lap["excluded"]
                        or "rewind" in (lap["flags"] or "").split(",")):
                    continue
                laps[kind].append(lap)
        for kind in DELTA_KINDS:
            for lap in sorted(laps[kind], key=lambda x: x["lap_time"])[:REF_CANDIDATE_LAPS]:
                ref = build_delta_ref(kind, lap, self.store.lap_frames(lap),
                                      route["lap_length"])
                if ref is not None:
                    return ref
        return None

    def invalidate(self, route_id: int | None) -> None:
        """A session on this route just closed: its best lap (the reference)
        may have changed, or it may have its first usable one."""
        if route_id is None:
            return
        with self._lock:
            self._cache.pop(route_id, None)

    def _load_near(self, x: float, z: float) -> list[RouteRef]:
        out = []
        for r in self.store.route_starts():
            if math.hypot(r["start_x"] - x, r["start_z"] - z) > ROUTE_START_RADIUS_M:
                continue
            with self._lock:
                ref = self._cache.get(r["id"])
            if ref is None:
                ref = self._build(r)
                if ref is None:
                    continue
                with self._lock:
                    self._cache[r["id"]] = ref
            out.append(ref)
        return out

    def _build(self, route: dict) -> RouteRef | None:
        for lap in self.store.route_ref_laps(route["id"], REF_CANDIDATE_LAPS):
            ref = build_ref(route["id"], route["name"], lap,
                            self.store.lap_frames(lap))
            if ref is not None and ref.length >= REF_MIN_COVER * route["lap_length"]:
                return ref
        return None


class _Cand:
    __slots__ = ("ref", "err", "t", "over")

    def __init__(self, ref: RouteRef) -> None:
        self.ref = ref
        self.err: float | None = None  # smoothed position error, m
        self.t: float | None = None    # time of the last sample
        self.over: float | None = None # progress where err went over the limit


class RouteMatcher:
    """One lap's decision: start with the candidates near the lap's start,
    drop each one the car visibly isn't following, and commit to the last
    one standing once the car has followed it far enough.

    "Show nothing until confident": `guess` stays None until a commit. A
    committed guess is re-checked every frame and withdrawn if the car
    drifts off it (a never-driven course sharing a known one's start line
    looks exactly like the known one until they part).

    A route the car was seen to leave is ruled out for the rest of the
    session (`ruled_out`, cleared by `reset`): the course doesn't change
    mid-event, and without the memory an unrecorded course sharing a known
    one's start line would be guessed and withdrawn again on every lap."""

    def __init__(self) -> None:
        self.cands: list[_Cand] = []
        self.guess: RouteRef | None = None
        self.done = True   # nothing left to decide on this lap
        self.ruled_out: set[int] = set()

    def start(self, refs: list[RouteRef], keep: RouteRef | None = None) -> None:
        """A new lap. `keep` = the guess carried over from the previous lap
        of this session: it stays committed and is only re-checked."""
        self.guess = keep
        self.cands = ([_Cand(keep)] if keep is not None else
                      [_Cand(r) for r in refs if r.route_id not in self.ruled_out])
        self.done = not self.cands

    def stop(self) -> None:
        """Nothing to decide until the next start() - the lap's references
        are still loading, or its decision is final."""
        self.cands = []
        self.guess = None
        self.done = True

    def reset(self) -> None:
        """A new session: forget everything, the ruled-out routes included."""
        self.stop()
        self.ruled_out = set()

    def feed(self, t: float, prog: float, x: float, z: float) -> None:
        """One frame: `prog` = lap progress (DistanceTraveled minus
        `lap_origin`), (x, z) = the car's world position."""
        if self.done or prog <= 0.0:
            return  # nothing to decide, or still on the grid
        if self.guess is not None and prog >= self.guess.length:
            return  # past the line: the post-finish coast proves nothing
        limit = MATCH_RETRACT_M if self.guess is not None else MATCH_ELIM_M
        alive = []
        for c in self.cands:
            ref = c.ref
            p = ref.pos_at(prog)
            if p is None:
                if prog > ref.length * (1 + MATCH_OVERRUN):
                    self.ruled_out.add(ref.route_id)
                    continue  # driven further than this route goes
                p = ref.x[-1], ref.z[-1]  # coasting past the line
            e = math.hypot(x - p[0], z - p[1])
            if c.err is None:
                c.err = e
            else:
                dt = max(0.0, t - c.t)
                c.err += (e - c.err) * (1.0 - math.exp(-dt / MATCH_TAU_S))
            c.t = t
            if c.err <= limit:
                c.over = None
            elif c.over is None:
                c.over = prog
            elif prog - c.over >= MATCH_SUSTAIN * ref.length:
                self.ruled_out.add(ref.route_id)
                continue  # off it, and still progressing: not this route
            alive.append(c)
        self.cands = alive
        if self.guess is not None:
            if not alive:
                log.info("Route guess %d (%s) withdrawn at progress %.0f",
                         self.guess.route_id, self.guess.name or "unnamed", prog)
                self.stop()
            return
        if not alive:
            self.done = True  # a course nobody has recorded from this start
            return
        if len(alive) == 1:
            c = alive[0]
            if c.err <= MATCH_OK_M and prog >= MATCH_COMMIT_FRAC * c.ref.length:
                self.guess = c.ref
                log.info("Route guess: %d (%s) at progress %.0f (%.0f%%), error %.1f m",
                         c.ref.route_id, c.ref.name or "unnamed", prog,
                         100.0 * prog / c.ref.length, c.err)
