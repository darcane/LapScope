/* Live dashboard: WebSocket feed -> canvas gauges at display refresh rate. */

const rpmG = initCanvas("rpm", 290, 250);
const fricG = initCanvas("friction", 250, 240);
const gripG = initCanvas("grip", 230, 240);
let stripG = initCanvas("strip", document.getElementById("strip").parentElement.clientWidth - 34, 280);
let liveMapG = initCanvas("livemap", document.getElementById("livemap").parentElement.clientWidth - 34, 280);
window.addEventListener("resize", () => {
  stripG = initCanvas("strip", document.getElementById("strip").parentElement.clientWidth - 34, 280);
  liveMapG = initCanvas("livemap", document.getElementById("livemap").parentElement.clientWidth - 34, 280);
});

const STRIP_CAP = 12 * 60; // ~12 s at 60 Hz
const state = {
  frame: null,
  lastMsg: 0,
  trail: [],       // [latG, lonG] history for friction circle
  strip: [],       // input history
  mph: localStorage.getItem("fc_mph") === "1",
};

const $ = (id) => document.getElementById(id);
const shiftLights = document.querySelectorAll("#shift-lights i");

/* live track map: path of the current session, thinned adaptively so long
   drives stay cheap to redraw at display refresh rate */
const LIVEMAP_CAP = 4000;
const liveMap = {
  pts: [],         // [x, z] world points; null = teleport break
  last: null,      // last stored point (skips the nulls)
  minDist: 3,      // m between stored points; doubles when thinned
  session: null,
  minX: Infinity, maxX: -Infinity, minZ: Infinity, maxZ: -Infinity,
};

function resetLiveMap(sessionId) {
  liveMap.session = sessionId;
  liveMap.pts = [];
  liveMap.last = null;
  liveMap.minDist = 3;
  liveMap.minX = liveMap.minZ = Infinity;
  liveMap.maxX = liveMap.maxZ = -Infinity;
}

function feedLiveMap(f) {
  // only draw during an actual event (race / time attack / point-to-point):
  // IsRaceOn is 1 in free roam too, so fast-travel sprawl would wreck the
  // map - race_mode comes from the recorder, which knows the difference.
  // The finished track stays on screen until the next event starts.
  if (f.session_id == null || !f.race_mode) return;
  if (f.session_id !== liveMap.session) resetLiveMap(f.session_id); // new session -> fresh track
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
    liveMap.pts = liveMap.pts.filter((p, i) => p === null || i % 2 === 0);
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

$("unit-toggle").textContent = state.mph ? "mph" : "km/h";
$("unit-toggle").onclick = () => {
  state.mph = !state.mph;
  localStorage.setItem("fc_mph", state.mph ? "1" : "0");
  $("unit-toggle").textContent = state.mph ? "mph" : "km/h";
};

/* no-data overlay: refresh server stats while visible */
async function pollStatus() {
  try {
    const st = await (await fetch("/api/status")).json();
    $("nd-port").textContent = st.udp_port;
    $("nd-stat").textContent =
      st.packets_total === 0
        ? `server: no packets received yet on UDP ${st.udp_port}` +
          (st.bad_packets ? ` (${st.bad_packets} wrong-size packets!)` : "")
        : `server: ${st.packets_total} packets received, last ${st.last_packet_age}s ago`;
  } catch { /* server briefly unavailable */ }
}
setInterval(() => { if (!$("nodata").classList.contains("hidden")) pollStatus(); }, 2000);
pollStatus();

let chipOrdinal = null;
async function updateCarChip(f) {
  if (f.car_ordinal === chipOrdinal) return;
  chipOrdinal = f.car_ordinal;
  let name = `Car #${f.car_ordinal}`;
  try { name = (await (await fetch(`/api/cars/${f.car_ordinal}`)).json()).name; } catch { }
  const chip = $("car-chip");
  chip.innerHTML = `${classBadge(CLASS_LETTERS[f.car_class] || "?", f.car_pi)}` +
    `${dtBadge(DRIVETRAINS[f.drivetrain_type] || "?")} <span class="car-nm"></span>`;
  chip.querySelector(".car-nm").textContent = name;
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

function render() {
  requestAnimationFrame(render);
  const f = state.frame;
  const stale = performance.now() - state.lastMsg > 2500;
  $("nodata").classList.toggle("hidden", !(stale || !f));
  if (!f) return;

  updateCarChip(f);
  drawRpm(rpmG, f.current_engine_rpm, f.engine_max_rpm, f.engine_idle_rpm, f.gear);
  drawFriction(fricG, state.trail, f.accel_x / 9.80665, f.accel_z / 9.80665);
  drawGrip(gripG, f.tire_combined_slip, f.tire_temp, f.norm_susp_travel);
  drawStrip(stripG, state.strip, STRIP_CAP);
  // heading from yaw: the packet's Velocity is car-local (always "forward"),
  // yaw is world-space - the car moves along (sin yaw, cos yaw)
  drawLiveMap(liveMapG, liveMap.pts, liveMap,
    f.race_mode && f.session_id != null
      ? { x: f.pos_x, z: f.pos_z, hx: Math.sin(f.yaw), hz: Math.cos(f.yaw) } : null);
  updateShiftLights(f.engine_max_rpm > 0 ? f.current_engine_rpm / f.engine_max_rpm : 0);

  const v = f.speed * (state.mph ? 2.23694 : 3.6);
  $("speed").textContent = Math.round(Math.max(0, v));
  $("speed-unit").textContent = state.mph ? "mph" : "km/h";
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
}

connect();
render();
