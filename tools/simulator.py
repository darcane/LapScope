"""Sends synthetic FH6 Data Out packets so the stack can be tested without the game.

Drives a fake car around a stadium-shaped circuit (two 600 m straights joined
by 120 m-radius half circles, ~1954 m per lap) with braking zones, cornering
G, tire slip near the limit, and per-lap pace variation so lap times differ.

Phases (mirrors real FH6 behavior):
- an optional free-roam warmup: lap counters stay zero, race clock counts
- one or more timed events: the race clock resets to 0 at each event start
  (this is how the server detects event/time-attack boundaries)

Usage (from the repo root, plain stdlib, no deps):
    python tools/simulator.py                              # ~3.5 laps, 1 event
    python tools/simulator.py --freeroam 20 --events 2 --duration 200 --wet
"""

from __future__ import annotations

import argparse
import math
import random
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.telemetry.packet import empty_fields, pack  # noqa: E402

STRAIGHT = 600.0
RADIUS = 120.0
PERIMETER = 2 * STRAIGHT + 2 * math.pi * RADIUS

V_STRAIGHT = 75.0     # m/s target on straights (~270 km/h)
GRIP_LAT = 12.0       # m/s^2 usable lateral acceleration
ACCEL_MAX = 6.0       # m/s^2
BRAKE_MAX = 13.0      # m/s^2
BRAKE_LOOKAHEAD = 220.0

GEAR_TOPS = [15, 28, 40, 52, 64, 78]  # m/s top speed per gear
MAX_RPM, IDLE_RPM = 7800.0, 900.0
CAR_ORDINAL = 269  # 1987 Porsche 959 in the community FH6 ordinal list


def track_point(s: float) -> tuple[float, float, float, float]:
    """Position (x, z), heading, curvature at arc length s along the loop."""
    s %= PERIMETER
    half_circle = math.pi * RADIUS
    if s < STRAIGHT:  # bottom straight, heading +x
        return -STRAIGHT / 2 + s, -RADIUS, 0.0, 0.0
    s -= STRAIGHT
    if s < half_circle:  # right turn (counter-clockwise around (L/2, 0))
        a = -math.pi / 2 + s / RADIUS
        return (STRAIGHT / 2 + RADIUS * math.cos(a), RADIUS * math.sin(a),
                a + math.pi / 2, 1.0 / RADIUS)
    s -= half_circle
    if s < STRAIGHT:  # top straight, heading -x
        return STRAIGHT / 2 - s, RADIUS, math.pi, 0.0
    s -= STRAIGHT  # left turn
    a = math.pi / 2 + s / RADIUS
    return (-STRAIGHT / 2 + RADIUS * math.cos(a), RADIUS * math.sin(a),
            a + math.pi / 2, 1.0 / RADIUS)


def corner_speed(curvature: float, pace: float) -> float:
    if curvature <= 1e-6:
        return V_STRAIGHT * pace
    return min(V_STRAIGHT, math.sqrt(GRIP_LAT / curvature)) * pace


JUMPS = False  # set by --jumps: sharp elevation spikes like cross-country jumps
JUMP_BUMPS = ((300.0, 25.0, 11.0), (1500.0, 30.0, 16.0))  # center, width, height


def track_elevation(s: float) -> float:
    """Rolling elevation, periodic over the lap (~19 m of range)."""
    sp = s % PERIMETER
    a = sp / PERIMETER * math.tau
    e = 105.0 + 9.0 * math.sin(a) + 3.5 * math.sin(2 * a + 1.3)
    if JUMPS:
        for c, w, h in JUMP_BUMPS:
            e += h * math.exp(-(((sp - c) / w) ** 2))
    return e


class Sim:
    def __init__(self, args) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.target = (args.host, args.port)
        self.dt = 1.0 / args.rate
        self.wet = args.wet
        self.f = empty_fields()
        self.f.update(is_race_on=1, engine_max_rpm=MAX_RPM, engine_idle_rpm=IDLE_RPM,
                      car_ordinal=CAR_ORDINAL, car_class=6, car_pi=987,  # FH6: 6 = R
                      drivetrain_type=2, num_cylinders=6, car_group=21,
                      race_position=1, fuel=1.0, pos_y=105.0)
        self.s = 0.0
        self.v = 30.0
        self.total_dist = 0.0
        self.sent = 0
        self.t0 = time.monotonic()

    def _pace_tick(self) -> None:
        """Advance physics one dt."""
        _, _, _, curv = track_point(self.s)
        min_v = corner_speed(curv, self.pace)
        for look in (60.0, 120.0, BRAKE_LOOKAHEAD):
            _, _, _, c_ahead = track_point(self.s + look)
            min_v = min(min_v, corner_speed(c_ahead, self.pace) + look / 18.0)
        if self.v < min_v - 0.5:
            self.lon_a = min(ACCEL_MAX, (min_v - self.v) * 0.8)
        elif self.v > min_v + 0.5:
            self.lon_a = max(-BRAKE_MAX, (min_v - self.v) * 1.5)
        else:
            self.lon_a = 0.0
        self.v = max(5.0, self.v + self.lon_a * self.dt)
        self.s += self.v * self.dt
        self.total_dist += self.v * self.dt

    pace = 1.0
    lon_a = 0.0
    impact_frames = 0

    def _send(self, race_time: float, lap_no: int, cur_lap: float,
              last: float, best: float) -> None:
        f = self.f
        x, z, heading, curv = track_point(self.s)
        lat_a = self.v * self.v * curv
        if self.impact_frames > 0:  # wall contact: brief violent lateral spike
            self.impact_frames -= 1
            lat_a += 60.0
        grip_used = math.hypot(lat_a / GRIP_LAT, self.lon_a / BRAKE_MAX)
        slip = max(0.0, grip_used + random.uniform(-0.05, 0.05))

        gear = next((i + 1 for i, top in enumerate(GEAR_TOPS) if self.v <= top), 6)
        lo = GEAR_TOPS[gear - 2] if gear >= 2 else 0.0
        span = GEAR_TOPS[gear - 1] - lo
        rpm = IDLE_RPM + (MAX_RPM * 0.95 - IDLE_RPM) * max(0.0, self.v - lo) / span
        throttle = int(min(255, max(0, self.lon_a / ACCEL_MAX * 255 + (40 if self.lon_a >= 0 else 0))))
        brake = int(min(255, max(0, -self.lon_a / BRAKE_MAX * 255)))
        torque = 420.0 * throttle / 255
        front_bias = 1.08
        puddle = [random.uniform(0.05, 0.2) if self.wet and random.random() < 0.15 else 0.0
                  for _ in range(4)]

        f.update(
            timestamp_ms=int((time.monotonic() - self.t0) * 1000) & 0xFFFFFFFF,
            current_engine_rpm=rpm,
            accel_x=lat_a, accel_y=0.2, accel_z=self.lon_a,
            vel_x=self.v * math.cos(heading), vel_z=self.v * math.sin(heading),
            ang_vel_y=self.v * curv, yaw=heading,
            norm_susp_travel=[min(1.0, 0.45 + 0.3 * abs(lat_a) / GRIP_LAT + 0.1 * random.random())] * 4,
            tire_slip_ratio=[slip * 0.6] * 4,
            wheel_rotation_speed=[self.v / 0.33] * 4,
            wheel_in_puddle=puddle,
            tire_slip_angle=[slip * front_bias, slip * front_bias, slip * 0.9, slip * 0.9],
            tire_combined_slip=[slip * front_bias, slip * front_bias, slip * 0.92, slip * 0.92],
            susp_travel_meters=[0.06] * 4,
            pos_x=x, pos_y=track_elevation(self.s), pos_z=z,
            speed=self.v, power=torque * rpm * math.tau / 60, torque=torque,
            tire_temp=[160 + 90 * slip + random.uniform(-3, 3) for _ in range(4)],
            boost=throttle / 255 * 14.0,
            distance_traveled=self.total_dist,
            best_lap=best, last_lap=last, current_lap=cur_lap,
            current_race_time=race_time, lap_number=lap_no,
            accel=throttle, brake=brake,
            steer=int(max(-127, min(127, curv * RADIUS * 90))), gear=gear,
        )
        self.sock.sendto(pack(f), self.target)
        self.sent += 1
        lag = self.t0 + self.sent * self.dt - time.monotonic()
        if lag > 0:
            time.sleep(lag)

    def freeroam(self, seconds: float) -> None:
        """Cruise: lap fields all zero, free-roam clock counting."""
        print(f"free roam for {seconds:.0f}s (no lap timing)")
        self.pace = 0.7
        t = 0.0
        while t < seconds:
            self._pace_tick()
            t += self.dt
            self._send(race_time=t, lap_no=0, cur_lap=0.0, last=0.0, best=0.0)

    def event(self, seconds: float, label: str, dirty: bool = False,
              race_laps: int | None = None) -> None:
        """Timed event: race clock restarts at 0, laps counted at the line.

        dirty=True injects a wall contact on lap 2 and a ~6 s rewind on lap 3
        (rewind scrubs the clocks, position, and distance backwards, exactly
        what the recorder's dirty-lap detection looks for).

        race_laps=N mimics an actual race: at the final line crossing the
        game does NOT increment LapNumber - LastLap updates and the race
        clock freezes for the finish cinematic.
        """
        print(f"{label}: ~{seconds:.0f}s of timed laps (lap ~{PERIMETER:.0f} m)")
        self.s = 0.0  # events grid you at the start line
        self.pace = random.uniform(0.97, 1.0)
        t, lap_start, lap_no = 0.0, 0.0, 0
        best = last = 0.0
        hist: list[tuple[float, float, float, float]] = []  # t, s, dist, v
        did_contact = did_rewind = False
        while t < seconds:
            self._pace_tick()
            t += self.dt
            hist.append((t, self.s, self.total_dist, self.v))
            if len(hist) > int(12 / self.dt):
                hist.pop(0)
            if self.s >= PERIMETER:
                completed = t - lap_start
                if race_laps is not None and lap_no + 1 >= race_laps:
                    last = completed
                    best = completed if best <= 0 else min(best, completed)
                    print(f"  final lap done: {last:6.3f}s - race finished")
                    self._finish_freeze(t, lap_no, last, best)
                    return
                self.s -= PERIMETER
                last = completed
                best = last if best <= 0 else min(best, last)
                lap_start = t
                lap_no += 1
                self.pace = random.uniform(0.955, 1.0)
                print(f"  lap {lap_no} done: {last:6.3f}s (best {best:6.3f}s)")
            if dirty and not did_contact and lap_no == 1 and t - lap_start > 15.0:
                did_contact = True
                self.impact_frames = 8
                self.v *= 0.8
                print("  ! wall contact injected")
            if dirty and not did_rewind and lap_no == 2 and t - lap_start > 20.0:
                did_rewind = True
                tgt = next((h for h in hist if h[0] >= t - 6.0), hist[0])
                print(f"  ! rewinding {t - tgt[0]:.1f}s")
                t0s, s0, d0 = t, self.s, self.total_dist
                steps = 30
                for k in range(1, steps + 1):
                    fr = k / steps
                    self.s = s0 + (tgt[1] - s0) * fr
                    self.total_dist = d0 + (tgt[2] - d0) * fr
                    ti = t0s + (tgt[0] - t0s) * fr
                    self._send(race_time=ti, lap_no=lap_no, cur_lap=ti - lap_start,
                               last=last, best=best)
                t, self.v = tgt[0], tgt[3]
                hist = [h for h in hist if h[0] <= tgt[0]]
                continue
            self._send(race_time=t, lap_no=lap_no, cur_lap=t - lap_start,
                       last=last, best=best)

    def sprint(self, seconds: float) -> None:
        """Point-to-point event (sprint/drag/street): LapNumber and CurrentLap
        stay 0 the whole run, the race clock runs from 0 and freezes at the
        finish line."""
        print(f"sprint: ~{seconds:.0f}s point-to-point (no lap counter)")
        self.s = 0.0
        self.pace = random.uniform(0.97, 1.0)
        t = 0.0
        while t < seconds:
            self._pace_tick()
            t += self.dt
            self._send(race_time=t, lap_no=0, cur_lap=0.0, last=0.0, best=0.0)
        print(f"  finish: run time {t:6.3f}s")
        self._finish_freeze(t, 0, 0.0, 0.0)

    def wta(self, laps: int) -> None:
        """World Time Attack (mirrors a real capture): every lap field stays
        0 for the whole event; the race clock counts from event load through
        a teleport to the track and a grid hold with DistanceTraveled pinned
        at 0; at the finish the game auto-stops the car and hard-resets
        DistanceTraveled while the clock keeps counting."""
        print(f"wta ({laps} laps): no lap fields at all - geometric detection")
        dead = dict(lap_no=0, cur_lap=0.0, last=0.0, best=0.0)
        t = 0.0
        # event load: parked far from the track, clock already counting
        for _ in range(int(6.0 / self.dt)):
            t += self.dt
            self._send_parked(t, -5000.0, -4000.0)
        # teleport to the grid, hold with the distance counter pinned at 0
        self.s, self.v, self.total_dist = 0.0, 0.0, 0.0
        gx, _, _, _ = track_point(0.0)
        for _ in range(int(5.0 / self.dt)):
            t += self.dt
            self._send_parked(t, gx, -RADIUS)
        print(f"  launch at rt={t:.1f}s")
        self.v = 8.0
        self.pace = random.uniform(0.97, 1.0)
        lap_start, lap_no = t, 0
        while lap_no < laps:
            self._pace_tick()
            t += self.dt
            if self.s >= PERIMETER:
                self.s -= PERIMETER
                lap_no += 1
                print(f"  lap {lap_no} done: {t - lap_start:6.3f}s (shown in-game only)")
                lap_start = t
                self.pace = random.uniform(0.955, 1.0)
            self._send(race_time=t, **dead)
        while self.v > 0.5:  # auto-stop after the line
            t += self.dt
            self.v = max(0.0, self.v - 6.0 * self.dt)
            self.s += self.v * self.dt
            self.total_dist += self.v * self.dt
            self.lon_a = 0.0
            self._send(race_time=t, **dead)
        print(f"  finished: distance resets, clock keeps counting (rt={t:.1f}s)")
        self.total_dist = 0.0
        for _ in range(int(4.0 / self.dt)):
            t += self.dt
            self._send(race_time=t, **dead)

    def _send_parked(self, race_time: float, x: float, z: float) -> None:
        """Stationary frame at an explicit position with all lap fields dead."""
        f = self.f
        f.update(
            timestamp_ms=int((time.monotonic() - self.t0) * 1000) & 0xFFFFFFFF,
            current_engine_rpm=IDLE_RPM, accel_x=0.0, accel_z=0.0,
            vel_x=0.0, vel_z=0.0, ang_vel_y=0.0,
            pos_x=x, pos_y=105.0, pos_z=z,
            speed=0.0, power=0.0, torque=0.0, boost=0.0,
            distance_traveled=0.0, best_lap=0.0, last_lap=0.0,
            current_lap=0.0, current_race_time=race_time, lap_number=0,
            accel=0, brake=0, steer=0, gear=1,
        )
        self.sock.sendto(pack(f), self.target)
        self.sent += 1
        lag = self.t0 + self.sent * self.dt - time.monotonic()
        if lag > 0:
            time.sleep(lag)

    def _finish_freeze(self, t: float, lap_no: int, last: float, best: float,
                       seconds: float = 3.0) -> None:
        """Post-finish cinematic: race clock frozen, car coasting down."""
        for _ in range(int(seconds / self.dt)):
            self.v = max(0.0, self.v - 8.0 * self.dt)
            self.s += self.v * self.dt
            self.total_dist += self.v * self.dt
            self.lon_a = 0.0
            self._send(race_time=t, lap_no=lap_no, cur_lap=0.0, last=last, best=best)

    def race_off(self, seconds: float = 3.0) -> None:
        self.f.update(is_race_on=0, speed=0.0, current_engine_rpm=IDLE_RPM)
        for _ in range(int(seconds / self.dt)):
            self.sock.sendto(pack(self.f), self.target)
            time.sleep(self.dt)
        self.f["is_race_on"] = 1


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9999)
    ap.add_argument("--rate", type=float, default=60.0, help="packets per second")
    ap.add_argument("--freeroam", type=float, default=0.0,
                    help="seconds of free-roam cruising before the first event")
    ap.add_argument("--events", type=int, default=1, help="number of timed events")
    ap.add_argument("--duration", type=float, default=150.0,
                    help="total seconds of timed driving, split across events")
    ap.add_argument("--wet", action="store_true", help="scatter puddles (wet track)")
    ap.add_argument("--dirty", action="store_true",
                    help="inject a wall contact (lap 2) and a rewind (lap 3)")
    ap.add_argument("--race", type=int, default=0, metavar="LAPS",
                    help="run a race that finishes after LAPS laps (LapNumber"
                         " does not increment at the final line, like the game)")
    ap.add_argument("--sprint", type=float, default=0.0, metavar="SECONDS",
                    help="run a point-to-point event (no lap counters at all)")
    ap.add_argument("--wta", type=int, default=0, metavar="LAPS",
                    help="run a World Time Attack: all lap fields dead, laps"
                         " only detectable geometrically")
    ap.add_argument("--jumps", action="store_true",
                    help="add sharp elevation spikes (cross-country jumps)")
    args = ap.parse_args()

    global JUMPS
    JUMPS = args.jumps

    sim = Sim(args)
    print(f"Sending to {args.host}:{args.port} at {args.rate:.0f} Hz"
          f"{' [wet]' if args.wet else ''}{' [jumps]' if args.jumps else ''}")
    if args.freeroam > 0:
        sim.freeroam(args.freeroam)
    if args.wta > 0:
        sim.wta(args.wta)
    elif args.sprint > 0:
        sim.sprint(args.sprint)
    elif args.race > 0:
        sim.event(args.duration, f"race ({args.race} laps)",
                  dirty=args.dirty, race_laps=args.race)
    else:
        for i in range(args.events):
            sim.event(args.duration / args.events, f"event {i + 1}/{args.events}",
                      dirty=args.dirty)
    sim.race_off()
    print(f"Done: {sim.sent} packets.")


if __name__ == "__main__":
    main()
