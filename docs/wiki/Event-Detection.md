# How LapScope detects events and laps

FH6's telemetry never says what's happening. There is no "event started"
message, no game-mode field, no lap-invalidated flag, no route names — and the
game doesn't even count the last lap of a race (`LapNumber` stops incrementing
at the finish). LapScope infers all of it from packet behavior.

Every rule on this page exists because a **real capture** broke a naive
version of the recorder, and each one is pinned by a headless regression test
(the scenario matrix in
[AGENTS.md](https://github.com/darcane/LapScope/blob/main/AGENTS.md) runs
through [`tests/test_scenarios.py`](https://github.com/darcane/LapScope/blob/main/tests/test_scenarios.py),
with matching [`tools/simulator.py`](https://github.com/darcane/LapScope/blob/main/tools/simulator.py)
flags for full-stack checks). The implementation lives in
[`app/recorder/laps.py`](https://github.com/darcane/LapScope/blob/main/app/recorder/laps.py).

## Sessions

- A **session** is a continuous stretch of `IsRaceOn = 1`. It ends after ~15
  seconds of race-off or silence — which is why a finished drive appears on
  the Analysis page about 15 s after packets stop.
- `CurrentRaceTime` warping back to ~0 **splits** the session: that's an event
  restart, or a free-roam time-attack launching.
- Sessions that end without a single completed lap or run are **discarded** —
  free-roam cruising and menu blips never clutter the list. If a real event
  gets discarded, that's an unrecognized event type: see
  [Capturing an Unrecognized Event](Capturing-an-Unrecognized-Event).

## Race mode vs. free roam

`IsRaceOn` is useless here — **it's 1 in free roam too** (it only separates
driving from menus). An actual event is recognized by any of:

- **A grid position:** `RacePosition > 0`, present from the very first
  countdown frame of every gridded race.
- **An odometer reset at launch:** World Time Attack and point-to-point events
  reset `DistanceTraveled` to 0 when the run starts.
- **Live lap fields:** `LapNumber` / `CurrentLap` actually counting.

Free roam has none of these (verified on real captures). The result is the
`race_mode` flag on every live frame — it drives the RACE MODE / FREE ROAM
chip, gates the lap timer, and (unless you enable the free-roam map in
Settings) the live track map.

## Circuit laps, and the finish problem

On circuits, `LapNumber` increments at the line and `LastLap` reports the
game's own time — easy. The hard part is the **finish**, which never
increments `LapNumber`. LapScope watches for five distinct finish signals,
each verified on real data:

1. **`LastLap` changes while `LapNumber` stands still** — the game posted a
   final-lap time without counting the lap. The classic circuit-race finish.
2. **`DistanceTraveled` hard-resets to ~0** — the real point-to-point finish
   (verified on a dirt-sprint capture, 2026-07-03). The car crosses at speed,
   `RacePosition` drops to 0, the stream gaps ~12 s for the results cinematic
   (race clock counting through it), then the game hands control back
   **parked at the line, brake held ~75 %, gear 1, odometer reset to 0**. On
   circuits the odometer only ever accumulates, so a reset is unambiguous —
   guarded against free-roam fast travel (which also zeroes it) by requiring
   real distance covered **and** the car to stay put across the reset (a fast
   travel teleports you away; a finish doesn't).
3. **The race clock freezes ≥ 1.5 s while `IsRaceOn` stays 1** — a fallback
   signal for finish cinematics that don't gap the stream.
4. **The stream cuts dead at the line (circuits)** — real circuit races stop
   Data Out the instant the race ends; the last packet lands within meters of
   the finish, so none of the signals above ever arrive. At session end, an
   open lap that covered ≥ 97 % of the session's typical lap length is
   completed from the lap clock's last reading plus the remaining meters at
   the last speed.
5. **The same cutoff on a gridded point-to-point** — verified on a real touge
   (2026-07-04): gridded 1v1, `CurrentLap` counting, crossing at 57 m/s with
   `RacePosition` 1 and `IsRaceOn` still 1… and the stream just stops. No
   reset, no freeze, no handback. Recovered at session end: gridded, launched
   from an odometer reset, `LapNumber` never incremented, real distance
   covered, still at speed → the run is timed and kept. **Cross-country shares
   this exact signature** (verified 2026-07-05: gridded start P8→P3, cut dead
   at the line at 67 m/s). These runs carry the **`cutoff` 🏁 flag**, because a
   lap-one quit or a game crash looks identical — the time is inferred, not
   confirmed by a finish signal.

Timed runs are measured **launch-to-line** (the clock at the odometer-reset
launch to the clock at the line, excluding the countdown) — matching the
game's own convention: a circuit lap's `LastLap` is launch-to-line too,
verified against a real session.

Abandoned events with none of these signatures are discarded.

## Event types and how they end

Different FH6 event types end in different ways — this table is the union of
the verified real captures and the simulator's regression scenarios:

| Event type | Lap fields during the run | Finish signature |
|---|---|---|
| Circuit race | `LapNumber` counts, `LastLap` posts | `LastLap`-change, or stream cut dead at the line |
| Rivals | endless laps, `LapNumber` counts | you leave — no finish needed |
| Sprint (bare) | none run | odometer-reset handback, or cut dead → `cutoff` 🏁 |
| Dirt sprint | `CurrentLap` runs, `LapNumber` stays 0 | odometer-reset handback after the results cinematic |
| Touge | `CurrentLap` runs, `LapNumber` stays 0 | stream cut dead at the line → `cutoff` 🏁 |
| Cross-country | `CurrentLap` runs, `LapNumber` stays 0 | stream cut dead at the line → `cutoff` 🏁 |
| World Time Attack | **all four lap fields stay 0** | odometer hard-reset while the clock keeps counting |

Point-to-point events may never start `CurrentLap` at all, so lap traces and
the live delta fall back to race-time-elapsed-since-lap-open whenever the
per-lap clock sits at zero.

## World Time Attack: laps without lap fields

WTA broadcasts **no lap information whatsoever** (verified on a real capture,
2026-07-02: `LapNumber`, `CurrentLap`, `LastLap`, `BestLap` all 0 for the
entire event, the race clock counting from event *load*, through a teleport to
the track and a grid hold with `DistanceTraveled` pinned at 0). So laps are
detected **geometrically**:

- The **launch** is where `DistanceTraveled` starts growing — that position
  becomes the anchor and the lap clock re-bases there.
- A **lap completes** at the closest approach to the anchor after the car has
  been ≥ 120 m away, is traveling within ~75° of the launch heading, and has
  covered enough distance — a crossing normally finalizes when the car *exits*
  the crossing circle.
- The **run finish** is the `DistanceTraveled` hard-reset (~18 000 → 0) while
  the clock keeps counting; the post-finish coast "lap" is deleted.
- A stream that dies *inside* the crossing circle (cut at the final line) has
  the pending closest approach finalized at session end, flagged `cutoff` 🏁.
- A single-frame position jump > 250 m **disarms** geometric detection — you
  never teleport mid-run, so it's a free-roam fast-travel giveaway.

## Dirty laps: ⏪ rewind and 💥 contact

The packet has no lap-invalidated field, so LapScope infers dirtiness:

- **⏪ Rewind** — the lap clock drops below its high-water mark while distance
  doesn't grow. (Comparing frame-to-frame instead of against the high-water
  mark misses gradual scrubbing — that bug shipped once.) Rewound-over
  stretches are trimmed from the charts and the map, so only the
  finally-driven line remains.
- **💥 Contact** — a ground-plane acceleration spike ≥ 45 m/s², beyond
  anything tires can generate.

**Jump landings are excused.** Horizon being Horizon, hard landings spike the
accelerometer exactly like a wall. A spike while **airborne** — all four
wheels at full droop (`NormalizedSuspensionTravel` < 0.15) with zero tire
force (`TireCombinedSlip` < 0.05) for ≥ 0.12 s — or within 0.35 s of touchdown
is classified as a **landing**, not contact: amber on the analysis map instead
of red, excluded from the Contacts count, and the airborne stretch is drawn as
an explicit takeoff ○ → flight → touchdown ▸ glyph on both maps. The
thresholds were calibrated on a real cross-country session where 5 of its 12
spikes were landings (touchdown compresses the suspension a frame or two
*before* the spike, and suspension drifts up to ~0.11 mid-flight — hence the
loose thresholds and the grace window), then verified to change nothing on
circuit sessions.

![Jump glyphs on the 3D analysis map](https://raw.githubusercontent.com/darcane/LapScope/main/docs/media/track-map-3d-jumps.png)

**Known gap:** in Rivals, the faintest wall touch invalidates the lap in-game,
but its lateral force is far below the contact threshold — and there is no
packet field to cross-check against. Light scrapes are therefore missed (and a
wall hit inside the 0.35 s post-landing grace is excused). Accepted trade-offs,
tracked in [issue #27](https://github.com/darcane/LapScope/issues/27).

Flags reset when a lap re-anchors (a WTA launch, a mid-session lap-timer
start), so pre-launch junk frames never dirty lap 1.

## Routes

The game never sends route names, so circuits are **fingerprinted**: same
start position within 80 m + lap length within 5 % = same route. Name a route
once and every past and future session on it picks the name up.

## Track type: auto-suggested, always yours to override

The track-type tag (🛣️ road, 🏙️ street, ⛰️ touge, 🟫 dirt, 🏞️ cross-country,
🏁 drag, ⏱️ WTC) is pre-filled at session close when the evidence is clear —
it stays a manual tag with a smart default, and your dropdown choice always
wins. The suggestion comes from, strongest first:

1. **The route's existing tag.** A route's surface doesn't change, so any tag
   already carried by another session on the same fingerprinted route is
   reused — including your own corrections. Changing a session's type also
   offers to retag every session on its route in one click.
2. **Geometric laps → WTC.** Laps found by loop closure back to the launch
   anchor (see above) only happen in World Time Attack.
3. **Surface evidence → dirt / cross-country / road.** Calibrated on real
   captures: dirt courses shake the suspension hard (10–16 % "rough" frames
   vs. under 3 % on any tarmac course) at a low jump rate; cross-country
   flies a crest every few hundred meters (≥ ~5 jumps/min); smooth suspension
   with no jumps is tarmac → road. Notably, **tire slip does not work** for
   this — it tracks driver aggression, not the ground (hard tarmac laps
   out-slip clean dirt runs).

Frames far off the course (`NormalizedDrivingLine` saturated at ±127) are
excluded from the surface evidence, so deliberately off-roading a tarmac
event can't fake a dirt tag. Street and touge read as tarmac (suggested
road — correct once, the route remembers); drag strips have almost no
cornering and are deliberately left untagged. When the evidence is thin or
sits between classes, no tag is suggested at all.

Reprocessing an old session back-fills its suggestion the same way — without
ever overwriting a tag you set yourself.

## Reprocess: inference fixes apply retroactively

Recordings are lossless raw packets, so the **Reprocess** button replays a
session's stored frames through a fresh tracker — every detection improvement
on this page can be applied to recordings made before it existed. (Refused
with a 409 while any recording is in progress, so a long replay can't freeze
live telemetry.)

## Known accepted trade-offs

Not bugs — revisit only with new signal data:

- A fresh-boot free-roam session starting at `DistanceTraveled` 0 that loops
  over its own start point *without* teleporting can produce one false
  geometric lap.
- A mid-run quit at speed is indistinguishable from a stream-cut
  point-to-point finish; such runs carry the `cutoff` 🏁 flag rather than
  being dropped.
