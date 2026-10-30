/* Shared PixiJS helpers for every Pixi scene (the cockpit, the Analysis map):
   colour tokens, the bitmap font, glows, smoothing, the map glyphs and the
   slip colour as a number. Load after common.js and js/vendor/pixi.min.js.

   Idioms every scene follows (see docs/spikes/pixi-cockpit-poc.md):
   - redraw through a GraphicsContext: clear() then draw;
   - change BitmapText colour with `tint`, never `style.fill` (that re-lays
     out the glyphs);
   - every non-ASCII glyph a scene prints must be in installFonts()'s `chars`;
   - glows via glowing(); no cacheAsTexture (it blurs when the fit grows). */

// mirrors style.css's tokens: the scene can't read var(--…)
const C = {
  text: "#e8eef6", muted: "#8494a7", edge: "#1d2634", track: "#151d29",
  panel: "#0b0f15", redzone: "#4b1f26", ledOff: "#161e2a",
  good: "#2fe6a8", warn: "#ffbe3d", bad: "#ff5d5d",
};

/* Rajdhani is a webfont: the bitmap font must be built after it has loaded,
   or the glyphs get baked from the fallback face. Bitmap text, not Text:
   Text re-rasterises (and re-uploads a texture) on every change, which for a
   60 Hz readout is every frame. */
async function installFonts() {
  await document.fonts.load("700 64px Rajdhani");
  PIXI.BitmapFont.install({
    name: "ls-display",
    style: { fontFamily: "Rajdhani", fontWeight: "700", fontSize: 64, fill: "#ffffff" },
    chars: [[" ", "~"], "−–—·°×"],  // every non-ASCII glyph used must be listed
    resolution: 2,
  });
}

function bitmapText(text, size, fill, anchorX = 0.5, anchorY = 0.5) {
  const t = new PIXI.BitmapText({ text, style: { fontFamily: "ls-display", fontSize: size, fill } });
  t.anchor.set(anchorX, anchorY);
  return t;
}

/* A shape that glows: the shape plus a blurred, additively blended copy
   sharing the same geometry (GraphicsContext) — one blur pass, and nothing
   to keep in sync. Returns the container; draw into `ctx`. */
function glowing(ctx, strength = 10) {
  const c = new PIXI.Container();
  const glow = new PIXI.Graphics(ctx);
  glow.filters = [new PIXI.BlurFilter({ strength, quality: 4 })];
  glow.blendMode = "add";
  c.addChild(glow, new PIXI.Graphics(ctx));
  return c;
}

/* frame-rate independent exponential smoothing toward `target`, with time
   constant `tauMs` (0 = no smoothing) */
function smooth(cur, target, dtMs, tauMs) {
  if (!tauMs || cur == null || !isFinite(cur)) return target;
  return cur + (target - cur) * (1 - Math.exp(-dtMs / tauMs));
}

/* ---------- colour ---------- */

/* hsl → a number Pixi can fill or tint with (h in degrees, s/l in 0–1) */
function hslHex(h, s, l) {
  h = (((h % 360) + 360) % 360) / 360;
  const q = l < 0.5 ? l * (1 + s) : l + s - l * s, p = 2 * l - q;
  const ch = (t) => {
    t = (t + 1) % 1;
    const v = t < 1 / 6 ? p + (q - p) * 6 * t : t < 1 / 2 ? q : t < 2 / 3 ? p + (q - p) * (2 / 3 - t) * 6 : p;
    return Math.round(v * 255);
  };
  return (ch(h + 1 / 3) << 16) | (ch(h) << 8) | ch(h - 1 / 3);
}

/* slipHue (common.js) as a number: the same colour the canvas map draws */
function slipHex(s) { return hslHex(slipHue(s), 0.75, 0.55); }

/* ---------- map glyphs ---------- */

const JUMP_COL = "#f59e0b", SPARK_COL = "#ef4444";

/* common.js's drawJump, for a GraphicsContext: Pixi has no dashed stroke,
   so the flight is laid out dash by dash. `ring` takes the hard-landing ring.
   Change one, change both (ARCHITECTURE.md invariants). */
function jumpGlyph(g, x0, y0, x1, y1, ring) {
  const len = Math.hypot(x1 - x0, y1 - y0);
  const ang = len > 0.5 ? Math.atan2(y1 - y0, x1 - x0) : -Math.PI / 2;
  const r = 4, ah = 9, ux = Math.cos(ang), uy = Math.sin(ang);
  for (let d = r + 1; d < len - ah * 0.7; d += 11) {
    const e = Math.min(d + 6, len - ah * 0.7);
    g.moveTo(x0 + ux * d, y0 + uy * d).lineTo(x0 + ux * e, y0 + uy * e);
  }
  // the dashes and the takeoff ring are one path, stroked together
  g.circle(x0, y0, r).stroke({ width: 2, color: JUMP_COL, cap: "round" });
  g.poly([x1 + ux * ah * 0.55, y1 + uy * ah * 0.55,
    x1 + Math.cos(ang + 2.5) * ah * 0.8, y1 + Math.sin(ang + 2.5) * ah * 0.8,
    x1 + Math.cos(ang - 2.5) * ah * 0.8, y1 + Math.sin(ang - 2.5) * ah * 0.8]).fill(JUMP_COL);
  if (ring) ring.circle(x1, y1, ah * 0.95).stroke({ width: 1.5, color: "#ffffff" });
}

/* the 4-point contact spark */
function sparkGlyph(g, x, y) {
  const pts = [];
  for (let k = 0; k < 8; k++) {
    const a = (k * Math.PI) / 4, rr = k % 2 ? 3 : 7.5;
    pts.push(x + Math.cos(a) * rr, y + Math.sin(a) * rr);
  }
  g.poly(pts).fill(SPARK_COL).stroke({ width: 1.5, color: "#ffffff" });
}
