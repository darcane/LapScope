"""Session and lap segmentation plus live delta-to-best-lap.

A session starts when frames arrive with IsRaceOn == 1 and ends after
RACE_OFF_GRACE seconds of race-off packets or silence. Two extra signals
subdivide what would otherwise be one long stream:

- CurrentRaceTime jumping backwards to ~0 means a new event started
  (event restart, or a free-roam time-attack circuit beginning) -> the
  current session is closed and a fresh one opened.
- CurrentLap starting to count while LapNumber sits still means the lap
  timer began mid-session (free-roam circuits) -> the open lap is
  re-anchored to that moment so its trace starts at the line.

Event finishes are tricky: the game ends an event *without* incrementing
LapNumber, so the final lap of a race - and the whole run of a
point-to-point event (sprint, drag, street, touge, cross-country) - would
otherwise be lost. Five finish signals fix that:

- LastLap changing while LapNumber stays put = the line was crossed and the
  event ended -> the open lap is completed with that time.
- The DistanceTraveled hard-reset. On a real dirt-sprint capture the finish
  looks like this: the car crosses the line at speed (RacePosition just
  dropped to 0), the stream gaps for the results cinematic (~12 s) while the
  race clock keeps counting, and when it resumes the game has parked the car
  at the line with the brake held and DistanceTraveled reset to 0. Since the
  odometer only ever accumulates on circuits (it never resets per lap), that
  reset to ~0 is an unambiguous finish. It fires whether or not the lap
  fields are alive - a dirt sprint runs CurrentLap (LapNumber/LastLap/BestLap
  stay 0 the whole run); World Time Attack runs nothing - as long as
  LapNumber never incremented (not a circuit) and this is a real event:
  gridded (a race) or a geometric launch (WTA). The single run is completed
  right there, timed launch-to-line: not the post-gap clock (the cinematic
  advanced it several seconds) but the PREVIOUS frame's clock, minus the
  launch clock so the countdown is excluded (the game's own lap times do the
  same - a circuit lap's LastLap equals launch-to-line, not clock-start-to-
  line). The route is fingerprinted from the run's start + covered distance.
- CurrentRaceTime freezing for a while during IsRaceOn == 1 = an alternative
  finish-cinematic signal (kept as a fallback): the session is marked
  finished and a never-counted-a-lap run is completed with the frozen clock.
- Telemetry stopping dead at the line: circuit races cut Data Out the
  instant the race ends (verified on real captures - the last frame lands
  within meters of the finish), so no signal above ever arrives. At session
  end, an open lap that covered a full lap's distance (compared to the
  session's completed laps) is the final lap; it is completed with the lap
  clock's last reading plus the remaining meters at the last speed.
- The same cutoff on a gridded *point-to-point race* with no completed laps
  to compare against. Some point-to-point events end this way instead of the
  odometer-reset handback above - a real touge cut Data Out dead at the line
  at speed (RacePosition 1, IsRaceOn still 1, no reset, no freeze). Recovered
  when gridded (RacePosition > 0 - never true in free roam), launched from a
  DistanceTraveled reset, LapNumber never incremented (not a circuit that
  completed laps), real distance covered, and still at speed when the stream
  stopped -> a run timed launch-to-line. Works whether or not CurrentLap runs
  (a touge counts it, a bare sprint doesn't). A circuit race quit during lap
  one, or the game closed mid-run, looks identical, so these laps carry a
  "cutoff" flag: the time is inferred, not confirmed by a finish signal.

Point-to-point events may never start the CurrentLap clock, so the lap
trace (and live delta) falls back to CurrentRaceTime elapsed since the lap
opened whenever CurrentLap sits at zero.

World Time Attack broadcasts NO lap information at all (verified on a real
capture): LapNumber, CurrentLap, LastLap and BestLap stay 0 for the whole
event; only CurrentRaceTime counts (from event load, through a teleport to
the track and a grid hold with DistanceTraveled pinned at 0), and at the
finish the game auto-stops the car and hard-resets DistanceTraveled while
the clock keeps counting. Laps are therefore detected geometrically: the
launch point (where DistanceTraveled starts growing) is the anchor, and a
lap completes at the closest approach to the anchor after driving away,
provided the car is traveling roughly the same direction it launched in
and has covered enough distance. The DistanceTraveled collapse is the
run-finished signal. A crossing normally finalizes when the car exits the
crossing circle; if the stream dies inside it instead, the pending closest
approach is finalized at session end and the lap flagged "cutoff".

Sessions that end without a single completed lap or run (free-roam
cruising, menu blips, abandoned events) are discarded entirely. Every
discard logs a one-line signal summary so event types the segmentation
doesn't recognize yet can be diagnosed from `docker compose logs`; setting
LS_KEEP_DISCARDED=1 keeps such sessions (raw frames included) instead of
deleting them - drive the unrecognized event once with the flag on and the
data needed to add support for it is preserved.

The packet carries no "lap invalidated" flag, so lap dirtiness is inferred:
- rewind: the lap clock ran backwards mid-lap (reversing on track never
  does that - time only runs forward), corroborated by DistanceTraveled
  not increasing;
- contact: a ground-plane acceleration spike far beyond what tires can
  generate (a wall or solid obstacle). A spike while airborne or right
  after touchdown is the landing of a jump, not contact - cross-country
  is full of those - so it is logged but never flags the lap.
Flags are stored per lap ("rewind,contact") and shown in the lap table.

All methods run on the asyncio event-loop thread.
"""

from __future__ import annotations

import bisect
import logging
import math
import os

log = logging.getLogger("lapscope.recorder")

RACE_OFF_GRACE = 15.0
FLUSH_INTERVAL = 1.0
RACE_TIME_RESET_JUMP = 10.0   # backwards jump (s) that signals a new event
PUDDLE_DEPTH_MIN = 0.03       # any wheel deeper than this counts as a wet frame
WET_FRAME_FRACTION = 0.02     # fraction of wet frames to auto-tag "wet"
REWIND_TIME_JUMP = 0.5        # lap clock going back by this much = rewind
IMPACT_ACCEL = 45.0           # m/s^2 in the ground plane (~4.6 g) = contact

# Airborne / jump-landing discrimination (verified on real cross-country,
# session 55: 5 of its 12 contact spikes were hard jump-landings). In flight
# every wheel hangs at full droop with zero tire force; on the ground even a
# coasting car shows load on some wheel. Touchdown compresses the suspension a
# frame or two BEFORE the accel spike crosses IMPACT_ACCEL, so a spike counts
# as the landing for a short grace window after the flight ends, not only
# while airborne. Mirrored in dashboard.js for the live map (keep in lockstep).
AIRBORNE_SUSP_MAX = 0.15      # all NormalizedSuspensionTravel below this
                              # (0 = full stretch; ~0.1 drift seen pre-landing)
AIRBORNE_SLIP_MAX = 0.05      # and all TireCombinedSlip below this = no tire
                              # touches the ground (driving shows >= ~0.1)
AIRBORNE_MIN_S = 0.12         # unloaded this long = a real flight, not a crest
LANDING_GRACE_S = 0.35        # spikes this soon after touchdown = the landing
                              # (real gaps measured at 0.02-0.07 s)
RT_FREEZE_SECONDS = 1.5       # frozen race clock for this long = event finished
FINISH_MIN_RT = 5.0           # ignore freezes before the clock really ran
PTP_MIN_RUN_TIME = 10.0       # shortest believable point-to-point run
FINAL_LAP_DIST_FRACTION = 0.97  # open lap covering this much of a typical lap
                                # when the stream cuts = finished final lap

# geometric lap detection for events with no lap fields (World Time Attack)
WTA_ARM_DIST = 120.0          # m away from the launch anchor before a crossing can count
WTA_CROSS_DIST = 60.0         # m from the anchor that counts as "at the line"
WTA_MIN_LAP_DIST = 500.0      # DistanceTraveled units a lap must cover before crossing
WTA_HEADING_COS = 0.25        # crossing direction within ~75 deg of the launch heading
WTA_DIST_COLLAPSE = 1000.0    # DistanceTraveled dropping this much = run finished
WTA_TELEPORT_JUMP = 250.0     # single-frame position jump = fast travel (free roam,
                              # not an event - you never teleport mid-run)
POINT_FINISH_DIST = 200.0     # DistanceTraveled must land this close to 0 for a
                              # reset to count as a point-to-point finish

# keep sessions that would otherwise be discarded (no completed laps) - for
# capturing event types the segmentation doesn't recognize yet
KEEP_DISCARDED = os.environ.get("LS_KEEP_DISCARDED", "0").lower() not in ("", "0", "false")


class SessionTracker:
    def __init__(self, store) -> None:
        self.store = store
        self.session_id: int | None = None
        self.last_frame_t: float | None = None
        self._frame_count = 0
        self._buffer: list[tuple[float, bytes]] = []
        self._last_flush = 0.0
        self._race_off_since: float | None = None

        self._lap_id: int | None = None
        self._lap_number: int | None = None
        self._lap_start_dist = 0.0
        self._lap_start_pos = (0.0, 0.0)
        self._cur_d: list[float] = []   # lap distance trace of the current lap
        self._cur_t: list[float] = []   # matching elapsed lap time
        self._ref_d: list[float] | None = None  # best lap reference
        self._ref_t: list[float] | None = None
        self.best_lap_time: float | None = None

        self._prev_race_time: float | None = None
        self._prev_cur_lap: float | None = None
        self._prev_dist: float | None = None
        self._prev_frame_rt: float | None = None  # last frame's race clock (pre-reset)
        self._prev_frame_t: float | None = None   # last frame's wall clock (pre-reset)
        self._prev_pos: tuple[float, float] | None = None  # last frame's world pos
        self._prev_speed = 0.0
        self._lap_distances: list[float] = []  # completed-lap lengths this session
        self._lap_max_elapsed = 0.0
        self._lap_open_rt: float | None = None
        self._launch_rt: float | None = None  # race clock when DistanceTraveled
                                               # started growing (= GO); the run
                                               # excludes the countdown before it
        self._lap_open_last_lap: float | None = None
        self._lap_flags: set[str] = set()
        self._first_rt: float | None = None
        self._event_finished = False
        self._finish_rt: float | None = None
        self._rt_freeze_since: float | None = None
        self._completed_laps = 0
        self._lap_fields_dead = True
        self._gridded = False  # RacePosition > 0 seen (a race, never free roam)
        self._wta_anchor: tuple[float, float] | None = None
        self._wta_heading: tuple[float, float] | None = None
        self._wta_armed = False
        self._wta_inside = False
        self._wta_best: tuple | None = None  # (d, t, rt, dist, x, z) closest pass
        self._wta_prev_pos: tuple[float, float] | None = None
        self._puddle_frames = 0
        self._air_since: float | None = None   # start of the current flight
        self._landing_grace_until = 0.0        # spikes before this t = landing
        self._over_impact = False              # inside a spike burst (log once)
        self._route_assigned = False
        self._session_start_t = 0.0
        self._diag_first_dist: float | None = None
        self._diag_max_ln = 0
        self._diag_max_cur = 0.0

    # -- per-frame entry point ------------------------------------------------

    def on_frame(self, t: float, raw: bytes, frame: dict) -> dict:
        self.last_frame_t = t
        if not frame["is_race_on"]:
            if self.session_id is not None:
                if self._race_off_since is None:
                    self._race_off_since = t
                elif t - self._race_off_since >= RACE_OFF_GRACE:
                    self._end_session(self._race_off_since)
            return {"session_id": self.session_id, "delta": None,
                    "session_best": self.best_lap_time, "lap_elapsed": None,
                    "race_mode": False}

        self._race_off_since = None

        # race timer warped back to ~0: a new event / time-attack started
        rt = frame["current_race_time"]
        if (self.session_id is not None and self._prev_race_time is not None
                and rt < self._prev_race_time - RACE_TIME_RESET_JUMP and rt < 5.0):
            log.info("Race timer reset (%.1fs -> %.1fs): splitting session",
                     self._prev_race_time, rt)
            self._end_session(t)

        # race timer frozen while still "racing": the finish cinematic /
        # results screen - the event is over even though IsRaceOn stays 1
        if (self.session_id is not None and self._prev_race_time is not None
                and rt > FINISH_MIN_RT and abs(rt - self._prev_race_time) < 1e-3):
            if self._rt_freeze_since is None:
                self._rt_freeze_since = t
            elif (not self._event_finished
                  and t - self._rt_freeze_since >= RT_FREEZE_SECONDS):
                self._event_finished = True
                self._finish_rt = rt
                log.info("Session %d: race timer frozen at %.1fs - event finished",
                         self.session_id, rt)
        else:
            self._rt_freeze_since = None
        self._prev_race_time = rt

        if self.session_id is None:
            self._start_session(t, frame)
        self._buffer.append((t, raw))
        self._frame_count += 1
        if frame["race_position"] > 0:
            self._gridded = True
        if any(d > PUDDLE_DEPTH_MIN for d in frame["wheel_in_puddle"]):
            self._puddle_frames += 1
        airborne = (all(s < AIRBORNE_SUSP_MAX for s in frame["norm_susp_travel"])
                    and all(s < AIRBORNE_SLIP_MAX for s in frame["tire_combined_slip"]))
        if airborne:
            if self._air_since is None:
                self._air_since = t
        else:
            if self._air_since is not None and t - self._air_since >= AIRBORNE_MIN_S:
                self._landing_grace_until = t + LANDING_GRACE_S
            self._air_since = None
        flying = self._air_since is not None and t - self._air_since >= AIRBORNE_MIN_S
        g = math.hypot(frame["accel_x"], frame["accel_z"])
        if g >= IMPACT_ACCEL:
            if flying or t < self._landing_grace_until:
                if not self._over_impact:
                    log.info("Session %s: jump landing (%.0f m/s^2) - not contact",
                             self.session_id, g)
            else:
                if "contact" not in self._lap_flags:
                    log.info("Session %s: contact spike (%.0f m/s^2), flagging lap",
                             self.session_id, g)
                self._lap_flags.add("contact")
            self._over_impact = True
        else:
            self._over_impact = False
        delta = self._lap_logic(t, frame)
        if t - self._last_flush >= FLUSH_INTERVAL:
            self.flush()
            self._last_flush = t
        # live lap clock for events that never start CurrentLap (WTA/sprint)
        lap_elapsed = None
        if (self._lap_id is not None and frame["current_lap"] <= 0.001
                and self._lap_open_rt is not None):
            e = frame["current_race_time"] - self._lap_open_rt
            if e > 0:
                lap_elapsed = e
        return {"session_id": self.session_id, "delta": delta,
                "session_best": self.best_lap_time, "lap_elapsed": lap_elapsed,
                "race_mode": self.race_mode(frame)}

    def race_mode(self, frame: dict) -> bool:
        """Is a timed event running right now, as opposed to free-roam
        cruising? IsRaceOn can't tell (it is 1 in free roam too). Verified on
        real captures: races grid you with RacePosition > 0 from the very
        first frame; lap-timed events run the lap fields; WTA / point-to-point
        events broadcast neither but reset DistanceTraveled at launch, which
        is exactly when the geometric anchor arms. Ends with the event."""
        if self.session_id is None or self._event_finished:
            return False
        return (frame["race_position"] > 0
                or not self._lap_fields_dead
                or self._wta_anchor is not None)

    def tick(self, now: float) -> None:
        """Watchdog: close the session if the game simply stopped sending."""
        if (self.session_id is not None and self.last_frame_t is not None
                and now - self.last_frame_t >= RACE_OFF_GRACE):
            self._end_session(self.last_frame_t)

    def shutdown(self, now: float) -> None:
        if self.session_id is not None:
            self._end_session(self.last_frame_t or now)

    def flush(self) -> None:
        if self._buffer and self.session_id is not None:
            self.store.add_frames(self.session_id, self._buffer)
            self._buffer = []

    # -- internals --------------------------------------------------------------

    def _start_session(self, t: float, frame: dict) -> None:
        self.session_id = self.store.create_session(t, frame)
        self._frame_count = 0
        self._last_flush = t
        self._lap_id = None
        self._lap_number = None
        self._ref_d = self._ref_t = None
        self.best_lap_time = None
        self._prev_cur_lap = None
        self._prev_frame_rt = None
        self._prev_frame_t = None
        self._prev_pos = None
        self._prev_speed = 0.0
        self._lap_distances = []
        self._launch_rt = None
        self._first_rt = frame["current_race_time"]
        self._event_finished = False
        self._finish_rt = None
        self._rt_freeze_since = None
        self._completed_laps = 0
        self._lap_fields_dead = True
        self._gridded = False
        self._wta_anchor = None
        self._wta_heading = None
        self._wta_armed = False
        self._wta_inside = False
        self._wta_best = None
        self._wta_prev_pos = None
        self._puddle_frames = 0
        self._air_since = None
        self._landing_grace_until = 0.0
        self._over_impact = False
        self._route_assigned = False
        self._session_start_t = t
        self._diag_first_dist = frame["distance_traveled"]
        self._diag_max_ln = frame["lap_number"]
        self._diag_max_cur = 0.0
        log.info("Session %d started (car ordinal %d, PI %d)",
                 self.session_id, frame["car_ordinal"], frame["car_pi"])

    def _end_session(self, end_t: float) -> None:
        self.flush()
        if self._lap_id is not None:
            # a geometric crossing normally finalizes when the car exits the
            # crossing circle; if the stream died inside it, the recorded
            # closest approach is still pending - complete that lap now
            self._finalize_wta_crossing(None)
        if self._lap_id is not None:
            is_run = True  # a point-to-point run (fingerprint the whole course)
            lap_time = self._point_to_point_run_time()
            if lap_time is not None:
                log.info("Session %d: point-to-point run captured (%.3fs)",
                         self.session_id, lap_time)
            else:
                lap_time = self._ptp_run_time_at_cutoff()
                if lap_time is not None:
                    self._lap_flags.add("cutoff")
                    log.info("Session %d: point-to-point run recovered at"
                             " telemetry cutoff (%.3fs) - no finish signal,"
                             " timed to the last packet", self.session_id, lap_time)
            if lap_time is None:
                is_run = False
                lap_time = self._final_lap_time_at_cutoff()
                if lap_time is not None:
                    log.info("Session %d: final lap recovered at telemetry"
                             " cutoff (%.3fs)", self.session_id, lap_time)
            if (is_run and lap_time is not None and not self._route_assigned
                    and self._prev_dist is not None):
                length = self._prev_dist - self._lap_start_dist
                if length > 100.0:
                    rid = self.store.match_or_create_route(
                        self._lap_start_pos[0], self._lap_start_pos[1], length)
                    self.store.set_session_route(self.session_id, rid)
            self.store.complete_lap(self._lap_id, end_t, lap_time, self._flags())
            if lap_time is not None:
                self._completed_laps += 1
            self._lap_id = None
        if self._completed_laps == 0:
            # signal summary for diagnosing event types the segmentation
            # doesn't recognize yet (e.g. World Time Attack)
            diag = ("dur=%.0fs rt=%.1f..%.1f maxLapNumber=%d maxCurrentLap=%.1f "
                    "finish_seen=%s gridded=%s launch=%s lastSpeed=%.1f dist=%+.0f" % (
                        end_t - self._session_start_t,
                        self._first_rt if self._first_rt is not None else float("nan"),
                        self._prev_race_time if self._prev_race_time is not None
                        else float("nan"),
                        self._diag_max_ln, self._diag_max_cur, self._event_finished,
                        self._gridded, self._launch_rt is not None,
                        self._prev_speed,
                        (self._prev_dist - self._diag_first_dist)
                        if self._prev_dist is not None and self._diag_first_dist is not None
                        else 0.0))
            if KEEP_DISCARDED:
                self.store.end_session(self.session_id, end_t, self._frame_count,
                                       conditions=None)
                self.store.mark_session_kept(self.session_id)
                log.info("Session %d KEPT with no completed laps "
                         "(LS_KEEP_DISCARDED=1) | diag: %s", self.session_id, diag)
            else:
                self.store.discard_session(self.session_id)
                log.info("Session %d discarded (no completed laps, %d frames) | diag: %s",
                         self.session_id, self._frame_count, diag)
        else:
            wet = (self._frame_count > 0
                   and self._puddle_frames / self._frame_count > WET_FRAME_FRACTION)
            self.store.end_session(self.session_id, end_t, self._frame_count,
                                   conditions="wet" if wet else None)
            log.info("Session %d ended (%d frames, %d laps%s)", self.session_id,
                     self._frame_count, self._completed_laps, ", wet" if wet else "")
        self.session_id = None
        self._race_off_since = None
        self._lap_number = None
        self._ref_d = self._ref_t = None
        self.best_lap_time = None
        self._prev_race_time = None
        self._prev_cur_lap = None
        self._prev_dist = None
        self._prev_frame_rt = None
        self._prev_frame_t = None
        self._prev_pos = None
        self._prev_speed = 0.0
        self._lap_distances = []
        self._lap_max_elapsed = 0.0
        self._lap_open_rt = None
        self._launch_rt = None
        self._lap_open_last_lap = None
        self._first_rt = None
        self._event_finished = False
        self._finish_rt = None
        self._rt_freeze_since = None
        self._lap_fields_dead = True
        self._gridded = False
        self._wta_anchor = None
        self._wta_heading = None
        self._wta_armed = False
        self._wta_inside = False
        self._wta_best = None
        self._wta_prev_pos = None
        self._air_since = None
        self._landing_grace_until = 0.0
        self._over_impact = False
        self._lap_flags = set()

    def _point_to_point_run_time(self) -> float | None:
        """Run time for an open lap in a session that ended without ever
        counting a lap: a finished point-to-point event (sprint, drag,
        street...) is one run, timed from launch to the finish signal (the
        frozen race clock, or the DistanceTraveled collapse). Abandoned
        events don't qualify - no finish was seen."""
        if (self._completed_laps == 0 and self._event_finished
                and self._first_rt is not None and self._first_rt < 5.0
                and self._finish_rt is not None):
            run = self._finish_rt - (self._launch_rt if self._launch_rt is not None
                                     else (self._lap_open_rt or 0.0))
            if run > PTP_MIN_RUN_TIME:
                return run
        return None

    def _finish_ptp_run(self, t: float, run_time: float) -> None:
        """Complete the single run of a point-to-point event at its finish,
        fingerprinting the route from the run's start position and covered
        distance. `t` is the last racing frame (the run ends there, before
        the odometer-reset frame); _prev_dist still holds the pre-reset
        odometer here, so it measures the whole course."""
        self.store.complete_lap(self._lap_id, t, run_time, self._flags())
        self._completed_laps += 1
        if not self._route_assigned and self._prev_dist is not None:
            length = self._prev_dist - self._lap_start_dist
            if length > 100.0:
                rid = self.store.match_or_create_route(
                    self._lap_start_pos[0], self._lap_start_pos[1], length)
                self.store.set_session_route(self.session_id, rid)
                self._route_assigned = True
        self._lap_id = None

    def _ptp_run_time_at_cutoff(self) -> float | None:
        """Run time for a gridded point-to-point race whose telemetry cut
        dead at the finish line. Verified on a real touge capture: the car
        crosses at speed (57 m/s, RacePosition 1, IsRaceOn still 1) and the
        stream simply stops - no DistanceTraveled reset, no clock freeze, no
        parked handback (unlike a dirt sprint). Some point-to-point events
        end this way, others hand control back with the odometer reset (that
        path finishes inline in _lap_logic); this covers the ones that don't.

        Requirements free roam can't meet: gridded (RacePosition > 0 - never
        true in free roam), a launch from a DistanceTraveled reset (launch_rt
        set), LapNumber never incremented (not a circuit with completed laps
        - those recover their final lap via _final_lap_time_at_cutoff), real
        distance covered, and speed at the cutoff (filters parked/AFK ends).
        Works whether or not CurrentLap runs (a touge counts it, a bare
        sprint doesn't). Timed launch-to-line, so the countdown is excluded.
        A circuit race quit during its first lap, or the game closed mid-run,
        is indistinguishable - hence the "cutoff" flag the caller adds."""
        if (self._completed_laps == 0 and not self._event_finished
                and self._gridded and self._diag_max_ln == 0
                and self._launch_rt is not None
                and self._prev_race_time is not None
                and self._prev_speed > 5.0
                and self._prev_dist is not None
                and self._prev_dist - self._lap_start_dist > WTA_MIN_LAP_DIST):
            run = self._prev_race_time - self._launch_rt
            if run > PTP_MIN_RUN_TIME:
                return run
        return None

    def _final_lap_time_at_cutoff(self) -> float | None:
        """Lap time for an open lap in a session that ended mid-race: circuit
        races cut Data Out the instant the race ends, so the final lap never
        gets a finish signal - the stream just stops at the line. If the open
        lap covered a (nearly) full lap's distance, it *is* the final lap.
        An abandoned mid-lap quit doesn't qualify - too little distance."""
        if (not self._lap_distances or self._prev_dist is None
                or self._lap_max_elapsed <= 1.0):
            return None
        # upper median: a partial first lap (server joined mid-lap) must not
        # drag the typical length down
        typical = sorted(self._lap_distances)[len(self._lap_distances) // 2]
        covered = self._prev_dist - self._lap_start_dist
        if typical <= 0 or covered < FINAL_LAP_DIST_FRACTION * typical:
            return None
        lap_time = self._lap_max_elapsed
        if covered < typical and self._prev_speed > 1.0:
            # the last frame landed a few meters short of the line
            lap_time += min((typical - covered) / self._prev_speed, 2.0)
        return lap_time

    def _lap_logic(self, t: float, frame: dict) -> float | None:
        ln = frame["lap_number"]
        dist = frame["distance_traveled"]
        cur = frame["current_lap"]
        rt = frame["current_race_time"]
        last = frame["last_lap"]
        self._diag_max_ln = max(self._diag_max_ln, ln)
        self._diag_max_cur = max(self._diag_max_cur, cur)

        if self._lap_fields_dead and (ln > 0 or cur > 0.001 or last > 0.001):
            self._lap_fields_dead = False  # normal lap telemetry; forever

        # launch = DistanceTraveled starts growing after the grid hold. Capture
        # the race clock here so a point-to-point run is timed from GO, not from
        # the countdown: the game's own lap times exclude it (verified - a
        # circuit lap's LastLap equals launch-to-line, not clock-start-to-line).
        if (self._launch_rt is None and self._prev_dist is not None
                and self._prev_dist < 1.0 and dist >= 1.0):
            self._launch_rt = rt

        # Point-to-point / WTA finish: the game hard-resets DistanceTraveled to
        # ~0 when the run completes (5951 -> 0 on a real dirt-sprint capture;
        # ~18000 -> 0 on WTA) while the race clock keeps counting through the
        # results screen. DistanceTraveled only ever accumulates on circuits
        # (never resets per lap), so a reset to near-zero is unambiguously a
        # finish - it fires whether or not the lap fields are alive (the real
        # dirt sprint runs CurrentLap; WTA runs nothing), as long as LapNumber
        # never incremented (not a circuit) and this is a real event: gridded
        # (a race) or a geometric launch (WTA). The car stays put while the
        # odometer resets (4.5 m on the capture) - a free-roam fast travel
        # resets it too but teleports you away, so a big jump disqualifies it.
        moved = (math.hypot(frame["pos_x"] - self._prev_pos[0],
                            frame["pos_z"] - self._prev_pos[1])
                 if self._prev_pos is not None else 0.0)
        if (self._diag_max_ln == 0 and self._prev_dist is not None
                and rt > FINISH_MIN_RT and self._prev_dist > WTA_MIN_LAP_DIST
                and dist < POINT_FINISH_DIST
                and dist < self._prev_dist - WTA_DIST_COLLAPSE
                and moved < WTA_TELEPORT_JUMP
                and (self._gridded
                     or (self._lap_fields_dead and self._wta_anchor is not None))):
            finished_now = not self._event_finished
            if finished_now:
                self._event_finished = True
                # the finish cinematic can gap the stream and advance the clock
                # several seconds (12.6 s on the capture); the run ended at the
                # PREVIOUS frame's clock, not this post-gap one
                self._finish_rt = self._prev_frame_rt
                log.info("Session %d: DistanceTraveled reset (%.0f -> %.0f)"
                         " - point-to-point run finished", self.session_id,
                         self._prev_dist, dist)
            if self._completed_laps > 0 and self._lap_id is not None:
                # WTA multi-lap: geometric crossings already timed the laps;
                # the open remainder is the post-finish coast, not a lap
                self._finalize_wta_crossing(frame)
                if self._lap_id is not None:
                    self.store.delete_lap(self._lap_id)
                    self._lap_id = None
            elif finished_now and self._lap_id is not None:
                # single run (dirt sprint / sprint): complete it here, timed
                # launch-to-line, before post-finish parked frames pollute it.
                # End the lap at the PREVIOUS (last racing) frame, not this
                # one: this frame has DistanceTraveled reset to 0 but is still
                # at the finish position, so keeping it in the lap makes the
                # analysis map (which orders points by DistanceTraveled) draw
                # a spurious streak from the grid across to the finish.
                launch = (self._launch_rt if self._launch_rt is not None
                          else (self._lap_open_rt or 0.0))
                run = (self._finish_rt - launch
                       if self._finish_rt is not None else None)
                if run is not None and run > PTP_MIN_RUN_TIME:
                    log.info("Session %d: point-to-point run captured (%.3fs,"
                             " launch to finish)", self.session_id, run)
                    self._finish_ptp_run(self._prev_frame_t or t, run)
            self._prev_dist = dist
            self._prev_frame_rt = rt
            self._prev_frame_t = t
            self._prev_pos = (frame["pos_x"], frame["pos_z"])
            self._prev_cur_lap = cur
            return None

        # point-to-point events may never start the lap clock; fall back to
        # race time elapsed since the lap opened
        def lap_elapsed() -> float:
            if cur > 0:
                return cur
            if self._lap_open_rt is not None and rt > self._lap_open_rt + 0.05:
                return rt - self._lap_open_rt
            return 0.0

        elapsed = lap_elapsed()

        # rewind: the lap clock ran backwards mid-lap. Reversing on track keeps
        # the clock counting up, so this only happens on rewind. Compared
        # against the lap's high-water mark, not the previous frame - the
        # rewind scrub moves the clock back gradually. Requiring distance to
        # not grow rules out the clock-reset jitter at lap boundaries.
        # The trace tail past the rewound-to point is dropped - the re-driven
        # stretch (with its rewound times) replaces it.
        if (self._lap_number is not None and ln == self._lap_number
                and 0.05 < elapsed < self._lap_max_elapsed - REWIND_TIME_JUMP
                and self._prev_dist is not None and dist <= self._prev_dist + 0.5):
            if "rewind" not in self._lap_flags:
                log.info("Session %s: rewind detected on lap %d (%.1fs -> %.1fs)",
                         self.session_id, ln + 1, self._lap_max_elapsed, elapsed)
            self._lap_flags.add("rewind")
            self._lap_max_elapsed = elapsed
            lap_dist_now = dist - self._lap_start_dist
            while self._cur_d and self._cur_d[-1] >= lap_dist_now:
                self._cur_d.pop()
                self._cur_t.pop()
        self._prev_dist = dist
        self._prev_frame_rt = rt
        self._prev_frame_t = t
        self._prev_pos = (frame["pos_x"], frame["pos_z"])
        self._prev_speed = frame["speed"]
        self._lap_max_elapsed = max(self._lap_max_elapsed, elapsed)

        if self._lap_number is None or ln < self._lap_number:
            # first frame of the session, or the lap counter was reset
            # (a rewind back across the start line also lands here)
            if self._lap_id is not None:
                self.store.complete_lap(self._lap_id, t, None, self._flags())
            self._open_lap(t, ln, dist, frame)
        elif ln > self._lap_number:
            lap_time = last if last > 0 else None
            self._complete_current_lap(t, lap_time, self._lap_number)
            self._open_lap(t, ln, dist, frame)
        elif (self._lap_id is not None and last > 0
              and self._lap_open_last_lap is not None
              and abs(last - self._lap_open_last_lap) > 1e-3
              and t - self._lap_opened_t > 5.0):
            # LastLap changed while LapNumber stayed put: the line was crossed
            # and the event ended (final lap of a race, or a point-to-point
            # finish) - the game stops counting laps instead of incrementing
            log.info("Session %d: event finish detected (last lap %.3fs)",
                     self.session_id, last)
            self._complete_current_lap(t, last, self._lap_number)
            self._event_finished = True  # race_mode drops right at the line
            self._lap_id = None  # nothing to reopen: the event is over
        elif (self._lap_id is not None
              and self._prev_cur_lap is not None and self._prev_cur_lap <= 0.0
              and 0.0 < cur < 3.0 and t - self._lap_opened_t > 5.0):
            # lap timer started counting after sitting dead for a while with
            # LapNumber unchanged: free-roam time-attack begins here -
            # re-anchor the open lap (a timer restarting right at a normal lap
            # boundary is excluded by the age check)
            log.info("Session %d: lap timer started mid-session, re-anchoring lap",
                     self.session_id)
            self._lap_opened_t = t
            self._lap_start_dist = dist
            self._lap_start_pos = (frame["pos_x"], frame["pos_z"])
            self._cur_d, self._cur_t = [], []
            self._lap_flags = set()  # the lap starts here; earlier flags aren't its
            self._lap_max_elapsed = 0.0
            self._lap_open_rt = rt
            self.store.restart_lap(self._lap_id, t, dist)
        self._prev_cur_lap = cur

        if self._lap_fields_dead and self._lap_id is not None:
            self._wta_logic(t, frame, rt, dist)

        if self._lap_id is None:
            return None  # event finished; coasting frames belong to no lap
        elapsed = lap_elapsed()  # _open_lap may have re-based the fallback
        lap_dist = dist - self._lap_start_dist
        if elapsed > 0 and lap_dist > 0 and (not self._cur_d or lap_dist > self._cur_d[-1]):
            self._cur_d.append(lap_dist)
            self._cur_t.append(elapsed)
        return self._delta(lap_dist, elapsed)

    def _wta_logic(self, t: float, frame: dict, rt: float, dist: float) -> None:
        """Geometric lap detection for events that broadcast no lap fields at
        all (World Time Attack): a lap is a return to the launch point."""
        # a single-frame position jump is a fast travel: this is free roam,
        # not an event - disarm (rewind scrubs move gradually, never this far)
        if self._wta_prev_pos is not None and self._wta_anchor is not None:
            jump = math.hypot(frame["pos_x"] - self._wta_prev_pos[0],
                              frame["pos_z"] - self._wta_prev_pos[1])
            if jump > WTA_TELEPORT_JUMP:
                log.info("Session %d: teleport (%.0f m) - disarming geometric"
                         " lap detection (free roam)", self.session_id, jump)
                self._wta_anchor = None
                self._wta_heading = None
                self._wta_armed = False
                self._wta_inside = False
                self._wta_best = None
        self._wta_prev_pos = (frame["pos_x"], frame["pos_z"])
        if self._wta_anchor is None:
            if dist < 1.0:
                # grid hold / event load: distance pinned at zero until GO
                self._lap_start_dist = dist
                return
            if self._lap_start_dist < 1.0:
                # launch: distance starts counting - re-anchor the open lap
                # here so lap 1 isn't timed from the loading screen
                self._wta_anchor = (frame["pos_x"], frame["pos_z"])
                self._lap_opened_t = t
                self._lap_start_dist = dist
                self._lap_start_pos = self._wta_anchor
                self._cur_d, self._cur_t = [], []
                self._lap_flags = set()  # pre-launch junk frames must not dirty lap 1
                self._lap_max_elapsed = 0.0
                self._lap_open_rt = rt
                self.store.restart_lap(self._lap_id, t, dist)
                log.info("Session %d: launch detected (race clock at %.1fs)",
                         self.session_id, rt)
            return
        if self._wta_heading is None:
            if frame["speed"] > 10.0:
                # Velocity is car-local (~(0, speed) whatever the direction),
                # so the world heading comes from yaw: the car moves along
                # (sin yaw, cos yaw) - verified against position deltas
                self._wta_heading = (math.sin(frame["yaw"]),
                                     math.cos(frame["yaw"]))
            return
        d = math.hypot(frame["pos_x"] - self._wta_anchor[0],
                       frame["pos_z"] - self._wta_anchor[1])
        if not self._wta_armed:
            self._wta_armed = d > WTA_ARM_DIST
            return
        if d < WTA_CROSS_DIST:
            aligned = frame["speed"] > 3.0 and (
                math.sin(frame["yaw"]) * self._wta_heading[0]
                + math.cos(frame["yaw"]) * self._wta_heading[1]) > WTA_HEADING_COS
            if (aligned and dist - self._lap_start_dist > WTA_MIN_LAP_DIST
                    and (self._wta_best is None or d < self._wta_best[0])):
                self._wta_best = (d, t, rt, dist, frame["pos_x"], frame["pos_z"])
            self._wta_inside = True
        elif self._wta_inside:
            self._finalize_wta_crossing(frame)
            self._wta_inside = False

    def _finalize_wta_crossing(self, frame: dict | None) -> None:
        """Close the lap at the recorded closest approach to the anchor.

        frame None = the session is ending with the crossing still pending
        (the stream died inside the crossing circle before exiting it, which
        is what normally finalizes a crossing): the lap is completed without
        reopening a new one, flagged cutoff - the car might have passed
        closer to the anchor had the stream continued."""
        if self._wta_best is None:
            return
        d, bt, brt, bdist, bx, bz = self._wta_best
        self._wta_best = None
        lap_time = brt - (self._lap_open_rt if self._lap_open_rt is not None else brt)
        if lap_time <= 1.0:
            return
        if frame is None:
            self._lap_flags.add("cutoff")
        log.info("Session %d: geometric lap %d done: %.3fs (crossed %.1f m"
                 " from the launch point%s)", self.session_id,
                 self._completed_laps + 1, lap_time, d,
                 ", finalized at stream end" if frame is None else "")
        self._complete_current_lap(bt, lap_time, self._lap_number)
        if frame is None:
            self._lap_id = None  # session is ending: nothing to reopen
            return
        self._open_lap(bt, self._lap_number, bdist, frame,
                       stored_ln=self._completed_laps)
        # the reopen used the current frame; rebase it to the crossing itself
        self._lap_start_pos = (bx, bz)
        self._lap_open_rt = brt

    def _complete_current_lap(self, t: float, lap_time: float | None, ln: int) -> None:
        self.store.complete_lap(self._lap_id, t, lap_time, self._flags())
        # a trace only works as a delta reference (or route fingerprint) if
        # it covers the lap from (near) the start line - not if the server
        # joined mid-lap
        full_trace = bool(self._cur_t) and self._cur_t[0] < 5.0
        if lap_time is not None:
            self._completed_laps += 1
            if self._cur_d:  # lap length, for spotting the cut-off final lap
                self._lap_distances.append(self._cur_d[-1])
            if full_trace and not self._route_assigned:
                self._assign_route()
            if full_trace and (self.best_lap_time is None
                               or lap_time < self.best_lap_time):
                self.best_lap_time = lap_time
                self._ref_d, self._ref_t = self._cur_d, self._cur_t
                log.info("Session %d: new best lap %.3fs (lap %d)",
                         self.session_id, lap_time, ln + 1)

    def _open_lap(self, t: float, ln: int, dist: float, frame: dict,
                  stored_ln: int | None = None) -> None:
        """stored_ln overrides the lap number written to the DB - geometric
        (WTA) laps all report LapNumber 0, so they get sequential numbers."""
        self._lap_opened_t = t
        self._lap_number = ln
        self._lap_start_dist = dist
        self._lap_start_pos = (frame["pos_x"], frame["pos_z"])
        self._cur_d, self._cur_t = [], []
        self._lap_flags = set()
        self._lap_max_elapsed = 0.0
        self._lap_open_rt = frame["current_race_time"]
        self._lap_open_last_lap = frame["last_lap"]
        self._lap_id = self.store.add_lap(
            self.session_id, ln if stored_ln is None else stored_ln, t, dist)

    def _flags(self) -> str | None:
        return ",".join(sorted(self._lap_flags)) or None

    def _assign_route(self) -> None:
        """Fingerprint the just-completed lap's start point + length."""
        if not self._cur_d:
            return
        route_id = self.store.match_or_create_route(
            self._lap_start_pos[0], self._lap_start_pos[1], self._cur_d[-1])
        self.store.set_session_route(self.session_id, route_id)
        self._route_assigned = True

    def _delta(self, lap_dist: float, cur: float) -> float | None:
        if not self._ref_d or cur <= 0 or lap_dist <= 0:
            return None
        d, rt = self._ref_d, self._ref_t
        if lap_dist >= d[-1]:
            ref = rt[-1]
        else:
            i = bisect.bisect_left(d, lap_dist)
            if i == 0:
                ref = rt[0]
            else:
                frac = (lap_dist - d[i - 1]) / (d[i] - d[i - 1])
                ref = rt[i - 1] + frac * (rt[i] - rt[i - 1])
        return cur - ref
