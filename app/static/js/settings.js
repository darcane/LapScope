/* User settings: display preferences stored per-browser in localStorage.
   These never touch the recorder — raw packets are stored losslessly and every
   conversion here happens at display time — so localStorage (not the backend)
   is the right home. Loaded after common.js on both pages; the dashboard and
   analysis pages read the converters below and re-render via onSettingsChange. */

const SETTINGS_KEY = "ls_settings";

const SETTINGS_DEFAULTS = {
  speed: "kmh",         // "kmh" | "mph"
  temp: "c",            // "c" | "f"  (packet TireTemp is Fahrenheit)
  dist: "km",           // "km" | "mi"
  freeroamMap: false,   // draw the live track map in free roam, not only races
  contactLayer: true,   // show contact-spike bursts on the analysis map
  defaultMapMode: "2d", // "2d" | "3d"  (absorbs legacy fc_mapmode)
  defaultColor: "speed", // "speed" | "slip"
};

/* One-time migration of the pre-Settings ad-hoc keys, then cached in memory. */
function loadSettings() {
  let stored = {};
  try { stored = JSON.parse(localStorage.getItem(SETTINGS_KEY) || "{}") || {}; }
  catch { stored = {}; }

  let migrated = false;
  if (stored.speed === undefined && localStorage.getItem("fc_mph") !== null) {
    stored.speed = localStorage.getItem("fc_mph") === "1" ? "mph" : "kmh";
    migrated = true;
  }
  if (stored.defaultMapMode === undefined && localStorage.getItem("fc_mapmode")) {
    stored.defaultMapMode = localStorage.getItem("fc_mapmode");
    migrated = true;
  }

  const merged = { ...SETTINGS_DEFAULTS, ...stored };
  if (migrated) {
    try { localStorage.setItem(SETTINGS_KEY, JSON.stringify(merged)); } catch { /* private mode */ }
  }
  return merged;
}

let _settings = loadSettings();
const _settingsListeners = new Set();

function getSettings() { return _settings; }

function saveSettings(patch) {
  _settings = { ..._settings, ...patch };
  try { localStorage.setItem(SETTINGS_KEY, JSON.stringify(_settings)); } catch { /* private mode */ }
  for (const cb of _settingsListeners) {
    try { cb(_settings); } catch { /* a bad listener must not block the rest */ }
  }
}

/* Subscribe to live changes; returns an unsubscribe fn. */
function onSettingsChange(cb) {
  _settingsListeners.add(cb);
  return () => _settingsListeners.delete(cb);
}

/* ---------- converters (all take/return numbers; *Unit() give labels) ---------- */

function speedFromMps(mps) {
  return _settings.speed === "mph" ? mps * 2.2369362921 : mps * 3.6;
}
function speedFromKmh(kmh) {
  return _settings.speed === "mph" ? kmh * 0.6213711922 : kmh;
}
function speedUnit() { return _settings.speed === "mph" ? "mph" : "km/h"; }

/* packet tire temps are Fahrenheit */
function tempFromF(f) { return _settings.temp === "c" ? (f - 32) * 5 / 9 : f; }
function tempUnit() { return _settings.temp === "c" ? "\u00b0C" : "\u00b0F"; }

function distFromM(m) { return _settings.dist === "mi" ? m / 1609.344 : m / 1000; }
function distUnit() { return _settings.dist === "mi" ? "mi" : "km"; }

/* tire-temp cell string for the grip gauge, e.g. "71°C" (input is Fahrenheit) */
function fmtTireTemp(f) { return `${Math.round(tempFromF(f))}${tempUnit()}`; }

/* ---------- settings panel (themed modal, reuses common.js modal chrome) ---------- */

function openSettings() {
  const backdrop = document.createElement("div");
  backdrop.className = "modal-backdrop";
  const box = document.createElement("div");
  box.className = "modal settings-modal";
  box.setAttribute("role", "dialog");
  box.setAttribute("aria-modal", "true");
  backdrop.appendChild(box);

  const h = document.createElement("h3");
  h.textContent = "Settings";
  box.appendChild(h);

  const p = document.createElement("p");
  p.textContent = "Preferences are saved in this browser only.";
  box.appendChild(p);

  const body = document.createElement("div");
  body.className = "settings-body";
  box.appendChild(body);

  // segmented picker: one row, two-or-more mutually exclusive options
  const seg = (label, key, opts) => {
    const row = document.createElement("div");
    row.className = "settings-row";
    const lab = document.createElement("span");
    lab.className = "settings-label";
    lab.textContent = label;
    row.appendChild(lab);
    const group = document.createElement("div");
    group.className = "settings-seg";
    for (const o of opts) {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = o.label;
      b.classList.toggle("active", _settings[key] === o.value);
      b.onclick = () => {
        saveSettings({ [key]: o.value });
        for (const x of group.children) x.classList.toggle("active", x === b);
      };
      group.appendChild(b);
    }
    row.appendChild(group);
    body.appendChild(row);
  };

  // on/off switch for a boolean setting
  const toggle = (label, key) => {
    const row = document.createElement("div");
    row.className = "settings-row";
    const lab = document.createElement("span");
    lab.className = "settings-label";
    lab.textContent = label;
    row.appendChild(lab);
    const sw = document.createElement("button");
    sw.type = "button";
    sw.className = "settings-switch";
    sw.setAttribute("role", "switch");
    const sync = () => {
      const on = !!_settings[key];
      sw.classList.toggle("on", on);
      sw.setAttribute("aria-checked", on ? "true" : "false");
    };
    sync();
    sw.onclick = () => { saveSettings({ [key]: !_settings[key] }); sync(); };
    row.appendChild(sw);
    body.appendChild(row);
  };

  const group = (title) => {
    const g = document.createElement("div");
    g.className = "settings-group-title";
    g.textContent = title;
    body.appendChild(g);
  };

  group("Units");
  seg("Speed", "speed", [{ label: "km/h", value: "kmh" }, { label: "mph", value: "mph" }]);
  seg("Tire temp", "temp", [{ label: "\u00b0C", value: "c" }, { label: "\u00b0F", value: "f" }]);
  seg("Distance", "dist", [{ label: "km", value: "km" }, { label: "mi", value: "mi" }]);

  group("Maps");
  toggle("Live map in free roam", "freeroamMap");
  toggle("Contact markers (analysis)", "contactLayer");
  seg("Default map view", "defaultMapMode", [{ label: "2D", value: "2d" }, { label: "3D", value: "3d" }]);
  seg("Default color", "defaultColor", [{ label: "Speed", value: "speed" }, { label: "Slip", value: "slip" }]);

  const actions = document.createElement("div");
  actions.className = "modal-actions";
  box.appendChild(actions);
  const ok = document.createElement("button");
  ok.className = "modal-ok primary";
  ok.textContent = "Done";
  const close = () => {
    document.removeEventListener("keydown", onKey, true);
    backdrop.remove();
  };
  ok.onclick = close;
  actions.appendChild(ok);

  const onKey = (e) => { if (e.key === "Escape") { e.preventDefault(); close(); } };
  document.addEventListener("keydown", onKey, true);
  backdrop.addEventListener("pointerdown", (e) => { if (e.target === backdrop) close(); });

  document.body.appendChild(backdrop);
  ok.focus();
}

/* Wire the header gear (present on both pages). */
(function bindSettingsButton() {
  const btn = document.getElementById("settings-btn");
  if (btn) btn.onclick = openSettings;
})();
