# AGENTS.md — ForzaCalibrator

Working knowledge for AI agents (and humans) touching this repo. The README covers
*using* the app; this file covers *changing* it.

## What this is

A single-container FastAPI app that receives Forza Horizon 6 "Data Out" UDP
telemetry, shows a live dashboard, and records sessions/laps into SQLite for lap
analysis. Vanilla-JS frontend, no build step, no test framework — verification is
done by running the simulator against the running container.

```
FH6 ──UDP 9999──▶ listener.py ─▶ packet.py parse ─┬─▶ hub.py ─▶ /ws/live ─▶ dashboard.js
                                                  └─▶ laps.py (SessionTracker) ─▶ store.py (SQLite)
                                                        REST /api (routes.py) ─▶ analysis.js
```

## Dev workflow (important, non-obvious)

- **Static files are baked into the image.** Any change under `app/` requires
  `docker compose build` + restart. There is no bind mount for code.
- The Claude Code preview config (`.claude/launch.json`) runs `docker compose up`
  and owns the process — stop the preview server AND `docker compose down` before
  rebuilding, then start the preview again.
- Verify with the simulator (no game needed), from the repo root:
  `python tools/simulator.py [--wet] [--dirty] [--race N] [--sprint SECS] [--jumps]`.
  It runs in real time (60 Hz), so a 180 s scenario takes 3 minutes — run it in the
  background and watch `docker compose logs -f` for the recorder's decisions.
- Sessions close ~15 s after packets stop (`RACE_OFF_GRACE`); wait for the
  "Session N ended/discarded" log line before asserting on the API.
- DB lives in `./data/telemetry.db` (bind mount, gitignored). Schema changes go in
  `store.MIGRATIONS` as `ALTER TABLE ... ADD COLUMN` statements — they run on every
  startup inside try/except (existing-column errors are swallowed).
- SQLite threading rule: the single `Store.db` connection is event-loop-thread
  only. API handlers run in FastAPI's threadpool and must use `Store.reader()`
  (short-lived connection; fine for small writes too, thanks to WAL).

## FH6 packet facts (hard-won, don't re-derive)

- Fixed **324-byte** little-endian packet per rendered frame; FH5 "Dash" layout
  plus `CarGroup/SmashableVelDiff/SmashableMass` at offsets 232–243 and one
  undocumented trailing pad byte. Parser: `app/telemetry/packet.py` (verified by
  round-trip self-test and against the real game).
- **Not in the packet** (features must work around these): route/track names,
  car name strings, weather, game mode (Rivals/race/free-roam), lap-invalidated
  flag, rival/opponent data.
- **`DistanceTraveled` is NOT meters** on real circuits: it advances by the same
  fixed amount every lap of a given route (~2.4–2.5× the true driven length) —
  a track-position parameter. Perfect for aligning laps and fingerprinting
  routes; never display it as a length (integrate `Speed` for that). The
  simulator emits true meters, so this quirk only shows on real-game data.
- `DrivetrainType`: 0=FWD 1=RWD 2=AWD. `CarClass`: index into D,C,B,A,S1,S2,X.
- Wheel arrays are ordered FL, FR, RL, RR. `TireTemp` is Fahrenheit.
- The game binds its own socket on ports **5200–5300** — never use them.
- Xbox-app (UWP) builds may block loopback; fallbacks documented in README.

## Event-detection model (laps.py is the heart)

The game gives no explicit session/event boundaries; `SessionTracker` infers them.
All the rules exist because some real behavior broke a naive version:

- Session = `IsRaceOn` stretch, ends after 15 s of race-off or silence.
- `CurrentRaceTime` warping back to ~0 splits sessions (event restarts,
  free-roam time-attacks).
- **Finishes don't increment `LapNumber`.** Final race laps are caught by
  `LastLap` changing while `LapNumber` is static; point-to-point events
  (sprint/drag/street/touge) are caught by the race clock freezing ≥1.5 s while
  `IsRaceOn` stays 1 (finish cinematic), then kept as one run timed by the
  frozen clock. Abandoned events (no finish signal) are discarded.
- Point-to-point events may never start `CurrentLap`; lap traces and the live
  delta fall back to race-time-elapsed-since-lap-open.
- Dirty-lap inference: rewind = lap clock below its high-water mark while
  distance doesn't grow (per-frame comparison misses gradual scrubs — that bug
  shipped once); contact = ground-plane |accel| ≥ 45 m/s². Stored as
  `laps.flags` ("rewind,contact").
- Routes are fingerprinted (start pos within 80 m + length within 5%) and named
  once by the user; names apply to every session on the route.
- Sessions with zero completed laps/runs are discarded at close and again at
  startup (`cleanup_sessions`). Every discard logs a `diag:` signal summary
  (duration, race-time range, max LapNumber/CurrentLap, finish seen, distance);
  `FC_KEEP_DISCARDED=1` (compose env passthrough) keeps such sessions instead —
  that's the capture path for event types the segmentation doesn't recognize
  yet (World Time Attack is the known open case, reported 2026-07-02).
- Track types are a manual tag; the allowed set lives in **three places that
  must stay in sync**: `TRACK_TYPES` (api/routes.py), `TRACK_META` (common.js),
  and the `#track-select` options (analysis.html).

When changing `_lap_logic`, walk every branch against: circuit race with finish,
Rivals (endless laps), free-roam cruise, free-roam time-attack, sprint, rewind
mid-lap, rewind across the start line, server joining mid-lap, event restart.

## Frontend conventions

- Vanilla JS + canvas; uPlot (vendored) only on the analysis page. No frameworks,
  no bundler — keep it that way, it's the point of the repo.
- Theme lives in CSS custom props in `style.css`; display font is vendored
  Rajdhani (OFL) in `app/static/fonts` — the app must work fully offline.
- Shared UI helpers (badges: class/PI, drivetrain, conditions, track type) live in
  `common.js` and are used by both pages.
- Server sends `Cache-Control: no-cache` for non-API paths (browsers cached stale
  JS once); keep that middleware.
- Canvas gauges are pure functions of passed state (`gauges.js`); DPR-scaled via
  `initCanvas`.

## Testing scenarios that must keep passing

| Scenario | Command | Expected |
|---|---|---|
| Laps + wet + route | `--freeroam 20 --events 2 --wet` | free-roam discarded, 2 sessions, wet-tagged, same route |
| Dirty laps | `--duration 180 --dirty` | lap 2 `contact`, lap 3 `rewind`, charts deduped |
| Race finish | `--race 3 --duration 200` | 3 laps all timed (last via finish detection), no phantom open lap |
| Point-to-point | `--sprint 75` | session kept, single run ≈75 s, route assigned |
| Jumps in 3D | `--sprint 75 --jumps` (or any + `--jumps`) | 3D map scale sane, spikes capped |
