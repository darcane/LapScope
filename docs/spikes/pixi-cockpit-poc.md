# Spike: PixiJS cockpit (POC)

Branch: `feat/pixi-cockpit-poc`. Status: **planning** — nothing built yet.

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
