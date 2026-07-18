/* Shared UI helpers: Forza-colored class badges, track-condition ribbons,
   and the jump glyph both track maps (live + analysis) draw with. */

/* ---------- jump glyph ----------
   A jump is drawn as its flight: a dashed line from takeoff (open circle) to
   touchdown (solid arrowhead pointing along the flight). A hard landing gets
   a glow + white impact ring. Deliberately nothing like the 4-point contact
   spark: dashes + an arrow read as "airborne, going that way" at a glance. */
function drawJump(ctx, x0, y0, x1, y1, { color = "#f59e0b", hard = false, scale = 1 } = {}) {
  const len = Math.hypot(x1 - x0, y1 - y0);
  // a straight-down drop projects to a point: keep a readable arrow anyway
  const ang = len > 0.5 ? Math.atan2(y1 - y0, x1 - x0) : -Math.PI / 2;
  const r = 3.2 * scale;   // takeoff circle
  const ah = 7 * scale;    // arrowhead length
  ctx.save();
  ctx.strokeStyle = color;
  ctx.fillStyle = color;
  ctx.lineWidth = 1.8 * scale;
  if (hard) {
    ctx.shadowColor = color;
    ctx.shadowBlur = 9;
  }
  if (len > r + ah) {  // flight path, clipped so it doesn't pierce the end glyphs
    ctx.setLineDash([5 * scale, 4 * scale]);
    ctx.beginPath();
    ctx.moveTo(x0 + Math.cos(ang) * (r + 1), y0 + Math.sin(ang) * (r + 1));
    ctx.lineTo(x1 - Math.cos(ang) * ah * 0.7, y1 - Math.sin(ang) * ah * 0.7);
    ctx.stroke();
    ctx.setLineDash([]);
  }
  ctx.beginPath();  // takeoff
  ctx.arc(x0, y0, r, 0, Math.PI * 2);
  ctx.stroke();
  ctx.beginPath();  // touchdown arrowhead
  ctx.moveTo(x1 + Math.cos(ang) * ah * 0.55, y1 + Math.sin(ang) * ah * 0.55);
  ctx.lineTo(x1 + Math.cos(ang + 2.5) * ah * 0.8, y1 + Math.sin(ang + 2.5) * ah * 0.8);
  ctx.lineTo(x1 + Math.cos(ang - 2.5) * ah * 0.8, y1 + Math.sin(ang - 2.5) * ah * 0.8);
  ctx.closePath();
  ctx.fill();
  if (hard) {  // impact ring where the car slammed down
    ctx.shadowBlur = 0;
    ctx.strokeStyle = "#fff";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.arc(x1, y1, ah * 0.95, 0, Math.PI * 2);
    ctx.stroke();
  }
  ctx.restore();
}

/* FH6 CarClass indices; 6 = R (new class, 901-998 PI), 7 = X (999 only) */
const CLASS_LETTERS = ["D", "C", "B", "A", "S1", "S2", "R", "X"];

/* Forza Horizon PI badge colors */
const CLASS_COLORS = {
  D: "#41c7e0",   // light blue
  C: "#f2d21f",   // yellow
  B: "#f7941e",   // orange
  A: "#e63946",   // red
  S1: "#b750e0",  // purple
  S2: "#2f6df6",  // blue
  R: "#ff3d7f",   // magenta - new FH6 class, 901-998 PI
  X: "#37e05c",   // green
};

function classBadge(letter, pi) {
  const color = CLASS_COLORS[letter] || "#7b8794";
  return `<span class="class-badge">` +
    `<span class="cls" style="background:${color}">${letter}</span>` +
    `<span class="pi">${pi ?? "–"}</span></span>`;
}

const CONDITION_META = {
  dry: ["☀️", "Dry"],
  wet: ["🌧️", "Wet"],
  snow: ["❄️", "Snow"],
};

/* untagged sessions show no badge (instead of a misleading default) */
function condBadge(cond) {
  if (!CONDITION_META[cond]) return "";
  const [icon, label] = CONDITION_META[cond];
  return `<span class="cond-badge cond-${cond}">${icon} ${label}</span>`;
}

/* course/track type is not in the packet - the recorder auto-suggests one at
   session close (road/dirt/cross/wtc, from surface + geometry evidence) and
   the user can always override; street/touge/drag stay manual-only */
const TRACK_META = {
  road: ["🛣️", "Road"],
  street: ["🏙️", "Street"],
  touge: ["⛰️", "Touge"],
  dirt: ["🟫", "Dirt"],
  cross: ["🏞️", "Cross-Country"],
  drag: ["🏁", "Drag"],
  wtc: ["⏱️", "WTC"],
};

function trackBadge(type) {
  if (!TRACK_META[type]) return "";
  const [icon, label] = TRACK_META[type];
  return `<span class="cond-badge track-${type}">${icon} ${label}</span>`;
}

/* DrivetrainType is in every packet: 0=FWD 1=RWD 2=AWD */
const DRIVETRAINS = ["FWD", "RWD", "AWD"];

function dtBadge(dt) {
  return `<span class="dt-badge dt-${dt}">${dt}</span>`;
}

/* ---------- raw packet fields ----------
   [name, count, unit, decimals] for every FH6 Data Out field, in packet order —
   mirrors FIELDS in app/telemetry/packet.py (keep the two in lockstep; the
   backend generates its raw_* channels from that same list). count 4 = wheel
   group ordered FL FR RL RR. Units are the packet's own (m/s, °F, 0–255…):
   the raw views deliberately skip the Settings unit conversions. */
const RAW_FIELDS = [
  ["is_race_on", 1, "", 0],
  ["timestamp_ms", 1, "ms", 0],
  ["engine_max_rpm", 1, "rpm", 0],
  ["engine_idle_rpm", 1, "rpm", 0],
  ["current_engine_rpm", 1, "rpm", 1],
  ["accel_x", 1, "m/s²", 3], ["accel_y", 1, "m/s²", 3], ["accel_z", 1, "m/s²", 3],
  ["vel_x", 1, "m/s", 3], ["vel_y", 1, "m/s", 3], ["vel_z", 1, "m/s", 3],
  ["ang_vel_x", 1, "rad/s", 3], ["ang_vel_y", 1, "rad/s", 3], ["ang_vel_z", 1, "rad/s", 3],
  ["yaw", 1, "rad", 3], ["pitch", 1, "rad", 3], ["roll", 1, "rad", 3],
  ["norm_susp_travel", 4, "0–1", 3],
  ["tire_slip_ratio", 4, "", 3],
  ["wheel_rotation_speed", 4, "rad/s", 1],
  ["wheel_on_rumble_strip", 4, "0/1", 0],
  ["wheel_in_puddle", 4, "m", 3],
  ["surface_rumble", 4, "", 3],
  ["tire_slip_angle", 4, "", 3],
  ["tire_combined_slip", 4, "", 3],
  ["susp_travel_meters", 4, "m", 4],
  ["car_ordinal", 1, "", 0], ["car_class", 1, "", 0], ["car_pi", 1, "", 0],
  ["drivetrain_type", 1, "", 0], ["num_cylinders", 1, "", 0],
  ["car_group", 1, "", 0], ["smashable_vel_diff", 1, "", 3], ["smashable_mass", 1, "", 3],
  ["pos_x", 1, "m", 2], ["pos_y", 1, "m", 2], ["pos_z", 1, "m", 2],
  ["speed", 1, "m/s", 2], ["power", 1, "W", 0], ["torque", 1, "N·m", 1],
  ["tire_temp", 4, "°F", 1],
  ["boost", 1, "psi", 2], ["fuel", 1, "0–1", 4], ["distance_traveled", 1, "m", 1],
  ["best_lap", 1, "s", 3], ["last_lap", 1, "s", 3],
  ["current_lap", 1, "s", 3], ["current_race_time", 1, "s", 3],
  ["lap_number", 1, "", 0], ["race_position", 1, "", 0],
  ["accel", 1, "0–255", 0], ["brake", 1, "0–255", 0],
  ["clutch", 1, "0–255", 0], ["handbrake", 1, "0–255", 0],
  ["gear", 1, "", 0], ["steer", 1, "±127", 0],
  ["normalized_driving_line", 1, "", 0], ["normalized_ai_brake_difference", 1, "", 0],
];
const RAW_WHEELS = ["fl", "fr", "rl", "rr"];

/* raw value -> display string (both raw views); dec comes from RAW_FIELDS */
function fmtRaw(v, dec) {
  if (v == null) return "—";
  if (typeof v === "number") return v.toFixed(dec);
  return String(v); // booleans (race_mode) pass through as true/false
}

/* ---------- themed modal dialogs (replace window.prompt / confirm / alert) ---------- */

function showModal({ title, message = "", extra = null, value = null, placeholder = "",
                     okText = "OK", cancelText = "Cancel",
                     danger = false, showCancel = true }) {
  return new Promise((resolve) => {
    const backdrop = document.createElement("div");
    backdrop.className = "modal-backdrop";
    const box = document.createElement("div");
    box.className = "modal" + (danger ? " danger" : "");
    box.setAttribute("role", "dialog");
    box.setAttribute("aria-modal", "true");
    backdrop.appendChild(box);

    const h = document.createElement("h3");
    h.textContent = title;
    box.appendChild(h);

    if (message) {
      const p = document.createElement("p");
      p.textContent = message;   // plain text: user-named sessions render literally
      box.appendChild(p);
    }
    if (extra) box.appendChild(extra);  // caller-built DOM, e.g. a link the
                                        // text-only message can't carry

    let inputEl = null;
    if (value !== null) {
      inputEl = document.createElement("input");
      inputEl.type = "text";
      inputEl.value = value;
      inputEl.placeholder = placeholder;
      inputEl.spellcheck = false;
      box.appendChild(inputEl);
    }

    const actions = document.createElement("div");
    actions.className = "modal-actions";
    box.appendChild(actions);

    const done = (result) => {
      document.removeEventListener("keydown", onKey, true);
      backdrop.remove();
      resolve(result);
    };
    if (showCancel) {
      const cancel = document.createElement("button");
      cancel.className = "modal-cancel";
      cancel.textContent = cancelText;
      cancel.onclick = () => done(null);
      actions.appendChild(cancel);
    }
    const ok = document.createElement("button");
    ok.className = "modal-ok " + (danger ? "danger-solid" : "primary");
    ok.textContent = okText;
    ok.onclick = () => done(inputEl ? inputEl.value : true);
    actions.appendChild(ok);

    const onKey = (e) => {
      if (e.key === "Escape") {
        e.preventDefault();
        done(null);
      } else if (e.key === "Enter" && inputEl && document.activeElement === inputEl) {
        e.preventDefault();
        done(inputEl.value);
      }
    };
    document.addEventListener("keydown", onKey, true);
    backdrop.addEventListener("pointerdown", (e) => { if (e.target === backdrop) done(null); });

    document.body.appendChild(backdrop);
    (inputEl || ok).focus();
    if (inputEl) inputEl.select();
  });
}

/* resolves to the entered string, or null when cancelled */
function uiPrompt(title, { value = "", message = "", extra = null, placeholder = "", okText = "Save" } = {}) {
  return showModal({ title, message, extra, value, placeholder, okText });
}

/* resolves to true, or null when cancelled */
function uiConfirm(title, message, { okText = "Confirm", danger = false } = {}) {
  return showModal({ title, message, okText, danger });
}

function uiAlert(title, message) {
  return showModal({ title, message, okText: "OK", showCancel: false });
}

/* ---------- update check (client-side, fail-soft, dismissible) ----------
   Exe users don't get `git pull`, so surface a "newer version available"
   notice: ask the backend which version we're running (/api/version), then
   compare against the latest GitHub Release from the browser. Strictly
   offline-first — any failure is swallowed, dev builds ("0.0.0") are skipped,
   and the GitHub call is cached for a day to respect the unauthenticated
   60 req/hr limit. No auto-download; the banner only links to the release. */

const UPDATE_REPO = "darcane/LapScope";
const UPDATE_CACHE_KEY = "ls_update_check";        // { ts, latest }
const UPDATE_DISMISS_KEY = "ls_update_dismissed";  // last dismissed version
const UPDATE_CACHE_TTL = 24 * 60 * 60 * 1000;      // 1 day

/* -1 / 0 / 1 for a<b / a==b / a>b over dotted numeric versions ("1.2.0"). */
function cmpVersion(a, b) {
  const pa = String(a).split(".").map((n) => parseInt(n, 10) || 0);
  const pb = String(b).split(".").map((n) => parseInt(n, 10) || 0);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const d = (pa[i] || 0) - (pb[i] || 0);
    if (d) return d < 0 ? -1 : 1;
  }
  return 0;
}

/* Latest release tag ("1.2.0", v-stripped), cached for a day. null on failure. */
async function fetchLatestVersion() {
  try {
    const cached = JSON.parse(localStorage.getItem(UPDATE_CACHE_KEY) || "null");
    if (cached && Date.now() - cached.ts < UPDATE_CACHE_TTL) return cached.latest;
  } catch { /* corrupt cache: fall through and refetch */ }
  try {
    const r = await fetch(`https://api.github.com/repos/${UPDATE_REPO}/releases/latest`);
    if (!r.ok) return null;
    const latest = String((await r.json()).tag_name || "").replace(/^v/, "");
    if (!latest) return null;
    localStorage.setItem(UPDATE_CACHE_KEY, JSON.stringify({ ts: Date.now(), latest }));
    return latest;
  } catch { return null; }
}

function showUpdateBanner(latest) {
  if (document.getElementById("update-banner")) return;
  const bar = document.createElement("div");
  bar.id = "update-banner";
  bar.className = "update-banner";

  const msg = document.createElement("span");
  const link = document.createElement("a");
  link.href = `https://github.com/${UPDATE_REPO}/releases/latest`;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  link.textContent = `LapScope v${latest} is available`;
  msg.append("A newer version of ", link, " \u2014 what's new");

  const close = document.createElement("button");
  close.className = "update-banner-x";
  close.setAttribute("aria-label", "Dismiss");
  close.textContent = "\u00d7";
  close.onclick = () => {
    localStorage.setItem(UPDATE_DISMISS_KEY, latest);
    bar.remove();
  };

  bar.append(msg, close);
  document.body.prepend(bar);
}

async function checkForUpdate() {
  let current;
  try {
    current = (await (await fetch("/api/version")).json()).version;
  } catch { return; }
  if (!current || current === "0.0.0") return;  // dev/source run: don't nag

  const latest = await fetchLatestVersion();
  if (!latest) return;
  if (cmpVersion(latest, current) <= 0) return;
  if (localStorage.getItem(UPDATE_DISMISS_KEY) === latest) return;
  showUpdateBanner(latest);
}

/* ---------- car-list auto-refresh (fail-soft, once a day) ----------
   The bundled car_ordinals.json goes stale as the game adds cars, so nudge
   the backend to re-download the community list from the repo (POST
   /api/cars/refresh; see app/cars.py). Same shape as the update check:
   browser-triggered, at most once per day per browser, silent on failure —
   the bundled copy keeps working offline. */

/* Pre-filled "name this car" issue: the ordinal lands in the form field, the
   merged answer lands in app/car_ordinals.json for everyone (see TODO's
   community self-heal loop). */
function unknownCarIssueUrl(ordinal) {
  return `https://github.com/${UPDATE_REPO}/issues/new?template=unknown_car.yml`
    + `&title=${encodeURIComponent(`car: ordinal ${ordinal}`)}&ordinal=${ordinal}`;
}

const CARDB_CHECK_KEY = "ls_cardb_check";      // ts of the last attempt
const CARDB_CHECK_TTL = 24 * 60 * 60 * 1000;   // 1 day

async function maybeRefreshCarList() {
  const last = parseInt(localStorage.getItem(CARDB_CHECK_KEY) || "0", 10);
  if (Date.now() - last < CARDB_CHECK_TTL) return;
  localStorage.setItem(CARDB_CHECK_KEY, String(Date.now()));  // even on failure: don't hammer
  try {
    const r = await fetch("/api/cars/refresh", { method: "POST" });
    if (!r.ok) return;
    const { added } = await r.json();
    // new names may resolve previously-unknown cars: redraw the session list
    if (added > 0 && typeof loadSessions === "function") loadSessions();
  } catch { /* offline / server restarting: bundled list keeps working */ }
}

function onReady(fn) {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", fn);
  } else {
    fn();
  }
}

onReady(checkForUpdate);
onReady(maybeRefreshCarList);
