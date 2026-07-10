/* Canvas drawing for the live dashboard: RPM arc, friction circle,
   tire grip diagram, input strip chart. All draw* functions are pure
   renders of the state passed in. */

function initCanvas(id, cssW, cssH) {
  const c = document.getElementById(id);
  // read per call, not once at module load: browser zoom / moving to a
  // different-DPI monitor changes devicePixelRatio, and the resize re-init
  // must pick up the new value or every canvas renders blurry
  const dpr = window.devicePixelRatio || 1;
  c.width = cssW * dpr;
  c.height = cssH * dpr;
  c.style.width = cssW + "px";
  c.style.height = cssH + "px";
  const ctx = c.getContext("2d");
  ctx.scale(dpr, dpr);
  return { ctx, w: cssW, h: cssH };
}

const COL = {
  muted: "#8494a7", text: "#e8eef6", accent: "#00d4ff",
  good: "#2fe6a8", warn: "#ffbe3d", bad: "#ff5d5d",
  grid: "#1c2634", dim: "#3a475c99",
};
const FONT = "Rajdhani, 'Segoe UI', sans-serif";

/* slip 0 -> green, ~1 -> amber, >1.15 -> red */
function slipColor(s) {
  const hue = Math.max(0, Math.min(120, 120 * (1.15 - s) / 1.15));
  return `hsl(${hue}, 75%, 55%)`;
}

function glow(ctx, color, blur, fn) {
  ctx.save();
  ctx.shadowColor = color;
  ctx.shadowBlur = blur;
  fn();
  ctx.restore();
}

function drawRpm(g, rpm, maxRpm, idleRpm, gear) {
  const { ctx, w, h } = g;
  ctx.clearRect(0, 0, w, h);
  // arc spans 135°..405°; keep the whole stroke inside the canvas so the top
  // of the dial is never clipped under the shift lights (lowest point: cy + r·sin45°)
  const pad = 13;
  const r = Math.min(w / 2 - pad, (h - 2 * pad) / (1 + Math.SQRT1_2));
  const cx = w / 2, cy = pad + r;
  const a0 = Math.PI * 0.75, a1 = Math.PI * 2.25;
  const max = maxRpm > 0 ? maxRpm : 8000;
  const frac = Math.max(0, Math.min(1, rpm / max));
  const redFrac = 0.9;

  ctx.lineWidth = 13;
  ctx.lineCap = "round";
  // track + redline zone
  ctx.strokeStyle = "#151d29";
  ctx.beginPath(); ctx.arc(cx, cy, r, a0, a1); ctx.stroke();
  ctx.strokeStyle = "#4b1f26";
  ctx.beginPath(); ctx.arc(cx, cy, r, a0 + (a1 - a0) * redFrac, a1); ctx.stroke();
  // value arc with glow
  if (frac > 0.005) {
    const col = frac > redFrac ? COL.bad : (frac > 0.78 ? COL.warn : COL.accent);
    glow(ctx, col, 14, () => {
      ctx.strokeStyle = col;
      ctx.beginPath(); ctx.arc(cx, cy, r, a0, a0 + (a1 - a0) * frac); ctx.stroke();
    });
  }
  // ticks each 500 rpm, numerals each 1000
  ctx.lineWidth = 1.5;
  ctx.strokeStyle = COL.muted;
  ctx.fillStyle = COL.muted;
  ctx.font = `600 11px ${FONT}`;
  ctx.textAlign = "center"; ctx.textBaseline = "middle";
  for (let v = 0; v <= max; v += 500) {
    const a = a0 + (a1 - a0) * (v / max);
    const major = v % 1000 === 0;
    ctx.globalAlpha = major ? 1 : 0.4;
    ctx.beginPath();
    ctx.moveTo(cx + Math.cos(a) * (r - 13), cy + Math.sin(a) * (r - 13));
    ctx.lineTo(cx + Math.cos(a) * (r - (major ? 20 : 17)), cy + Math.sin(a) * (r - (major ? 20 : 17)));
    ctx.stroke();
    ctx.globalAlpha = 1;
    if (major)
      ctx.fillText(String(v / 1000), cx + Math.cos(a) * (r - 32), cy + Math.sin(a) * (r - 32));
  }
  // gear
  const gearTxt = gear === 0 ? "R" : gear === 11 ? "N" : String(gear);
  ctx.fillStyle = frac > redFrac ? COL.bad : COL.accent;
  ctx.font = `700 54px ${FONT}`;
  glow(ctx, ctx.fillStyle, 18, () => ctx.fillText(gearTxt, cx, cy - 8));
  ctx.fillStyle = COL.muted;
  ctx.font = `600 13px ${FONT}`;
  ctx.fillText(`${Math.round(rpm)} RPM`, cx, cy + 28);
}

function drawFriction(g, trail, latG, lonG) {
  const { ctx, w, h } = g;
  ctx.clearRect(0, 0, w, h);
  const cx = w / 2, cy = h / 2;
  const maxG = 1.6, scale = (Math.min(w, h) / 2 - 16) / maxG;

  // soft field inside the outer ring
  const grad = ctx.createRadialGradient(cx, cy, 0, cx, cy, 1.5 * scale);
  grad.addColorStop(0, "rgba(0, 212, 255, 0.05)");
  grad.addColorStop(1, "rgba(0, 212, 255, 0)");
  ctx.fillStyle = grad;
  ctx.beginPath(); ctx.arc(cx, cy, 1.5 * scale, 0, Math.PI * 2); ctx.fill();

  ctx.strokeStyle = COL.grid;
  ctx.fillStyle = COL.muted;
  ctx.font = `600 10px ${FONT}`;
  ctx.textAlign = "center";
  ctx.lineWidth = 1;
  for (const ring of [0.5, 1.0, 1.5]) {
    ctx.beginPath(); ctx.arc(cx, cy, ring * scale, 0, Math.PI * 2); ctx.stroke();
    ctx.fillText(ring.toFixed(1), cx + ring * scale - 1, cy - 4);
  }
  ctx.beginPath(); ctx.moveTo(cx - maxG * scale, cy); ctx.lineTo(cx + maxG * scale, cy); ctx.stroke();
  ctx.beginPath(); ctx.moveTo(cx, cy - maxG * scale); ctx.lineTo(cx, cy + maxG * scale); ctx.stroke();
  ctx.fillText("BRAKE", cx, cy + maxG * scale + 10);
  ctx.fillText("ACCEL", cx, cy - maxG * scale - 4);

  // trail: connected line, fading toward the oldest point
  if (trail.length > 1) {
    ctx.lineWidth = 2;
    ctx.lineCap = "round";
    for (let i = 1; i < trail.length; i++) {
      const alpha = (i / trail.length) * 0.5;
      ctx.strokeStyle = `rgba(0, 212, 255, ${alpha.toFixed(3)})`;
      ctx.beginPath();
      ctx.moveTo(cx + trail[i - 1][0] * scale, cy - trail[i - 1][1] * scale);
      ctx.lineTo(cx + trail[i][0] * scale, cy - trail[i][1] * scale);
      ctx.stroke();
    }
  }
  // current
  const gTot = Math.hypot(latG, lonG);
  const dotCol = gTot > 1.1 ? COL.warn : COL.text;
  glow(ctx, dotCol, 10, () => {
    ctx.fillStyle = dotCol;
    ctx.beginPath();
    ctx.arc(cx + latG * scale, cy - lonG * scale, 5, 0, Math.PI * 2);
    ctx.fill();
  });
}

// tempFmt maps a raw Fahrenheit tire temp to its display string (the caller
// applies the user's °C/°F unit); the hot/cold color thresholds stay in
// Fahrenheit because that's what the packet always carries.
function drawGrip(g, slip, temps, susp, tempFmt = (t) => `${Math.round(t)}°`) {
  const { ctx, w, h } = g;
  ctx.clearRect(0, 0, w, h);
  const carW = w * 0.32, carH = h * 0.62;
  const cx = w / 2, cy = h / 2 - 6;

  // car silhouette: body + cabin
  ctx.strokeStyle = COL.dim;
  ctx.lineWidth = 1.5;
  roundRect(ctx, cx - carW / 2, cy - carH / 2, carW, carH, 16);
  ctx.stroke();
  ctx.strokeStyle = "#2a3546";
  roundRect(ctx, cx - carW * 0.32, cy - carH * 0.18, carW * 0.64, carH * 0.42, 8);
  ctx.stroke();

  const tw = 27, th = 44;
  const px = carW / 2 + 24, py = carH / 2 - 13;
  const pos = [ // FL FR RL RR
    [cx - px, cy - py], [cx + px, cy - py],
    [cx - px, cy + py], [cx + px, cy + py],
  ];
  const labels = ["FL", "FR", "RL", "RR"];
  ctx.textAlign = "center";
  for (let i = 0; i < 4; i++) {
    const [x, y] = pos[i];
    const s = slip[i];
    const col = slipColor(s);
    const paint = () => {
      ctx.fillStyle = col;
      roundRect(ctx, x - tw / 2, y - th / 2, tw, th, 7);
      ctx.fill();
    };
    if (s > 1.0) glow(ctx, col, 13, paint); else paint();
    ctx.fillStyle = "#0b0e13";
    ctx.font = `700 12px ${FONT}`;
    ctx.textBaseline = "middle";
    ctx.fillText(s.toFixed(1), x, y + 1);
    ctx.fillStyle = COL.muted;
    ctx.font = `600 10px ${FONT}`;
    ctx.fillText(labels[i], x, y - th / 2 - 10);
    const tF = temps[i];
    ctx.fillStyle = tF < 160 ? "#7fb2ff" : tF > 230 ? "#ff8a5d" : COL.text;
    ctx.font = `600 11px ${FONT}`;
    ctx.fillText(tempFmt(tF), x, y + th / 2 + 11);

    // spring compression: outer-side bar, filled bottom-up (1 = bottomed out)
    if (susp) {
      const c = Math.max(0, Math.min(1, susp[i]));
      const bw = 6, side = i % 2 === 0 ? -1 : 1;
      const bx = x + side * (tw / 2 + 10) - bw / 2;
      ctx.fillStyle = "#0a0e14";
      ctx.strokeStyle = COL.grid;
      ctx.lineWidth = 1;
      roundRect(ctx, bx, y - th / 2, bw, th, 3);
      ctx.fill(); ctx.stroke();
      ctx.fillStyle = c > 0.95 ? COL.bad : c > 0.82 ? COL.warn : COL.accent;
      const fh = Math.max(1.5, th * c);
      roundRect(ctx, bx, y - th / 2 + (th - fh), bw, fh, 3);
      ctx.fill();
    }
  }
}

function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

/* Live track map: the path driven so far this event, plus the car.
   pts: [[worldX, worldZ], ...] (teleports/new sessions reset the whole
   path upstream); ext: {minX, maxX, minZ, maxZ, hits, jumps}; car:
   {x, z, hx, hz} or null (hx/hz = world heading unit vector, from yaw). */
function drawLiveMap(g, pts, ext, car) {
  const { ctx, w, h } = g;
  ctx.clearRect(0, 0, w, h);
  if (pts.length < 2) {
    ctx.fillStyle = COL.muted;
    ctx.font = `600 13px ${FONT}`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText("waiting for a race — the track draws here once an event starts", w / 2, h / 2);
    return;
  }
  const pad = 18;
  const spanX = Math.max(ext.maxX - ext.minX, 1e-6);
  const spanZ = Math.max(ext.maxZ - ext.minZ, 1e-6);
  const scale = Math.min((w - 2 * pad) / spanX, (h - 2 * pad) / spanZ);
  // screen (x, -z): same handedness as the analysis page's 2D map
  const X = (x) => (x - ext.minX) * scale + (w - spanX * scale) / 2;
  const Y = (z) => (ext.maxZ - z) * scale + (h - spanZ * scale) / 2;

  ctx.strokeStyle = "rgba(0, 212, 255, 0.55)";
  ctx.lineWidth = 2;
  ctx.lineJoin = "round";
  ctx.beginPath();
  ctx.moveTo(X(pts[0][0]), Y(pts[0][1]));
  for (let i = 1; i < pts.length; i++) ctx.lineTo(X(pts[i][0]), Y(pts[i][1]));
  ctx.stroke();

  // where the recording began
  ctx.fillStyle = COL.good;
  ctx.beginPath();
  ctx.arc(X(pts[0][0]), Y(pts[0][1]), 4, 0, Math.PI * 2);
  ctx.fill();

  // jump flights (takeoff -> touchdown) with the shared glyph from common.js
  if (ext.jumps) {
    for (const j of ext.jumps)
      drawJump(ctx, X(j.x0), Y(j.z0), X(j.x1), Y(j.z1), { hard: j.hard });
  }

  // collision points (contact spikes) as red sparks, under the car marker
  if (ext.hits && ext.hits.length) {
    for (const h of ext.hits) {
      const hx = X(h[0]), hy = Y(h[1]);
      glow(ctx, "#ef4444", 8, () => {
        ctx.fillStyle = "#ef4444";
        ctx.strokeStyle = "#fff";
        ctx.lineWidth = 1.2;
        ctx.beginPath();
        for (let k = 0; k < 8; k++) {
          const a = (k * Math.PI) / 4;
          const rr = k % 2 ? 2.4 : 6;
          const px = hx + Math.cos(a) * rr, py = hy + Math.sin(a) * rr;
          k === 0 ? ctx.moveTo(px, py) : ctx.lineTo(px, py);
        }
        ctx.closePath();
        ctx.fill();
        ctx.stroke();
      });
    }
  }

  if (car) {
    const cx = X(car.x), cy = Y(car.z);
    glow(ctx, COL.accent, 12, () => {
      ctx.fillStyle = COL.text;
      ctx.beginPath();
      ctx.arc(cx, cy, 5, 0, Math.PI * 2);
      ctx.fill();
    });
    // heading tick from yaw (screen y grows toward -z)
    ctx.strokeStyle = COL.text;
    ctx.lineWidth = 2;
    ctx.lineCap = "round";
    ctx.beginPath();
    ctx.moveTo(cx + car.hx * 7, cy - car.hz * 7);
    ctx.lineTo(cx + car.hx * 14, cy - car.hz * 14);
    ctx.stroke();
  }
}

/* buf: array of {th (0-100), br (0-100), st (-100..100)} */
function drawStrip(g, buf, capacity) {
  const { ctx, w, h } = g;
  ctx.clearRect(0, 0, w, h);
  ctx.strokeStyle = COL.grid;
  ctx.lineWidth = 1;
  for (const fy of [0.25, 0.5, 0.75]) {
    ctx.beginPath(); ctx.moveTo(0, h * fy); ctx.lineTo(w, h * fy); ctx.stroke();
  }
  if (buf.length < 2) return;
  const pad = 4;
  const x = (i) => w - (buf.length - 1 - i) * (w / capacity);
  const yFromPct = (p) => h - pad - (p / 100) * (h - 2 * pad);

  // area fills under throttle and brake, then crisp lines on top
  const area = (get, fill) => {
    ctx.fillStyle = fill;
    ctx.beginPath();
    ctx.moveTo(x(0), h - pad);
    for (let i = 0; i < buf.length; i++) ctx.lineTo(x(i), yFromPct(get(buf[i])));
    ctx.lineTo(x(buf.length - 1), h - pad);
    ctx.closePath();
    ctx.fill();
  };
  const line = (get, color, width) => {
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    ctx.beginPath();
    for (let i = 0; i < buf.length; i++) {
      const y = get(buf[i]);
      i === 0 ? ctx.moveTo(x(i), y) : ctx.lineTo(x(i), y);
    }
    ctx.stroke();
  };
  area((s) => s.th, "rgba(47, 230, 168, 0.09)");
  area((s) => s.br, "rgba(255, 93, 93, 0.09)");
  line((s) => yFromPct(50 + s.st / 2), "#93a3b8", 1);           // steering, centered
  line((s) => yFromPct(s.th), COL.good, 1.8);                   // throttle
  line((s) => yFromPct(s.br), COL.bad, 1.8);                    // brake
}
