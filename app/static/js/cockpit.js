/* Cockpit POC: the live dashboard as one PixiJS (WebGL) scene instead of
   seven Canvas 2D panels. Spike plan + verdict: docs/spikes/pixi-cockpit-poc.md.

   PixiJS 8.22.0 (MIT), vendored byte-for-byte from the npm tarball as
   js/vendor/pixi.min.js — no CDN, the app must work offline.

   Everything is laid out in a fixed design space (DESIGN) and the whole scene
   is scaled to fit the stage, so the proportions never break at any window
   size; the renderer itself always runs at the stage's CSS size × DPR, so the
   scaled scene stays crisp. */

const DESIGN = { w: 1600, h: 600 };
const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");

// mirrors style.css's tokens: the scene can't read var(--…)
const C = {
  text: "#e8eef6", muted: "#8494a7", edge: "#1d2634", panel: "#0b0f15",
  good: "#2fe6a8", warn: "#ffbe3d", bad: "#ff5d5d",
};
// the Settings accent, re-read on every settings change
let accent = accentDef().accent;

const stage = document.getElementById("cockpit-stage");
let app, scene;

/* Rajdhani is a webfont: the bitmap font must be built after it has loaded,
   or the glyphs get baked from the fallback face. Bitmap text, not Text:
   Text re-rasterises (and re-uploads a texture) on every change, which for a
   60 Hz readout is every frame. */
async function installFonts() {
  await document.fonts.load("700 64px Rajdhani");
  PIXI.BitmapFont.install({
    name: "ls-display",
    style: { fontFamily: "Rajdhani", fontWeight: "700", fontSize: 64, fill: "#ffffff" },
    chars: [[" ", "~"], "−·°"],
    resolution: 2,
  });
}

function bitmapText(text, size, fill) {
  return new PIXI.BitmapText({ text, style: { fontFamily: "ls-display", fontSize: size, fill } });
}

/* A stroke that glows: the shape plus a blurred, additively blended copy
   sharing the same geometry (GraphicsContext), so the glow costs one blur
   pass and no second set of draw calls to keep in sync. */
function glowing(ctx, strength = 10) {
  const c = new PIXI.Container();
  const glow = new PIXI.Graphics(ctx);
  glow.filters = [new PIXI.BlurFilter({ strength, quality: 4 })];
  glow.blendMode = "add";
  c.addChild(glow, new PIXI.Graphics(ctx));
  return c;
}

/* ---------- step 1 test scene: proves loading, resize, DPR, glow, text ---------- */

const ARC0 = Math.PI * 0.75, ARC1 = Math.PI * 2.25;  // 270° dial, gap at the bottom

function buildTestScene() {
  const root = new PIXI.Container();

  // the design-space frame: if this is ever cropped or stretched, layout() is wrong
  const frame = new PIXI.Graphics()
    .roundRect(1, 1, DESIGN.w - 2, DESIGN.h - 2, 18)
    .fill({ color: C.panel, alpha: 0.55 })
    .stroke({ width: 2, color: C.edge });
  root.addChild(frame);

  const cx = DESIGN.w / 2, cy = DESIGN.h / 2 + 30, r = 230;
  const track = new PIXI.Graphics()
    .arc(cx, cy, r, ARC0, ARC1)
    .stroke({ width: 18, color: C.edge, cap: "round" });
  root.addChild(track);

  const litCtx = new PIXI.GraphicsContext();
  const lit = glowing(litCtx);
  root.addChild(lit);

  // the needle is drawn once and rotated: no per-frame geometry
  const needle = new PIXI.Container();
  needle.position.set(cx, cy);
  needle.addChild(glowing(new PIXI.GraphicsContext()
    .moveTo(0, 0).lineTo(r - 34, 0)
    .stroke({ width: 5, color: C.bad, cap: "round" }), 6));
  root.addChild(needle);

  const big = bitmapText("0", 150, C.text);
  big.anchor.set(0.5);
  big.position.set(cx, cy - 10);
  const unit = bitmapText("TEST SWEEP", 30, C.muted);
  unit.anchor.set(0.5);
  unit.position.set(cx, cy + 80);
  const info = bitmapText("", 26, C.muted);
  info.position.set(28, 22);
  root.addChild(big, unit, info);

  let phase = 0;
  return {
    root,
    update(dt) {
      // reduced motion: hold a fixed reading instead of sweeping forever
      if (!reducedMotion.matches) phase += dt / 60 * 0.6;
      const v = reducedMotion.matches ? 0.62 : (Math.sin(phase) + 1) / 2;
      const a = ARC0 + (ARC1 - ARC0) * v;
      litCtx.clear().arc(cx, cy, r, ARC0, a).stroke({ width: 18, color: accent, cap: "round" });
      needle.rotation = a;
      big.text = String(Math.round(v * 300));
      const rr = app.renderer;
      info.text = `PIXI ${PIXI.VERSION} · ${rr.name ?? rr.type} · `
        + `${stage.clientWidth}×${stage.clientHeight} css · `
        + `${rr.canvas.width}×${rr.canvas.height} px · res ${rr.resolution} · `
        + `${Math.round(app.ticker.FPS)} fps`;
    },
  };
}

/* ---------- sizing ---------- */

/* Our own handler instead of Pixi's `resizeTo`: that one never re-reads
   devicePixelRatio (browser zoom / another monitor, issue #74) and isn't
   frame-throttled (issue #71). */
function layout() {
  const w = Math.max(1, stage.clientWidth), h = Math.max(1, stage.clientHeight);
  app.renderer.resize(w, h, window.devicePixelRatio || 1);
  const s = Math.min(w / DESIGN.w, h / DESIGN.h);
  scene.root.scale.set(s);
  scene.root.position.set((w - DESIGN.w * s) / 2, (h - DESIGN.h * s) / 2);
}

/* ---------- accessibility ---------- */

/* The scene is one canvas: say what it shows, at a reading pace rather than
   per frame (a per-frame aria-label is noise for a screen reader). */
function describe() {
  stage.setAttribute("aria-label", "Cockpit proof of concept — test sweep running");
}

/* ---------- boot ---------- */

async function boot() {
  app = new PIXI.Application();
  try {
    await app.init({
      preference: "webgl",
      antialias: true,
      autoDensity: true,
      resolution: window.devicePixelRatio || 1,
      backgroundAlpha: 0,
      hello: false,  // the console banner links to pixijs.com
      width: Math.max(1, stage.clientWidth),
      height: Math.max(1, stage.clientHeight),
    });
  } catch (e) {
    document.getElementById("cockpit-fail-why").textContent = String(e && e.message || e);
    document.getElementById("cockpit-fail").classList.remove("hidden");
    stage.setAttribute("aria-label", "Cockpit unavailable — no WebGL");
    return;
  }
  stage.appendChild(app.canvas);
  await installFonts();

  scene = buildTestScene();
  app.stage.addChild(scene.root);
  layout();
  window.addEventListener("resize", rafThrottle(layout));
  app.ticker.add((t) => scene.update(t.deltaTime));

  onSettingsChange(() => { accent = accentDef().accent; });
  describe();
}

boot();
