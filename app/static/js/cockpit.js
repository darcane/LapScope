/* Cockpit POC: the live dashboard as one PixiJS (WebGL) scene instead of
   seven Canvas 2D panels. Spike plan + verdict: docs/spikes/pixi-cockpit-poc.md.

   PixiJS 8.22.0 (MIT), vendored byte-for-byte from the npm tarball as
   js/vendor/pixi.min.js — no CDN, the app must work offline.

   Everything is laid out in a fixed design space (DESIGN) and the whole scene
   is scaled to fit the stage, so the proportions never break at any window
   size; the renderer itself always runs at the stage's CSS size × DPR, so the
   scaled scene stays crisp.

   Each component owns a container and an update(f, dt): static geometry is
   built once (Pixi keeps unchanged Graphics on the GPU, so there is no
   per-frame cost for it), and only values move per tick. */

const DESIGN = { w: 1600, h: 600 };
const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");
const params = new URLSearchParams(location.search);
// value smoothing time constant (ms). Telemetry: lag is a bug, so the default
// only bridges the gap between 60 Hz packets on a faster display. ?smooth=0
// shows raw packets, larger values let the spike compare feels.
const SMOOTH_MS = Math.max(0, Number(params.get("smooth") ?? 16)) || 0;
const DEBUG = params.has("debug");

// mirrors style.css's tokens: the scene can't read var(--…)
const C = {
  text: "#e8eef6", muted: "#8494a7", edge: "#1d2634", track: "#151d29",
  panel: "#0b0f15", redzone: "#4b1f26", ledOff: "#161e2a",
  good: "#2fe6a8", warn: "#ffbe3d", bad: "#ff5d5d",
};
// the Settings accent, re-read on every settings change
let accent = accentDef().accent;

const stage = document.getElementById("cockpit-stage");
let app, root;
const components = [];

/* ---------- live feed (a copy of dashboard.js's; shared live.js if adopted) ---------- */

const STALE_MS = 2500;
const feed = { frame: null, lastMsg: 0, connected: false, connLabel: null };

const RECONNECT_MIN = 1500;
const RECONNECT_MAX = 15000;
let reconnectDelay = RECONNECT_MIN;

function connect() {
  const scheme = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${scheme}//${location.host}/ws/live`);
  ws.onmessage = (ev) => {
    // hidden tab: nothing renders, so don't parse frames nobody will see
    if (document.hidden) return;
    feed.frame = JSON.parse(ev.data);
    feed.lastMsg = performance.now();
  };
  ws.onopen = () => {
    feed.connected = true;
    reconnectDelay = RECONNECT_MIN;
    setConn("live", "ok");
  };
  ws.onclose = () => {
    feed.connected = false;
    setConn("reconnecting…", "err");
    setTimeout(connect, reconnectDelay);
    reconnectDelay = Math.min(RECONNECT_MAX, reconnectDelay * 2);
  };
  ws.onerror = () => ws.close();
}

document.addEventListener("visibilitychange", () => {
  if (!document.hidden && feed.frame) feed.lastMsg = performance.now();
});

function setConn(text, cls) {
  if (feed.connLabel === text) return;
  feed.connLabel = text;
  const el = document.getElementById("conn");
  el.querySelector("span").textContent = text;
  el.className = `chip ${cls}`;
}

/* ---------- drawing helpers ---------- */

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

/* frame-rate independent exponential smoothing toward `target` */
function smooth(cur, target, dtMs) {
  if (!SMOOTH_MS || cur == null || !isFinite(cur)) return target;
  return cur + (target - cur) * (1 - Math.exp(-dtMs / SMOOTH_MS));
}

function gearText(g) { return g === 0 ? "R" : g === 11 ? "N" : String(g); }

/* ---------- components ---------- */

const REDLINE = 0.9;      // tach arc red from here (fraction of max rpm)
const AMBER = 0.78;
const ARC0 = Math.PI * 0.75, ARC1 = Math.PI * 2.25;  // 270° dial, gap at the bottom
const TACH = { cx: 800, cy: 330, r: 222, w: 18 };

/* RPM dial with the gear in the middle and speed in the bottom gap. The
   scale (ticks, numerals, red zone) depends on the car's max rpm, so it is
   rebuilt only when that changes — i.e. on a car change. */
function makeTach() {
  const { cx, cy, r, w } = TACH;
  const c = new PIXI.Container();

  const scale = new PIXI.Container();
  const valueCtx = new PIXI.GraphicsContext();
  const value = glowing(valueCtx, 12);

  const gear = bitmapText("N", 190, "#ffffff");
  gear.position.set(cx, cy - 18);
  const rpmTxt = bitmapText("0 RPM", 26, C.muted);
  rpmTxt.position.set(cx, cy + 76);
  const speed = bitmapText("0", 96, C.text);
  speed.position.set(cx, cy + r - 22);
  const unit = bitmapText(speedUnit(), 26, C.muted);
  unit.position.set(cx, cy + r + 34);
  c.addChild(scale, value, gear, rpmTxt, speed, unit);

  let builtMax = null, rpmS = null, lastGear = null, punch = 0;

  function buildScale(max) {
    scale.removeChildren().forEach((ch) => ch.destroy());
    const g = new PIXI.Graphics()
      .arc(cx, cy, r, ARC0, ARC1).stroke({ width: w, color: C.track, cap: "round" })
      .arc(cx, cy, r, ARC0 + (ARC1 - ARC0) * REDLINE, ARC1).stroke({ width: w, color: C.redzone, cap: "round" });
    // ticks each 500 rpm, numerals each 1000 (thinned on very high-revving engines)
    const numStep = max > 12000 ? 2000 : 1000;
    for (let v = 0; v <= max; v += 500) {
      const a = ARC0 + (ARC1 - ARC0) * (v / max);
      const major = v % 1000 === 0;
      const r0 = r - w / 2 - 6, r1 = r0 - (major ? 16 : 9);
      g.moveTo(cx + Math.cos(a) * r0, cy + Math.sin(a) * r0)
        .lineTo(cx + Math.cos(a) * r1, cy + Math.sin(a) * r1)
        .stroke({ width: major ? 3 : 2, color: C.muted, alpha: major ? 0.9 : 0.4 });
      if (v % numStep === 0) {
        const n = bitmapText(String(v / 1000), 26, C.muted);
        n.position.set(cx + Math.cos(a) * (r1 - 22), cy + Math.sin(a) * (r1 - 22));
        scale.addChild(n);
      }
    }
    scale.addChildAt(g, 0);
    builtMax = max;
  }

  return {
    root: c,
    update(f, dt) {
      const max = f.engine_max_rpm > 0 ? f.engine_max_rpm : 8000;
      if (max !== builtMax) buildScale(max);
      rpmS = smooth(rpmS, Math.max(0, f.current_engine_rpm), dt);
      const frac = Math.max(0, Math.min(1, rpmS / max));
      const col = frac > REDLINE ? C.bad : frac > AMBER ? C.warn : accent;
      valueCtx.clear();
      if (frac > 0.005) {
        const a = ARC0 + (ARC1 - ARC0) * frac;
        valueCtx.arc(cx, cy, r, ARC0, a).stroke({ width: w, color: col, cap: "round" });
        // the lit head: a bright bar across the arc's leading edge
        valueCtx.moveTo(cx + Math.cos(a) * (r - w), cy + Math.sin(a) * (r - w))
          .lineTo(cx + Math.cos(a) * (r + w), cy + Math.sin(a) * (r + w))
          .stroke({ width: 4, color: "#ffffff", cap: "round" });
      }
      rpmTxt.text = `${Math.round(rpmS)} RPM`;

      // gear: punches on a change (not on the first frame, not with reduced motion)
      if (lastGear !== null && f.gear !== lastGear && !reducedMotion.matches) punch = 1;
      lastGear = f.gear;
      punch = Math.max(0, punch - dt / 140);
      gear.text = gearText(f.gear);
      gear.tint = frac > REDLINE ? C.bad : accent;  // tint, not style.fill: no re-layout
      gear.scale.set(1 + 0.18 * punch * punch);

      speed.text = String(Math.round(Math.max(0, speedFromMps(f.speed))));
      unit.text = speedUnit().toUpperCase();
    },
  };
}

/* Ten shift LEDs across the top: fill from 55 % of max rpm to the redline,
   4 green / 3 amber / 3 red — the same rule and colours as the classic
   dashboard. On the limiter they flash (70 ms half-period, as style.css's
   `blink`); with reduced motion they hold lit instead. */
function makeShiftLights() {
  const N = 10, W = 52, H = 16, GAP = 14;
  const x0 = DESIGN.w / 2 - (N * W + (N - 1) * GAP) / 2, y = 22;
  const colors = [...Array(4).fill(C.good), ...Array(3).fill(C.warn), ...Array(3).fill(C.bad)];
  const c = new PIXI.Container();
  const off = new PIXI.Graphics();
  for (let i = 0; i < N; i++)
    off.roundRect(x0 + i * (W + GAP), y, W, H, H / 2).fill(C.ledOff).stroke({ width: 1, color: "#202a38" });
  const litCtx = new PIXI.GraphicsContext();
  const lit = glowing(litCtx, 8);
  c.addChild(off, lit);
  let litMask = -1, limiterT = 0;

  return {
    root: c,
    update(f, dt) {
      const frac = f.engine_max_rpm > 0 ? f.current_engine_rpm / f.engine_max_rpm : 0;
      let mask = 0;
      for (let i = 0; i < N; i++) if (frac >= 0.55 + i * 0.042) mask |= 1 << i;
      if (mask !== litMask) {  // geometry only changes when an LED does
        litCtx.clear();
        for (let i = 0; i < N; i++)
          if (mask & (1 << i)) litCtx.roundRect(x0 + i * (W + GAP), y, W, H, H / 2).fill(colors[i]);
        litMask = mask;
      }
      if (frac > 0.97 && !reducedMotion.matches) {
        limiterT += dt;
        lit.alpha = Math.floor(limiterT / 70) % 2 ? 0.25 : 1;
      } else {
        limiterT = 0;
        lit.alpha = 1;
      }
    },
  };
}

/* Before the first packet: the scene stays up (idle dial) with one line
   saying why nothing moves. The full setup instructions live on the Live page. */
function makeWaiting() {
  const t = bitmapText("WAITING FOR TELEMETRY · SETUP STEPS ON THE LIVE PAGE", 26, C.muted);
  t.position.set(DESIGN.w / 2, 78);
  return { root: t, update() {} };
}

/* ?debug: renderer facts + fps, for the spike's measurements. Lives in screen
   space (app.stage, unscaled), not in the design space: a letterboxed scene
   would otherwise put it on top of the shift lights. */
function makeDebug() {
  const t = bitmapText("", 22, C.muted, 0, 0);
  t.position.set(16, 12);
  return {
    root: t,
    update() {
      const rr = app.renderer;
      t.text = `PIXI ${PIXI.VERSION} · ${rr.name} · ${rr.canvas.width}×${rr.canvas.height} px`
        + ` · res ${rr.resolution} · ${Math.round(app.ticker.FPS)} fps · smooth ${SMOOTH_MS} ms`;
    },
  };
}

/* ---------- frame loop ---------- */

// what the scene shows before the first packet: a parked car at idle
const IDLE_FRAME = {
  current_engine_rpm: 0, engine_max_rpm: 8000, engine_idle_rpm: 800,
  gear: 11, speed: 0,
};

let waiting;
function tick(t) {
  const f = feed.frame;
  const stale = performance.now() - feed.lastMsg > STALE_MS;
  if (f && feed.connected) setConn(stale ? "paused" : "live", stale ? "warn" : "ok");
  waiting.root.visible = !f;
  // a stalled stream (game lost focus) dims the cluster instead of freezing
  // it at full brightness, which reads as live
  root.alpha = f && stale ? 0.45 : 1;
  const frame = f || IDLE_FRAME;
  for (const c of components) c.update(frame, t.deltaMS);
}

/* ---------- sizing ---------- */

/* Our own handler instead of Pixi's `resizeTo`: that one never re-reads
   devicePixelRatio (browser zoom / another monitor, issue #74) and isn't
   frame-throttled (issue #71). */
function layout() {
  const w = Math.max(1, stage.clientWidth), h = Math.max(1, stage.clientHeight);
  app.renderer.resize(w, h, window.devicePixelRatio || 1);
  const s = Math.min(w / DESIGN.w, h / DESIGN.h);
  root.scale.set(s);
  root.position.set((w - DESIGN.w * s) / 2, (h - DESIGN.h * s) / 2);
}

/* ---------- accessibility ---------- */

/* The scene is one canvas: say what it shows, at a reading pace rather than
   per frame (a per-frame aria-label is noise for a screen reader). */
function describe() {
  const f = feed.frame;
  stage.setAttribute("aria-label", !f
    ? "Cockpit — waiting for telemetry"
    : `Cockpit — ${Math.round(speedFromMps(Math.max(0, f.speed)))} ${speedUnit()},`
      + ` gear ${f.gear === 0 ? "reverse" : f.gear === 11 ? "neutral" : f.gear},`
      + ` ${Math.round(f.current_engine_rpm)} of ${Math.round(f.engine_max_rpm)} rpm`);
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

  root = new PIXI.Container();
  app.stage.addChild(root);
  waiting = makeWaiting();
  for (const c of [makeShiftLights(), makeTach(), waiting]) {
    components.push(c);
    root.addChild(c.root);
  }
  if (DEBUG) {
    const d = makeDebug();
    components.push(d);
    app.stage.addChild(d.root);
  }

  layout();
  window.addEventListener("resize", rafThrottle(layout));
  app.ticker.add(tick);
  setVisibleInterval(describe, 1000);
  describe();

  onSettingsChange(() => { accent = accentDef().accent; });
  connect();
}

boot();
