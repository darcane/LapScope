"""Dump the recorder-relevant signals of a stored session, for diagnosing
event types the segmentation doesn't recognize yet.

Workflow: set FC_KEEP_DISCARDED=1 (see docker-compose.yml / .env), drive the
unrecognized event once so its raw frames are kept, then inspect it here -
no game or container needed, the tool reads data/telemetry.db directly.

Usage (from the repo root, plain stdlib):
    python tools/inspect_session.py --list
    python tools/inspect_session.py <session_id> [--db data/telemetry.db]

Prints one line per transition the segmentation cares about: IsRaceOn flips,
race-clock resets and freezes, DistanceTraveled resets, lap-field activity,
RacePosition changes, stream gaps, and single-frame position jumps.
"""

from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.telemetry.packet import parse  # noqa: E402

GAP_SECONDS = 0.5          # stream gap worth reporting
RT_FREEZE_SECONDS = 1.5    # matches laps.RT_FREEZE_SECONDS
DIST_DROP = 500.0          # DistanceTraveled falling this much = reset
POS_JUMP = 250.0           # single-frame teleport (matches laps.WTA_TELEPORT_JUMP)


def fmt_frame(t0: float, t: float, f: dict) -> str:
    return (f"t={t - t0:8.2f}s rt={f['current_race_time']:8.2f} "
            f"dist={f['distance_traveled']:9.1f} v={f['speed']:5.1f} "
            f"pos=({f['pos_x']:8.1f},{f['pos_z']:8.1f}) "
            f"ln={f['lap_number']} cur={f['current_lap']:.2f} "
            f"last={f['last_lap']:.2f} best={f['best_lap']:.2f} "
            f"racePos={f['race_position']} on={f['is_race_on']}")


def list_sessions(db: sqlite3.Connection) -> None:
    rows = db.execute(
        "SELECT s.id, s.started_at, s.ended_at, s.frame_count, s.kept,"
        " COUNT(l.lap_time) AS laps"
        " FROM sessions s LEFT JOIN laps l ON l.session_id = s.id"
        " GROUP BY s.id ORDER BY s.started_at").fetchall()
    print(f"{'id':>4} {'started':<17} {'dur':>7} {'frames':>7} {'laps':>4} kept")
    for sid, t0, t1, frames, kept, laps in rows:
        started = datetime.fromtimestamp(t0).strftime("%Y-%m-%d %H:%M")
        dur = f"{t1 - t0:6.0f}s" if t1 else "     ?"
        print(f"{sid:>4} {started:<17} {dur:>7} {frames:>7} {laps:>4} {'yes' if kept else ''}")


def inspect(db: sqlite3.Connection, session_id: int) -> None:
    rows = db.execute("SELECT t, raw FROM frames WHERE session_id = ? ORDER BY t",
                      (session_id,)).fetchall()
    if not rows:
        print(f"session {session_id}: no stored frames"
              " (only kept/FC_KEEP_DISCARDED sessions retain them after cleanup)")
        return
    laps = db.execute(
        "SELECT lap_number, lap_time, flags FROM laps WHERE session_id = ?"
        " ORDER BY started_t", (session_id,)).fetchall()

    t0 = rows[0][0]
    prev_t: float | None = None
    prev: dict | None = None
    freeze_start: float | None = None
    events: list[str] = []

    def ev(t: float, msg: str) -> None:
        events.append(f"  t={t - t0:8.2f}s  {msg}")

    for t, raw in rows:
        try:
            f = parse(raw)
        except Exception:
            ev(t, "! unparseable frame")
            continue
        if prev is not None:
            if t - prev_t > GAP_SECONDS:
                ev(t, f"stream gap: {t - prev_t:.2f}s of silence")
            if f["is_race_on"] != prev["is_race_on"]:
                ev(t, f"IsRaceOn {prev['is_race_on']} -> {f['is_race_on']}")
            rt, prt = f["current_race_time"], prev["current_race_time"]
            if rt < prt - 1.0:
                ev(t, f"race clock jumped back {prt:.2f} -> {rt:.2f}")
            if abs(rt - prt) < 1e-3 and f["is_race_on"]:
                if freeze_start is None:
                    freeze_start = t
            elif freeze_start is not None:
                if t - freeze_start >= RT_FREEZE_SECONDS:
                    ev(freeze_start, f"race clock FROZEN at {prt:.2f} for"
                       f" {t - freeze_start:.1f}s (finish signal)")
                freeze_start = None
            d, pd = f["distance_traveled"], prev["distance_traveled"]
            if d < pd - DIST_DROP:
                ev(t, f"DistanceTraveled reset {pd:.0f} -> {d:.0f} (finish/launch signal)")
            elif pd < 1.0 and d >= 1.0:
                ev(t, f"DistanceTraveled starts growing (launch), rt={rt:.2f}")
            if f["lap_number"] != prev["lap_number"]:
                ev(t, f"LapNumber {prev['lap_number']} -> {f['lap_number']}")
            for field in ("current_lap", "last_lap", "best_lap"):
                if prev[field] <= 0.001 < f[field]:
                    ev(t, f"{field} starts counting: {f[field]:.3f}")
            if (f["last_lap"] > 0.001 and prev["last_lap"] > 0.001
                    and abs(f["last_lap"] - prev["last_lap"]) > 1e-3):
                ev(t, f"LastLap {prev['last_lap']:.3f} -> {f['last_lap']:.3f} (finish signal)")
            if f["race_position"] != prev["race_position"]:
                ev(t, f"RacePosition {prev['race_position']} -> {f['race_position']}")
            jump = math.hypot(f["pos_x"] - prev["pos_x"], f["pos_z"] - prev["pos_z"])
            if jump > POS_JUMP:
                ev(t, f"position jump {jump:.0f} m (teleport)")
        prev, prev_t = f, t
    if freeze_start is not None and prev_t - freeze_start >= RT_FREEZE_SECONDS:
        ev(freeze_start, f"race clock FROZEN at {prev['current_race_time']:.2f}"
           f" until the stream ends ({prev_t - freeze_start:.1f}s)")

    print(f"session {session_id}: {len(rows)} frames over {prev_t - t0:.1f}s")
    print(f"  first: {fmt_frame(t0, t0, parse(rows[0][1]))}")
    print(f"  last:  {fmt_frame(t0, prev_t, prev)}")
    print(f"stored laps: " + (", ".join(
        f"#{ln + 1}={lt:.3f}s{('[' + fl + ']') if fl else ''}" if lt else f"#{ln + 1}=open"
        for ln, lt, fl in laps) if laps else "none"))
    print(f"{len(events)} signal transitions:")
    for line in events:
        print(line)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("session_id", nargs="?", type=int)
    ap.add_argument("--db", default="data/telemetry.db")
    ap.add_argument("--list", action="store_true", help="list stored sessions")
    args = ap.parse_args()
    if not Path(args.db).exists():
        sys.exit(f"database not found: {args.db} (run from the repo root?)")
    db = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    if args.list or args.session_id is None:
        list_sessions(db)
    else:
        inspect(db, args.session_id)


if __name__ == "__main__":
    main()
