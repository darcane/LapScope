"""Targeted SessionTracker regressions the simulator scenarios can't stage.

These drive hand-built packets straight into the tracker (no simulator
course geometry) to pin down frame-exact behaviors: flag hygiene across lap
re-anchors, race_mode dropping at a LastLap-change finish, and the
listener's recorder-crash fallback keeping the WebSocket frame contract.
"""

from __future__ import annotations

import math
from pathlib import Path

from app.recorder.laps import SessionTracker, suggest_track_type
from app.recorder.store import Store
from app.telemetry.packet import empty_fields, pack, parse


class Driver:
    """Feeds hand-built packets to a real tracker at a synthetic 60 Hz."""

    def __init__(self, tmp_path) -> None:
        self.store = Store(str(Path(tmp_path) / "telemetry.db"))
        self.tracker = SessionTracker(self.store)
        self.f = empty_fields()
        self.f.update(is_race_on=1, car_ordinal=1, car_class=6, car_pi=900,
                      drivetrain_type=2,
                      # wheels on the ground: all-zero suspension + slip (the
                      # empty_fields default) reads as airborne to the landing
                      # classifier, which would excuse every contact spike
                      norm_susp_travel=[0.5] * 4, tire_combined_slip=[0.3] * 4)
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


GROUND = dict(norm_susp_travel=[0.5] * 4, tire_combined_slip=[0.3] * 4, accel_x=0.0)
AIR = dict(norm_susp_travel=[0.0] * 4, tire_combined_slip=[0.0] * 4, accel_x=0.0)


def drive(d: Driver, n: int, **kw) -> None:
    """n frames of steady driving: lap clock and odometer advance."""
    for _ in range(n):
        d.send(current_lap=d.f["current_lap"] + 1 / 60,
               distance_traveled=d.f["distance_traveled"] + 1.0,
               speed=60.0, **kw)


def lap_flags_after(d: Driver) -> str:
    """Cross the line to complete lap 1, then return its flags."""
    d.send(lap_number=1, current_lap=0.0, last_lap=6.0, speed=60.0, **GROUND)
    drive(d, 60, **GROUND)
    timed = [lap for lap in d.finish() if lap["lap_time"] is not None]
    assert len(timed) >= 1
    return timed[0]["flags"] or ""


def test_landing_spike_after_flight_is_not_contact(tmp_path):
    """A contact-threshold spike right after a real flight (all wheels
    unloaded, no tire force, >= AIRBORNE_MIN_S) is the landing of a jump -
    the lap stays clean. Matches real cross-country captures (session 55):
    touchdown compresses the suspension a frame or two before the spike."""
    d = Driver(tmp_path)
    drive(d, 120, **GROUND)                          # 2 s on the ground
    drive(d, 30, **AIR)                              # 0.5 s flight
    drive(d, 2, **{**GROUND, "accel_x": 110.0})      # touchdown spike, loaded
    drive(d, 120, **GROUND)
    assert "contact" not in lap_flags_after(d)


def test_short_hop_spike_still_flags_contact(tmp_path):
    """Wheels unloaded for only a few frames (a crest, not a flight) does not
    excuse a spike: still contact."""
    d = Driver(tmp_path)
    drive(d, 120, **GROUND)
    drive(d, 4, **AIR)                               # 0.07 s < AIRBORNE_MIN_S
    drive(d, 2, **{**GROUND, "accel_x": 110.0})
    drive(d, 120, **GROUND)
    assert "contact" in lap_flags_after(d)


def test_spike_after_landing_grace_still_flags_contact(tmp_path):
    """A spike well after touchdown (past LANDING_GRACE_S) is real contact -
    e.g. landing a jump, then hitting a rock two seconds later."""
    d = Driver(tmp_path)
    drive(d, 120, **GROUND)
    drive(d, 30, **AIR)                              # a real flight...
    drive(d, 60, **GROUND)                           # ...landed 1 s ago
    drive(d, 2, **{**GROUND, "accel_x": 110.0})
    drive(d, 120, **GROUND)
    assert "contact" in lap_flags_after(d)


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


def test_suggest_track_type_matrix():
    """The calibrated decision tree (thresholds swept from real captures):
    each class from its evidence, None whenever the evidence is thin,
    drag-like, or in the gap zone between surfaces."""
    base = dict(geometric_laps=0, drive_s=120.0, drive_n=7200, corner_n=3000,
                rough_n=7000, rough_hi=0, jumps=0)
    assert suggest_track_type(**base) == "road"
    assert suggest_track_type(**{**base, "rough_hi": 1000, "jumps": 4}) == "dirt"
    assert suggest_track_type(**{**base, "jumps": 12}) == "cross"
    assert suggest_track_type(**{**base, "geometric_laps": 2}) == "wtc"
    assert suggest_track_type(**{**base, "corner_n": 100}) is None   # drag-like
    assert suggest_track_type(**{**base, "rough_hi": 400}) is None   # gap zone
    assert suggest_track_type(**{**base, "drive_s": 20.0}) is None   # too little


def _course_drive(d: Driver, n: int, rough: bool, on_line: bool) -> None:
    """n frames of fast, twisty driving: smooth tarmac or washboard-rough,
    on or off the course line (NormalizedDrivingLine saturates at 127 far
    off course)."""
    for i in range(n):
        d.send(current_lap=d.f["current_lap"] + 1 / 60,
               distance_traveled=d.f["distance_traveled"] + 1.0,
               speed=60.0, steer=60 if i % 2 else -60,
               normalized_driving_line=0 if on_line else 127,
               norm_susp_travel=[0.5 + (0.06 if rough and i % 2 else 0.0)] * 4,
               tire_combined_slip=[0.3] * 4, accel_x=0.0)


def _fly(d: Driver, frames: int, on_line: bool) -> None:
    """A flight (all wheels unloaded); whether it counts as a jump depends
    on the frame it launched FROM being on the course line."""
    for _ in range(frames):
        d.send(current_lap=d.f["current_lap"] + 1 / 60,
               distance_traveled=d.f["distance_traveled"] + 1.0,
               speed=60.0, normalized_driving_line=0 if on_line else 127,
               **AIR)


def _finish_event_track_type(d: Driver) -> str | None:
    """Cross the line to complete the run, close the session, and return the
    auto-suggested track type."""
    d.send(lap_number=1, current_lap=0.0, last_lap=50.0, speed=60.0, **GROUND)
    d.finish()
    return d.store.get_session(1)["track_type"]


def test_rough_driving_with_jumps_reads_dirt(tmp_path):
    """Washboard suspension + an occasional flight, all on the course line:
    the session is auto-tagged dirt (control for the off-line gate below)."""
    d = Driver(tmp_path)
    for _ in range(2):
        _course_drive(d, 25 * 60, rough=True, on_line=True)
        _fly(d, 20, on_line=True)
    assert _finish_event_track_type(d) == "dirt"


def test_off_line_roughness_cannot_fake_dirt(tmp_path):
    """The anti-troll gate: a tarmac event with deliberate off-road
    excursions (driving line saturated while bouncing through the grass,
    jumping ditches) still reads road - off-line frames and off-line
    takeoffs contribute no surface evidence."""
    d = Driver(tmp_path)
    for _ in range(2):
        _course_drive(d, 25 * 60, rough=False, on_line=True)  # tarmac proper
        _course_drive(d, 8 * 60, rough=True, on_line=False)   # grass excursion
        _fly(d, 20, on_line=False)                            # ditch jump
    assert _finish_event_track_type(d) == "road"


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
