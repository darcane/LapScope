"""Targeted SessionTracker regressions the simulator scenarios can't stage.

These drive hand-built packets straight into the tracker (no simulator
course geometry) to pin down frame-exact behaviors: flag hygiene across lap
re-anchors, race_mode dropping at a LastLap-change finish, and the
listener's recorder-crash fallback keeping the WebSocket frame contract.
"""

from __future__ import annotations

import math
from pathlib import Path

from app.recorder.laps import SessionTracker
from app.recorder.store import Store
from app.telemetry.packet import empty_fields, pack, parse


class Driver:
    """Feeds hand-built packets to a real tracker at a synthetic 60 Hz."""

    def __init__(self, tmp_path) -> None:
        self.store = Store(str(Path(tmp_path) / "telemetry.db"))
        self.tracker = SessionTracker(self.store)
        self.f = empty_fields()
        self.f.update(is_race_on=1, car_ordinal=1, car_class=6, car_pi=900,
                      drivetrain_type=2)
        self.t = 0.0

    def send(self, **kw) -> dict:
        """Advance one frame (race clock included unless overridden) and
        return the tracker extras for it."""
        self.t += 1 / 60
        self.f["current_race_time"] += 1 / 60
        self.f.update(**kw)
        raw = pack(self.f)
        return self.tracker.on_frame(self.t, raw, parse(raw))

    def finish(self) -> list[dict]:
        """Close the session (stream went silent) and return its laps."""
        self.tracker.shutdown(self.t + 20.0)
        laps = self.store.session_laps(1)
        self.store.close()
        return laps


def drive_circle(d: Driver, radius: float = 120.0, v: float = 40.0,
                 laps: float = 1.3) -> None:
    """Drive a circle through the launch point: one geometric (WTA) lap,
    then far enough past it that the crossing finalizes on circle exit."""
    dist, total = 0.0, 2 * math.pi * radius * laps
    while dist < total:
        dist += v / 60
        ang = dist / radius
        # circle centered at (0, radius), launched at the origin heading +x;
        # the car moves along (sin yaw, cos yaw), so yaw = pi/2 - angle
        d.send(pos_x=radius * math.sin(ang), pos_z=radius * (1 - math.cos(ang)),
               distance_traveled=dist, speed=v, yaw=math.pi / 2 - ang,
               accel_x=0.0)


def test_pre_launch_contact_not_flagged_on_geometric_lap(tmp_path):
    """A contact spike while parked at the WTA grid (event load / grid hold,
    before launch) must not dirty lap 1: the launch re-anchor starts the lap
    fresh, flags included."""
    d = Driver(tmp_path)
    for i in range(120):  # 2 s grid hold, DistanceTraveled pinned at 0
        d.send(pos_x=0.0, pos_z=0.0, distance_traveled=0.0, speed=0.0,
               accel_x=60.0 if i == 60 else 0.0)
    drive_circle(d)
    timed = [lap for lap in d.finish() if lap["lap_time"] is not None]
    assert len(timed) == 1
    assert "contact" not in (timed[0]["flags"] or "")


def test_mid_session_reanchor_clears_flags(tmp_path):
    """A contact picked up while cruising before a free-roam time-attack's
    lap timer starts must not stick to the re-anchored lap."""
    d = Driver(tmp_path)
    d.f["current_race_time"] = 50.0  # mid-session clock, no reset-split
    x = 0.0

    def cruise(**kw):
        nonlocal x
        x += 0.5
        # odometer already ran (free roam), so the geometric path stays inert
        d.send(pos_x=x, distance_traveled=5000.0 + x, speed=30.0, **kw)

    for i in range(6 * 60):  # 6 s of cruising, lap fields dead
        cruise(accel_x=60.0 if i == 120 else 0.0, current_lap=0.0)
    for i in range(10 * 60):  # the lap timer starts: re-anchor fires here
        cruise(current_lap=(i + 1) / 60, accel_x=0.0)
    cruise(lap_number=1, current_lap=0.0, last_lap=10.0)  # line crossed

    timed = [lap for lap in d.finish() if lap["lap_time"] is not None]
    assert len(timed) == 1
    assert timed[0]["lap_time"] == 10.0
    assert "contact" not in (timed[0]["flags"] or "")


def test_race_mode_drops_at_lastlap_finish(tmp_path):
    """The LastLap-change finish ends the event: race_mode must go False on
    that frame, not seconds later when the clock freeze is noticed."""
    d = Driver(tmp_path)
    d.f["race_position"] = 3  # gridded
    for i in range(6 * 60):  # lap 1
        d.send(current_lap=(i + 1) / 60, distance_traveled=(i + 1) * 1.0,
               speed=60.0)
    d.send(lap_number=1, current_lap=0.0, last_lap=62.0,
           distance_traveled=361.0, speed=60.0)
    for i in range(6 * 60):  # lap 2 (the final lap)
        extras = d.send(current_lap=(i + 1) / 60,
                        distance_traveled=362.0 + i, speed=60.0)
        assert extras["race_mode"] is True
    # finish: LastLap changes while LapNumber stays put
    extras = d.send(last_lap=61.5, current_lap=0.0, speed=60.0)
    assert extras["race_mode"] is False
    extras = d.send(speed=40.0)  # post-finish coast stays out of race mode
    assert extras["race_mode"] is False

    timed = [lap for lap in d.finish() if lap["lap_time"] is not None]
    assert [lap["lap_time"] for lap in timed] == [62.0, 61.5]


def test_listener_fallback_keeps_frame_contract(tmp_path):
    """A recorder crash must not shrink the published frame: the extras keep
    the documented shape (session_id/delta/session_best/lap_elapsed/race_mode)."""
    from app.telemetry.hub import Hub
    from app.telemetry.listener import TelemetryProtocol

    class Boom:
        def on_frame(self, t, raw, frame):
            raise RuntimeError("boom")

    hub = Hub()
    q = hub.subscribe()
    proto = TelemetryProtocol(hub, Boom())
    proto.datagram_received(pack(empty_fields()), ("127.0.0.1", 50000))
    frame = q.get_nowait()
    for key in ("session_id", "delta", "session_best", "lap_elapsed", "race_mode"):
        assert key in frame
    assert frame["race_mode"] is False
