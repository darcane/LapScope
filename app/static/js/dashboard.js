/* Live dashboard: the shared feed (live.js) -> canvas gauges at display
   refresh rate. */

let rpmG, fricG, gripG, stripG, liveMapG;
function initCanvases() {
  // re-run in full on resize: browser zoom / a different-DPI monitor changes
  // devicePixelRatio, and every canvas needs the new backing scale, not just
  // the two whose CSS width follows the layout
  rpmG = initCanvas("rpm", 290, 250);
  fricG = initCanvas("friction", 250, 240);
  gripG = initCanvas("grip", 230, 240);
  stripG = initCanvas("strip", document.getElementById("strip").parentElement.clientWidth - 34, 280);
  liveMapG = initCanvas("livemap", document.getElementById("livemap").parentElement.clientWidth - 34, 280);
}
initCanvases();
// same reason as the analysis page: a resize drag fires far faster than five
// canvas reallocations take (issue #71)
window.addEventListener("resize", rafThrottle(initCanvases));

const $ = (id) => document.getElementById(id);
const shiftLights = document.querySelectorAll("#shift-lights i");

function fmtLap(s) {
  if (!s || s <= 0) return "–:--.---";
  const m = Math.floor(s / 60);
  return `${m}:${(s - m * 60).toFixed(3).padStart(6, "0")}`;
}

onSettingsChange(() => {
  refreshCanvasTheme(); // accent switch: gauges pick it up next rAF frame
  syncRawPanel();
});

/* The recorder can't write (disk full, database locked): packets still
   arrive and every gauge still moves, so nothing on screen would say the
   drive isn't being saved. Not dismissible — it clears itself when the
   next flush succeeds (issue #64). */
function showWriteError(st) {
  let bar = document.getElementById("rec-banner");
  if (!st.write_error) { if (bar) bar.remove(); return; }
  if (!bar) {
    bar = document.createElement("div");
    bar.id = "rec-banner";
    bar.className = "rec-banner";
    document.body.prepend(bar);
  }
  const lost = st.frames_dropped ? ` ${st.frames_dropped} frames lost so far.` : "";
  bar.textContent = `⚠ Not recording — the database write failed: ${st.write_error}.` +
    `${lost} Free up disk space; recording resumes on its own.`;
}

/* server stats: the no-data overlay's line while it is visible, and the
   write-failure banner at all times */
async function pollStatus() {
  try {
    const st = await (await fetch("/api/status")).json();
    showWriteError(st);
    if ($("nodata").classList.contains("hidden")) return;
    $("nd-port").textContent = st.udp_port;
    const stat = $("nd-stat");
    if (st.udp_error) {
      stat.textContent = st.udp_error;
      stat.classList.add("error");
    } else {
      stat.classList.remove("error");
      stat.textContent =
        st.packets_total === 0
          ? `server: no packets received yet on UDP ${st.udp_port}` +
            (st.bad_packets ? ` (${st.bad_packets} wrong-size packets!)` : "")
          : `server: ${st.packets_total} packets received, last ${st.last_packet_age}s ago`;
    }
  } catch { /* server briefly unavailable */ }
}
setVisibleInterval(pollStatus, 2000);
pollStatus();

let chipOrdinal = null;
let chipSeq = 0;  // quick car changes race their fetches: only the latest may render
async function updateCarChip(f) {
  if (f.car_ordinal === chipOrdinal) return;
  chipOrdinal = f.car_ordinal;
  const seq = ++chipSeq;
  let name = `Car #${f.car_ordinal}`, known = false;
  try {
    const info = await (await fetch(`/api/cars/${f.car_ordinal}`)).json();
    name = info.name;
    known = info.known;
  } catch { }
  if (seq !== chipSeq) return;  // a newer car's fetch superseded this one
  const chip = $("car-chip");
  chip.innerHTML = `${classBadge(CLASS_LETTERS[f.car_class] || "?", f.car_pi)}` +
    `${dtBadge(DRIVETRAINS[f.drivetrain_type] || "?")} <span class="car-nm"></span>`;
  const nm = chip.querySelector(".car-nm");
  nm.textContent = name;
  if (!known) {  // ordinal missing from the community list
    nm.classList.add("car-unknown");
    nm.title = "Unknown car — name or report it from the Analysis page";
  }
  chip.style.display = "";
}

function balanceText(f) {
  const front = (Math.abs(f.tire_slip_angle[0]) + Math.abs(f.tire_slip_angle[1])) / 2;
  const rear = (Math.abs(f.tire_slip_angle[2]) + Math.abs(f.tire_slip_angle[3])) / 2;
  if (Math.max(front, rear) < 0.5) return ["NEUTRAL", ""];
  if (front > rear * 1.2) return ["UNDERSTEER", "understeer"];
  if (rear > front * 1.2) return ["OVERSTEER", "oversteer"];
  return ["NEUTRAL", ""];
}

function updateShiftLights(frac) {
  // LEDs fill from 55% rpm to redline; all blink on the limiter
  const box = document.getElementById("shift-lights");
  box.classList.toggle("limiter", frac > 0.97);
  shiftLights.forEach((led, i) => {
    led.classList.toggle("on", frac >= 0.55 + i * 0.042);
  });
}

/* ---------- raw telemetry panel (Settings → Raw data) ----------
   Every packet field of the live WS frame, verbatim — RAW_FIELDS (common.js)
   mirrors packet.py FIELDS — plus the tracker extras the listener merges in.
   The DOM is built once; each frame rewrites only the cells whose text
   changed, so 60 Hz updates stay cheap. Hold freezes the display only —
   recording and the rest of the dashboard keep running. */
const RAW_EXTRAS = [
  ["session_id", "", 0], ["race_mode", "", 0], ["delta", "s", 3],
  ["session_best", "s", 3], ["lap_elapsed", "s", 3],
];
const rawPanel = { cells: [], held: false };

function initRawPanel() {
  const body = $("raw-body");

  const grid = () => {
    const g = document.createElement("div");
    g.className = "raw-grid";
    body.appendChild(g);
    return g;
  };
  const addCell = (holder, label, unit, get, dec) => {
    const c = document.createElement("div");
    c.className = "raw-cell";
    const lab = document.createElement("span");
    lab.textContent = label;
    if (unit) {
      const u = document.createElement("em");
      u.textContent = unit;
      lab.appendChild(u);
    }
    const val = document.createElement("b");
    c.append(lab, val);
    holder.appendChild(c);
    rawPanel.cells.push({ el: val, get, dec, txt: null });
  };

  const scalars = grid();
  for (const [name, count, unit, dec] of RAW_FIELDS) {
    if (count === 1) addCell(scalars, name, unit, (f) => f[name], dec);
  }

  // the ten wheel-group fields as one table, FL FR RL RR columns (packet order)
  const table = document.createElement("table");
  table.className = "raw-wheels";
  const head = table.insertRow();
  for (const h of ["", "FL", "FR", "RL", "RR"]) {
    const th = document.createElement("th");
    th.textContent = h;
    head.appendChild(th);
  }
  for (const [name, count, unit, dec] of RAW_FIELDS) {
    if (count !== 4) continue;
    const tr = table.insertRow();
    tr.insertCell().textContent = unit ? `${name} (${unit})` : name;
    for (let i = 0; i < 4; i++)
      rawPanel.cells.push({ el: tr.insertCell(), get: (f) => (f[name] || [])[i], dec, txt: null });
  }
  body.appendChild(table);

  // not packet data: the fields the recorder merges into the frame
  const sub = document.createElement("div");
  sub.className = "raw-sub";
  sub.textContent = "tracker extras";
  body.appendChild(sub);
  const extras = grid();
  for (const [name, unit, dec] of RAW_EXTRAS)
    addCell(extras, name, unit, (f) => f[name], dec);

  const hold = $("raw-hold");
  hold.onclick = () => {
    rawPanel.held = !rawPanel.held;
    hold.classList.toggle("on", rawPanel.held);
    hold.textContent = rawPanel.held ? "▶ Resume" : "⏸ Hold";
  };
}

function updateRawPanel(f) {
  for (const c of rawPanel.cells) {
    const txt = fmtRaw(c.get(f), c.dec);
    if (txt !== c.txt) { c.txt = txt; c.el.textContent = txt; }
  }
}

function syncRawPanel() {
  $("raw-panel").style.display = getSettings().rawLive ? "" : "none";
}

/* The two canvases whose numbers appear nowhere else on the page (speed, g,
   inputs, gear and the lap clock are all DOM text already). Refreshed at
   1 Hz onto a plain aria-label rather than a live region: a screen reader
   reads it on demand instead of announcing it 60 times a second (#70). */
let lastCanvasLabel = 0;
const WHEEL_NAMES = ["front left", "front right", "rear left", "rear right"];

function describeCanvases(f) {
  const now = performance.now();
  if (now - lastCanvasLabel < 1000) return;
  lastCanvasLabel = now;
  $("grip").setAttribute("aria-label", "Tire grip — " + WHEEL_NAMES.map(
    (w, i) => `${w}: slip ${f.tire_combined_slip[i].toFixed(2)},`
      + ` ${fmtTireTemp(f.tire_temp[i])}`).join("; "));
  $("rpm").setAttribute("aria-label",
    `${Math.round(f.current_engine_rpm)} of ${Math.round(f.engine_max_rpm)} rpm,`
    + ` gear ${f.gear === 0 ? "reverse" : f.gear === 11 ? "neutral" : f.gear}`);
}

function render() {
  requestAnimationFrame(render);
  const f = live.frame;
  // The full-screen "waiting for telemetry" overlay is for the first-run case
  // only (no frame ever). Once data has arrived, a stall must NOT re-cover the
  // page: FH6 stops Data Out whenever it loses focus, so clicking away from
  // the game to read the dashboard on a second screen would otherwise slam an
  // opaque overlay — with the wrong diagnosis — over everything (issue #69).
  // A stale-but-connected stream just flips the chip to PAUSED.
  $("nodata").classList.toggle("hidden", !!f);
  syncConn();
  if (!f) return;

  updateCarChip(f);
  drawRpm(rpmG, f.current_engine_rpm, f.engine_max_rpm, f.engine_idle_rpm, f.gear);
  drawFriction(fricG, live.trail, f.accel_x / G, f.accel_z / G);
  drawGrip(gripG, f.tire_combined_slip, f.tire_temp, f.norm_susp_travel, fmtTireTemp);
  drawStrip(stripG, live.strip, STRIP_LEN);
  // heading from yaw: the packet's Velocity is car-local (always "forward"),
  // yaw is world-space - the car moves along (sin yaw, cos yaw)
  drawLiveMap(liveMapG, liveMap.pts, liveMap,
    mapActive(f) && f.session_id != null
      ? { x: f.pos_x, z: f.pos_z, hx: Math.sin(f.yaw), hz: Math.cos(f.yaw) } : null);
  updateShiftLights(f.engine_max_rpm > 0 ? f.current_engine_rpm / f.engine_max_rpm : 0);

  const v = speedFromMps(f.speed);
  $("speed").textContent = Math.round(Math.max(0, v));
  $("speed-unit").textContent = speedUnit();
  $("power").textContent = powerFromW(Math.max(0, f.power)).toFixed(0);
  $("power-unit").textContent = powerUnit();
  $("boost").textContent = fmtBoost(Math.max(0, f.boost));
  $("boost-unit").textContent = boostUnit();

  const latG = f.accel_x / G, lonG = f.accel_z / G;
  $("latg").textContent = latG.toFixed(2);
  $("long").textContent = lonG.toFixed(2);
  $("totg").textContent = Math.hypot(latG, lonG).toFixed(2);

  const [txt, cls] = balanceText(f);
  const bal = $("balance");
  bal.textContent = txt;
  bal.className = `balance ${cls}`;

  const th = f.accel / 2.55, br = f.brake / 2.55, st = f.steer / 1.27;
  $("bar-th").style.height = th.toFixed(0) + "%";
  $("bar-br").style.height = br.toFixed(0) + "%";
  $("thr-val").textContent = th.toFixed(0);
  $("brk-val").textContent = br.toFixed(0);
  $("steer-ind").style.left = `calc(${(50 + st * 0.44).toFixed(1)}% - 9px)`;
  $("gear-mini").textContent = f.gear === 0 ? "R" : f.gear === 11 ? "N" : f.gear;

  // lap timing only means something during an event; in free roam the
  // fallback lap clock would just count up meaninglessly
  const race = !!f.race_mode;
  const flag = $("race-flag");
  flag.textContent = race ? "RACE MODE" : "FREE ROAM";
  flag.classList.toggle("on", race);
  $("lap-cur").textContent = race ? fmtLap(f.current_lap || f.lap_elapsed) : "–:--.---";
  $("lap-last").textContent = fmtLap(f.last_lap);
  $("lap-best").textContent = fmtLap(f.session_best ?? f.best_lap);
  $("lap-no").textContent = race ? `${f.lap_number + 1} / ${f.race_position || "–"}` : "– / –";

  const d = $("delta");
  if (f.delta == null) {
    d.textContent = "—";
    d.className = "";
  } else {
    d.textContent = (f.delta >= 0 ? "+" : "−") + Math.abs(f.delta).toFixed(3);
    d.className = f.delta >= 0 ? "pos" : "neg";
  }

  if (getSettings().rawLive && !rawPanel.held) updateRawPanel(f);
  describeCanvases(f);
}

initRawPanel();
syncRawPanel();
connect();
render();
