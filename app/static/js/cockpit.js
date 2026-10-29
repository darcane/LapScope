/* Cockpit POC: the live dashboard as one PixiJS (WebGL) scene instead of
   seven Canvas 2D panels. Spike plan + verdict: docs/spikes/pixi-cockpit-poc.md.

   PixiJS 8.22.0 (MIT), vendored byte-for-byte from the npm tarball as
   js/vendor/pixi.min.js — no CDN, the app must work offline.

   Every component is drawn in one fixed design space: the cockpit cluster in
   1600×600, the map + input trace strip below it (y 600–900). A LAYOUT then
   places each component's slot (wide: as drawn; tall: re-stacked for
   portrait screens) and the whole scene is scaled to fit the stage, so the
   proportions never break; the renderer itself always runs at the stage's
   CSS size × DPR, so the scaled scene stays crisp.

   Each component owns a container and an update(f, dt): static geometry is
   built once (Pixi keeps unchanged Graphics on the GPU, so there is no
   per-frame cost for it), and only values move per tick. */

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

/* The feed (WebSocket, trail + input buffers, track-map path, contact and
   jump detection) is live.js, shared with the classic dashboard: this file
   only reads `live` / `liveMap` and draws. */

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
  // power and boost flank the speed, outside the dial's open bottom
  const power = bitmapText("0", 44, C.text);
  power.position.set(cx - 330, cy + r - 4);
  const powerUnitTxt = bitmapText("", 22, C.muted);
  powerUnitTxt.position.set(cx - 330, cy + r + 30);
  const boost = bitmapText("0.0", 44, C.text);
  boost.position.set(cx + 330, cy + r - 4);
  const boostUnitTxt = bitmapText("", 22, C.muted);
  boostUnitTxt.position.set(cx + 330, cy + r + 30);
  c.addChild(scale, value, gear, rpmTxt, speed, unit, power, powerUnitTxt, boost, boostUnitTxt);

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

      // before the first packet the waiting line takes the speed slot
      speed.visible = unit.visible = f !== IDLE_FRAME;
      speed.text = String(Math.round(Math.max(0, speedFromMps(f.speed))));
      unit.text = speedUnit().toUpperCase();
      power.text = powerFromW(Math.max(0, f.power || 0)).toFixed(0);
      powerUnitTxt.text = `POWER ${powerUnit().toUpperCase()}`;
      boost.text = fmtBoost(Math.max(0, f.boost || 0));
      boostUnitTxt.text = `BOOST ${boostUnit().toUpperCase()}`;
    },
  };
}

/* Ten shift LEDs across the top: fill from 55 % of max rpm to the redline,
   4 green / 3 amber / 3 red — the same rule and colours as the classic
   dashboard. On the limiter they flash (70 ms half-period, as style.css's
   `blink`); with reduced motion they hold lit instead. */
function makeShiftLights() {
  const N = 10, W = 52, H = 16, GAP = 14;
  const x0 = TACH.cx - (N * W + (N - 1) * GAP) / 2, y = 22;
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

/* Throttle and brake as brackets on either side of the dial (brake fills up
   the left one, throttle up the right), steering as a short arc over the
   top with a centre notch — a wheel's top marker. */
const INPUT_R = TACH.r + 44;
const PEDAL_SPAN = Math.PI * 0.44;                    // ~80° per bracket
const BRK0 = ARC0, THR0 = Math.PI * 0.25;             // both start at the dial's open bottom
const STEER_MID = -Math.PI / 2, STEER_HALF = Math.PI / 6;

function makeInputs() {
  const { cx, cy } = TACH, R = INPUT_R, W = 12;
  const at = (a, rr) => [cx + Math.cos(a) * rr, cy + Math.sin(a) * rr];
  const c = new PIXI.Container();
  const track = new PIXI.Graphics()
    .arc(cx, cy, R, BRK0, BRK0 + PEDAL_SPAN).stroke({ width: W, color: C.track, cap: "round" })
    .arc(cx, cy, R, THR0, THR0 - PEDAL_SPAN, true).stroke({ width: W, color: C.track, cap: "round" })
    .arc(cx, cy, R, STEER_MID - STEER_HALF, STEER_MID + STEER_HALF).stroke({ width: 6, color: C.track, cap: "round" })
    .moveTo(cx, cy - R - 13).lineTo(cx, cy - R + 13).stroke({ width: 2, color: C.muted });
  const fillCtx = new PIXI.GraphicsContext();
  const brkLbl = bitmapText("BRK", 22, C.muted);
  brkLbl.position.set(...at(BRK0, R + 30));
  const thrLbl = bitmapText("THR", 22, C.muted);
  thrLbl.position.set(...at(THR0, R + 30));
  c.addChild(track, glowing(fillCtx, 8), brkLbl, thrLbl);

  let th = null, br = null, st = null;
  return {
    root: c,
    update(f, dt) {
      th = smooth(th, Math.max(0, Math.min(1, (f.accel || 0) / 255)), dt);
      br = smooth(br, Math.max(0, Math.min(1, (f.brake || 0) / 255)), dt);
      st = smooth(st, Math.max(-1, Math.min(1, (f.steer || 0) / 127)), dt);
      fillCtx.clear();
      if (br > 0.005)
        fillCtx.arc(cx, cy, R, BRK0, BRK0 + PEDAL_SPAN * br).stroke({ width: W, color: C.bad, cap: "round" });
      if (th > 0.005)
        fillCtx.arc(cx, cy, R, THR0, THR0 - PEDAL_SPAN * th, true).stroke({ width: W, color: C.good, cap: "round" });
      const a = STEER_MID + STEER_HALF * st;
      if (Math.abs(st) > 0.005)
        fillCtx.arc(cx, cy, R, STEER_MID, a, st < 0).stroke({ width: 6, color: accent, cap: "round" });
      fillCtx.circle(...at(a, R), 9).fill(C.text);
    },
  };
}

function fmtLap(s) {
  if (!s || s <= 0) return "–:--.---";
  const m = Math.floor(s / 60);
  return `${m}:${(s - m * 60).toFixed(3).padStart(6, "0")}`;
}

/* Race-mode progress, 0 = free roam … 1 = timed event, eased in tick(). The
   timing and delta blocks slide in and brighten as it rises: lap timing only
   means something during an event (free roam's fallback lap clock would just
   count up), so outside one they step back rather than vanish. */
let raceP = 0;
const RACE_EASE_MS = 320;
const raceEased = () => raceP * raceP * (3 - 2 * raceP);  // smoothstep

/* Top-left: race-mode chip, lap / position, the running lap, last and best. */
function makeTiming() {
  const x = 60;
  const c = new PIXI.Container();
  const chipBg = new PIXI.Graphics();
  const chip = bitmapText("FREE ROAM", 22, C.muted, 0, 0.5);
  chip.position.set(x + 14, 96);
  const lapPos = bitmapText("LAP –  ·  P –", 40, C.text, 0, 0.5);
  lapPos.position.set(x, 150);
  const cur = bitmapText("–:--.---", 88, C.text, 0, 0.5);
  cur.position.set(x, 224);
  const lastL = bitmapText("LAST", 20, C.muted, 0, 0.5);
  lastL.position.set(x, 290);
  const bestL = bitmapText("SESSION BEST", 20, C.muted, 0, 0.5);
  bestL.position.set(x + 220, 290);
  const last = bitmapText("–:--.---", 40, C.text, 0, 0.5);
  last.position.set(x, 324);
  const best = bitmapText("–:--.---", 40, C.text, 0, 0.5);
  best.position.set(x + 220, 324);
  c.addChild(chipBg, chip, lapPos, cur, lastL, bestL, last, best);

  let chipRace = null;
  return {
    root: c,
    update(f) {
      const race = !!f.race_mode;
      if (race !== chipRace) {  // the chip's geometry only changes with the mode
        chip.text = race ? "RACE MODE" : "FREE ROAM";
        chip.tint = race ? "#001018" : C.muted;
        chipBg.clear().roundRect(x, 80, chip.width + 28, 32, 16);
        if (race) chipBg.fill(accent);
        else chipBg.stroke({ width: 2, color: C.edge });
        chipRace = race;
      }
      lapPos.text = race
        ? `LAP ${(f.lap_number || 0) + 1}  ·  P ${f.race_position || "–"}`
        : "LAP –  ·  P –";
      cur.text = race ? fmtLap(f.current_lap || f.lap_elapsed) : "–:--.---";
      last.text = fmtLap(f.last_lap);
      best.text = fmtLap(f.session_best ?? f.best_lap);
      const e = raceEased();
      c.alpha = 0.3 + 0.7 * e;
      c.x = -28 * (1 - e);
    },
  };
}

/* Top-right: delta to session best, as a number and a centre-zero bar —
   slower (+) fills right in red, faster (−) fills left in green, full scale
   at ±2 s. Crossing zero pulses the number once. */
const DELTA_FULL_S = 2;
function makeDelta() {
  const cx = 1335, x0 = 1120, x1 = 1550, y = 226, H = 16;  // clear of the throttle bracket
  const c = new PIXI.Container();
  const label = bitmapText("DELTA · SESSION BEST", 20, C.muted);
  label.position.set(cx, 96);
  const num = bitmapText("—", 88, C.text);
  num.position.set(cx, 160);
  const track = new PIXI.Graphics()
    .roundRect(x0, y, x1 - x0, H, H / 2).fill(C.track)
    .moveTo(cx, y - 8).lineTo(cx, y + H + 8).stroke({ width: 2, color: C.muted });
  const fillCtx = new PIXI.GraphicsContext();
  const ticks = [["−2", x0], ["−1", (x0 + cx) / 2], ["0", cx], ["+1", (cx + x1) / 2], ["+2", x1]].map(([s, tx]) => {
    const t = bitmapText(s, 20, C.muted);
    t.position.set(tx, y + H + 24);
    return t;
  });
  c.addChild(label, num, track, glowing(fillCtx, 8), ...ticks);

  let lastSign = 0, pulse = 0;
  return {
    root: c,
    update(f, dt) {
      const d = f.delta;
      fillCtx.clear();
      if (d == null) {
        num.text = "—";
        num.tint = C.text;
      } else {
        num.text = (d >= 0 ? "+" : "−") + Math.abs(d).toFixed(3);
        const col = d >= 0 ? C.bad : C.good;
        num.tint = col;
        const half = (x1 - x0) / 2;
        const len = Math.min(1, Math.abs(d) / DELTA_FULL_S) * half;
        if (len > 1) fillCtx.roundRect(d >= 0 ? cx : cx - len, y, len, H, H / 2).fill(col);
        const sign = Math.sign(d);
        if (sign && lastSign && sign !== lastSign && !reducedMotion.matches) pulse = 1;
        if (sign) lastSign = sign;
      }
      pulse = Math.max(0, pulse - dt / 320);
      num.scale.set(1 + 0.14 * pulse * pulse);
      const e = raceEased();
      c.alpha = 0.3 + 0.7 * e;
      c.x = 28 * (1 - e);
    },
  };
}

/* Bottom-left: friction circle — lateral g across, longitudinal g up
   (accel) / down (brake), ±1.6 g full scale, rings at 0.5 / 1 / 1.5 g, the
   last ~1.5 s as a trail fading toward its oldest point. The dot turns amber
   past 1.1 g. Readouts beside it.
   Jump landings and wall hits spike far past any tire's grip (10 g+). Neither
   the dot nor the trail leaves the gauge for one: both pin to the rim, and the
   rim flashes red in the spike's direction while the readouts show its size. */
const FRICTION_MAX_G = 1.6;
const IMPACT_FLASH_MS = 700;
function makeFriction() {
  const cx = 180, cy = 480, R = 100, k = R / FRICTION_MAX_G;  // ACCEL label clears the LAST/BEST row
  const c = new PIXI.Container();
  const grid = new PIXI.Graphics()
    .circle(cx, cy, 1.5 * k).fill({ color: accent, alpha: 0.04 });
  for (const ring of [0.5, 1.0, 1.5]) grid.circle(cx, cy, ring * k).stroke({ width: 1.5, color: C.edge });
  grid.moveTo(cx - R, cy).lineTo(cx + R, cy).moveTo(cx, cy - R).lineTo(cx, cy + R)
    .stroke({ width: 1.5, color: C.edge });
  const labels = [["ACCEL", cx, cy - R - 14], ["BRAKE", cx, cy + R + 14],
    ["1.0", cx + k + 14, cy - 12], ["1.5", cx + 1.5 * k + 6, cy - 12]].map(([s, x, y]) => {
    const t = bitmapText(s, 18, C.muted);
    t.position.set(x, y);
    return t;
  });
  const trailCtx = new PIXI.GraphicsContext();
  const dotCtx = new PIXI.GraphicsContext();
  const hitCtx = new PIXI.GraphicsContext();
  const hitG = glowing(hitCtx, 10);
  c.addChild(grid, ...labels, new PIXI.Graphics(trailCtx), hitG, glowing(dotCtx, 8));
  // a g vector → gauge point, pinned to the rim past full scale
  const pt = (lat, lon) => {
    const tot = Math.hypot(lat, lon), s = tot > FRICTION_MAX_G ? FRICTION_MAX_G / tot : 1;
    return [cx + lat * k * s, cy - lon * k * s];
  };
  let hitAt = -Infinity;

  const read = ["LAT", "LON", "TOTAL"].map((name, i) => {
    const l = bitmapText(name, 18, C.muted, 0, 0.5);
    l.position.set(cx + R + 46, cy - 52 + i * 52);
    const v = bitmapText("0.00", 36, C.text, 0, 0.5);
    v.position.set(cx + R + 46, cy - 30 + i * 52);
    c.addChild(l, v);
    return v;
  });

  return {
    root: c,
    update(f) {
      const tr = live.trail;
      trailCtx.clear();
      for (let i = 1; i < tr.length; i++) {
        const [x0, y0] = pt(...tr[i - 1]), [x1, y1] = pt(...tr[i]);
        trailCtx.moveTo(x0, y0).lineTo(x1, y1)
          .stroke({ width: 2.5, color: accent, alpha: (i / tr.length) * 0.55, cap: "round" });
      }
      const lat = (f.accel_x || 0) / G, lon = (f.accel_z || 0) / G, tot = Math.hypot(lat, lon);
      const over = tot > FRICTION_MAX_G, now = performance.now();
      if (over) {
        // the newest spike owns the flash; its arc points where the hit came from
        hitAt = now;
        const a = Math.atan2(-lon, lat), r = FRICTION_MAX_G * k;
        hitCtx.clear().arc(cx, cy, r, a - 0.35, a + 0.35)
          .stroke({ width: 6, color: C.bad, cap: "round" });
      }
      const age = now - hitAt;
      // reduced motion: hold the flash, then cut it — no fade
      hitG.visible = age < IMPACT_FLASH_MS;
      hitG.alpha = reducedMotion.matches ? 1 : 1 - age / IMPACT_FLASH_MS;
      const [dx, dy] = pt(lat, lon);
      dotCtx.clear().circle(dx, dy, 8).fill(over ? C.bad : tot > 1.1 ? C.warn : C.text);
      read[0].text = lat.toFixed(2);
      read[1].text = lon.toFixed(2);
      read[2].text = tot.toFixed(2);
      read[2].tint = over ? C.bad : tot > 1.1 ? C.warn : C.text;
    },
  };
}

/* the classic gauges' slipColor: hue 120 (green) at 0 slip → 0 (red) at
   1.15+, as a number Pixi can tint with */
function slipHex(s) {
  const h = Math.max(0, Math.min(120, 120 * (1.15 - s) / 1.15)) / 360, sat = 0.75, l = 0.55;
  const q = l + sat - l * sat, p = 2 * l - q;
  const ch = (t) => {
    t = (t + 1) % 1;
    const v = t < 1 / 6 ? p + (q - p) * 6 * t : t < 1 / 2 ? q : t < 2 / 3 ? p + (q - p) * (2 / 3 - t) * 6 : p;
    return Math.round(v * 255);
  };
  return (ch(h + 1 / 3) << 16) | (ch(h) << 8) | ch(h - 1 / 3);
}

/* the classic balanceText: front vs rear slip angle */
function balance(f) {
  const a = f.tire_slip_angle;
  if (!a) return ["NEUTRAL", C.muted];
  const front = (Math.abs(a[0]) + Math.abs(a[1])) / 2, rear = (Math.abs(a[2]) + Math.abs(a[3])) / 2;
  if (Math.max(front, rear) < 0.5) return ["NEUTRAL", C.muted];
  if (front > rear * 1.2) return ["UNDERSTEER", C.warn];
  if (rear > front * 1.2) return ["OVERSTEER", C.bad];
  return ["NEUTRAL", C.muted];
}

/* Bottom-right: the car from above. Each tire is coloured by combined slip
   (glowing past 1.0) with the value on it, its temperature outboard of it
   (blue < 160 °F, orange > 230 °F — packet units, whatever the display unit)
   and its spring travel as a bar on the outer side (amber > 82 %, red > 95 %).
   The axle that is sliding more lights up with the balance read-out. */
const TIRE_ORDER = ["FL", "FR", "RL", "RR"];
function makeTires() {
  const cx = 1330, cy = 462, bodyW = 104, bodyH = 176, tw = 34, th = 58;
  const px = bodyW / 2 + 26, py = bodyH / 2 - 30;
  const pos = [[cx - px, cy - py], [cx + px, cy - py], [cx - px, cy + py], [cx + px, cy + py]];
  const suspX = (i) => pos[i][0] + (i % 2 === 0 ? -1 : 1) * (tw / 2 + 14) - 4;
  const c = new PIXI.Container();
  const body = new PIXI.Graphics()
    .roundRect(cx - bodyW / 2, cy - bodyH / 2, bodyW, bodyH, 22).stroke({ width: 2, color: "#3a475c" })
    .roundRect(cx - bodyW * 0.32, cy - bodyH * 0.16, bodyW * 0.64, bodyH * 0.4, 10).stroke({ width: 2, color: "#2a3546" });
  const axleCtx = new PIXI.GraphicsContext();
  const shell = new PIXI.Graphics();
  for (let i = 0; i < 4; i++) {
    const [x, y] = pos[i];
    shell.roundRect(x - tw / 2, y - th / 2, tw, th, 9).fill(C.track)
      .roundRect(suspX(i), y - th / 2, 8, th, 4).fill("#0a0e14").stroke({ width: 1, color: C.edge });
  }
  const tireCtx = new PIXI.GraphicsContext();   // slip fills up to 1.0
  const hotCtx = new PIXI.GraphicsContext();    // past 1.0: same fill, glowing
  const suspCtx = new PIXI.GraphicsContext();
  c.addChild(glowing(axleCtx, 10), body, shell, new PIXI.Graphics(tireCtx), glowing(hotCtx, 10),
    new PIXI.Graphics(suspCtx));

  const slipTxt = [], tempTxt = [];
  for (let i = 0; i < 4; i++) {
    const [x, y] = pos[i], front = i < 2, left = i % 2 === 0;
    const lbl = bitmapText(TIRE_ORDER[i], 18, C.muted);
    lbl.position.set(x, front ? y - th / 2 - 14 : y + th / 2 + 14);
    const st = bitmapText("0.0", 20, "#ffffff");
    st.position.set(x, y);
    st.tint = "#0b0e13";
    // temps sit outboard of the suspension bar
    const tt = bitmapText("–", 22, "#ffffff", left ? 1 : 0, 0.5);
    tt.position.set(left ? suspX(i) - 10 : suspX(i) + 18, y);
    c.addChild(lbl, st, tt);
    slipTxt.push(st);
    tempTxt.push(tt);
  }
  const bal = bitmapText("NEUTRAL", 24, "#ffffff");
  bal.position.set(cx, cy + bodyH / 2 + 44);
  c.addChild(bal);

  return {
    root: c,
    update(f) {
      // no slip data yet (idle scene): leave the dark shells, don't paint "0.0" green
      const slip = f.tire_combined_slip || null;
      const temps = f.tire_temp || null;
      const susp = f.norm_susp_travel || [0, 0, 0, 0];
      tireCtx.clear();
      hotCtx.clear();
      suspCtx.clear();
      for (let i = 0; i < 4; i++) {
        const [x, y] = pos[i], s = slip ? slip[i] || 0 : null;
        if (s != null) (s > 1.0 ? hotCtx : tireCtx).roundRect(x - tw / 2, y - th / 2, tw, th, 9).fill(slipHex(s));
        slipTxt[i].text = s == null ? "" : s.toFixed(1);
        const tF = temps ? temps[i] : null;
        tempTxt[i].text = tF == null ? "–" : fmtTireTemp(tF);
        tempTxt[i].tint = tF == null ? C.muted : tF < 160 ? "#7fb2ff" : tF > 230 ? "#ff8a5d" : C.text;
        const cmp = Math.max(0, Math.min(1, susp[i] || 0)), fh = Math.max(2, th * cmp);
        suspCtx.roundRect(suspX(i), y + th / 2 - fh, 8, fh, 4)
          .fill(cmp > 0.95 ? C.bad : cmp > 0.82 ? C.warn : accent);
      }
      const [txt, col] = balance(f);
      bal.text = txt;
      bal.tint = col;
      axleCtx.clear();
      if (txt !== "NEUTRAL") {
        const y = txt === "UNDERSTEER" ? cy - py : cy + py;
        axleCtx.moveTo(cx - px, y).lineTo(cx + px, y).stroke({ width: 6, color: col, alpha: 0.7, cap: "round" });
      }
    },
  };
}

/* Section 2, left: the live track map in a 720×260 box.

   Pixi re-tessellates a Graphics whenever its geometry changes, so a 4000-
   point path rebuilt on every new point would be the most expensive thing
   in the scene. Instead the path lives in WORLD coordinates in chunks of
   CHUNK points: finished chunks are frozen, only the open one is rebuilt
   when a point lands, and fitting the track into the box is just the world
   container's transform. The catch is stroke width, which scales with that
   transform — so chunks are built at width / scale and rebuilt only when the
   fit scale drifts > 15 % from what they were built at (the bounds stop
   growing after the first lap).
   Jumps (amber dashed flight, takeoff ring → touchdown arrow; a hard landing
   glows with a white impact ring) and contact sparks (red, glowing) are the
   classic map's glyphs, drawn in box coordinates so they keep their size,
   and redrawn only when live.js adds one or the fit moves. */
const MAP_BOX = { x: 40, y: 620, w: 720, h: 260 };
const MAP_CHUNK = 256;
const JUMP_COL = "#f59e0b", SPARK_COL = "#ef4444";

/* common.js's drawJump, for a GraphicsContext: Pixi has no dashed stroke,
   so the flight is laid out dash by dash. `ring` takes the hard-landing ring. */
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

function sparkGlyph(g, x, y) {
  const pts = [];
  for (let k = 0; k < 8; k++) {
    const a = (k * Math.PI) / 4, rr = k % 2 ? 3 : 7.5;
    pts.push(x + Math.cos(a) * rr, y + Math.sin(a) * rr);
  }
  g.poly(pts).fill(SPARK_COL).stroke({ width: 1.5, color: "#ffffff" });
}

function makeMap() {
  const B = MAP_BOX, pad = 18, padTop = 40;  // padTop: clear of the title row
  const c = new PIXI.Container();
  const frame = new PIXI.Graphics()
    .roundRect(B.x, B.y, B.w, B.h, 14).fill({ color: C.panel, alpha: 0.5 }).stroke({ width: 1.5, color: C.edge });
  const title = bitmapText("TRACK MAP", 18, C.muted, 0, 0.5);
  title.position.set(B.x + 16, B.y + 18);
  const hint = bitmapText("THE TRACK DRAWS HERE ONCE AN EVENT STARTS", 20, C.muted);
  hint.position.set(B.x + B.w / 2, B.y + B.h / 2);
  const clip = new PIXI.Graphics().rect(B.x, B.y, B.w, B.h).fill(0xffffff);
  const world = new PIXI.Container();
  world.mask = clip;
  const carCtx = new PIXI.GraphicsContext();
  const jumpCtx = new PIXI.GraphicsContext();   // soft landings
  const hardCtx = new PIXI.GraphicsContext();   // hard landings: same glyph, glowing
  const ringCtx = new PIXI.GraphicsContext();
  const sparkCtx = new PIXI.GraphicsContext();
  const marks = new PIXI.Container();
  marks.mask = new PIXI.Graphics().rect(B.x, B.y, B.w, B.h).fill(0xffffff);
  marks.addChild(marks.mask, new PIXI.Graphics(jumpCtx), glowing(hardCtx, 6),
    new PIXI.Graphics(ringCtx), glowing(sparkCtx, 6));
  c.addChild(frame, title, hint, clip, world, marks, glowing(carCtx, 8));
  let marksKey = "";

  let chunks = [], builtGen = -1, builtLen = 0, builtScale = 0;
  const strokeFor = (sc) => ({ width: 2.5 / sc, color: accent, alpha: 0.6, join: "round", cap: "round" });
  function drawChunk(g, i0, i1, sc) {
    const pts = liveMap.pts;
    g.clear().moveTo(pts[i0][0], pts[i0][1]);
    for (let i = i0 + 1; i <= i1; i++) g.lineTo(pts[i][0], pts[i][1]);
    g.stroke(strokeFor(sc));
  }
  function rebuildAll(sc) {
    world.removeChildren().forEach((ch) => ch.destroy());
    chunks = [];
    builtLen = 0;
    builtScale = sc;
  }

  return {
    root: c,
    update(f) {
      const pts = liveMap.pts, n = pts.length;
      hint.visible = n < 2;
      carCtx.clear();
      if (n < 2) {
        if (chunks.length) rebuildAll(0);
        if (marksKey) { marksKey = ""; for (const g of [jumpCtx, hardCtx, ringCtx, sparkCtx]) g.clear(); }
        return;
      }
      const spanX = Math.max(liveMap.maxX - liveMap.minX, 1), spanZ = Math.max(liveMap.maxZ - liveMap.minZ, 1);
      const sc = Math.min((B.w - 2 * pad) / spanX, (B.h - pad - padTop) / spanZ);
      // world (x, z) -> box: x right, z up (same handedness as the classic map)
      world.scale.set(sc, -sc);
      world.position.set(B.x + (B.w - spanX * sc) / 2 - liveMap.minX * sc,
        B.y + padTop + (B.h - pad - padTop + spanZ * sc) / 2 + liveMap.minZ * sc);
      if (liveMap.gen !== builtGen || Math.abs(sc / builtScale - 1) > 0.15) {
        rebuildAll(sc);
        builtGen = liveMap.gen;
      }
      if (n !== builtLen) {
        // chunk k covers points [k*CHUNK, (k+1)*CHUNK] — sharing an end point
        // with the next, so the line has no gaps
        for (let k = Math.max(0, chunks.length - 1); k * MAP_CHUNK < n - 1; k++) {
          if (!chunks[k]) { chunks[k] = new PIXI.Graphics(); world.addChild(chunks[k]); }
          drawChunk(chunks[k], k * MAP_CHUNK, Math.min(n - 1, (k + 1) * MAP_CHUNK), builtScale);
        }
        builtLen = n;
      }
      // start point, marks + car, in box coordinates (constant size whatever the fit)
      const X = (x) => world.position.x + x * sc, Y = (z) => world.position.y - z * sc;
      const key = `${liveMap.marks}|${sc}|${world.position.x}|${world.position.y}`;
      if (key !== marksKey) {
        marksKey = key;
        for (const g of [jumpCtx, hardCtx, ringCtx, sparkCtx]) g.clear();
        for (const j of liveMap.jumps) {
          jumpGlyph(j.hard ? hardCtx : jumpCtx, X(j.x0), Y(j.z0), X(j.x1), Y(j.z1), j.hard ? ringCtx : null);
        }
        for (const [hx, hz] of liveMap.hits) sparkGlyph(sparkCtx, X(hx), Y(hz));
      }
      carCtx.circle(X(pts[0][0]), Y(pts[0][1]), 5).fill(C.good);
      if (mapActive(f) && f.session_id != null && f.pos_x != null) {
        const cx = X(f.pos_x), cy = Y(f.pos_z), hx = Math.sin(f.yaw), hz = Math.cos(f.yaw);
        carCtx.circle(cx, cy, 6).fill(C.text)
          .moveTo(cx + hx * 9, cy - hz * 9).lineTo(cx + hx * 18, cy - hz * 18)
          .stroke({ width: 3, color: C.text, cap: "round" });
      }
    },
  };
}

/* Section 2, right: the last ~12 s of throttle (green), brake (red) and
   steering (grey, centred), newest at the right edge. Redrawn only when a
   sample arrives — at most once per packet, not per display frame. */
const TRACE_BOX = { x: 800, y: 620, w: 760, h: 260 };
function makeTrace() {
  const B = TRACE_BOX, pad = 34, top = B.y + pad, bot = B.y + B.h - 14, H = bot - top;
  const c = new PIXI.Container();
  const frame = new PIXI.Graphics()
    .roundRect(B.x, B.y, B.w, B.h, 14).fill({ color: C.panel, alpha: 0.5 }).stroke({ width: 1.5, color: C.edge });
  for (const fy of [0.25, 0.5, 0.75]) frame.moveTo(B.x + 12, top + H * fy).lineTo(B.x + B.w - 12, top + H * fy);
  frame.stroke({ width: 1, color: C.edge });
  const legend = [["INPUT TRACE", C.muted], ["THROTTLE", C.good], ["BRAKE", C.bad], ["STEERING", "#93a3b8"]];
  let lx = B.x + 16;
  const labels = legend.map(([txt, col]) => {
    const t = bitmapText(txt, 18, col, 0, 0.5);
    t.position.set(lx, B.y + 18);
    lx += t.width + 22;
    return t;
  });
  const linesCtx = new PIXI.GraphicsContext();
  c.addChild(frame, ...labels, new PIXI.Graphics(linesCtx));

  let drawnN = -1;
  return {
    root: c,
    update() {
      const buf = live.strip;
      if (live.stripN === drawnN) return;
      drawnN = live.stripN;
      linesCtx.clear();
      if (buf.length < 2) return;
      const step = (B.w - 24) / STRIP_LEN, x0 = B.x + B.w - 12 - (buf.length - 1) * step;
      const y = (v) => bot - v * H;
      const series = (key, map) => {
        linesCtx.moveTo(x0, map(buf[0][key] / 100));
        for (let i = 1; i < buf.length; i++) linesCtx.lineTo(x0 + i * step, map(buf[i][key] / 100));
      };
      // areas under throttle and brake, then the lines
      for (const [key, col] of [["th", C.good], ["br", C.bad]]) {
        series(key, y);
        linesCtx.lineTo(x0 + (buf.length - 1) * step, bot).lineTo(x0, bot).fill({ color: col, alpha: 0.09 });
      }
      series("st", (v) => y(0.5 + v / 2));
      linesCtx.stroke({ width: 1.5, color: "#93a3b8" });
      series("th", y);
      linesCtx.stroke({ width: 2.5, color: C.good, join: "round" });
      series("br", y);
      linesCtx.stroke({ width: 2.5, color: C.bad, join: "round" });
    },
  };
}

/* Before the first packet: the dial idles and the speed slot says why
   nothing moves. The full setup instructions live on the Live page. */
function makeWaiting() {
  const c = new PIXI.Container();
  const a = bitmapText("WAITING FOR TELEMETRY", 30, C.text);
  a.position.set(TACH.cx, TACH.cy + TACH.r - 14);
  const b = bitmapText("SETUP STEPS ON THE LIVE PAGE", 22, C.muted);
  b.position.set(TACH.cx, TACH.cy + TACH.r + 22);
  c.addChild(a, b);
  return { root: c, update() {} };
}

/* ?debug: renderer facts + fps, for the spike's measurements. Lives in screen
   space (app.stage, unscaled), not in the design space: a letterboxed scene
   would otherwise put it on top of the shift lights. */
function makeDebug() {
  const t = bitmapText("", 22, C.muted, 0, 1);
  return {
    root: t,
    update() {
      const rr = app.renderer;
      t.position.set(12, stage.clientHeight - 8);  // bottom-left, clear of the LEDs
      t.text = `PIXI ${PIXI.VERSION} · ${rr.name} · ${rr.canvas.width}×${rr.canvas.height} px`
        + ` · res ${rr.resolution} · ${Math.round(app.ticker.FPS)} fps · update ${perf.updMs.toFixed(2)} ms`
        + ` · layout ${currentLayout} · smooth ${SMOOTH_MS} ms`;
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
  const f = live.frame;
  const stale = syncConn();
  waiting.root.visible = !f;
  // a stalled stream (game lost focus) dims the cluster instead of freezing
  // it at full brightness, which reads as live
  root.alpha = f && stale ? 0.45 : 1;
  const frame = f || IDLE_FRAME;
  const raceTarget = frame.race_mode ? 1 : 0;
  const step = t.deltaMS / RACE_EASE_MS;
  raceP = reducedMotion.matches ? raceTarget
    : raceTarget > raceP ? Math.min(raceTarget, raceP + step)
      : Math.max(raceTarget, raceP - step);
  const t0 = performance.now();
  for (const c of components) if (c.slot.visible) c.update(frame, t.deltaMS);
  // exponentially averaged per-frame update cost (JS only: excludes the GPU)
  perf.updMs += (performance.now() - t0 - perf.updMs) * 0.05;
}
const perf = { updMs: 0 };

/* ---------- sizing ---------- */

/* Where each slot goes, per layout, as an offset from where its component
   draws itself (a slot not listed is hidden — and not updated). "tall"
   re-stacks the cluster for portrait screens: dial, then timing, then delta,
   then friction circle beside the tire car, then map, then trace. */
const SECTION1 = ["cluster", "timing", "delta", "friction", "tires"];
const WIDE = Object.fromEntries([...SECTION1, "map", "trace"].map((k) => [k, [0, 0]]));
const TALL = {
  cluster: [-400, 0], timing: [0, 540], delta: [-935, 820],
  friction: [0, 764], tires: [-720, 764], map: [0, 770], trace: [-780, 1050],
};
const pick = (o, keys) => Object.fromEntries(keys.map((k) => [k, o[k]]));
const LAYOUTS = [  // in order of preference
  { name: "wide", w: 1600, h: 900, slots: WIDE },
  { name: "tall", w: 800, h: 1950, slots: TALL },
  { name: "wide-compact", w: 1600, h: 600, slots: pick(WIDE, SECTION1) },
  { name: "tall-compact", w: 800, h: 1370, slots: pick(TALL, SECTION1) },
];
// below this scale 22 px design text drops under ~11 px: such a layout is
// only used when nothing else is readable either
const MIN_SCALE = 0.5;
let currentLayout = null;

/* Our own handler instead of Pixi's `resizeTo`: that one never re-reads
   devicePixelRatio (browser zoom / another monitor, issue #74) and isn't
   frame-throttled (issue #71). */
function layout() {
  const w = Math.max(1, stage.clientWidth), h = Math.max(1, stage.clientHeight);
  app.renderer.resize(w, h, window.devicePixelRatio || 1);
  const fit = (L) => Math.min(w / L.w, h / L.h);
  // The map + trace are worth keeping whenever they fit readably, so the
  // full layouts are tried first and the compact ones only after. Within a
  // tier, wide vs tall goes to whichever fills more of the screen (a portrait
  // tablet can just fit "wide" readably, letterboxed in a band); ties keep
  // LAYOUTS order. Nothing readable at all: the biggest text wins.
  const fill = (L) => fit(L) ** 2 * L.w * L.h / (w * h);
  const best = (tier) => tier.reduce((a, b) => (fill(b) > fill(a) + 0.02 ? b : a));
  const readable = (full) => LAYOUTS.filter((l) => fit(l) >= MIN_SCALE && ("map" in l.slots) === full);
  const tier = readable(true).length ? readable(true) : readable(false);
  const L = tier.length ? best(tier) : LAYOUTS.reduce((a, b) => (fit(b) > fit(a) ? b : a));
  currentLayout = L.name;
  for (const c of components) {
    if (!c.slotName) continue;
    const off = L.slots[c.slotName];
    c.slot.visible = !!off;
    if (off) c.slot.position.set(off[0], off[1]);
  }
  const s = fit(L);
  root.scale.set(s);
  root.position.set((w - L.w * s) / 2, (h - L.h * s) / 2);
}

/* ---------- accessibility ---------- */

/* The scene is one canvas: say what it shows, at a reading pace rather than
   per frame (a per-frame aria-label is noise for a screen reader). */
function describe() {
  const f = live.frame;
  stage.setAttribute("aria-label", !f
    ? "Cockpit — waiting for telemetry"
    : `Cockpit — ${Math.round(speedFromMps(Math.max(0, f.speed)))} ${speedUnit()},`
      + ` gear ${f.gear === 0 ? "reverse" : f.gear === 11 ? "neutral" : f.gear},`
      + ` ${Math.round(f.current_engine_rpm)} of ${Math.round(f.engine_max_rpm)} rpm,`
      + ` throttle ${Math.round(f.accel / 2.55)}%, brake ${Math.round(f.brake / 2.55)}%`
      + (f.race_mode
        ? `; race mode, lap ${f.lap_number + 1}, position ${f.race_position || "unknown"},`
          + ` lap time ${fmtLap(f.current_lap || f.lap_elapsed)}`
          + (f.delta == null ? "" : `, ${Math.abs(f.delta).toFixed(2)} s ${f.delta >= 0 ? "behind" : "ahead of"} session best`)
        : "; free roam"));
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
    stage.setAttribute("aria-label", "Cockpit unavailable — no renderer could start");
    return;
  }
  stage.appendChild(app.canvas);
  await installFonts();

  root = new PIXI.Container();
  app.stage.addChild(root);
  waiting = makeWaiting();
  const slots = {};
  const place = (slotName, c) => {
    if (!slots[slotName]) root.addChild(slots[slotName] = new PIXI.Container());
    c.slotName = slotName;
    c.slot = slots[slotName];
    c.slot.addChild(c.root);
    components.push(c);
  };
  for (const c of [makeShiftLights(), makeInputs(), makeTach(), waiting]) place("cluster", c);
  place("timing", makeTiming());
  place("delta", makeDelta());
  place("friction", makeFriction());
  place("tires", makeTires());
  place("map", makeMap());
  place("trace", makeTrace());
  if (DEBUG) {
    const d = makeDebug();
    d.slot = d.root;  // screen space, always visible
    components.push(d);
    app.stage.addChild(d.root);
  }

  layout();
  window.addEventListener("resize", rafThrottle(layout));
  app.ticker.add(tick);
  setVisibleInterval(describe, 1000);
  describe();

  onSettingsChange(() => {
    accent = accentDef().accent;
  });
  connect();
}

boot();
