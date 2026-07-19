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
  power: "kw",          // "kw" | "hp" | "ps"  (packet Power is Watts)
  boost: "psi",         // "psi" | "bar"  (packet Boost is psi)
  accent: "cyan",       // key into ACCENTS below
  freeroamMap: false,   // draw the live track map in free roam, not only races
  contactLayer: true,   // show contact sparks + jump glyphs on the analysis map
  defaultMapMode: "2d", // "2d" | "3d"  (absorbs legacy fc_mapmode)
  defaultColor: "speed", // "speed" | "slip"
  rawLive: false,       // raw telemetry value grid on the live dashboard
  rawAnalysis: false,   // raw values-at-cursor table on the analysis page
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
  // restyle the page before listeners run, so canvas redraws triggered by
  // them already see the new --accent
  if ("accent" in patch) applyAccent();
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

/* packet power is Watts; hp = mechanical horsepower, PS = metric horsepower */
function powerFromW(w) {
  return _settings.power === "hp" ? w / 745.699872
    : _settings.power === "ps" ? w / 735.49875
    : w / 1000;
}
function powerUnit() {
  return _settings.power === "hp" ? "hp" : _settings.power === "ps" ? "PS" : "kW";
}

/* packet boost is psi; bar values are ~7× smaller, so they get an extra decimal */
function boostFromPsi(psi) { return _settings.boost === "bar" ? psi * 0.0689475729 : psi; }
function boostUnit() { return _settings.boost === "bar" ? "bar" : "psi"; }
function fmtBoost(psi) { return boostFromPsi(psi).toFixed(_settings.boost === "bar" ? 2 : 1); }

/* tire-temp cell string for the grip gauge, e.g. "71°C" (input is Fahrenheit) */
function fmtTireTemp(f) { return `${Math.round(tempFromF(f))}${tempUnit()}`; }

/* ---------- accent theme (issue #25) ----------
   Curated presets, not a free color wheel, so contrast against the dark
   palette stays readable everywhere. Each entry:
   - accent: what CSS --accent becomes (all light enough for the #001018
     text that sits on accent-filled pills/buttons);
   - pick: the chart-friendly shade used as overlay color A on the analysis
     page (identical between map and charts);
   - clash: index in the analysis BASE_PICK_COLORS palette that sits too
     close to this accent — analysis.js swaps that one for cyan so six
     overlaid laps stay tellable-apart. */
const ACCENTS = {
  cyan:    { label: "Cyan",    accent: "#00d4ff", pick: "#22d3ee", clash: -1 },
  magenta: { label: "Magenta", accent: "#ff3d7f", pick: "#ff3d7f", clash: 4 },
  violet:  { label: "Violet",  accent: "#9d6bff", pick: "#9d6bff", clash: 2 },
  sunset:  { label: "Sunset",  accent: "#ff8c2e", pick: "#ff8c2e", clash: 1 },
  lime:    { label: "Lime",    accent: "#a3e635", pick: "#a3e635", clash: 3 },
  frost:   { label: "Frost",   accent: "#e8f1fb", pick: "#e8f1fb", clash: 5 },
};

function accentDef() { return ACCENTS[_settings.accent] || ACCENTS.cyan; }

/* "#rrggbb" -> "rgba(r, g, b, a)": canvas strokes need alpha'd accents */
function hexRgba(hex, a) {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
}

/* The whole CSS theme keys off --accent (style.css derives glows and fills
   from it via color-mix); canvas renderers can't use var() and instead pull
   accentDef() again on every settings change (gauges.js / analysis.js). */
function applyAccent() {
  document.documentElement.style.setProperty("--accent", accentDef().accent);
}
applyAccent();

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

  // accent swatches: one color dot per curated preset (no free color wheel)
  const swatches = (label, key) => {
    const row = document.createElement("div");
    row.className = "settings-row";
    const lab = document.createElement("span");
    lab.className = "settings-label";
    lab.textContent = label;
    row.appendChild(lab);
    const group = document.createElement("div");
    group.className = "settings-swatches";
    for (const [value, a] of Object.entries(ACCENTS)) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "settings-swatch";
      b.style.setProperty("--sw", a.accent);
      b.title = a.label;
      b.setAttribute("aria-label", `${a.label} accent`);
      b.classList.toggle("active", _settings[key] === value);
      b.onclick = () => {
        saveSettings({ [key]: value });
        for (const x of group.children) x.classList.toggle("active", x === b);
      };
      group.appendChild(b);
    }
    row.appendChild(group);
    body.appendChild(row);
  };

  group("Theme");
  swatches("Accent", "accent");

  group("Units");
  seg("Speed", "speed", [{ label: "km/h", value: "kmh" }, { label: "mph", value: "mph" }]);
  seg("Tire temp", "temp", [{ label: "\u00b0C", value: "c" }, { label: "\u00b0F", value: "f" }]);
  seg("Distance", "dist", [{ label: "km", value: "km" }, { label: "mi", value: "mi" }]);
  seg("Power", "power", [{ label: "kW", value: "kw" }, { label: "hp", value: "hp" }, { label: "PS", value: "ps" }]);
  seg("Boost", "boost", [{ label: "psi", value: "psi" }, { label: "bar", value: "bar" }]);

  group("Maps");
  toggle("Live map in free roam", "freeroamMap");
  toggle("Contact & jump markers (analysis)", "contactLayer");
  seg("Default map view", "defaultMapMode", [{ label: "2D", value: "2d" }, { label: "3D", value: "3d" }]);
  seg("Default color", "defaultColor", [{ label: "Speed", value: "speed" }, { label: "Slip", value: "slip" }]);

  // raw packet values, game-native units - no conversions on purpose
  group("Raw data");
  toggle("Raw telemetry panel (live)", "rawLive");
  toggle("Raw data at cursor (analysis)", "rawAnalysis");

  // Car-name list: server-side state (not a browser preference) — shows the
  // community list's size/age and re-downloads it on demand. The same refresh
  // also runs automatically once a day (common.js maybeRefreshCarList).
  group("Car list");
  {
    const row = document.createElement("div");
    row.className = "settings-row";
    const status = document.createElement("span");
    status.className = "settings-label settings-carlist-status";
    status.textContent = "…";
    row.appendChild(status);
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "settings-refresh";
    btn.textContent = "Refresh now";
    row.appendChild(btn);
    body.appendChild(row);

    const show = (info) => {
      const when = info.fetched_at
        ? `updated ${new Date(info.fetched_at * 1000).toLocaleDateString()}`
        : "bundled list";
      status.textContent = `${info.total} car names · ${when}`;
    };
    fetch("/api/cars").then((r) => r.json()).then(show)
      .catch(() => { status.textContent = "car list unavailable"; });

    btn.onclick = async () => {
      btn.disabled = true;
      status.textContent = "refreshing…";
      try {
        const r = await fetch("/api/cars/refresh", { method: "POST" });
        const out = await r.json();
        if (!r.ok) throw new Error(out.detail || "refresh failed");
        status.textContent = `${out.total} car names · `
          + (out.added ? `${out.added} new` : "already up to date");
        if (out.added > 0 && typeof loadSessions === "function") loadSessions();
      } catch (e) {
        status.textContent = e.message || "refresh failed (offline?)";
      }
      btn.disabled = false;
    };
  }

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
