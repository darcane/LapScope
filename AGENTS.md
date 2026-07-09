# AGENTS.md — LapScope

Working knowledge for AI agents (and humans) touching this repo: workflow rules
and the hard-won behavioral facts that must not be re-derived or regressed.

## Documentation map — read in this order

1. **This file** — dev workflow, FH6 packet facts, the event-detection model,
   test matrix. The *why* behind the code.
2. **[ARCHITECTURE.md](ARCHITECTURE.md)** — what lives where: file
   responsibilities, data flow, DB schema, API surface, WebSocket contract,
   concurrency rules, simulator flags, and the cross-file invariants that must
   stay in sync. Consult it before touching anything structural.
3. **[README.md](README.md)** — user-facing: setup, in-game settings,
   troubleshooting flows (no packets, unrecognized event types).
4. **[TODO.md](TODO.md)** — open backlog and in-flight investigations.

Keep all four current: a detection change usually touches this file's model
section, a new endpoint/table belongs in ARCHITECTURE.md, a new user-visible
feature in the README, and finished TODO items get pruned.

## What this is

A single-container FastAPI app that receives Forza Horizon 6 "Data Out" UDP
telemetry, shows a live dashboard, and records sessions/laps into SQLite for lap
analysis. Vanilla-JS frontend, no build step. The recorder's decisions are
covered by a fast headless `pytest` harness (drives the simulator's scenarios
straight through `SessionTracker`, no game or container — see
[tests/](tests/)); running the simulator against the live container is still the
way to verify the frontend and the real UDP path.

```
FH6 ──UDP 9999──▶ listener.py ─▶ packet.py parse ─┬─▶ hub.py ─▶ /ws/live ─▶ dashboard.js
                                                  └─▶ laps.py (SessionTracker) ─▶ store.py (SQLite)
                                                        REST /api (routes.py) ─▶ analysis.js
```

## Dev workflow (important, non-obvious)

- **After each iteration of changes, create a local git commit.** **Never push
  to a remote** — the owner handles pushing themselves.
- **Work item → branch → PR.** Pick an item from [TODO.md](TODO.md) (once the
  repo is on GitHub, from issues instead), cut a `feat/…` or `fix/…` branch off
  `main`, commit there, and land it via a pull request with CI green — not by
  committing straight to `main`. `main` is the protected, release branch.
  Until the GitHub remote exists this is local-only (branch + commits, owner
  pushes/opens the PR).
- **Static files are baked into the image.** Any change under `app/` requires
  `docker compose build` + restart. There is no bind mount for code.
- The Claude Code preview config (`.claude/launch.json`) runs `docker compose up`
  and owns the process — stop the preview server AND `docker compose down` before
  rebuilding, then start the preview again.
- **Run the tests before opening a PR** — CI runs the same on every PR. From the
  repo root: `pytest -q` and `ruff check .` (install the tooling once with
  `pip install -r requirements-dev.txt`). The recorder scenarios in
  `tests/test_scenarios.py` are the matrix at the bottom of this file, driven
  headlessly through `SessionTracker` in ~2 s by a fake-socket harness
  (`tests/harness.py`) — no container, no real-time wait.
- Verify with the simulator (no game needed), from the repo root:
  `python tools/simulator.py [--wet] [--dirty] [--race N] [--sprint SECS] [--dirt SECS] [--jumps]`.
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
  flag, rival/opponent data. Game mode is *inferred* instead — see
  `SessionTracker.race_mode` below.
- **`IsRaceOn` is 1 in free roam too** — it only separates driving from menus.
  Events vs cruising: races grid you with `RacePosition > 0` from the very first
  countdown frame; WTA / point-to-point events reset `DistanceTraveled` to 0 at
  launch; free roam has neither (verified on real captures, 2026-07-02).
- **`Velocity*` is car-local** like `Acceleration*`: ~`(0, 0, speed)` whatever
  the world direction — useless for heading. `Yaw` IS world-space: the car moves
  along `(sin yaw, cos yaw)` in world X/Z (verified against position deltas).
- **`DistanceTraveled` is NOT meters** on real circuits: it advances by the same
  fixed amount every lap of a given route (~2.4–2.5× the true driven length) —
  a track-position parameter. Perfect for aligning laps and fingerprinting
  routes; never display it as a length (integrate `Speed` for that). The
  simulator emits true meters, so this quirk only shows on real-game data.
- `DrivetrainType`: 0=FWD 1=RWD 2=AWD. `CarClass`: index into D,C,B,A,S1,S2,R,X
  (R is new in FH6: 901–998 PI; X is 999 only — verified on a real 998 car).
- Wheel arrays are ordered FL, FR, RL, RR. `TireTemp` is Fahrenheit.
- The game binds its own socket on ports **5200–5300** — never use them.
- Xbox-app (UWP) builds may block loopback; fallbacks documented in README.

## Event-detection model (laps.py is the heart)

The game gives no explicit session/event boundaries; `SessionTracker` infers them.
All the rules exist because some real behavior broke a naive version:

- Session = `IsRaceOn` stretch, ends after 15 s of race-off or silence.
- `CurrentRaceTime` warping back to ~0 splits sessions (event restarts,
  free-roam time-attacks).
- **Finishes don't increment `LapNumber`.** Five signals: `LastLap` changing
  while `LapNumber` is static; **the `DistanceTraveled` hard-reset** (the real
  point-to-point finish, below); the race clock freezing ≥1.5 s while `IsRaceOn`
  stays 1 (a fallback finish-cinematic signal); **the stream cutting dead at the
  finish line** (real circuit races, verified: the last packet lands within
  meters of the line — an open lap that covered ≥97% of the session's typical
  lap length is completed from the lap clock's last reading plus the remaining
  meters at the last speed); and the same cutoff on a *gridded point-to-point
  race* with no laps to compare distance against (gridded, launched from a
  `DistanceTraveled` reset, `LapNumber` never incremented, real distance
  covered, cut at speed → timed launch-to-line, flagged `cutoff` 🏁 because a
  lap-one quit / game-close looks identical). Abandoned events with none of
  these signatures are discarded.
- **The real point-to-point finish is a `DistanceTraveled` hard-reset**
  (verified on a dirt-sprint capture, 2026-07-03: a "2018 Subaru WRX STI ARX"
  gridded event). The car crosses at speed, `RacePosition` drops to 0, the
  stream **gaps ~12 s for the results cinematic** (race clock counting through
  it), then the game hands control back **parked at the line, brake held
  (~75 %), gear 1, `DistanceTraveled` reset to 0** (this is the "gear 1 / 75 %
  brake, then gear R" the driver sees). The odometer only ever *accumulates* on
  circuits (never resets per lap), so a reset to ~0 is an unambiguous finish; it
  fires whether or not the lap fields are alive — **a dirt sprint runs
  `CurrentLap` the whole way while `LapNumber`/`LastLap`/`BestLap` stay 0**
  (so `_lap_fields_dead` is False and the geometric/WTA path never engages) —
  as long as `LapNumber` never incremented (not a circuit) and it's a real event
  (gridded, or a WTA geometric launch). Guards against a free-roam fast-travel
  (which also zeroes the odometer): the run must have covered real distance
  (`_prev_dist > WTA_MIN_LAP_DIST`) and the car must **stay put** across the
  reset (`< 250 m` — a fast-travel teleports you away). The run is completed
  right there and timed **launch-to-line**: `_finish_rt` is the *previous*
  frame's clock (the cinematic gap advanced the current one), minus `_launch_rt`
  (the clock when `DistanceTraveled` first grew), so the countdown is excluded —
  matching the game's own convention (a circuit lap's `LastLap` = launch-to-line,
  not clock-start-to-line, verified on session 22). Simulate with
  `python tools/simulator.py --dirt 40`.
- **Not every point-to-point does the handback — a touge cut Data Out dead at
  the line instead** (verified 2026-07-04: gridded 1v1, `CurrentLap` counting,
  crossing at 57 m/s with `RacePosition` 1 and `IsRaceOn` still 1, then the
  stream just stops — no reset, no freeze, no handback). That's the circuit
  cut-dead finish on a gridded point-to-point with the lap fields alive, so it
  reaches session end with an open lap and no `_event_finished`. Recovered by
  `_ptp_run_time_at_cutoff`: gridded, `_launch_rt` set, `LapNumber` never
  incremented (circuits recover their final lap via `_final_lap_time_at_cutoff`
  instead), real distance covered, still at speed → run timed launch-to-line,
  flagged `cutoff`. Simulate with `python tools/simulator.py --dirt 40 --cut`.
  **Cross-country shares this exact signature** (verified 2026-07-05, session 55:
  gridded start P8→P3, `CurrentLap` counting the whole way with `LapNumber` at 0,
  stream cut dead at the line at 67 m/s) — recovered by the same gridded cut-dead
  path, timed launch-to-line = 144.747 s, `cutoff`. No separate handling; it was
  the last unconfirmed point-to-point type, so all known event types are covered.
- Point-to-point events may never start `CurrentLap`; lap traces and the live
  delta fall back to race-time-elapsed-since-lap-open.
- **World Time Attack broadcasts no lap fields at all** (real capture, 2026-07-02:
  LapNumber/CurrentLap/LastLap/BestLap all 0 for the whole event; the clock counts
  from event *load*, through a teleport + grid hold with `DistanceTraveled` pinned
  at 0). Laps are detected geometrically (`_wta_logic`): launch = distance starts
  growing → anchor + lap re-based there; a lap completes at the closest approach
  to the anchor after being ≥120 m away, traveling within ~75° of the launch
  heading, having covered ≥500 dist-units. The run finish is `DistanceTraveled`
  hard-resetting (~18000 → 0) while the clock keeps counting; the post-finish
  coast "lap" is deleted. A crossing normally finalizes when the car *exits*
  the crossing circle; a stream that dies inside it instead has the pending
  closest approach finalized at session end, flagged `cutoff` (simulate with
  `--wta 3 --cut`). A single-frame position jump >250 m (fast travel)
  disarms the geometric detection — you never teleport mid-run, so it's a
  free-roam giveaway. Remaining caveat: a fresh-boot free-roam session starting
  at `DistanceTraveled` 0 that loops back over its start point *without*
  teleporting can still produce a geometric lap — accepted trade-off.
- `SessionTracker.race_mode` (in the per-frame extras merged into every
  WebSocket frame, alongside `session_id`/`delta`/`lap_elapsed`): True while a
  timed event is running — `RacePosition > 0`, live lap fields, or the geometric
  launch anchor armed; False in free roam and once the event finishes (every
  finish signal drops it, including the LastLap-change finish). The
  dashboard gates the lap timer, the RACE MODE / FREE ROAM chip, and the live
  track map on it. Verified transitions on real captures: race = on from the
  first grid frame; WTA = off during event-load/grid-hold, on at launch, off at
  the finish-line distance collapse.
- `POST /api/sessions/{id}/reprocess` (UI: Reprocess button) replays stored
  frames through a fresh `SessionTracker` via `_ReplayStore` (laps/routes real,
  session row untouched, discard suppressed) — recovers laps recorded before a
  detection fix. Must stay `async def` (writes on the event-loop connection),
  which also means the replay blocks the loop — it 409s while **any** session
  is recording, or a long replay would freeze live telemetry mid-race.
- `sessions.kept = 1` exempts a session from the startup no-laps cleanup
  (LS_KEEP_DISCARDED captures and reprocessed sessions set it).
- Dirty-lap inference: rewind = lap clock below its high-water mark while
  distance doesn't grow (per-frame comparison misses gradual scrubs — that bug
  shipped once); contact = ground-plane |accel| ≥ 45 m/s². Stored as
  `laps.flags` ("rewind,contact"). **Known contact-flag limits (real
  cross-country, session 55):** hard jump-landings trip it (false positives —
  roughly half of that race's spikes were landings, not the AI bumps they looked
  like), and light Rivals wall-scrapes stay below the threshold (false negatives;
  there is no lap-invalidated packet field to cross-check against). Improving
  both is tracked in TODO.md ("Contact & lap-invalidation detection"). Flags
  reset when a lap re-anchors (the WTA launch, a mid-session lap-timer start):
  pre-launch junk frames must not dirty lap 1.
- Routes are fingerprinted (start pos within 80 m + length within 5%) and named
  once by the user; names apply to every session on the route.
- Sessions with zero completed laps/runs are discarded at close and again at
  startup (`cleanup_sessions`). Every discard logs a `diag:` signal summary
  (duration, race-time range, max LapNumber/CurrentLap, finish seen, gridded,
  launch anchor, last speed, distance); `LS_KEEP_DISCARDED=1` (compose env
  passthrough; a repo-root `.env` file works too) keeps such sessions instead —
  that's the capture path for event types the segmentation doesn't recognize
  yet. Inspect a kept capture with `python tools/inspect_session.py <id>`
  (`--list` to enumerate): it prints every signal transition the segmentation
  cares about, straight from `data/telemetry.db`, no container needed.
- Session ids are handed out by an in-memory monotonic counter
  (`Store._next_session_id`), never SQLite's rowid: discards delete the max
  rowid, which plain `INTEGER PRIMARY KEY` would reuse — and the live map
  resets on session-id *change*, so a reused id left stale points on screen.
- Track types are a manual tag; the allowed set lives in **three places that
  must stay in sync**: `TRACK_TYPES` (api/routes.py), `TRACK_META` (common.js),
  and the `#track-select` options (analysis.html).

When changing `_lap_logic`, walk every branch against: circuit race with finish,
Rivals (endless laps), free-roam cruise, free-roam time-attack, sprint, dirt
sprint (CurrentLap counts + distance-reset finish), World
Time Attack (no lap fields), rewind mid-lap, rewind across the start line,
server joining mid-lap, event restart.

## Frontend conventions

- Vanilla JS + canvas; uPlot (vendored) only on the analysis page. No frameworks,
  no bundler — keep it that way, it's the point of the repo.
- Theme lives in CSS custom props in `style.css`; display font is vendored
  Rajdhani (OFL) in `app/static/fonts` — the app must work fully offline.
- Shared UI helpers (badges: class/PI, drivetrain, conditions, track type) live in
 `common.js` and are used by both pages.
- User display preferences (units, map toggles) live in `settings.js`, stored
 **`localStorage`-only** under one `ls_settings` key — there is no backend for
 them and there should not be: the recorder stores raw packets and every
 conversion (`speedFromMps`, `tempFromF`, `distFromM`, …) is applied at display
 time, so units never touch stored data. Pages read via `getSettings()` and
 re-render through `onSettingsChange`. Migrated the pre-Settings `fc_mph` /
 `fc_mapmode` keys once on load.
- Server sends `Cache-Control: no-cache` for non-API paths (browsers cached stale
  JS once); keep that middleware.
- Canvas gauges are pure functions of passed state (`gauges.js`); DPR-scaled via
  `initCanvas`.
- The live track map resets on session-id change and on any single-frame
  position jump > 250 m (grid snap / event restart — a car can't move that far
  in 1/60 s; keeping the old points would wreck the bounds). The finished
  track intentionally stays on screen until the next event starts drawing.

## Testing scenarios that must keep passing

Each row is both a headless assertion in `tests/test_scenarios.py` (run through
the fake-socket harness — fast, no container) and a manual simulator command for
full-stack / visual checks. Keep the two in step: a new detection scenario needs
a row here, a test, and usually a simulator flag.

| Scenario | Command | Expected |
|---|---|---|
| Laps + wet + route | `--freeroam 20 --events 2 --wet` | free-roam discarded, 2 sessions, wet-tagged, same route |
| Dirty laps | `--duration 180 --dirty` | lap 2 `contact`, lap 3 `rewind`, charts deduped |
| Race finish | `--race 3 --duration 200` | 3 laps all timed (last via finish detection), no phantom open lap |
| Point-to-point | `--sprint 75` | session kept, single run ≈75 s, route assigned |
| Real dirt sprint | `--dirt 40` | single run ≈40 s (CurrentLap counts, `DistanceTraveled`-reset finish), route assigned, no phantom coast lap |
| Touge (cut at line) | `--dirt 40 --cut` | single run ≈40 s flagged `cutoff` (CurrentLap counts, stream cut dead at speed), route assigned |
| Sprint, stream cut at line | `--sprint 60 --cut` | session kept, single run ≈60 s flagged `cutoff`, route assigned |
| World Time Attack | `--wta 3` | launch + 3 geometric laps + distance-reset finish, no post-finish phantom lap |
| WTA, stream cut at the line | `--wta 3 --cut` | 3 geometric laps, last flagged `cutoff` (pending crossing finalized at session end), no phantom lap |
| Jumps in 3D | `--sprint 75 --jumps` (or any + `--jumps`) | 3D map scale sane, spikes capped |
| Race-mode gating | `--freeroam 35 --race 3` | chip FREE ROAM then RACE MODE; timer dashed in free roam; live map draws the race only |
