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
| [app/main.py](app/main.py) | Wiring: lifespan creates Store/Hub/SessionTracker, binds the UDP endpoint (a busy port is caught, logged, and surfaced via `app.state.udp_error` / `/api/status` rather than crash-exiting — the dashboard keeps serving), runs a 1 s watchdog that closes sessions on silence. `no-cache` middleware for non-`/api` paths (keep it — stale-JS bug shipped once). Serves `app/static/` at `/`. |
| [app/telemetry/packet.py](app/telemetry/packet.py) | 324-byte Data Out struct: `parse()`, `pack()` (simulator/tests), `empty_fields()`, `FIELDS` name/count table. Self-test via `python app/telemetry/packet.py`. |
| [app/telemetry/listener.py](app/telemetry/listener.py) | `asyncio.DatagramProtocol`: counts packets, warns once on wrong size (hex dump), parses, feeds tracker, publishes frame+extras to hub. Recorder exceptions never kill the stream. |
| [app/telemetry/hub.py](app/telemetry/hub.py) | Fan-out to WebSocket subscriber queues (drop-oldest on slow clients) + stream stats used by `/api/status`. |
| [app/recorder/laps.py](app/recorder/laps.py) | **The heart.** `SessionTracker`: session boundaries, lap segmentation, finish detection, geometric (WTA) laps, dirty-lap flags, wet detection, route fingerprint triggers, live delta, `race_mode`, and the track-type auto-suggestion (`suggest_track_type` + per-frame surface accumulators; written at session close with COALESCE so user tags win). All tunable thresholds are module constants at the top. |
| [app/recorder/store.py](app/recorder/store.py) | SQLite persistence: schema, `MIGRATIONS`, session/lap/route/car-name CRUD, manual-edit overrides (`edits` table, merged into `session_laps` at read time), monotonic session-id counter, `reader()` for threadpool access. |
| [app/recorder/reprocess.py](app/recorder/reprocess.py) | Replays a session's stored raw frames through a fresh `SessionTracker` via `_ReplayStore` (laps/routes written for real; session row and frames untouched; discard suppressed). |
| [app/api/routes.py](app/api/routes.py) | REST API (table below) + constants: `CAR_CLASSES`, `CONDITIONS`, `TRACK_TYPES`, `DRIVETRAINS`, `CHANNELS` (channel-name → frame extractor for lap data; a loop appends generated `raw_<field>` / `raw_<field>_<fl\|fr\|rl\|rr>` channels — every `packet.FIELDS` entry verbatim, packet-native units — for the analysis raw-data view). |
| [app/cars.py](app/cars.py) | Community car-name list (`CarOrdinal` → name), three layers, top wins: `car_names` DB override > downloaded `DATA_DIR/car_ordinals.json` > bundled `app/car_ordinals.json`. `refresh()` re-downloads the maintained copy from this repo's `main` (validated, atomically persisted, hot-swapped); triggered by the browser daily + Settings "Refresh now" via `POST /api/cars/refresh`. |

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
- `edits(id, session_id→sessions CASCADE, kind, anchor_t, value, created_at)`
  — manual session edits, applied at read time (raw frames and the recorder's
  lap rows are never rewritten). `kind`: `dismiss_contact` (anchor = the
  collision peak's frame time, matched ±0.5 s), `flags` (`value` = the full
  flags CSV, `""` = none) and `exclude_lap` (both anchored at the lap-span
  midpoint). Keyed by frame time so a reprocess — which recreates lap rows
  under recycled rowids — keeps them.

Schema changes: append `ALTER TABLE ... ADD COLUMN` to `store.MIGRATIONS`;
each runs on every startup inside try/except (existing-column errors swallowed).
New tables go straight into `SCHEMA` (`CREATE TABLE IF NOT EXISTS` is idempotent).

## REST API (`/api`)

| Endpoint | Notes |
|---|---|
| `GET /status` | Packet counters, last-packet age/size, active session, session best, `version` (`app.__version__`), and `udp_error` (non-null when the UDP port could not be bound). First stop when "nothing works". |
| `GET /version` | `{"version": app.__version__}` — the running build. The frontend compares it (client-side) against the latest GitHub Release for the update notice; `"0.0.0"` (dev/source run) suppresses the check. |
| `GET /sessions` | List with route/car-name joins, lap counts, best lap. |
| `PATCH /sessions/{id}` | `name` (`""` clears → display falls back to route/date), `conditions` (`dry/wet/snow`, `""` clears), `track_type` (`road/street/touge/dirt/cross/drag/wtc`, `""` clears). |
| `POST /sessions/{id}/reprocess` | Rebuild laps from stored frames (async def — event-loop writes). 409 while **any** session records: the synchronous replay would stall the event loop and freeze live telemetry. Manual edits survive (time-keyed — see the `edits` table). |
| `DELETE /sessions/{id}` | Cascades frames+laps. 409 while recording. |
| `GET /sessions/{id}/laps` | Session + laps with `is_best` / `gap_to_best` (excluded laps never score). Each lap carries effective `flags`, detected `flags_auto`, `excluded`; the session carries `edit_count` (drives the Reset-edits button). |
| `PATCH /laps/{id}` | Manual lap curation as read-time edits: `flags` (full CSV, `""` = none; a value equal to the detected flags removes the override), `excluded` (drop/restore from bests+counts). |
| `POST /laps/{id}/dismiss_contact` | "Not a contact": body `{t}` from the collision list. 404 if no real (non-landing) collision peak within ±0.5 s; re-dismissing an already-dismissed marker is an idempotent no-op. Lifts the lap's `contact` flag once no real (non-landing, non-dismissed) contact remains — only ever removes flags. |
| `DELETE /sessions/{id}/edits` | Reset edits: drops every manual edit of the session, back to pure detection. |
| `GET /laps/{id}/data?channels=&max_points=` | Distance-indexed channel arrays; drops rewound-over samples; decimates to `max_points`. Channel names = `CHANNELS` keys in routes.py. `lap_time` falls back to time-since-lap-start when the packet lap clock never ran (WTA / bare sprints keep `CurrentLap` at 0), so the Δ-time chart works for those events. Also returns `collisions` (contact-spike peaks, `landing: true/false`, `t` = the peak's frame time — the dismissal handle — and `dismissed: true` when a manual edit matched it) and `jumps` (airborne segments: takeoff → touchdown world coords + `dist0/dist1`, `air_s`, `hard` + peak `g` when the landing spiked) — both computed on the full-resolution trace, never decimated away. |
| `GET /laps/{id}/export.csv`, `GET /sessions/{id}/export.csv` | CSV download (streamed per lap, `Content-Disposition` filename built from the session's display name — sanitized ASCII, Windows-safe). Full resolution: every kept frame of the same rewind-trimmed trace `/data` serves, **no decimation**; canonical metric units with unit-suffixed headers (`speed_kmh`, `pos_x_m`, `boost_psi`, `lap_time_s` incl. the dead-lap-clock fallback). The session variant concatenates the **timed, non-excluded** laps (`lap` column) — exclusions honored like bests/counts, the untimed post-finish coast skipped because a re-import would mint a time for it; the per-lap variant exports either kind — asking for one lap is explicit. |
| `POST /import/csv?name=` | The reverse trip: raw body = a LapScope CSV export (text/csv — no multipart, no new dependency). Rebuilds a session: frames synthesized via `packet.pack()` (CSV channels real, rest neutral filler — suspension parked grounded so the airborne classifier can't fire, `NormalizedDrivingLine` saturated so the flat fake suspension is never read as surface evidence, and each lap group's final frame carries the group's lap time as `LastLap` so a reprocess re-times every lap through the LastLap-change finish instead of wiping them), lap groups become completed laps timed by the clock's high-water mark, laid end-to-end on fresh time/distance axes. No car/route metadata (`car_ordinal` NULL → "Unknown car", `car_known` true so the unknown-car affordances stay quiet). `async def` like reprocess (event-loop Store writes) incl. the 409-while-recording guard; 400s name the offending line, nothing written unless the whole file parses. |
| `PATCH /routes/{id}`, `GET/PATCH /cars/{ordinal}` | Route: `name` renames, `track_type` retags **every session on the route** at once (the analysis page offers this when a session's type is changed; `""` clears them all). Car override (`name: ""` reverts to the bundled/downloaded name). `GET` also returns `known` (ordinal resolvable without the `Car #<id>` fallback). |
| `GET /cars`, `POST /cars/refresh` | Car-list metadata (`total`, `fetched_at`) / re-download the community list (see app/cars.py row above; 502 with a readable `detail` on failure — the current list stays). Registered before `/cars/{ordinal}` so `refresh` isn't parsed as an ordinal. |

## WebSocket `/ws/live` frame

Every parsed packet field (snake_case per `packet.FIELDS`, wheel groups as
4-element lists ordered FL FR RL RR) **plus** tracker extras merged in by the
listener: `session_id`, `delta` (vs session-best), `session_best`,
`lap_elapsed` (fallback clock when `CurrentLap` is dead), `race_mode`, `_t`.

## Frontend (`app/static/`, vanilla JS, no build step — keep it that way)

| File | Responsibility |
|---|---|
| `index.html` + `js/dashboard.js` | Live page: WebSocket → `requestAnimationFrame` render loop; live track map state (`feedLiveMap`: resets on session-id change or >250 m jump — except a pause-split resume: same place + race clock kept its value keeps the path; `feedCollision` also tracks jump flights); race-mode gating of timer/chip/map; no-data overlay polling `/api/status`. Raw-telemetry panel (`#raw-panel`, hidden unless the `rawLive` setting is on): every WS-frame field verbatim in a value grid + FL/FR/RL/RR wheels table built once from `RAW_FIELDS`, per-frame updates rewrite only changed cells, ⏸ Hold freezes the display only. |
| `js/gauges.js` | Pure canvas renderers (RPM arc, friction circle, grip panel, input strip, live map incl. jump glyphs); `initCanvas` handles DPR scaling. |
| `analysis.html` + `js/analysis.js` | Session browser, lap table, 2D/3D track map (drag-to-rotate 3D, chart-hover → map marker on every picked trace, jump/contact layers, chart drag-zoom → highlighted span), multi-lap comparison (issue #30): `state.picks` is an ordered cross-session tray (cap 6, `PICK_COLORS` palette shared by table badges, tray chips, map traces and chart series; letters A–F), `picks[0]` = the reference lap — Δ-time and slip charts, zoom window, hover index, map extras and the PNG caption are based on it (★ on a chip promotes). A single pick keeps the speed/slip gradient coloring; ≥2 picks switch the map to solid per-lap colors and disable `#color-mode`. Charts are uPlot, x = the reference's DistanceTraveled track position (others interpolated onto it; drag-zoom syncs across all charts, double-click resets). Picks persist while browsing sessions (the lone best-lap auto-pick is replaced; any manual pick pins the tray; sidebar cards get a ＋ best-lap quick-add) and are dropped for a session that is reprocessed (recycled lap rowids) or deleted. Manual editing: right-click a contact spark → dismiss (works on any picked lap's markers), per-lap ✎ flags editor + 🗑/↩ exclude toggle in the lap table, Reset-edits header button (visible when `edit_count > 0`); `reloadSession()` refreshes after an edit without resetting the tray. Export: ⬇ per lap / Export CSV header button (plain navigations — the endpoints send `Content-Disposition`), Save PNG next to the map (`exportMapPng` composites the cached clean frame `mapCursor.snap` + a caption bar listing every picked lap over a solid `--bg` fill — the canvas itself is transparent). Import CSV above the session list (`bindImport`: file picker → raw `text/csv` POST → the rebuilt session is selected). Raw data at cursor (`#raw-section`, `rawAnalysis` setting): picks are fetched with the `raw_*` channels appended (`channelList()` — same request, so the arrays stay index-aligned with the chart cursor), one table row per raw channel × one column per pick; `setMapCursor` fills cells at the hovered position (reference by index, others via `lowerBound` on dist like the map dots); toggling the setting on refetches picks that lack raw channels (`syncRawSection`). |
| `js/common.js` | Shared badges (class/PI, drivetrain, conditions, track type incl. `TRACK_META`) + `RAW_FIELDS`/`RAW_WHEELS`/`fmtRaw` (the packet field list both raw views build from) + the `drawJump` canvas glyph both maps use + themed modal dialogs (`uiPrompt`/`uiConfirm`/`uiAlert` — never use `window.prompt/confirm/alert`) + the fail-soft client-side update check (`/api/version` vs the GitHub Releases API; dismissible `.update-banner`, 24 h cached, skipped on `0.0.0`) + the once-a-day car-list refresh trigger (`maybeRefreshCarList` → `POST /api/cars/refresh`) and `unknownCarIssueUrl` (pre-filled `unknown_car.yml` issue). |
| `js/settings.js` | User display preferences, `localStorage`-only (no backend — conversions are display-time, the recorder stores raw packets). One JSON key `ls_settings`; converters (`speedFromMps`/`speedFromKmh`/`speedUnit`, `tempFromF`/`tempUnit`/`fmtTireTemp`, `distFromM`/`distUnit`); `getSettings`/`saveSettings`/`onSettingsChange` pub-sub; `openSettings()` themed panel (reuses `common.js` modal chrome). Loaded after `common.js` on both pages; the ⚙ header button (`#settings-btn`) opens it. |
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
  `--dirt … --cut` models the touge variant (stream cut dead at the line);
  `--wta … --cut` dies inside the crossing circle at the final line (the
  pending geometric crossing is finalized at session end, flagged `cutoff`).
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
| [tests/test_tracker.py](tests/test_tracker.py) | Direct-drive tracker regressions the scenarios can't stage: flag hygiene across lap re-anchors, `race_mode` dropping at a LastLap-change finish, the listener's crash-fallback frame shape. |
| [tests/test_api.py](tests/test_api.py) | Endpoint functions run directly against a harness-produced store (stub request, no HTTP server): the `lap_time` channel fallback for dead lap clocks. |
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
- [.github/workflows/wiki.yml](.github/workflows/wiki.yml): mirrors
  [docs/wiki/](docs/wiki/) to the GitHub wiki on every merge to `main` that
  touches it (plus manual dispatch). `docs/wiki/` is the source of truth —
  direct wiki edits get overwritten by the next sync.

### Windows exe (plug-and-play build for normal users)

- Entry point [run_desktop.py](run_desktop.py): defaults `DATA_DIR` to
  `%LOCALAPPDATA%\LapScope`, runs uvicorn on `127.0.0.1:8000`, and opens the
  browser. It imports the `app.main:app` object by reference (not the string
  form) so PyInstaller statically follows the whole `app` package. A busy
  HTTP port is pre-checked with an actionable message + pause-before-exit
  (a second double-clicked exe must not crash-exit with an unreadable
  console flash — mirrors the UDP-port handling in `app/main.py`).
- [LapScope.spec](LapScope.spec): PyInstaller **onedir** build. Bundles the full
  `app/static/` tree via `Tree(...)` (HTML/CSS/JS **plus** the binary
  `fonts/*.woff2`, `css/uplot.min.css`, `js/vendor/uplot.iife.min.js`) to
  `app/static` and `app/car_ordinals.json` to `app/`, matching the runtime paths
  in [app/main.py](app/main.py) and [app/api/routes.py](app/api/routes.py).
  `hiddenimports` cover uvicorn/websockets submodules that are imported lazily.
  Build locally: `pip install -r requirements.txt -r requirements-build.txt &&
  pyinstaller LapScope.spec` -> `dist/LapScope/LapScope.exe`. For a
  release-faithful build (pinned Python + hash-locked deps) and download
  verification, see [docs/BUILDING.md](docs/BUILDING.md); CI installs from the
  hash-pinned [requirements-build.lock](requirements-build.lock) with
  `--require-hashes`.
- Brand artwork: `assets/logo-alone.png` (the speedometer + road mark) and
  `assets/logo-with-brand.png` (mark + wordmark, used on the README hero). Both
  are raster (gradients + glow), so they stay PNG rather than being traced to
  SVG. The derived exe icon `assets/lapscope.ico` and web
  `app/static/img/logo.png` (favicon + header mark) are committed directly — the
  mark is used as-is (centered on a transparent square, only downscaled; no
  crop/round/distortion). The build/CI never rasterizes, they just consume the
  committed files.
- `app.__version__` ([app/\_\_init\_\_.py](app/__init__.py)) is `0.0.0` in source
  and stamped with the git tag at release-build time.
- [.github/workflows/release.yml](.github/workflows/release.yml): fires **only on
  `v*` tags** (separate from PR CI in [.github/workflows/ci.yml](.github/workflows/ci.yml)),
  builds on `windows-latest`, optionally code-signs via SignPath Foundation
  (inert unless the `SIGNPATH_API_TOKEN` secret is set), zips `dist/LapScope`,
  writes SHA256 `checksums.txt` over the final (signed) zip, and publishes a
  GitHub Release with both attached (notes templated on whether it was signed).

## Cross-file invariants (change one → change all)

- Track-type set: `TRACK_TYPES` (api/routes.py) = `TRACK_META` (common.js)
  = `#track-select` options (analysis.html); everything `suggest_track_type`
  (laps.py) can return must be a member of `TRACK_TYPES` (locked by a test
  in test_api.py).
- Settings map options: the `defaultMapMode` / `defaultColor` values offered by
  the panel (`settings.js`) must match the `#map-mode` (`2d`/`3d`) and
  `#color-mode` (`speed`/`slip`) options in analysis.html; `analysis.js` seeds
  `state.mapMode`/`state.colorMode` from them and writes changes back via
  `saveSettings`. The `ls_settings` schema (`speed` kmh/mph, `temp` c/f, `dist`
  km/mi, `power` kw/hp/ps, `boost` psi/bar, `accent` — a key into `ACCENTS`,
  `freeroamMap`, `contactLayer`, `defaultMapMode`, `defaultColor`, `rawLive`,
  `rawAnalysis`) lives in `settings.js` and migrates the legacy
  `fc_mph` / `fc_mapmode` keys on first load.
- Accent theme: CSS derives every accent-tinted style from `--accent`
  (style.css), which `applyAccent()` (settings.js) sets from `ACCENTS`; canvas
  renderers can't use `var()` and re-read `accentDef()` on settings change
  (`refreshCanvasTheme` in gauges.js, `accentPickPalette` in analysis.js) —
  keep new accent-colored drawing code on one of those two paths.
- Conditions set: `CONDITIONS` (api/routes.py) = `CONDITION_META` (common.js)
  = `#cond-select` options (analysis.html).
- Car classes / colors: `CAR_CLASSES` (api/routes.py) = `CLASS_LETTERS` +
  `CLASS_COLORS` (common.js).
- Teleport threshold 250 m: `WTA_TELEPORT_JUMP` (laps.py) = live-map jump reset
  (dashboard.js) = `POS_JUMP` (inspect_session.py).
- Contact spike threshold + landing discrimination: `IMPACT_ACCEL`,
  `AIRBORNE_SUSP_MAX`, `AIRBORNE_SLIP_MAX`, `AIRBORNE_MIN_S` and
  `LANDING_GRACE_S` (laps.py) drive the per-lap `contact` flag and the map
  collision markers (spikes while airborne / just after touchdown are jump
  landings, not contact) — the `/laps/{id}/data` endpoint imports them and
  tags each collision `landing: true/false`; `dashboard.js` duplicates all
  five constants for the live map (keep the two in lockstep). The same
  airborne classifier also yields explicit jump segments (takeoff →
  touchdown): `/laps/{id}/data` returns them as `jumps`, `dashboard.js`
  tracks them live in `feedCollision`, and both maps render them through the
  shared `drawJump` glyph (common.js): dashed flight line, takeoff circle,
  touchdown arrowhead, glow + impact ring on hard landings.
- `RT_FREEZE_SECONDS` (laps.py) = the same constant in inspect_session.py.
- Packet layout: `_STRUCT` and `FIELDS` in packet.py must stay in lockstep
  (asserted by the module self-test).
- Raw field list: `RAW_FIELDS` (common.js) mirrors `FIELDS` (packet.py)
  name-for-name and in order (wheel groups FL FR RL RR); both raw views and the
  `raw_*` channel names the analysis page requests are built from it. The
  backend generates its `raw_*` channels from `FIELDS` directly (locked by a
  test in test_api.py), so only the JS copy can drift — update it with any
  packet change.
