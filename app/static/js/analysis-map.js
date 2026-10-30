/* The Analysis track map on PixiJS (issue #99; plan, measurements and
   verdict in docs/spikes/analysis-replay.md). Behind ?pixi until it reaches
   parity with the canvas map (drawCanvasMap in analysis.js), which stays the
   fallback: if no renderer starts, `pixiMap` stays null and nothing changes.

   analysis.js owns the state, the data and the projection (mapProjection);
   drawMap() sets everything said outside the pixels, then hands this file a
   scene to paint. Loaded before analysis.js; it only touches analysis.js's
   globals at call time.

   Lines live in the projection's raw (pre-fit) space, so the fit, the wheel
   zoom and the pan are the `lines` container's transform: hovering, zooming
   and panning move a matrix instead of re-tessellating ~12 000 points. They
   are rebuilt when what they show changes (picks, colours, 2D/3D, the 3D
   yaw, the chart zoom span) or when the effective scale drifts > 15 % from
   the one they were built at — stroke width scales with the transform, so
   lines are laid out at width / scale (the cockpit map's rule).

   Nothing draws on its own: draw() schedules one render on the next frame,
   and an idle page renders nothing at all. */

const PIXI_MAP = new URLSearchParams(location.search).has("pixi");
let pixiMap = null;

const LINE_REBUILD_DRIFT = 0.15;
// a lone lap's gradient: hue quantised so neighbouring segments of one colour
// share a stroke — 2° is below what the eye separates on a 3 px line
const GRAD_HUE_STEP = 2;
// the gradient outside a chart zoom span. The canvas map strokes 0.22 per
// ~1 px segment with round caps, so ~3 overlap at every pixel and it reads
// at ~0.6 (measured); one stroke per run has no overlap, hence the number
const FADED_ALPHA = 0.55;

function makePixiMap(app) {
  // three layers: lines (raw space, transformed), the screen-space overlay
  // (start arrow, zoom span ends, jump glyphs, contact sparks — constant
  // size whatever the zoom, redrawn on every draw(): a few dozen shapes),
  // and the hover / pin dots, the only layer a chart hover touches. Save PNG
  // extracts everything but the dots.
  const lines = new PIXI.Container();
  const endsCtx = new PIXI.GraphicsContext();   // zoom span ends, glowing
  const startCtx = new PIXI.GraphicsContext();
  const jumpCtx = new PIXI.GraphicsContext();   // soft landings
  const hardCtx = new PIXI.GraphicsContext();   // hard landings: same glyph, glowing
  const ringCtx = new PIXI.GraphicsContext();   // ...and their impact ring
  const sparkCtx = new PIXI.GraphicsContext();
  const overlayCtxs = [endsCtx, startCtx, jumpCtx, hardCtx, ringCtx, sparkCtx];
  const overlay = new PIXI.Container();
  overlay.addChild(glowing(endsCtx, 5), new PIXI.Graphics(startCtx), new PIXI.Graphics(jumpCtx),
    glowing(hardCtx, 6), new PIXI.Graphics(ringCtx), glowing(sparkCtx, 5));
  const markCtx = new PIXI.GraphicsContext();
  const markers = glowing(markCtx, 6);
  app.stage.addChild(lines, overlay, markers);
  let builtKey = "", builtScale = 0, sizeKey = "", queued = false;
  const ids = new WeakMap();
  let nextId = 0;
  const idOf = (o) => { if (!ids.has(o)) ids.set(o, ++nextId); return ids.get(o); };
  // read by the spike's measurements (docs/spikes/analysis-replay.md)
  const perf = { builds: 0, buildMs: 0, renders: 0 };

  function renderNow() {
    app.render();
    perf.renders++;
  }
  function render() {
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => {
      queued = false;
      renderNow();
    });
  }

  // only on a real change of CSS size or DPR (#71, #74); resize() re-reads
  // the resolution, autoDensity keeps the CSS size
  function resize(cssW, cssH) {
    const dpr = window.devicePixelRatio || 1;
    const key = `${cssW}x${cssH}@${dpr}`;
    if (key === sizeKey) return;
    sizeKey = key;
    app.renderer.resize(cssW, cssH, dpr);
  }

  function clear() {
    for (const c of lines.removeChildren()) c.destroy({ context: true });
    builtKey = "";
  }

  function drawOverlay({ loaded, ref, proj }) {
    for (const c of overlayCtxs) c.clear();
    if (!loaded.length) return;
    const { P, at } = proj;
    if (ref) {
      const A = ref.data;
      const zi = zoomIndexRange(A);
      if (zi) {  // span endpoints: filled = where the window starts, hollow = ends
        const zc = PICK_COLORS[0];
        for (const [idx, fill, ring] of [[zi[0], zc, "#ffffff"], [zi[1], "#0b1520", zc]]) {
          const [x, y] = at(A, idx, false);
          endsCtx.circle(x, y, 5.5).fill(fill).stroke({ width: 2.5, color: ring });
        }
      }
      const { sx, sy, arrow } = mapStartArrow(A, at);
      startCtx.circle(sx, sy, 6).fill(START_COLOR);
      if (arrow) {
        const { stem, head } = arrow;
        startCtx.moveTo(stem[0], stem[1]).lineTo(stem[2], stem[3])
          .stroke({ width: 2.5, color: START_COLOR, cap: "round" });
        startCtx.poly(head).fill(START_COLOR);
      }
    }
    const { jumps, hits } = mapGlyphs(loaded, ref, P);
    for (const j of jumps) {
      jumpGlyph(j.hard ? hardCtx : jumpCtx, j.x0, j.y0, j.x1, j.y1, j.hard ? ringCtx : null,
        { color: j.color, scale: 0.8 });
    }
    for (const h of hits) sparkGlyph(sparkCtx, h.x, h.y, { color: h.fill, r: h.r });
  }

  function drawMarkers() {
    markCtx.clear();
    for (const d of mapMarkerDots())
      markCtx.circle(d.x, d.y, d.r).fill(d.fill).stroke({ width: d.width, color: d.ring });
    render();
  }

  // what the lines show, as a string: a change means a rebuild
  function keyOf({ loaded, ref, three, grad }) {
    return [
      loaded.map((p) => `${idOf(p.data)}:${p.color}`).join(","),
      ref ? idOf(ref.data) : "-",
      three ? `3d:${map3d.yaw}` : "2d",
      grad ? state.colorMode : "solid",
      JSON.stringify(state.zoomRange), PICK_COLORS[0],  // the halo's colour
    ].join("|");
  }

  function build({ loaded, ref, three, proj, grad }, sc) {
    const t0 = performance.now();
    clear();
    const g = new PIXI.Graphics();
    const { raw } = proj;
    const pt = (d, i, ground) => {
      const c = d.channels;
      return raw(c.pos_x[i], c.pos_y ? c.pos_y[i] : 0, c.pos_z[i], ground);
    };
    const path = (d, ground, i0 = 0, i1 = d.channels.pos_x.length - 1) => {
      let [x, y] = pt(d, i0, ground);
      g.moveTo(x, y);
      for (let i = i0 + 1; i <= i1; i++) { [x, y] = pt(d, i, ground); g.lineTo(x, y); }
    };
    const W = (px) => px / sc;  // CSS px -> raw units at the build scale

    if (three) {
      // ground shadow + elevation posts first, so the ribbon reads as floating
      for (const p of loaded) if (p !== ref) {
        path(p.data, true);
        g.stroke({ width: W(1.2), color: 0x5b6675, alpha: 0.22 });
      }
      if (ref) {
        path(ref.data, true);
        g.stroke({ width: W(3), color: 0x000000, alpha: 0.5 });
        for (let i = 0; i < ref.data.channels.pos_x.length; i += 14) {
          const [gx, gy] = pt(ref.data, i, true), [ax, ay] = pt(ref.data, i, false);
          g.moveTo(gx, gy).lineTo(ax, ay);
        }
        g.stroke({ width: W(1), color: 0x7b8794, alpha: 0.18 });
      }
    }

    // non-reference traces: each in its tray color, under the reference
    for (const p of loaded) if (p !== ref) {
      path(p.data, false);
      g.stroke({ width: W(2), color: p.color });
    }

    if (ref) {
      const A = ref.data, n = A.channels.pos_x.length;
      const zi = zoomIndexRange(A);
      if (zi) {  // halo under the zoomed stretch
        path(A, false, zi[0], zi[1]);
        g.stroke({ width: W(11), color: PICK_COLORS[0], alpha: 0.3, join: "round", cap: "round" });
      }
      if (grad) {
        // one stroke per run of segments sharing a (quantised) colour and
        // alpha; outside the zoom window everything fades, like the canvas
        const { vals, hue, sat } = grad;
        let runKey = null, runColor = 0, runAlpha = 1;
        // round caps only at full alpha: faded runs would overlap their caps
        // at every colour change and bead the line with doubled alpha
        const flush = () => {
          if (runKey != null) {
            g.stroke({ width: W(3), color: runColor, alpha: runAlpha, cap: runAlpha < 1 ? "butt" : "round" });
          }
        };
        for (let i = 1; i < n; i++) {
          const h = Math.round(hue(vals[i]) / GRAD_HUE_STEP) * GRAD_HUE_STEP;
          const alpha = zi && (i <= zi[0] || i > zi[1]) ? FADED_ALPHA : 1;
          const key = `${h}|${alpha}`;
          if (key !== runKey) {
            flush();
            runKey = key;
            runColor = hslHex(h, sat, 0.55);
            runAlpha = alpha;
            const [x, y] = pt(A, i - 1, false);
            g.moveTo(x, y);
          }
          const [x, y] = pt(A, i, false);
          g.lineTo(x, y);
        }
        flush();
      } else {
        // overlay: the reference wears its solid tray color, on top and wider
        path(A, false);
        g.stroke({ width: W(3), color: ref.color });
      }
    }
    lines.addChild(g);
    builtScale = sc;
    perf.builds++;
    perf.buildMs = performance.now() - t0;
  }

  return {
    canvas: app.canvas,
    perf,
    renderNow,
    draw(scene) {
      resize(scene.cssW, scene.cssH);
      if (!scene.loaded.length) {
        clear();
      } else {
        const { proj } = scene;
        const sc = proj.scale * mapView.zoom;
        const key = keyOf(scene);
        if (key !== builtKey || Math.abs(sc / builtScale - 1) > LINE_REBUILD_DRIFT) {
          build(scene, sc);
          builtKey = key;
        }
        lines.scale.set(sc);
        lines.position.set(proj.offX * mapView.zoom + mapView.panX,
                           proj.offY * mapView.zoom + mapView.panY);
      }
      drawOverlay(scene);
      drawMarkers();  // renders
    },
    drawMarkers,
    // the map as drawn minus the hover / pin dots, for Save PNG: an extract
    // of the stage at the device pixel size, on the export's own `background`
    // (extract hands back premultiplied pixels — composited again over a fill
    // they wash out every translucent stroke, so they get an opaque one here)
    snapshot(background) {
      const dpr = app.renderer.resolution;
      markers.visible = false;
      const snap = app.renderer.extract.canvas({
        target: app.stage,
        frame: new PIXI.Rectangle(0, 0, app.screen.width, app.screen.height),
        resolution: dpr,
        antialias: true,
        clearColor: background,
      });
      markers.visible = true;
      render();
      return { snap, dpr };
    },
  };
}

/* Start the renderer; on success the Pixi canvas takes the #trackmap slot.
   Any failure leaves `pixiMap` null — the canvas map simply keeps drawing. */
async function bootPixiMap() {
  if (!PIXI_MAP || typeof PIXI === "undefined") return;
  const app = new PIXI.Application();
  try {
    await app.init({
      preference: "webgl",
      antialias: true,
      autoDensity: true,
      resolution: window.devicePixelRatio || 1,
      backgroundAlpha: 0,
      hello: false,      // the console banner links to pixijs.com
      autoStart: false,  // no ticker: draw() renders on demand
      width: 1,
      height: 1,
    });
  } catch (e) {
    console.warn("Pixi track map unavailable, using the canvas map:", e);
    return;
  }
  app.canvas.id = "trackmap";
  app.canvas.setAttribute("role", "img");
  pixiMap = makePixiMap(app);
  // the map column can change width without the window resizing (the side
  // panel wraps under it below 1100 px); the window listener in analysis.js
  // still catches a DPR change at the same CSS size (#74)
  let lastW = 0;
  pixiMap.observer = new ResizeObserver(rafThrottle((entries) => {
    const w = entries[entries.length - 1].contentRect.width;
    if (w !== lastW) { lastW = w; drawMap(); }
  }));
  mountMapCanvas();
  drawMap();
}

/* The detail pane is re-cloned from its template on every session switch,
   with a fresh <canvas id="trackmap">: swap the one Pixi canvas (and its
   WebGL context) into that slot instead of starting a renderer per pane. */
function mountPixiCanvas() {
  const slot = $("#trackmap");
  if (!pixiMap || !slot || slot === pixiMap.canvas) return;
  pixiMap.canvas.setAttribute("aria-label", slot.getAttribute("aria-label") || "");
  slot.replaceWith(pixiMap.canvas);
  pixiMap.observer.disconnect();
  pixiMap.observer.observe(pixiMap.canvas.parentElement);
}
