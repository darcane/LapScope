# Spike: PixiJS cockpit (POC)

Branch: `feat/pixi-cockpit-poc`. Status: **done — verdict: adopt Pixi** (see
Verdict at the bottom).

## Question the spike answers

Does a single-surface, WebGL-rendered cockpit (PixiJS) look and feel clearly
better than today's seven-panel Canvas 2D dashboard — at an acceptable cost in
bundle size, CPU, code complexity and accessibility — so that it is worth
replacing `index.html`?

The POC is a **new page beside the existing one**. `index.html`,
`dashboard.js` and `gauges.js` are not touched; the POC only reads the same
`/ws/live` feed.

## Scope

In:

- `app/static/cockpit.html` — reachable by URL only (`/cockpit.html`), no nav
  link yet.
- `app/static/js/cockpit.js` — the scene, the components, the WebSocket feed.
- `app/static/js/vendor/pixi.min.js` — vendored IIFE build (global `PIXI`),
  one exact pinned 8.x version, no CDN at runtime (the app must work offline).
- Reuses `common.js` + `settings.js` as-is: units (`speedFromMps`, …),
  accent (`accentDef`, `onSettingsChange`), `rafThrottle`.

Out (for this POC): the Analysis page, the raw-telemetry panel, the
write-error banner, the car chip, a light theme, touching the backend, tests
beyond `ruff`/`pytest` staying green.

## Layout

One Pixi `Application` filling the viewport, two sections:

```
┌──────────────────────── shift lights ───────────────────────────┐
│  LAP 2/3   P1          ▓▓▓▓▓▓░░ −0.214 ░░░░          BEST 1:32.4│  timing strip + delta bar
│                                                                  │
│  ┌friction┐      ╭──── RPM arc ────╮        ┌ tire car ──────┐   │
│  │ circle │     │      4           │        │ slip/temp/susp │   │
│  └────────┘     │   187 km/h       │        └────────────────┘   │
│                  ╰─ thr/brk arcs ──╯                             │
│                  steering ◀━━●━━▶                                │
├──────────────────────────────────────────────────────────────────┤
│  live map                     │  input trace (12 s)              │  section 2 (collapsible)
└──────────────────────────────────────────────────────────────────┘
```

Narrow / portrait: section 2 stacks below; the cluster scales as one unit
(laid out in a fixed design space, e.g. 1600×600, then scaled to fit), so the
proportions never break.

## Architecture inside cockpit.js

- **Feed** — a minimal copy of `dashboard.js`'s `connect()` (wss on https,
  backoff, drop frames while `document.hidden`). Copying is deliberate for
  the POC; if it graduates, the feed moves to a shared `live.js` used by both
  pages.
- **Components** — each a small object owning a `PIXI.Container`:
  `build()` (static parts, once), `layout(w, h)` (on resize), and
  `update(frame, dt)` (per tick). One Pixi ticker drives all of them; it is
  stopped while the tab is hidden.
- **Static vs dynamic split** — bezels, scale ticks and labels are drawn once
  and cached (`cacheAsTexture`); only needles, arcs, fills and numbers redraw
  per tick. This is the main perf lever over today's full-canvas redraws.
- **Numbers** — `BitmapText` from a Rajdhani bitmap font installed after the
  `FontFace` has loaded; plain `Text` re-rasterises on every change and would
  cost a texture upload per digit change at 60 Hz.
- **Glow** — core only: a blurred, additively blended copy of the lit layer
  (`BlurFilter` + `blendMode: "add"`). `pixi-filters` (GlowFilter etc.) is a
  stretch evaluation, not a dependency of the POC.
- **Theme** — accent from `accentDef()`, re-read on `onSettingsChange`;
  everything else as constants mirroring `style.css`'s tokens.

## Components (milestone order)

1. Shift-light bar (incl. limiter flash) + RPM arc with redline zone +
   gear + speed.
2. Throttle/brake as inner arcs of the tach; steering bar under it.
3. Timing strip: lap, position, current / last / best, race-mode chip;
   delta bar (centre-zero, green/red fill, numeric).
4. Friction circle with fading trail.
5. Tire car outline: four tires coloured by slip (`slipColor` rule), temp
   readout, suspension travel bar per corner, balance text.
6. Section 2: live map (port of `drawLiveMap`, incl. the 250 m jump reset and
   session-id reset) + input trace.

## Motion design

All motion is tied to real events and is short:

- Needles / arcs: light critically-damped smoothing, time constant ≤ 1 frame
  by default — this is telemetry, lag is a bug. A query param `?smooth=` to
  compare values during the spike.
- Gear change: number punches (scale 1.15 → 1, ~120 ms).
- Race-mode on/off (`f.race_mode`): timing strip and delta bar slide/fade in;
  free roam dims them and the map collapses.
- Delta crossing zero: one pulse of the delta bar.
- Rev limiter: shift lights flash (same cadence as today's CSS `blink`).
- Stale stream (> 2.5 s): whole cluster desaturates, "PAUSED" chip.
- Stretch: contact flash at the screen edge (needs `feedCollision`'s
  detection copied — only if time allows).

**Reduced motion** (`prefers-reduced-motion: reduce`): no tweens or pulses,
state changes snap, limiter holds lit instead of flashing — the same policy
as `style.css`'s block.

## Accessibility & house rules

- The Pixi canvas gets `role="img"` and an `aria-label`, refreshed at ~1 Hz
  (not per frame) with speed / gear / rpm / lap state, like
  `describeCanvases()` today.
- Resize via `rafThrottle`; Pixi `resizeTo` + `autoDensity`, resolution from
  `devicePixelRatio` re-read on resize (monitor / zoom change, issue #74).
- No outbound requests: Pixi and the font are local files.
- If WebGL/WebGPU is unavailable, show a short message with a link back to
  `index.html` instead of a blank page.

## How we judge it (exit criteria)

| Criterion | Measure |
|---|---|
| Looks better | side-by-side screenshots at 1920×1080 and a phone width, same frame |
| Smooth | steady 60 fps during `--race 3`; frame time from the ticker |
| Cheap enough | main-thread CPU vs `index.html` on the same simulator run (Chrome performance panel) |
| Size | vendored Pixi bytes + `cockpit.js` lines vs `gauges.js` + `dashboard.js` |
| Works everywhere it must | Chrome/Edge/Firefox, the desktop exe's browser launch, offline |
| Maintainable | can a component be read and changed without knowing Pixi internals? |

Outcome is one of: adopt (plan the replacement of `index.html`, then the
Analysis replay on the same stack), adopt Canvas 2D instead (take the layout
and motion ideas, drop Pixi), or drop.

## Verifying

- Run from source, not Docker: uvicorn on port 8010 with `DATA_DIR` pointed
  at a scratch dir and `TELEMETRY_UDP_PORT=9998`, so the real container and
  database are untouched.
- Drive it with `python tools/simulator.py --race 3 --port 9998` runs
  (`--freeroam 35 --race 3` for the race-mode transition, `--jumps` for
  friction/suspension extremes).
- Screenshots via headless Playwright Chromium (the app's browser pane runs
  hidden, so rAF — and therefore the Pixi ticker — never fires there).
  Headless WebGL falls back to SwiftShader: fine for looks, not for perf
  numbers — take those in a real browser.

## Steps (one local commit each, never pushed)

1. Vendor Pixi (exact 8.x version, IIFE build) + empty `cockpit.html`
   rendering a test shape — proves loading, offline, resize, DPR.
2. Feed + component skeleton + milestone 1 (tach, gear, speed, shift lights).
3. Milestones 2–3 (pedals, steering, timing, delta) + race-mode transition.
4. Milestones 4–5 (friction circle, tire car).
5. Milestone 6 (map + trace), reduced motion, aria-label, WebGL fallback.
6. Measure against the exit criteria; write the verdict at the bottom of
   this file.

## Open questions

- Exact Pixi version to pin (latest stable 8.x at vendor time).
- Is section 2 part of the cockpit at all, or does the cockpit stay one
  section with the map/trace as an optional overlay?
- Does the cockpit need the raw-telemetry panel and write-error banner before
  it could replace `index.html`, or do they stay HTML overlays on top of the
  canvas? (Probably the latter.)

## Progress

### Step 1 — vendor + bare page (done)

- Pinned **pixi.js 8.22.0** (released 2026-10-01, MIT). `dist/pixi.min.js`
  copied byte-for-byte from the npm tarball after checking its sha512 against
  the registry's `integrity`. **840 KB** minified (uPlot is 50 KB) — the
  heaviest asset in the app by far; part of the size verdict.
- Offline: the build mentions jsDelivr only for the KTX/Basis
  compressed-texture transcoders, which load solely if such a texture is
  requested (never, here). `hello: false` drops the console banner. A
  headless load made zero off-machine requests.
- `cockpit.html` + `cockpit.js`: fixed 1600×600 design space scaled to fit;
  own resize handler (`rafThrottle`, re-reads `devicePixelRatio` — Pixi's
  `resizeTo` does neither); Rajdhani `BitmapFont` built after the webfont
  loads; glow = shared `GraphicsContext` + `BlurFilter` + additive blend;
  WebGL-failure overlay linking to `index.html`; reduced motion holds the
  test sweep still.
- Verified in headless Chromium at 1600×900@1x, 1280×800@2x, 390×844@3x:
  backing store = CSS size × DPR, crisp text and strokes, no page scroll,
  no console errors. FPS there is SwiftShader (software GL) — not a perf
  number.
- Noted for later: on a portrait phone the 1600×600 frame leaves most of the
  screen empty — the stacked portrait layout is a real requirement, not a
  nicety.

### Step 2 — feed + tach, gear, speed, shift lights (done)

- Live feed copied from `dashboard.js` (wss on https, backoff, drop frames
  while hidden, connection chip incl. PAUSED). A stalled stream dims the
  whole scene to 45 % rather than freezing it at full brightness.
- Components are `{ root, update(f, dtMs) }` objects ticked from one
  `app.ticker` callback. Same thresholds as the classic dashboard: tach amber
  > 78 %, red > 90 %; LEDs 4 green / 3 amber / 3 red from 55 % rpm; limiter
  flash > 97 % at 70 ms (held lit under reduced motion).
- Before the first packet the scene shows an idle dial plus one
  "waiting for telemetry" line (setup steps stay on the Live page).
- `?smooth=<ms>` (default 16) sets the rpm smoothing time constant;
  `?debug` shows renderer / resolution / fps in screen space.
- **Finding — `cacheAsTexture` is not needed (or wanted).** Unchanged
  `Graphics` already stay on the GPU with no per-frame rebuild, and a cached
  texture would blur whenever the fit-to-window scale grows. The tach scale is
  rebuilt only when `engine_max_rpm` changes (a car change).
- **Finding — colour changes go through `tint`.** `BitmapText` is installed
  white; setting `style.fill` re-lays out the glyphs, `tint` is free.
- LED geometry is redrawn only when the lit set changes (a bitmask compare),
  not every tick.
- Verified headless against `simulator.py --port 9998 --race 2`: gear, rpm,
  speed and the 1 Hz aria-label track the stream; chip live → paused and the
  scene dims after the stream stops. Limiter, LED fill and gear punch checked
  with injected frames (the simulator never passes ~94 % rpm), both with and
  without reduced motion. No console errors.

### Step 3 — pedals, steering, timing, delta, race-mode transition (done)

- Scene layout settled for the rest of the POC: timing top-left, delta
  top-right, friction circle bottom-left and tire car bottom-right (step 4),
  the tach centred. Throttle/brake are brackets either side of the dial
  (brake fills up the left, throttle up the right), steering a short arc over
  it with a centre notch; power and boost flank the speed.
- Timing block: race-mode chip, lap / position, running lap, last, session
  best. Delta block: number + centre-zero bar (red right = slower, green
  left = faster, full scale ±2 s), one pulse when the sign flips.
- `race_mode` eases a shared 0→1 progress (320 ms, smoothstep): both blocks
  slide in and brighten in an event and step back (30 % alpha, offset) in
  free roam — dashed, not hidden. Reduced motion snaps it.
- aria-label now also carries throttle/brake and, in an event, lap, position,
  lap time and delta in words.
- **Gotcha — bitmap fonts only contain the glyphs you list.** The en dash in
  `–:--.---` rendered as garbage until it was added to `chars`; every
  non-ASCII character the scene prints must be in that list.
- **Bug caught in verification:** the race easing dropped back whenever
  progress already equalled its target (`>` vs `>=`), so the blocks
  flickered mid-race. A raw `/ws/live` capture showed the stream's
  `race_mode` never flickers — it was the easing, now clamped to the target.
- Verified headless: simulator `--freeroam 12 --race 2` (chip, lap clock,
  slide-in on the event start), injected frames for the delta bar both
  ways, the zero-crossing pulse and the free-roam fade. No console errors.

### Step 4 — friction circle + tire car (done)

- Friction circle bottom-left: ±1.6 g, rings at 0.5 / 1 / 1.5 g, 90-sample
  trail (fed in the socket handler while `is_race_on`, like the classic
  page) fading toward its oldest point, dot amber past 1.1 g and pinned to
  the rim on an off-scale spike; LAT / LON / TOTAL readouts beside it.
- Tire car bottom-right: tires coloured by combined slip with the classic
  `slipColor` hue rule (converted to a hex number — the scene tints with
  numbers, not CSS `hsl()`), glowing past 1.0; temperature outboard in the
  user's unit with the classic blue < 160 °F / orange > 230 °F bands; spring
  travel bar per corner (amber > 82 %, red > 95 %); balance read-out from the
  classic slip-angle rule, with the sliding axle lit.
- Slip fills are split across two contexts — up to 1.0 plain, past 1.0
  glowing — so the blur pass only ever covers the tires that earn it.
- Verified headless: injected frames covering every colour band, oversteer
  and understeer, and a 3 g spike; a live `--sprint 40 --jumps` run (slip
  to 1.22, suspension to 0.81, trail filling to 90). No console errors.
- Desktop now has the whole first section. Portrait phones still letterbox
  it into a thin strip — the stacked layout is part of step 5.

### Step 5 — map, trace, layouts, reduced motion, fallback (done)

- **Section 2:** live track map (left) and 12 s input trace (right) under
  the cluster. The map follows the classic path rules (race / time attack
  only unless Settings opts free roam in; new session unless the same race
  resumed; > 250 m jump resets; ≥ 3 m spacing; thinning past 4000 points).
  **Contact sparks and jump glyphs are not ported**: their detection mirrors
  `laps.py` and would become a third copy — it belongs in a shared
  `live.js`, the first refactor if the cockpit is adopted.
- **Finding — Pixi re-tessellates on every geometry change.** A 4000-point
  path rebuilt per new point would be the scene's biggest cost, so the path
  lives in world coordinates in frozen 256-point chunks (only the open one
  rebuilds) and the fit-to-box is the container's transform. Stroke width
  scales with that transform, so chunks are rebuilt only when the fit scale
  drifts > 15 %. The trace redraws only when a sample arrives, not per
  display frame. Canvas 2D needed none of this — a real complexity cost on
  the verdict's side of the ledger.
- **Layouts:** components draw in one design space and sit in slot
  containers that a layout offsets (so a slot move never fights a
  component's own slide animation). Four layouts — `wide` 1600×900,
  `tall` 800×1950 (portrait re-stack), and compact versions without
  section 2. Pick: a readable (scale ≥ 0.5) layout with the map first,
  wide vs tall by screen fill; compact only if none is; else largest text.
  Verified: 1600×960 / 1280×800@2 / 1600×640 → wide, 820×1180 tablet →
  tall, 390×844 phone → tall-compact; live resize switches layout, no page
  scroll anywhere. Hidden slots are skipped in the tick.
- **Finding — Pixi 8.22 falls back to Canvas2D on its own.** With 3D APIs
  disabled the whole scene still renders (renderer `canvas`), minus the blur
  glows (filters are skipped). The failure overlay is reworded for the case
  where nothing at all can start.
- Reduced motion re-checked across everything: race transition snaps, no
  gear punch, no delta pulse, limiter held lit.
- Before the first packet the tires stay dark shells instead of a
  misleading green "0.0".
- `?debug` moved to the bottom-left and now shows the JS update cost per
  frame (exponential average) and the active layout. Headless: 0.4–0.75 ms
  per frame with every component live — CPU/GPU numbers in a real browser
  are step 6.

### First real-game run (owner, after step 5)

Looked right in both wide and tall layouts on real FH6 data (120 fps, 0.38 ms
update). One fix: jump landings and wall hits (10 g+) drew the friction trail
far outside the gauge, across the timing block and the map — Pixi has no
canvas edge to clip it the way the classic 2D gauge does. Trail points now pin
to the 1.6 g rim like the dot already did, and a spike past full scale flashes
a red arc on the rim in the hit's direction (700 ms fade; held then cut under
reduced motion) while the readouts turn red and still show the true size.

### Step 6 — measurements

Bench: Edge 156 (Chromium), headed, real GPU (RTX 5070 Ti, AMD Zen 5 CPU,
Windows 11), 1600×900 viewport at DPR 1, 120 Hz display. One simulator race
(`--race 8 --jumps`, 60 Hz packets) and the two pages alternated A B A B,
25 s windows, one page open at a time. Renderer metrics from CDP
`Performance.getMetrics`; CPU from the Windows process times of every process
of that Edge instance (script: scratchpad `bench.py`, not committed).

| per second of driving | classic `index.html` | cockpit | cockpit, render capped 60 Hz |
|---|---|---|---|
| frames presented | 122 / 122 | 122 / 122 | 60 (Pixi) |
| frames > 25 ms | 0 / 0 | 0 / 0 | 0 |
| main-thread task time | 146 / 146 ms | 184 / 193 ms | 131 ms |
| — of it script | 58 / 58 ms | 166 / 174 ms | 113 ms |
| — of it layout + style | 39 / 37 ms | 1.7 / 1.7 ms | 2.0 ms |
| renderer process CPU | 24 / 22 % of a core | 26 / 21 % | 19 % |
| GPU process CPU | 35 / 33 % | 47 / 32 % | 14 % |
| JS heap | 2–3 MB | 20–33 MB | 20 MB |

(two values = the two rounds.) Owner's real-game run, same machine: 120 fps,
0.38 ms JS update per frame, both wide and tall layouts.

- **Smooth:** both pages hold the display's full 120 Hz with no long frames.
  Pass.
- **Cheap enough:** the cockpit trades the classic page's DOM work (layout +
  style, ~38 ms/s from rewriting text nodes every frame) for Pixi's JS
  tessellation and draw submission (~3× the script time). Uncapped it costs
  ~25 % more main-thread time and about the same process CPU — noise between
  rounds is as large as the difference. Both pages redraw on *every* display
  frame, though, so on a 120 Hz screen each one draws every 60 Hz packet
  twice. Capping Pixi to 60 Hz (`app.ticker.maxFPS`, one probe, not
  committed) drops the cockpit **below** the classic page on every row: 10 %
  less main-thread time, less than half the GPU-process CPU. The right
  version of that is packet-driven rendering (draw when a frame arrived or an
  animation is running), not a fixed 60 — the game sends Data Out per
  *rendered* frame, so a player running FH6 at 120 fps gets 120 Hz packets.
  Pass, with that as the first optimisation.
- **Size:** `pixi.min.js` 841 KB (237 KB gzipped) — served from localhost or
  read from disk by the exe, so it costs ~nothing at load; the heap is
  ~10× the classic page's, which is irrelevant at 20–33 MB. Code: 993 lines
  (`cockpit.js`) vs 907 (`dashboard.js` + `gauges.js`) for more on screen
  (shift lights, pedal arcs, delta bar, layouts, race transition) — but the
  cockpit lacks the car chip, raw-telemetry panel, write-error banner and
  contact sparks the classic page has. Expect ~1 200 lines at parity.
  Acceptable.
- **Works everywhere it must:**
  - Edge (Chromium) headed on real GPU: WebGL, no errors. Chrome is the same
    engine; not installed here, not separately run.
  - Firefox: **not verified.** Headless Firefox (no Playwright build
    installed) failed to create its software GL compositor and screenshotted
    the page before Pixi drew — inconclusive, not a failure. Needs one manual
    look in a real Firefox before `index.html` is replaced.
  - No WebGL: Pixi's own Canvas2D fallback renders everything but the glows
    (step 5).
  - Offline: no runtime request leaves the machine — Pixi's only URLs are
    for KTX/Basis texture transcoders the cockpit never loads (step 1).
  - Desktop exe: `LapScope.spec` ships `Tree("app/static")` whole, so the
    page and Pixi are in the bundle with no spec change; the exe was not
    built for this spike (the release workflow's selftest would be the
    check).
- **Maintainable:** each component is one function, 40–130 lines, with a
  `root` container and an `update(f)` — readable without Pixi internals for
  ordinary changes (a colour, a threshold, a label). Three Pixi-specific
  idioms must be learned, all now documented above and in the code: redraw
  through a `GraphicsContext` (`clear()` then draw), change `BitmapText`
  colour with `tint` and only use glyphs installed in the font, and build
  glows with `glowing()`. The two places that needed real Pixi knowledge —
  the chunked map (re-tessellation cost) and pinning the friction trail (no
  canvas edge to clip) — are exactly where Canvas2D was simpler. Pass,
  with that cost named.
- **Looks better:** the owner's call after the real-game run ("all looks
  good"), with the screenshots in this doc's history.

## Verdict

**Adopt Pixi** for the live page. It holds full refresh with headroom, costs
about what the classic page costs (less, once rendering follows the packet
rate), works offline and without WebGL, and the one-surface cockpit with
eased motion is the thing Canvas2D-per-panel was not giving us. The extra
complexity is real but local (map chunks, glows), and it buys the same stack
for the Analysis ghost replay next.

Before the cockpit replaces `index.html`, in this order:

1. **Shared `live.js`** — feed, `liveMap`, trail/strip buffers and the
   contact/landing detection (`feedCollision`) out of `dashboard.js`, used by
   both pages. Then port contact sparks and jump glyphs to the cockpit map.
2. **Packet-driven rendering** — draw only when a frame arrived or an
   animation (race transition, gear punch, delta pulse, impact flash, limiter)
   is live; idle at a fixed low rate otherwise.
3. **Parity overlays** — car chip, write-error banner and the raw-telemetry
   panel as HTML over the canvas (not in Pixi).
4. **Firefox** — one manual check on real hardware.
5. **Swap** — cockpit becomes `index.html`; the classic dashboard stays one
   release behind a link, then goes with `gauges.js`.

Open questions answered: section 2 stays part of the cockpit (it carries the
map and trace in every layout that can read them); overlays stay HTML.

### Adoption 1 — shared `live.js` (done)

The feed both live pages read is one file now: `js/live.js` holds the
WebSocket (backoff, wss, hidden-tab frame drop), the connection chip, the
friction trail and input strip, and the track map's path plus the contact /
jump detection mirrored from `laps.py`. `dashboard.js` and `cockpit.js` only
draw from `live` / `liveMap`; the cockpit's copied feed is gone, so the
`laps.py` mirror is back to one copy. The cockpit map now draws contact
sparks and jump glyphs (hard landings glowing with the white ring), redrawn
only when `liveMap.marks` moves or the fit changes. Verified with
`--duration 180 --dirty --jumps` against both pages at once: identical path,
1 contact, 9 jumps (all hard, as the simulator lands them), no console errors;
ruff + 238 tests green (the stale-threshold test now reads `live.js`).
