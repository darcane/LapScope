# The FH6 "Data Out" packet

Forza Horizon 6 broadcasts one fixed **324-byte little-endian UDP packet per
rendered frame** while you're driving (nothing in menus, photo mode, or the
pause screen). The layout is FH4/FH5's 324-byte "Dash" format; FH6's official
documentation newly names the FH-specific block at offsets 232–243
(`CarGroup`, `SmashableVelDiff`, `SmashableMass`). The final byte (offset 323)
is undocumented padding, present since FH4.

- Official reference: [FH6 Data Out documentation](https://support.forza.net/hc/en-us/articles/51744149102611-Forza-Horizon-6-Data-Out-Documentation)
- LapScope's parser: [`app/telemetry/packet.py`](https://github.com/darcane/LapScope/blob/main/app/telemetry/packet.py)
  — the struct is annotated field by field and verified by a round-trip
  self-test (`python app/telemetry/packet.py`) and against the real game.

## Layout at a glance

| Offset block | Fields |
|---|---|
| 0–7 | `IsRaceOn` (i32), `TimestampMS` (u32) |
| 8–19 | `EngineMaxRpm`, `EngineIdleRpm`, `CurrentEngineRpm` |
| 20–43 | `AccelerationX/Y/Z`, `VelocityX/Y/Z` (both **car-local**, m/s²; X=right, Y=up, Z=forward) |
| 44–67 | `AngularVelocityX/Y/Z`; `Yaw`, `Pitch`, `Roll` (**world-space**) |
| 68–211 | Wheel arrays ×4: `NormalizedSuspensionTravel` (0 = full stretch, 1 = full compression), `TireSlipRatio`, `WheelRotationSpeed`, `WheelOnRumbleStrip`, `WheelInPuddleDepth`, `SurfaceRumble`, `TireSlipAngle`, `TireCombinedSlip` (>1 = past the grip limit), `SuspensionTravelMeters` |
| 212–231 | `CarOrdinal`, `CarClass`, `CarPerformanceIndex`, `DrivetrainType`, `NumCylinders` |
| 232–243 | `CarGroup`, `SmashableVelDiff`, `SmashableMass` (the FH-only block) |
| 244–267 | `PositionX/Y/Z` (world meters); `Speed` (m/s), `Power` (W), `Torque` (Nm) |
| 268–295 | `TireTemp` ×4 (**Fahrenheit**); `Boost` (psi), `Fuel`, `DistanceTraveled` |
| 296–311 | `BestLap`, `LastLap`, `CurrentLap`, `CurrentRaceTime` (seconds) |
| 312–322 | `LapNumber` (u16, 0-based), `RacePosition`, `Accel`, `Brake`, `Clutch`, `HandBrake` (0–255), `Gear` (0 = reverse), `Steer` (−127..127), `NormalizedDrivingLine`, `NormalizedAIBrakeDifference` |
| 323 | undocumented padding |

All wheel arrays are ordered **FL, FR, RL, RR**.

## What's NOT in the packet

Half of LapScope exists to work around what the game *doesn't* send:

| Missing | LapScope's workaround |
|---|---|
| Route / track names | Circuits are fingerprinted from lap geometry (start position + lap length); you name a route once and every session on it picks the name up. |
| Car name strings | `CarOrdinal` is looked up in a bundled community list (`app/car_ordinals.json`), with your own overrides on top. |
| Weather | Wet conditions are inferred from `WheelInPuddleDepth` over the session; snow is a manual tag. |
| Game mode (race / Rivals / free roam) | Inferred — see [Event Detection](Event-Detection). `IsRaceOn` does **not** mean "in an event" (below). |
| Event boundaries / finishes | Inferred from lap fields, the odometer, the race clock, and the stream itself — see [Event Detection](Event-Detection). |
| Lap-invalidated flag | Inferred dirty-lap flags: ⏪ rewind and 💥 contact — see [Event Detection](Event-Detection). |
| Rival / opponent data | Only your car is broadcast, so LapScope compares you against your own best lap and the grip limit. |

## Field quirks (verified on real captures)

These are the facts that shaped the app — each one broke a naive assumption:

- **`IsRaceOn` is 1 in free roam too.** It only separates *driving* from
  *menus*. Detecting an actual event takes other signals: races grid you with
  `RacePosition > 0` from the first countdown frame; World Time Attack and
  point-to-point events reset `DistanceTraveled` to 0 at launch; free roam has
  neither.
- **`DistanceTraveled` is NOT meters on real circuits.** It advances by the
  same fixed amount every lap of a given route — roughly 2.4–2.5× the true
  driven length — making it a *track-position parameter*, not an odometer in
  meters. That's perfect for aligning two laps by track position (how the A/B
  comparison charts work) and for fingerprinting routes, but useless as a
  length: the "Driven" figure on the analysis page is integrated from `Speed`
  instead.
- **`VelocityX/Y/Z` is car-local**, like `AccelerationX/Y/Z`: it reads
  ~`(0, 0, speed)` whatever direction you're going in the world, so it can't
  give a heading. **`Yaw` is world-space** — the car moves along
  `(sin yaw, cos yaw)` in world X/Z (verified against position deltas).
- **`TireTemp` is Fahrenheit**, whatever your in-game units.
- `DrivetrainType`: 0 = FWD, 1 = RWD, 2 = AWD.
- `CarClass` indexes into **D, C, B, A, S1, S2, R, X** — the **R class is new
  in FH6** (901–998 PI; X is 999 only, verified on a real 998 car).
- **The game binds its own socket on UDP ports 5200–5300** — pointing Data Out
  there sends telemetry to the game itself. Never use that range.
- **A title update can change the packet.** LapScope logs a warning with the
  received size and a hex dump instead of crashing — see
  [Troubleshooting](Troubleshooting#wrong-size-packets).

## Recording format

LapScope stores the raw 324-byte packets losslessly (~70 MB per driving hour),
so every improvement to the inference can be replayed onto old recordings with
the **Reprocess** button — nothing has to be re-driven.
