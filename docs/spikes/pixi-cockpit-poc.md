# Spike: PixiJS cockpit (POC)

Branch: `feat/pixi-cockpit-poc`. Status: **step 4 done** (see Progress at the
bottom).

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
