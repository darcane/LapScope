"""Replay stored laps through the live route matcher and report how it did
(issue #108) - the calibration sweep behind the MATCH_* thresholds in
app/recorder/route_match.py.

Every completed lap of every session on a route is fed, frame by frame,
through a fresh RouteMatcher whose candidates are the routes starting near
that lap's start - with each route's reference built from a lap of ANOTHER
session (leave-one-out: matching a lap against itself would prove nothing).
The verdict is then compared with the route the session was fingerprinted
onto at the line.

Usage (from the repo root, plain stdlib; opens the database read-only):
    python tools/sweep_route_match.py [--db data/telemetry.db] [--first-only]
                                      [--session N] [-v]

Outcomes per lap:
  right     committed to the fingerprinted route (and stayed)
  wrong     committed to another route and stayed there - the failure that
            matters
  withdrawn committed, then withdrew the guess (a wrong commit caught)
  none      every candidate dropped: shows nothing (correct when the lap's
            route has no reference of its own)
  pending   never decided: candidates still tied at the line
"""

from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.recorder import route_match as rm  # noqa: E402
from app.recorder.store import ROUTE_START_RADIUS_M  # noqa: E402
from app.telemetry.packet import parse  # noqa: E402


class ReadOnlyDB:
    def __init__(self, path: str) -> None:
        self.conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        self.conn.row_factory = sqlite3.Row

    def routes(self) -> dict[int, dict]:
        return {r["id"]: dict(r) for r in self.conn.execute(
            "SELECT id, name, start_x, start_z, lap_length FROM routes")}

    def laps(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT l.*, s.route_id FROM laps l JOIN sessions s ON s.id = l.session_id"
            " WHERE l.lap_time IS NOT NULL AND s.route_id IS NOT NULL"
            " ORDER BY l.session_id, l.started_t")]

    def frames(self, lap: dict) -> list[tuple[float, bytes]]:
        end = lap["ended_t"] if lap["ended_t"] is not None else 1e18
        return [(r[0], r[1]) for r in self.conn.execute(
            "SELECT t, raw FROM frames WHERE session_id = ? AND t >= ? AND t <= ?"
            " ORDER BY t", (lap["session_id"], lap["started_t"], end))]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", default="data/telemetry.db")
    ap.add_argument("--first-only", action="store_true",
                    help="only each session's first completed lap (the lap the"
                         " feature exists for)")
    ap.add_argument("--session", type=int, help="only this session")
    ap.add_argument("-v", "--verbose", action="store_true", help="one line per lap")
    a = ap.parse_args()

    db = ReadOnlyDB(a.db)
    routes = db.routes()
    laps = db.laps()
    by_route: dict[int, list[dict]] = {}
    for lap in laps:
        by_route.setdefault(lap["route_id"], []).append(lap)
    for v in by_route.values():
        v.sort(key=lambda lap: lap["lap_time"])

    # per route: usable references from distinct sessions, fastest first -
    # two are enough for leave-one-out
    refs: dict[int, list[rm.RouteRef]] = {}

    def route_refs(rid: int) -> list[rm.RouteRef]:
        if rid not in refs:
            got, seen = [], set()
            for lap in by_route.get(rid, []):
                if lap["session_id"] in seen:
                    continue
                ref = rm.build_ref(rid, routes[rid]["name"], lap, db.frames(lap))
                if ref is not None and ref.length >= rm.REF_MIN_COVER * routes[rid]["lap_length"]:
                    ref.session_id = lap["session_id"]
                    got.append(ref)
                    seen.add(lap["session_id"])
                    if len(got) == 2:
                        break
            refs[rid] = got
        return refs[rid]

    tally: Counter = Counter()
    commit_at: list[float] = []
    first_seen: set[int] = set()
    for lap in laps:
        sid = lap["session_id"]
        if a.session is not None and sid != a.session:
            continue
        if a.first_only and sid in first_seen:
            continue
        first_seen.add(sid)
        frames = db.frames(lap)
        if not frames:
            continue
        p0 = parse(frames[0][1])
        cands = []
        for rid, r in routes.items():
            if math.hypot(r["start_x"] - p0["pos_x"], r["start_z"] - p0["pos_z"]) > ROUTE_START_RADIUS_M:
                continue
            other = [x for x in route_refs(rid) if x.session_id != sid]
            if other:
                cands.append(other[0])
        own_known = any(c.route_id == lap["route_id"] for c in cands)
        m = rm.RouteMatcher()
        m.start(cands)
        start = rm.lap_origin(lap["start_distance"])
        committed = None
        commit_prog = None
        withdrawn = False
        length = routes[lap["route_id"]]["lap_length"]
        for t, raw in frames:
            p = parse(raw)
            m.feed(t, p["distance_traveled"] - start, p["pos_x"], p["pos_z"])
            if m.guess is not None and committed is None:
                committed = m.guess.route_id
                commit_prog = (p["distance_traveled"] - start) / length
            if committed is not None and m.guess is None:
                withdrawn = True
                break
        if withdrawn:
            out = "withdrawn"
        elif committed is not None:
            out = "right" if committed == lap["route_id"] else "wrong"
            if out == "right":
                commit_at.append(commit_prog)
        elif m.cands:
            out = "pending"
        else:
            out = "none"
        key = f"{out}{'' if own_known else ' (route unknown)'}"
        tally[key] += 1
        if a.verbose or out == "wrong":
            print(f"session {sid:4d} lap {lap['id']:5d} route {lap['route_id']:4d}"
                  f" {routes[lap['route_id']]['name'] or '':<28.28} cands={len(cands)}"
                  f" -> {key}"
                  + (f" [guessed {committed}]" if committed not in (None, lap["route_id"]) else "")
                  + (f" at {100 * commit_prog:.0f}%" if commit_prog is not None else ""))

    print()
    for k, n in sorted(tally.items()):
        print(f"{k:<28} {n:5d}")
    if commit_at:
        commit_at.sort()
        q = lambda f: commit_at[min(len(commit_at) - 1, int(f * len(commit_at)))]  # noqa: E731
        print(f"\nright commits at lap progress: median {100 * q(0.5):.0f}%,"
              f" p90 {100 * q(0.9):.0f}%, max {100 * commit_at[-1]:.0f}%")


if __name__ == "__main__":
    main()
