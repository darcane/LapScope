# Spike: Analysis track map on PixiJS, with ghost lap replay

Branch: `feat/analysis-replay`. Issue:
[#99](https://github.com/darcane/LapScope/issues/99). Status: **adopted — the Pixi map with the ghost replay is the Analysis
map** (steps 1–7 done; see Verdict and Step 7).

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

### Step 3 — static scene behind `?pixi` (done)

- `drawMap()` split: what the map says outside its pixels (aria-label,
  cursor, colour select, legend, side stats) stays in `drawMap`; the
  projection is `mapProjection()` (`raw` pre-fit coordinates, then fit +
  zoom + pan), the gradient `mapGradient()` (values + hue + saturation from
  common.js's ramps — `slipColor` / `speedColor` are gone), the side stats
  `renderMapSide()`; the old pixels are `drawCanvasMap()`. The canvas path is
  **pixel-identical** to before in 2D/3D × speed/slip × 1 and 3 picks.
- `js/analysis-map.js`: lines in `raw` space in one `Graphics`, fit/zoom/pan
  as the container transform, rebuilt on a content change or > 15 % scale
  drift. A lone lap's gradient is one stroke per run of segments sharing a
  colour, hue quantised to 2°. Side by side with the canvas map the lines,
  3D shadows, elevation posts and gradients match; the start dot/arrow, zoom
  halo endpoints, glyphs and markers are step 4.
- **No chunking.** `MAP_CHUNK` exists in the cockpit because its path
  *grows* — only the open chunk is re-tessellated. Analysis data never
  appends, so any rebuild is all-or-nothing and chunks would buy nothing.
- **Finding — Pixi tessellates at render, not at `stroke()`.** Building the
  `Graphics` costs ~0.5 ms whatever the size; the real cost lands in the
  next `app.render()`. Any timing has to include the render.
- **3D rotate, measured** (headless Chromium, SwiftShader — CPU-side cost
  only, real-GPU numbers are step 6): one rotate step = `drawMap()` +
  rebuild + render, median of 60 —

  | | canvas map | Pixi map |
  |---|---|---|
  | 1 lap, gradient (1 248 pts) | 3.4 ms | 2.5 ms |
  | 5 laps, overlay (5 753 pts) | 5.1 ms | 2.3 ms |

  A full-detail rebuild is cheaper than the canvas redraw it replaces, so
  **no decimation while dragging** for now; step 6 re-checks at ~12 000
  points on a real GPU.
- Behaviour: an idle page renders nothing (0 renders in 2 s); a small wheel
  zoom moves the transform with no rebuild, zooming 1× → 7.4× rebuilt 8 times;
  switching sessions swaps the one Pixi canvas into the re-cloned pane (one
  `#trackmap`, listeners bound once, aria-label current); a forced
  `app.init()` failure leaves the canvas map drawing, with one console
  warning. Sizing: `ResizeObserver` on the map column (width changes only)
  plus the existing window `resize`; `renderer.resize` only on a real
  CSS-size or DPR change.
- Until step 4, `?pixi` has no hover/pin markers, no right-click dismissal
  (hit markers are cleared) and Save PNG says so instead of exporting.

### Step 4 — parity (done)

- Geometry the two renderers must agree on is computed once, in
  analysis.js, and painted by each: `mapStartArrow` (start dot + drive
  arrow), `mapGlyphs` (jump flights and contact sparks in draw order, ref
  full-strength / others dimmed, honouring the contact-layer setting, and
  filling `hitMarkers`), `mapMarkerDots` (hover dot on every pick + pin
  ring). The canvas map now paints from them too and stays pixel-identical.
- Pixi layers: lines (raw space) → overlay (screen space, redrawn each
  `draw()`: a few dozen shapes) → hover/pin dots (the only thing a chart
  hover touches). Glows via `glowing()` for the span ends, hard landings,
  sparks and dots — the canvas map's `shadowBlur` cases.
- Kit glyphs take options — `jumpGlyph(..., { color, scale })`,
  `sparkGlyph(..., { color, r })` — with the cockpit's values as defaults
  (cockpit screenshots unchanged); the Analysis map uses `scale: 0.8`,
  `drawJump`'s size.
- Save PNG: `renderer.extract.canvas` of the stage with the dots hidden,
  same file name and caption bar as the canvas export.
- **Finding — extract returns premultiplied pixels.** Composited over the
  export's fill they washed out every translucent stroke (the zoom halo, the
  faded gradient, 3D shadows). Extracting onto the export's own opaque
  background (`clearColor`) makes the PNG match the screen.
- **Finding — the canvas gradient's fade is brighter than its number.** It
  strokes 0.22 alpha per ~1 px segment with round caps; ~3 overlap at every
  pixel, so it reads at ~0.6 (measured from screenshots). Pixi strokes one
  path per colour run, so it uses 0.55 to look the same — and butt caps on
  faded runs, since overlapping round caps at each colour change beaded the
  line.
- Verified side by side (canvas vs `?pixi`, same session): hover + pin,
  chart zoom span (halo, ends, faded rest), Save PNG, 3D with all laps +
  hover, wheel zoom + pan, contact layer off — all matching; hit markers
  identical (screen positions, lap, time) and a right-click on a Pixi
  spark opens the dismiss dialog. No console errors.

### Step 5 — replay (done)

- `js/analysis-replay.js`: play/pause (`aria-pressed`), a range scrubber
  (0.1 s steps; arrows, Page Up/Down and Home/End for free), a speed select
  (0.25×–4×), By time / By distance (`aria-pressed`), the clock and a
  per-pick Δ readout in tray colours. Space toggles playback on the focused
  map (the Pixi canvas is `tabindex=0`). The row only shows on the Pixi map
  with a reference lap; the canvas fallback has no replay.
- Ghosts: a filled disc in the pick's colour with its letter (bitmap font,
  so the analysis page now installs it too), glowing, reference on top.
  They replace the hover dot while the replay is engaged (in time alignment
  the two would point at different laps' positions); the pin ring stays.
- **Finding — `t` is the wrong clock.** Lap A of the test session has a
  6 s rewind: `t` runs to 48.05 s against an official 41.57 s, so a
  `t`-timed replay drives the rewound stretch twice and every gap after it
  is 6 s off. The raw `lap_time` channel is no better as an axis: it drops
  back at a rewind and reads ~0 on the crossing frame (the Δ chart shows
  that as a spike in its last sample). `lapClock(d)` keeps the samples the
  game kept — a rewind discards the ones it took back — and continues the
  clock across the crossing reset by elapsed time. Its end equals the
  official lap time (41.566 vs 41.567); final gaps equal the lap-time
  differences (B +1.17, C +0.05, D +0.87).
- **Finding — the replay's cursor writes swallow a chart hover.** While
  playing, `setCursor` on every chart each frame overwrites the mouse
  position uPlot is about to apply, so the hover never reached
  `setMapCursor`. A `pointermove` listener on each chart's overlay pauses
  playback first; from then on uPlot's own hover scrubs the replay.
- Open questions settled: finished laps **park at their line, dimmed**
  (reads as the finish order); **no trails**; the readout lists **every
  pick** vs A; cross-route compares keep the existing warning (distance
  alignment is as meaningless there as the Δ chart).
- Verified headless (5 picks incl. one open lap): 1× advances 2.0 s in 2 s
  at ~59 renders/s, 4× advances 4.0 s/s; the chart cursor and the raw-table
  index follow A every frame; by distance stacks the ghosts on A; a chart
  hover pauses and scrubs (index matches); paused = 0 renders; Space
  toggles; a zoom span sets the scrubber bounds and playback loops inside
  it; the full lap stops at the slowest finish; a new reference resets the
  replay; the canvas map shows no replay row. No console errors.

### Step 6 — measurements

Bench: Edge (Chromium) headed on the maintainer's machine (real GPU, 120 Hz
display), 1600×900 at DPR 1, driven by Playwright over CDP; renderer metrics
from `Performance.getMetrics`, CPU from the Windows process times of that
Edge instance's renderer and GPU processes. Stress case: **Seaside Park
Sprint**, the longest route in the real database with ≥ 6 timed runs, six
runs across six sessions copied read-only into a scratch database —
1 327–1 488 points per run, **8 473 in all** (the ~12 000 the plan assumed
needs runs past ~4 000 frames, which this database has no six of). Canvas
map and `?pixi` alternated, two rounds, 8 s per window; scripts
(`bench_edge.py`, `idle_profile.py`) stay in the scratchpad, not committed.

| per second | canvas map | Pixi map |
|---|---|---|
| **idle** — main-thread task time | 0.6 / 0.6 ms | 0.6 / 0.6 ms |
| **chart hover sweep** (~32 moves/s) — task time | 74 / 72 ms | 87 / 86 ms |
| — of it script / layout + style | 8 / 18 ms | 21 / 16 ms |
| **3D rotate drag** (~32 steps/s) — task time | 96 / 95 ms | 121 / 125 ms |
| **replay playing**, 6 ghosts, chart cursors following | — | 158 / 152 ms |
| — renders | — | 120 / 118 (the display rate) |
| frames > 25 ms, any window | 0 | 0 |

(two values = the two rounds; process CPU is in `bench-final.json` but too
noisy between rounds — the GPU process swung 5 → 13 % idle on the *same*
page — to carry an argument.)

- **Idle draws nothing — after a fix.** The first run had the idle Pixi
  page at ~15 ms/s with zero renders. A CDP sampling profile showed Pixi's
  shared `Ticker.system` running a rAF loop at display rate for two
  listeners: the federated pointer events (unused: the map listens with DOM
  events) and the GC scheduler (unused: lines are destroyed explicitly).
  `autoStart: false` only stops the *app's* ticker. Stopping the system
  ticker brings idle to the canvas page's 0.6 ms/s, and the profile shows no
  Pixi frame at all. (The cockpit has the same loop, but it runs its own rAF
  loop anyway, so it changes nothing there.)
- **3D rotate — headless had it backwards.** SwiftShader timed a full Pixi
  rebuild *below* the canvas redraw (step 3); on the real GPU the first run
  measured 177 ms/s vs the canvas map's 109 — ~5.5 ms per step, which at a
  real drag's display rate would be most of a frame budget. The decimation
  step 3 left out is in: while a rotate drag lasts, lines keep every Nth
  sample so at most `DRAG_POINT_BUDGET` (2 500) points are laid out, and
  release redraws at full detail (checked: the released frame equals a fresh
  full draw pixel for pixel; the thinned drag frame differs in 143 pixels
  out of ~690 000). Now ~3.8 ms per step against the canvas map's ~3.0.
- **Hover** costs ~17 % more main-thread time than the canvas map's cached
  blit: the Pixi path re-renders the scene (lines unchanged, so no
  tessellation), the canvas path copies one bitmap. Both run with no long
  frames; it is the only interaction where the canvas map is cheaper and
  stays so.
- **Playback holds the display rate**: 118–120 renders/s on a 120 Hz screen
  with six ghosts, the chart cursors and raw table following every frame,
  ~1.3 ms of main thread per frame, no frame over 25 ms.

## Verdict

**Adopt the Pixi map, with the replay, as the default** (step 7). It
reaches parity with the canvas map — every item in #99's list, checked side
by side — costs nothing while idle, holds the display rate during playback
of the stress case, and costs +18 % (chart hover) and +29 % (3D rotate) main-thread time
over the canvas map on the two interactions it shares with it, with no long
frames in either. The canvas map stays as the fallback
(`?canvas`, or a renderer that fails to start), never a blank box.

What it cost, for the record: four Pixi-specific findings the canvas map
never needed — tessellation lands in `render()` not `stroke()`, extract
returns premultiplied pixels, the shared system ticker survives
`autoStart: false`, and a 3D rotate must thin its lines — plus one
replay-specific one, the lap clock (`lapClock`), that any renderer would
have hit. All five are documented in the code where they bite.

### Step 7 — swap (done)

- The Pixi map is the default; `?canvas` (or a renderer that fails to
  start) keeps the Canvas 2D map, which has everything but the replay. The
  `?pixi` flag is gone.
- `bootPixiMap()`'s promise is kept as `pixiMapBoot`; `tools/capture_media.py`
  awaits it before shooting the map, and its rotating-3D recipe renders
  synchronously (`pixiMap.renderNow()`) after each `drawMap()`, since the
  Pixi map draws on the next frame.
- Docs: ARCHITECTURE.md rows (`analysis-map.js`, `analysis-replay.js`,
  `pixi-kit.js`, the vendored Pixi), an AGENTS.md frontend convention, the
  wiki's Feature-Tour "Ghost replay" section and a Troubleshooting entry for
  a missing replay row, and a README feature line. No glyph changed, so the
  Event-Detection page is untouched.
- Re-verified on the default URL: Pixi starts, the replay row shows and
  every replay check passes; `?canvas` gets the canvas map with no replay
  row; canvas vs Pixi parity (markers, glyph positions, hit markers, PNG)
  unchanged.

### Next

- Recapture the README/wiki media with the new map and a replay GIF:
  [#100](https://github.com/darcane/LapScope/issues/100).
- The Δ chart's last-sample spike found on the way:
  [#104](https://github.com/darcane/LapScope/issues/104).
