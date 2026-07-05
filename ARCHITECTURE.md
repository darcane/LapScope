# ARCHITECTURE.md — LapScope

Structural map of the repo: what lives where, how data flows, and the contracts
between the parts. Behavioral knowledge (FH6 packet quirks, event-detection
rules) lives in [AGENTS.md](AGENTS.md); usage in [README.md](README.md).

## Runtime topology

One Docker container: FastAPI + uvicorn, asyncio, SQLite. No external services,
no build step, fully offline.

```
FH6 ──UDP 9999──▶ listener.py ─▶ packet.py parse ─┬─▶ hub.py ─▶ /ws/live ─▶ dashboard.js
                                                  └─▶ laps.py SessionTracker ─▶ store.py (SQLite)
                                                          ▲                        │
                                          reprocess.py ───┘        routes.py /api ─┴─▶ analysis.js
```

## Backend files

| File | Responsibility |
|---|---|
| [app/main.py](app/main.py) | Wiring: lifespan creates Store/Hub/SessionTracker, binds the UDP endpoint, runs a 1 s watchdog that closes sessions on silence. `no-cache` middleware for non-`/api` paths (keep it — stale-JS bug shipped once). Serves `app/static/` at `/`. |
| [app/telemetry/packet.py](app/telemetry/packet.py) | 324-byte Data Out struct: `parse()`, `pack()` (simulator/tests), `empty_fields()`, `FIELDS` name/count table. Self-test via `python app/telemetry/packet.py`. |
| [app/telemetry/listener.py](app/telemetry/listener.py) | `asyncio.DatagramProtocol`: counts packets, warns once on wrong size (hex dump), parses, feeds tracker, publishes frame+extras to hub. Recorder exceptions never kill the stream. |
| [app/telemetry/hub.py](app/telemetry/hub.py) | Fan-out to WebSocket subscriber queues (drop-oldest on slow clients) + stream stats used by `/api/status`. |
| [app/recorder/laps.py](app/recorder/laps.py) | **The heart.** `SessionTracker`: session boundaries, lap segmentation, finish detection, geometric (WTA) laps, dirty-lap flags, wet detection, route fingerprint triggers, live delta, `race_mode`. All tunable thresholds are module constants at the top. |
| [app/recorder/store.py](app/recorder/store.py) | SQLite persistence: schema, `MIGRATIONS`, session/lap/route/car-name CRUD, monotonic session-id counter, `reader()` for threadpool access. |
| [app/recorder/reprocess.py](app/recorder/reprocess.py) | Replays a session's stored raw frames through a fresh `SessionTracker` via `_ReplayStore` (laps/routes written for real; session row and frames untouched; discard suppressed). |
| [app/api/routes.py](app/api/routes.py) | REST API (table below) + constants: `CAR_CLASSES`, `CONDITIONS`, `TRACK_TYPES`, `DRIVETRAINS`, `CHANNELS` (channel-name → frame extractor for lap data). Loads `app/car_ordinals.json` (community car-name list). |

## Concurrency model (breaking this corrupts data)

- Everything in `SessionTracker` and the single `Store.db` connection runs on
  the **asyncio event-loop thread** (UDP callbacks, watchdog, `async def`
  handlers). `reprocess` endpoint is `async def` **on purpose** for this reason.
- Plain `def` API handlers run in FastAPI's **threadpool** and must use
  `Store.reader()` (short-lived connection; WAL makes small writes there fine —
  renames/tags do this).
- Hub `publish()` never blocks: full subscriber queues drop their oldest frame.

## SQLite schema (`data/telemetry.db`, WAL)

- `sessions(id, started_at, ended_at, name, car_ordinal, car_class, car_pi,
  drivetrain_type, frame_count, conditions, route_id, track_type, kept)`
  — `id` comes from `Store._next_session_id` (monotonic, never reused; rowid
  reuse broke live-map reset). `kept=1` exempts from no-laps cleanup.
- `frames(id, session_id→sessions CASCADE, t, raw)` — raw 324-byte packets,
  lossless; ~70 MB per driving hour. Index on `(session_id, t)`.
- `laps(id, session_id→sessions CASCADE, lap_number, lap_time, started_t,
  ended_t, start_distance, flags)` — `flags` is CSV: `rewind`, `contact`,
  `cutoff`. `lap_time NULL` = incomplete.
- `routes(id, name, start_x, start_z, lap_length)` — fingerprint: start within
  80 m + length within 5 %.
- `car_names(ordinal, name)` — user overrides of the bundled ordinal list.

Schema changes: append `ALTER TABLE ... ADD COLUMN` to `store.MIGRATIONS`;
each runs on every startup inside try/except (existing-column errors swallowed).

## REST API (`/api`)

| Endpoint | Notes |
|---|---|
| `GET /status` | Packet counters, last-packet age/size, active session, session best. First stop when "nothing works". |
| `GET /sessions` | List with route/car-name joins, lap counts, best lap. |
| `PATCH /sessions/{id}` | `name`, `conditions` (`dry/wet/snow`, `""` clears), `track_type` (`road/street/touge/dirt/cross/drag/wtc`, `""` clears). |
| `POST /sessions/{id}/reprocess` | Rebuild laps from stored frames (async def — event-loop writes). 409 while recording. |
| `DELETE /sessions/{id}` | Cascades frames+laps. 409 while recording. |
| `GET /sessions/{id}/laps` | Session + laps with `is_best` / `gap_to_best`. |
| `GET /laps/{id}/data?channels=&max_points=` | Distance-indexed channel arrays; drops rewound-over samples; decimates to `max_points`. Channel names = `CHANNELS` keys in routes.py. |
| `PATCH /routes/{id}`, `GET/PATCH /cars/{ordinal}` | Rename route / car override. |

## WebSocket `/ws/live` frame

Every parsed packet field (snake_case per `packet.FIELDS`, wheel groups as
4-element lists ordered FL FR RL RR) **plus** tracker extras merged in by the
listener: `session_id`, `delta` (vs session-best), `session_best`,
`lap_elapsed` (fallback clock when `CurrentLap` is dead), `race_mode`, `_t`.

## Frontend (`app/static/`, vanilla JS, no build step — keep it that way)

| File | Responsibility |
|---|---|
| `index.html` + `js/dashboard.js` | Live page: WebSocket → `requestAnimationFrame` render loop; live track map state (`feedLiveMap`: resets on session-id change or >250 m jump); race-mode gating of timer/chip/map; no-data overlay polling `/api/status`. |
| `js/gauges.js` | Pure canvas renderers (RPM arc, friction circle, grip panel, input strip, live map); `initCanvas` handles DPR scaling. |
| `analysis.html` + `js/analysis.js` | Session browser, lap table, 2D/3D track map (color by speed/slip, drag-to-rotate 3D, chart-hover → map marker), A/B comparison charts (uPlot, x = DistanceTraveled track position). |
| `js/common.js` | Shared badges (class/PI, drivetrain, conditions, track type incl. `TRACK_META`) + themed modal dialogs (`uiPrompt`/`uiConfirm`/`uiAlert` — never use `window.prompt/confirm/alert`). |
| `css/style.css` | Theme = CSS custom props. `css/fonts.css` + `fonts/` = vendored Rajdhani (OFL); app must work fully offline. |
| `js/vendor/uplot.iife.min.js` | Only dependency, vendored, analysis page only. |

## Tools (repo root, stdlib only, no container needed)

- [tools/simulator.py](tools/simulator.py) — synthetic packet sender, runs in
  real time at 60 Hz. Flags: `--host --port --rate --freeroam S --events N
  --duration S --wet --dirty --race LAPS --sprint SECS --cut --dirt SECS
  --wta LAPS --jumps`. Stadium loop for circuits, open winding course for
  sprints (a looping sprint would falsely trip geometric lap detection).
  `--dirt` models the verified real point-to-point race (CurrentLap counts,
  `DistanceTraveled`-reset finish after a results-cinematic stream gap);
  `--dirt … --cut` models the touge variant (stream cut dead at the line).
- [tools/inspect_session.py](tools/inspect_session.py) — dumps every
  segmentation-relevant signal transition of a stored session straight from
  the DB (`--list` to enumerate). The capture-diagnosis workflow is in the
  README ("an event type isn't being recorded").

## Tests (`tests/`, pytest — no container, no game)

| File | Responsibility |
|---|---|
| [tests/harness.py](tests/harness.py) | `FakeSocket` parses each packet the simulator "sends" and feeds it straight into a real `SessionTracker` + temp-file `Store`; the simulator's clock is stubbed (`_FastClock`) so a 3-minute scenario runs in milliseconds. `run(scenario, tmp_path, …)` plays a scenario and returns the closed store; `sessions()` / `completed_laps()` / `flags_of()` are assertion helpers. |
| [tests/test_packet.py](tests/test_packet.py) | Packet invariants: `_STRUCT.size == PACKET_SIZE`, `FIELDS`↔`_STRUCT` value-count lockstep, scalar + wheel-array round trip. |
| [tests/test_scenarios.py](tests/test_scenarios.py) | The AGENTS.md event-detection matrix as headless assertions (free-roam discard, dirty flags, race finish, sprint/dirt/touge point-to-point, WTA geometric laps, jumps). |
| [conftest.py](conftest.py), [pyproject.toml](pyproject.toml) | Put the repo root + `tests/` on `sys.path`; `pytest` testpaths and `ruff` lint config (defaults: pyflakes F + E4/E7/E9, line length 100). |

Run `pytest -q` and `ruff check .` from the repo root (tooling in
[requirements-dev.txt](requirements-dev.txt)); CI ([.github/workflows/ci.yml](.github/workflows/ci.yml))
runs both plus the `packet.py` self-test on every push and PR. The harness reuses
the **simulator's** scenario code, so the frames under test are byte-identical to
the real UDP stream — keep new detection scenarios in `tools/simulator.py` and
assert them here.

## Configuration & deployment

- Env vars: `TELEMETRY_UDP_PORT` (9999), `DATA_DIR` (/app/data),
  `LS_KEEP_DISCARDED` (0; 1 keeps no-lap sessions — compose passes it through,
  a repo-root `.env` file works too).
- [Dockerfile](Dockerfile): python:3.12-slim, `COPY app ./app` — **code and
  static files are baked in**; changing anything under `app/` requires
  `docker compose build`. [docker-compose.yml](docker-compose.yml): ports
  8000/tcp + 9999/udp, `./data` bind mount.
- Dependencies: `fastapi`, `uvicorn[standard]` — that's all
  ([requirements.txt](requirements.txt)).
- [.claude/launch.json](.claude/launch.json): preview server runs
  `docker compose up` and owns the process.

### Windows exe (plug-and-play build for normal users)

- Entry point [run_desktop.py](run_desktop.py): defaults `DATA_DIR` to
  `%LOCALAPPDATA%\LapScope`, runs uvicorn on `127.0.0.1:8000`, and opens the
  browser. It imports the `app.main:app` object by reference (not the string
  form) so PyInstaller statically follows the whole `app` package.
- [LapScope.spec](LapScope.spec): PyInstaller **onedir** build. Bundles the full
  `app/static/` tree via `Tree(...)` (HTML/CSS/JS **plus** the binary
  `fonts/*.woff2`, `css/uplot.min.css`, `js/vendor/uplot.iife.min.js`) to
  `app/static` and `app/car_ordinals.json` to `app/`, matching the runtime paths
  in [app/main.py](app/main.py) and [app/api/routes.py](app/api/routes.py).
  `hiddenimports` cover uvicorn/websockets submodules that are imported lazily.
  Build locally: `pip install -r requirements.txt -r requirements-build.txt &&
  pyinstaller LapScope.spec` -> `dist/LapScope/LapScope.exe`.
- Exe icon: `assets/lapscope.ico` (committed; the brand track+lens mark).
  Regenerate from `assets/lapscope.svg` with
  [tools/make_icon.py](tools/make_icon.py) only when the artwork changes — the
  build/CI never rasterizes, they just consume the committed `.ico`.
- `app.__version__` ([app/\_\_init\_\_.py](app/__init__.py)) is `0.0.0` in source
  and stamped with the git tag at release-build time.
- [.github/workflows/release.yml](.github/workflows/release.yml): fires **only on
  `v*` tags** (separate from PR CI in [.github/workflows/ci.yml](.github/workflows/ci.yml)),
  builds on `windows-latest`, zips `dist/LapScope`, writes SHA256 `checksums.txt`,
  and publishes a GitHub Release with both attached.

## Cross-file invariants (change one → change all)

- Track-type set: `TRACK_TYPES` (api/routes.py) = `TRACK_META` (common.js)
  = `#track-select` options (analysis.html).
- Conditions set: `CONDITIONS` (api/routes.py) = `CONDITION_META` (common.js)
  = `#cond-select` options (analysis.html).
- Car classes / colors: `CAR_CLASSES` (api/routes.py) = `CLASS_LETTERS` +
  `CLASS_COLORS` (common.js).
- Teleport threshold 250 m: `WTA_TELEPORT_JUMP` (laps.py) = live-map jump reset
  (dashboard.js) = `POS_JUMP` (inspect_session.py).
- Contact spike threshold: `IMPACT_ACCEL` (laps.py) drives both the per-lap
  `contact` flag and the map collision markers — the `/laps/{id}/data`
  endpoint imports it; `dashboard.js` duplicates it as `IMPACT_ACCEL` for the
  live map (keep the two in lockstep).
- `RT_FREEZE_SECONDS` (laps.py) = the same constant in inspect_session.py.
- Packet layout: `_STRUCT` and `FIELDS` in packet.py must stay in lockstep
  (asserted by the module self-test).
