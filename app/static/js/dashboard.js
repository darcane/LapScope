/* Live dashboard: WebSocket feed -> canvas gauges at display refresh rate. */

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
window.addEventListener("resize", initCanvases);

const STRIP_CAP = 12 * 60; // ~12 s at 60 Hz
const state = {
  frame: null,
  lastMsg: 0,
  trail: [],       // [latG, lonG] history for friction circle
  strip: [],       // input history
};

const $ = (id) => document.getElementById(id);
const shiftLights = document.querySelectorAll("#shift-lights i");

/* live track map: path of the current session, thinned adaptively so long
   drives stay cheap to redraw at display refresh rate */
const LIVEMAP_CAP = 4000;
// ground-plane acceleration (m/s^2) that counts as a contact/collision, plus
// the airborne/jump-landing discrimination (a spike while airborne or right
// after touchdown is a landing, not contact); mirrors IMPACT_ACCEL /
// AIRBORNE_* / LANDING_GRACE_S in app/recorder/laps.py (keep in lockstep).
const IMPACT_ACCEL = 45;
const AIRBORNE_SUSP_MAX = 0.15;
const AIRBORNE_SLIP_MAX = 0.05;
const AIRBORNE_MIN_S = 0.12;
const LANDING_GRACE_S = 0.35;
const liveMap = {
  pts: [],         // [x, z] world points
  last: null,      // last stored point
  minDist: 3,      // m between stored points; doubles when thinned
  session: null,
  minX: Infinity, maxX: -Infinity, minZ: Infinity, maxZ: -Infinity,
  hits: [],        // [x, z] world points where a contact spike fired
  jumps: [],       // {x0, z0, x1, z1, hard} takeoff -> touchdown segments
  overImpact: false, // above the threshold last frame (edge-detect one hit/impact)
  airSince: null,  // when the current all-wheels-unloaded stretch began
  airStart: null,  // [x, z] where that stretch began (the takeoff point)
  graceUntil: 0,   // spikes before this time are jump landings, not contact
};

function resetLiveMap(sessionId) {
  liveMap.session = sessionId;
  liveMap.pts = [];
  liveMap.last = null;
  liveMap.minDist = 3;
  liveMap.minX = liveMap.minZ = Infinity;
  liveMap.maxX = liveMap.maxZ = -Infinity;
  liveMap.hits = [];
  liveMap.jumps = [];
  liveMap.overImpact = false;
  liveMap.airSince = null;
  liveMap.airStart = null;
  liveMap.graceUntil = 0;
}

// One marker per impact: register on the rising edge only, so grinding a wall
// (many frames over the threshold) leaves a single dot. Gated exactly like the
// map path — races/time-attacks only, same session — and runs after
// feedLiveMap so a session change / grid snap has already cleared old hits.
function mapActive(f) {
  // races/time-attacks always; free roam only when the user opts in
  return f.race_mode || getSettings().freeroamMap;
}

function feedCollision(f) {
  if (f.session_id == null || !mapActive(f) || f.session_id !== liveMap.session) {
    liveMap.overImpact = false;
    liveMap.airSince = null;
    liveMap.airStart = null;
    return;
  }
  const t = f._t;
  const airborne = f.norm_susp_travel.every((s) => s < AIRBORNE_SUSP_MAX)
    && f.tire_combined_slip.every((s) => s < AIRBORNE_SLIP_MAX);
  if (airborne) {
    if (liveMap.airSince == null) {
      liveMap.airSince = t;
      liveMap.airStart = [f.pos_x, f.pos_z];
    }
  } else {
    if (liveMap.airSince != null && t - liveMap.airSince >= AIRBORNE_MIN_S) {
      liveMap.graceUntil = t + LANDING_GRACE_S;
      // a real flight just ended: this frame is the touchdown
      liveMap.jumps.push({ x0: liveMap.airStart[0], z0: liveMap.airStart[1],
                           x1: f.pos_x, z1: f.pos_z, hard: false });
    }
    liveMap.airSince = null;
  }
  const flying = liveMap.airSince != null && t - liveMap.airSince >= AIRBORNE_MIN_S;
  if (Math.hypot(f.accel_x, f.accel_z) >= IMPACT_ACCEL) {
    if (!liveMap.overImpact) {
      if (flying || t < liveMap.graceUntil) {
        // the landing of a jump, not contact: mark its glyph hard, no spark
        const j = liveMap.jumps[liveMap.jumps.length - 1];
        if (j) j.hard = true;
      } else {
        liveMap.hits.push([f.pos_x, f.pos_z]);
      }
    }
    liveMap.overImpact = true;
  } else {
    liveMap.overImpact = false;
  }
}

function feedLiveMap(f) {
  // only draw during an actual event (race / time attack / point-to-point):
  // IsRaceOn is 1 in free roam too, so fast-travel sprawl would wreck the
  // map - race_mode comes from the recorder, which knows the difference.
  // The finished track stays on screen until the next event starts.
  if (f.session_id == null || !mapActive(f)) return;
  if (f.session_id !== liveMap.session) {
    // A pause longer than the recorder's grace (photo mode, a long sit in the
    // pause menu) splits the recording into a new session id, but the race
    // itself resumes exactly where it stopped. Keep drawing in that case:
    // same place (within the 250 m teleport rule below) AND the race clock
    // kept its value - a restart or a genuinely new event resets it to ~0,
    // which is the very signal the recorder splits sessions on.
    const resumed = liveMap.last != null
      && Math.hypot(f.pos_x - liveMap.last[0], f.pos_z - liveMap.last[1]) < 250
      && f.current_race_time > 5;
    if (resumed) liveMap.session = f.session_id;
    else resetLiveMap(f.session_id); // new event -> fresh track
  }
  const x = f.pos_x, z = f.pos_z;
  if (liveMap.last) {
    const jump = Math.hypot(x - liveMap.last[0], z - liveMap.last[1]);
    if (jump < liveMap.minDist) return;
    // a car can't move 250 m in one frame: that's a grid snap / event
    // restart. Start the track fresh from here - keeping the old points
    // would wreck the scale (the bounds span both places) and overlay
    // two different pieces of world on one map.
    if (jump > 250) resetLiveMap(f.session_id);
  }
  liveMap.last = [x, z];
  liveMap.pts.push(liveMap.last);
  liveMap.minX = Math.min(liveMap.minX, x); liveMap.maxX = Math.max(liveMap.maxX, x);
  liveMap.minZ = Math.min(liveMap.minZ, z); liveMap.maxZ = Math.max(liveMap.maxZ, z);
  if (liveMap.pts.length > LIVEMAP_CAP) { // free roam can sprawl: thin + relax
    liveMap.pts = liveMap.pts.filter((_, i) => i % 2 === 0);
    liveMap.minDist *= 2;
  }
}

function fmtLap(s) {
  if (!s || s <= 0) return "–:--.---";
  const m = Math.floor(s / 60);
  return `${m}:${(s - m * 60).toFixed(3).padStart(6, "0")}`;
}

function connect() {
  const ws = new WebSocket(`ws://${location.host}/ws/live`);
  ws.onmessage = (ev) => {
    const f = JSON.parse(ev.data);
    state.frame = f;
    state.lastMsg = performance.now();
    if (f.is_race_on) {
      state.trail.push([f.accel_x / 9.80665, f.accel_z / 9.80665]);
      if (state.trail.length > 90) state.trail.shift();
      state.strip.push({ th: f.accel / 2.55, br: f.brake / 2.55, st: f.steer / 1.27 });
      if (state.strip.length > STRIP_CAP) state.strip.shift();
    }
    feedLiveMap(f);
    feedCollision(f);
  };
  ws.onopen = () => setConn("live", "ok");
  ws.onclose = () => { setConn("reconnecting…", "err"); setTimeout(connect, 1500); };
  ws.onerror = () => ws.close();
}

function setConn(text, cls) {
  const el = $("conn");
  el.querySelector("span").textContent = text;
  el.className = `chip ${cls}`;
}

// toggling the free-roam-map setting off mid-drive: drop the accumulated path
// so it doesn't linger on screen (races clear it on their own on session change)
onSettingsChange(() => {
  if (!getSettings().freeroamMap) resetLiveMap(liveMap.session);
  syncRawPanel();
});

/* no-data overlay: refresh server stats while visible */
async function pollStatus() {
  try {
    const st = await (await fetch("/api/status")).json();
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
setInterval(() => { if (!$("nodata").classList.contains("hidden")) pollStatus(); }, 2000);
pollStatus();

let chipOrdinal = null;
async function updateCarChip(f) {
  if (f.car_ordinal === chipOrdinal) return;
  chipOrdinal = f.car_ordinal;
  let name = `Car #${f.car_ordinal}`, known = false;
  try {
    const info = await (await fetch(`/api/cars/${f.car_ordinal}`)).json();
    name = info.name;
    known = info.known;
  } catch { }
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

function render() {
  requestAnimationFrame(render);
  const f = state.frame;
  const stale = performance.now() - state.lastMsg > 2500;
  $("nodata").classList.toggle("hidden", !(stale || !f));
  if (!f) return;

  updateCarChip(f);
  drawRpm(rpmG, f.current_engine_rpm, f.engine_max_rpm, f.engine_idle_rpm, f.gear);
  drawFriction(fricG, state.trail, f.accel_x / 9.80665, f.accel_z / 9.80665);
  drawGrip(gripG, f.tire_combined_slip, f.tire_temp, f.norm_susp_travel, fmtTireTemp);
  drawStrip(stripG, state.strip, STRIP_CAP);
  // heading from yaw: the packet's Velocity is car-local (always "forward"),
  // yaw is world-space - the car moves along (sin yaw, cos yaw)
  drawLiveMap(liveMapG, liveMap.pts, liveMap,
    mapActive(f) && f.session_id != null
      ? { x: f.pos_x, z: f.pos_z, hx: Math.sin(f.yaw), hz: Math.cos(f.yaw) } : null);
  updateShiftLights(f.engine_max_rpm > 0 ? f.current_engine_rpm / f.engine_max_rpm : 0);

  const v = speedFromMps(f.speed);
  $("speed").textContent = Math.round(Math.max(0, v));
  $("speed-unit").textContent = speedUnit();
  $("power").textContent = Math.max(0, f.power / 1000).toFixed(0);
  $("boost").textContent = Math.max(0, f.boost).toFixed(1);

  const latG = f.accel_x / 9.80665, lonG = f.accel_z / 9.80665;
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
}

initRawPanel();
syncRawPanel();
connect();
render();
