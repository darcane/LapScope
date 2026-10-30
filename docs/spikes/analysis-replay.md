# Spike: Analysis track map on PixiJS, with ghost lap replay

Branch: `feat/analysis-replay`. Issue:
[#99](https://github.com/darcane/LapScope/issues/99). Status: **in progress** — step 2
(shared kit) done.

Follows the cockpit spike ([`pixi-cockpit-poc.md`](pixi-cockpit-poc.md)),
whose verdict named this as the next user of the same stack.

## Question the spike answers

Can the Analysis track map move to PixiJS and gain an **animated replay of
the compared laps** (one ghost marker per pick, in sync, with a live gap
readout) — while every current map feature keeps working, an idle page draws
nothing, and a 6-lap compare on a long route plays back at the display rate?

Outcome is one of: adopt (Pixi map becomes the default, the Canvas 2D map
stays as the fallback), replay-on-canvas (keep Canvas 2D and add the replay
there, drop Pixi for this page), or drop.

## What exists today

- `drawMap()` in `analysis.js` (~360 lines, Canvas 2D) draws every pick in
  2D or 3D: fit-to-box, wheel zoom + pan (`mapView`), drag-to-rotate in 3D
  (`map3d`), speed/slip gradient for a lone lap, solid `PICK_COLORS` at ≥2
  picks, the chart-zoom halo + span endpoints, the start dot + direction
  arrow, jump glyphs (`drawJump`, common.js) and contact sparks, and the
  side stats (`#map-side`).
- It caches its finished frame (`mapCursor.snap`) plus the projection
  (`mapCursor.proj`), so a chart hover only blits the cache and draws the
  hover dot / pin ring per pick (`drawMapMarker`). Right-click hit-testing
  for contact dismissal runs on `hitMarkers`, refilled per draw.
- `exportMapPng()` composites `mapCursor.snap` with a Canvas 2D caption bar.
- `/laps/{id}/data` returns ≤ 2000 points per lap (`max_points`), `t`
  relative to the lap start, `dist` (the charts' shared x-axis) and
  positions, all index-aligned. A 6-lap compare is ~12 000 points.

## Scope

In:

- `js/pixi-kit.js` — the shared Pixi helpers, loaded by `index.html` and
  `analysis.html`, with the cockpit refactored onto it.
- `js/analysis-map.js` — the Pixi map + replay, mounted in the `#trackmap`
  slot. `analysis.js` keeps state, data loading, charts and the canvas
  fallback; the map module reads `state.picks` and the same `mapView` /
  `map3d` / `mapCursor` objects.
- Replay controls under the map (HTML, not Pixi).
- Docs: ARCHITECTURE.md, AGENTS.md frontend conventions, the wiki's
  Feature-Tour "Track map" section.

Out:

- The charts — they stay uPlot.
- The classic live page (`gauges.js` keeps its own `slipColor`; #101
  decides its fate).
- Backend changes — no new endpoint unless a step below proves one is needed.
- New README/wiki media — recaptured in
  [#100](https://github.com/darcane/LapScope/issues/100).

## Decisions (to confirm or overturn while building)

### 1. Pixi replaces the whole map canvas, not a layer over it

A replay-only layer over today's canvas would need the same projection
anyway, plus a second canvas with its own size/DPR handling and pointer
events split between two surfaces (hover, drag, wheel, right-click). One
surface, two layers:

- **static layer** — lines, zoom halo, start dot + arrow, jump glyphs,
  sparks. Rebuilt only on data, view, colour-mode, settings or size change.
- **marker layer** — hover dot, pin ring, ghost markers. The only thing that
  changes while hovering or playing.

The Canvas 2D `drawMap()` stays as is, as the fallback (`?canvas`, or
`app.init()` throwing).

### 2. One projection, two cost models

The projection (fit, 3D yaw + axonometric tilt + elevation exaggeration,
the user's zoom/pan) moves out of `drawMap()` into one function both paths
use.

- **2D:** points are stored in world coordinates; fit, zoom and pan are the
  container transform. Lines are rebuilt only when the effective scale drifts
  > 15 % (stroke width scales with the transform) — the cockpit's `makeMap()`
  rule, in frozen chunks of `MAP_CHUNK` points.
- **3D:** not an affine map of the flat track — the elevation is added as a
  screen-space offset after rotation — so rotation **must** rebuild the
  geometry. While a rotate-drag is in progress, draw every Nth point; draw
  full detail on release. Step 3 measures whether a full rebuild of 12 000
  points is cheap enough to skip the decimation.

### 3. Save PNG: extract the static layer

`app.renderer.extract.canvas(staticLayer)` replaces `mapCursor.snap`, at the
same pixel size; the caption bar in `exportMapPng()` is unchanged. The
marker layer is never part of the image, so hover dots and ghosts can't leak
into an export.

### 4. Both alignments ship

The replay head is one number, `head`, in the active alignment's unit:

| | by distance | by time |
|---|---|---|
| `head` is | a track position on the reference's `dist` | lap-elapsed seconds |
| each pick's marker | `lowerBound(p.dist, head)` — today's hover-dot rule | interpolated on its own `t` |
| what shows the gap | the readout (Δ time) | the distance between the markers, plus the readout |
| reference index for the charts | `lowerBound(ref.dist, head)` | `lowerBound(ref.t, head)` |

The gap readout is the **Δ time at the reference's current track position**
in both modes, so it always equals the Δ chart under the cursor. By distance
is close to free (the hover marker, animated); by time is the "race" view.
Positions between samples are interpolated, so a 2000-point lap doesn't
stutter at slow playback speeds.

### 5. Linking to the charts

- Replay → charts: the reference index drives `setMapCursor` and the uPlot
  cursor (`setCursor` on one chart; `cursor.sync` moves the rest). A guard
  flag keeps that programmatic move from coming back through the
  `setCursor` hook as a new head.
- Charts → replay: hovering a chart **pauses** playback and moves the head
  there (scrubbing by hovering). Leaving the charts leaves the head where it
  is.
- A chart drag-zoom (`state.zoomRange`, track positions) becomes the
  playback range, converted to time in time mode; playback loops inside it.
  Double-click reset widens it again.
- The pin (`togglePin`) is unchanged: a hollow ring at the pinned spot, and
  the raw table falls back to it.

### 6. Drawing on demand

`autoStart: false`, no Pixi ticker — like the cockpit, but Analysis has no
packet stream, so there is no idle rate either:

- **idle:** nothing draws. A data, hover, zoom, pan, rotate, resize, colour
  mode or settings change calls `requestDraw()`, which schedules one rAF.
- **playing:** a rAF loop that advances `head` by real time × speed and
  stops itself on pause, at the end of the range (unless looping), or when
  the tab is hidden.

### 7. Controls and accessibility

- Under the map, in the existing `.legend` row's style: play/pause
  `<button type="button" aria-pressed>`, an `<input type="range">` scrubber
  (keyboard for free), a speed select (0.25× / 0.5× / 1× / 2× / 4×), and a
  By time / By distance segment (`aria-pressed` on each). Space toggles play
  while the map has focus.
- The gap readout as HTML text beside the controls, not in the scene.
- The canvas keeps `role="img"`; its `aria-label` names what is drawn, and
  while playing it is refreshed at ~1 Hz (lap letters, head position, gap),
  not per frame.
- Reduced motion: nothing moves until play is pressed; no easing, no trails;
  the scrubber works as usual. Anything that loops gets a line in
  `style.css`'s `prefers-reduced-motion` block.
- Map actions that aren't playback (Save PNG) stay where they are. No new
  header buttons.

## Shared kit (`js/pixi-kit.js`)

Moved out of `cockpit.js`, not copied:

- `C` — colour tokens mirroring `style.css` (the scene can't read `var()`).
- `installFonts()` + `bitmapText()` — the `chars` list must cover every
  non-ASCII glyph either page prints (ghost letters A–F are ASCII; `Δ` and
  `±` would need adding).
- `glowing()`, `smooth()`.
- `jumpGlyph()` / `sparkGlyph()` — the `drawJump` (common.js) ↔ `jumpGlyph`
  invariant now covers both Pixi pages.
- One colour rule for slip and speed. `slipColor` / `speedColor`
  (`analysis.js`, CSS `hsl()`) and `slipHex` (`cockpit.js`, a number) encode
  the same hue ramps twice: the kit holds the hue functions and an
  `hslHex()`, and both forms derive from them. `common.js` gets the CSS
  variants so the canvas fallback still has them without loading Pixi.

## Steps (one local commit each)

1. **This note.**
2. **Kit:** `js/pixi-kit.js`, the cockpit moved onto it, slip/speed colour
   rule unified. Exit: cockpit screenshots before/after identical at
   1600×900 and a phone width; `analysis.js` canvas map unchanged.
3. **Static scene behind `?pixi`:** `js/analysis-map.js`, the shared
   projection, 2D + 3D, speed/slip gradient and solid overlay colours,
   chunked world-space lines, sizing (`ResizeObserver` + window `resize`
   through `rafThrottle`, resize only on a real change), renderer-failure
   fallback to the canvas map. Measure the 3D rotate rebuild here.
4. **Parity:** wheel zoom + pan, rotate, zoom halo + span endpoints, start
   dot + arrow, jump glyphs + sparks (contact layer setting), right-click
   dismiss (hit-test on projected marker positions), hover dot on every pick
   + pin ring, Save PNG via extract, `aria-label`, accent / `PICK_COLORS`
   re-read on `onSettingsChange`.
5. **Replay:** controls, `head`, both alignments, gap readout, play loop,
   chart link (cursor both ways, zoom range, hover-pauses), reduced motion.
6. **Measure** against the exit criteria below; write the verdict here.
7. **Swap:** Pixi map becomes the default, `?canvas` keeps the old one
   reachable, a failed init falls back to it. Docs updated (see Scope).

## Exit criteria

| Criterion | Measure |
|---|---|
| Parity | every item in #99's "Nothing existing regresses" list, checked by hand in `?pixi` vs the canvas map on the same session, 2D and 3D, 1 lap and 6 laps |
| Idle draws nothing | rAF/draw counter (`?debug`) stays at 0 with the mouse still and the page idle |
| Playback is smooth | 6-lap compare on a long route, headed Edge on a real GPU: frames presented = display rate, no frames > 25 ms, CDP `Performance.getMetrics` + process CPU vs the canvas map's hover cost |
| 3D rotate | same bench, rotate-drag: frames presented and script time per frame, full detail vs decimated |
| Charts linked | scrub → chart cursor + raw table follow; chart hover → head moves, playback pauses; drag-zoom → playback stays inside the span |
| Fallback | `?canvas`, and a forced `app.init()` failure, both show the canvas map, never a blank box |
| Offline | no request leaves the machine (Pixi is already vendored, 8.22.0) |
| Tests | `pytest -q` and `ruff check .` green |

Headless SwiftShader (Playwright) is fine for looks and behaviour, never for
performance numbers. Verify from source: uvicorn with `DATA_DIR` on a
scratch copy of a real `telemetry.db` (a long route with ≥6 laps driven).

## Open questions

- Ghost markers in time mode on a lap that finished earlier: park at the
  line (dimmed) or vanish? Parking reads better as a "finish order".
- Does a trailing tail behind each ghost (last ~2 s) help reading, or just
  add clutter at 6 picks? Off under reduced motion either way.
- Gap readout per pick (A vs B, A vs C, …) or only the nearest rival? With
  6 picks a per-pick list is six lines — probably the list, in tray colours.
- Cross-route compares (`maybeWarnCrossRoute`): time mode still works,
  distance mode is as meaningless as the Δ chart there. Hide the toggle, or
  just leave the existing warning?

## Progress

### Step 2 — shared kit (done)

- `js/pixi-kit.js` holds `C`, `installFonts()` + `bitmapText()`,
  `glowing()`, `smooth()`, `hslHex()` / `slipHex()`, `jumpGlyph()` /
  `sparkGlyph()` (+ `JUMP_COL` / `SPARK_COL`), moved out of `cockpit.js`;
  `index.html` loads it after Pixi. `smooth()` now takes its time constant
  as an argument — `SMOOTH_MS` is the cockpit's `?smooth=` param and stays
  there.
- One slip/speed rule: `slipHue()` / `speedHue()` in `common.js` (so the
  canvas map keeps working without Pixi); `analysis.js`'s `slipColor` /
  `speedColor` wrap them in `hsl()`, the kit's `slipHex` in `hslHex()`.
  `slipHex` checked against the old implementation over slip −0.5…3 in
  0.001 steps: identical. `gauges.js` keeps its own copy (classic page, #101).
- Verified headless (Playwright Chromium) against a scratch server: the
  same 1 200 captured simulator frames (`--race 3 --jumps`) fed through a
  stubbed WebSocket into the cockpit on `main` and on this branch —
  1600×900@1 and 390×844@3 screenshots pixel-identical (no pixel differs by
  more than 8/255); the Analysis map in speed and slip colouring likewise
  identical. No console errors.

