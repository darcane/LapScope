/* Classic live dashboard (classic.html): the shared feed (live.js) -> canvas
   gauges at display refresh rate. Replaced by the cockpit (index.html,
   cockpit.js). Removed with gauges.js or kept as a Lite view: issue #101. */

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

watchStatus();

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
  syncNoData(f);  // first-run setup steps (live-ui.js)
  syncConn();
  if (!f) return;

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

  updateLiveChrome(f);
  describeCanvases(f);
}

initRawPanel();
syncRawPanel();
connect();
render();
