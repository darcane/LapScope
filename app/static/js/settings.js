/* User settings: display preferences stored per-browser in localStorage.
   These never touch the recorder — raw packets are stored losslessly and every
   conversion here happens at display time — so localStorage (not the backend)
   is the right home. Loaded after common.js on both pages; the dashboard and
   analysis pages read the converters below and re-render via onSettingsChange. */

const SETTINGS_KEY = "ls_settings";

/* Schema version of the stored object. Nothing reads it yet — it exists so a
   future release *can* migrate rather than guess, which is only possible if
   1.0 ships the stamp before it puts this shape in everyone's browser. */
const SETTINGS_V = 1;

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
  onlineChecks: true,   // may this browser contact GitHub at all (issue #76)
};

/* ---------- accent theme (issue #25) ----------
   Curated presets, not a free color wheel, so contrast against the dark
   palette stays readable everywhere. Each entry:
   - accent: what CSS --accent becomes (all light enough for the #001018
     text that sits on accent-filled pills/buttons);
   - pick: the chart-friendly shade used as overlay color A on the analysis
     page (identical between map and charts);
   - clash: index in the analysis BASE_PICK_COLORS palette that sits too
     close to this accent — analysis.js swaps that one for cyan so six
     overlaid laps stay tellable-apart.
   Declared up here because loadSettings validates a stored accent against
   its keys, and a `const` is unreachable until its own line runs. */
const ACCENTS = {
  cyan:    { label: "Cyan",    accent: "#00d4ff", pick: "#22d3ee", clash: -1 },
  magenta: { label: "Magenta", accent: "#ff3d7f", pick: "#ff3d7f", clash: 4 },
  violet:  { label: "Violet",  accent: "#9d6bff", pick: "#9d6bff", clash: 2 },
  sunset:  { label: "Sunset",  accent: "#ff8c2e", pick: "#ff8c2e", clash: 1 },
  lime:    { label: "Lime",    accent: "#a3e635", pick: "#a3e635", clash: 3 },
  frost:   { label: "Frost",   accent: "#e8f1fb", pick: "#e8f1fb", clash: 5 },
};

/* What each non-boolean key is allowed to hold. Booleans coerce with !!, so
   they need no entry. */
const SETTINGS_VALUES = {
  speed: ["kmh", "mph"],
  temp: ["c", "f"],
  dist: ["km", "mi"],
  power: ["kw", "hp", "ps"],
  boost: ["psi", "bar"],
  accent: Object.keys(ACCENTS),
  defaultMapMode: ["2d", "3d"],
  defaultColor: ["speed", "slip"],
};

/* An unknown stored value never *broke* anything — every converter is
   `x === "known" ? A : B`, so it silently took the default branch. But
   _settings kept it, and the Settings panel marks a chip active by comparing
   against _settings: a stale `accent: "teal"` rendered cyan while no swatch
   showed as selected, and the same for every seg() row (issue #72).
   browse.js has validated its own two keys this way since it was written. */
function coerceSettings(stored) {
  const out = { ...SETTINGS_DEFAULTS };
  for (const [key, def] of Object.entries(SETTINGS_DEFAULTS)) {
    const v = stored[key];
    if (v === undefined) continue;
    if (typeof def === "boolean") out[key] = !!v;
    else if (SETTINGS_VALUES[key].includes(v)) out[key] = v;
  }
  return out;
}

function storeSettings(s) {
  try {
    localStorage.setItem(SETTINGS_KEY, JSON.stringify({ v: SETTINGS_V, ...s }));
  } catch { /* private mode */ }
}

/* One-time migration of the pre-Settings ad-hoc keys, then cached in memory. */
function loadSettings() {
  let stored = {};
  try { stored = JSON.parse(localStorage.getItem(SETTINGS_KEY) || "{}") || {}; }
  catch { stored = {}; }
  if (typeof stored !== "object" || Array.isArray(stored)) stored = {};

  if (stored.speed === undefined && localStorage.getItem("fc_mph") !== null)
    stored.speed = localStorage.getItem("fc_mph") === "1" ? "mph" : "kmh";
  if (stored.defaultMapMode === undefined && localStorage.getItem("fc_mapmode"))
    stored.defaultMapMode = localStorage.getItem("fc_mapmode");

  const merged = coerceSettings(stored);
  // write back whenever the stored copy isn't already this version: that
  // covers the legacy migration and, just as importantly, stamps the copies
  // written by every build before this one
  if (stored.v !== SETTINGS_V) storeSettings(merged);
  return merged;
}

let _settings = loadSettings();
const _settingsListeners = new Set();

function getSettings() { return _settings; }

function saveSettings(patch) {
  _settings = { ..._settings, ...patch };
  storeSettings(_settings);
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

/* ---------- accent theme (issue #25) ---------- (ACCENTS is declared above) */

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

/* Database sizes for the Storage row. Not a user preference — bytes are
   bytes — and binary units on purpose, since that is what the OS reports for
   the same file. */
function fmtBytes(n) {
  if (!(n > 0)) return "0 MB";
  const mb = n / 1048576;
  if (mb >= 1024) return `${(mb / 1024).toFixed(2)} GB`;
  return `${mb.toFixed(mb < 10 ? 1 : 0)} MB`;
}

/* ---------- settings panel (themed modal, reuses common.js modal chrome) ---------- */

function openSettings() {
  // same <dialog> chrome as common.js showModal: focus trap, Escape and
  // focus restored to the gear button, all for free (issue #70)
  const box = document.createElement("dialog");
  box.className = "modal settings-modal";
  box.setAttribute("aria-labelledby", "settings-title");

  const h = document.createElement("h3");
  h.id = "settings-title";
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
    return sw;
  };

  const group = (title) => {
    const g = document.createElement("div");
    g.className = "settings-group-title";
    g.textContent = title;
    body.appendChild(g);
  };

  /* A muted paragraph under a group. Only the Privacy group has one: a switch
     labelled "check for updates" can't say what is contacted, how often, or
     what is sent, and that is the whole question it exists to answer. */
  const note = (text) => {
    const n = document.createElement("p");
    n.className = "settings-note";
    n.textContent = text;
    body.appendChild(n);
    return n;
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

  // Status line + one action button. The rows below it are the exception to
  // "localStorage-only": they act on server-side state, so they read their
  // own status from the API instead of from _settings.
  const actionRow = (btnText) => {
    const row = document.createElement("div");
    row.className = "settings-row";
    const status = document.createElement("span");
    status.className = "settings-label settings-refresh-status";
    status.textContent = "…";
    row.appendChild(status);
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "settings-refresh";
    btn.textContent = btnText;
    row.appendChild(btn);
    body.appendChild(row);
    return { status, btn };
  };

  // Bundled reference lists: each row shows the list's size/age and
  // re-downloads it on demand. The same refreshes also run automatically once
  // a day (common.js maybeRefreshCarList / maybeRefreshTrackList).
  // `summarize` turns the refresh response into the "what changed" half of the
  // status line, and says whether the session list has to be redrawn because
  // names moved.
  const refreshRow = (path, unit, summarize) => {
    const { status, btn } = actionRow("Refresh now");

    fetch(`/api/${path}`).then((r) => r.json()).then((info) => {
      const when = info.fetched_at
        ? `updated ${new Date(info.fetched_at * 1000).toLocaleDateString()}`
        : "bundled list";
      status.textContent = `${info.total} ${unit} · ${when}`;
    }).catch(() => { status.textContent = `${unit} unavailable`; });

    btn.onclick = async () => {
      btn.disabled = true;
      status.textContent = "refreshing…";
      try {
        const r = await fetch(`/api/${path}/refresh`, { method: "POST" });
        const out = await r.json();
        if (!r.ok) throw new Error(out.detail || "refresh failed");
        const { text, changed } = summarize(out);
        status.textContent = `${out.total} ${unit} · ${text}`;
        if (changed && typeof loadSessions === "function") loadSessions();
      } catch (e) {
        status.textContent = e.message || "refresh failed (offline?)";
      }
      btn.disabled = false;
    };
  };

  // Everything LapScope sends anywhere, in one place, with a way to say no
  // (issue #76). Sits directly above the two rows it governs.
  group("Privacy");
  const onlineSw = toggle("Check GitHub for updates and lists", "onlineChecks");
  const onlineNote = note(
    "On: once a day LapScope asks GitHub for the latest release, and asks it "
    + "for the community car and track lists below. Nothing about you or your "
    + "driving is sent — only the request itself. Off: it never reaches out on "
    + "its own, and the bundled lists keep working. Refresh now is a manual "
    + "call and works either way.");
  // LS_OFFLINE is the whole install's answer and overrides the browser's, so
  // show it as what it is rather than leaving a switch that does nothing.
  serverInfo().then((info) => {
    if (!info.offline) return;
    onlineSw.disabled = true;
    onlineSw.classList.remove("on");
    onlineSw.setAttribute("aria-checked", "false");
    onlineNote.textContent =
      "Turned off for this whole install with LS_OFFLINE. LapScope makes no "
      + "outbound calls at all, whatever this browser is set to, and the "
      + "bundled car and track lists are the ones in use.";
  });

  group("Car list");
  refreshRow("cars", "car names", (out) => ({
    text: out.added ? `${out.added} new` : "already up to date",
    changed: out.added > 0,
  }));

  // Official-route catalogue: what names a course on its first completed lap
  // (app/tracks.py). "named" counts routes already in this database that the
  // refreshed catalogue could identify, which is the number the user cares
  // about — new entries they've never driven change nothing they can see.
  group("Track list");
  refreshRow("tracks", "tracks", (out) => ({
    text: out.named ? `${out.named} route${out.named === 1 ? "" : "s"} named`
      : (out.added ? `${out.added} new` : "already up to date"),
    changed: out.named > 0,
  }));

  // Deleting a session frees pages *inside* the database file and nothing
  // else — SQLite only hands space back to the drive on VACUUM. Without this
  // row, someone who deletes half their history to recover disk space sees
  // zero bytes come back and no explanation (issue #59).
  group("Storage");
  const storageRow = () => {
    const { status, btn } = actionRow("Compact now");
    const size = (info) => {
      const free = info.free_bytes > 0
        ? ` · ${fmtBytes(info.free_bytes)} reclaimable` : "";
      status.textContent = `${fmtBytes(info.db_bytes)} of recordings${free}`;
    };

    fetch("/api/storage").then((r) => r.json()).then(size)
      .catch(() => { status.textContent = "database size unavailable"; });

    btn.onclick = async () => {
      const ok = await uiConfirm(
        "Compact database?",
        "Rebuilds the database file so the space freed by deleted sessions "
        + "goes back to the drive. It needs as much free disk space as the "
        + "database currently uses, and LapScope pauses while it runs.",
        { okText: "Compact" });
      if (!ok) return;
      btn.disabled = true;
      status.textContent = "compacting…";
      try {
        const r = await fetch("/api/storage/compact", { method: "POST" });
        const out = await r.json();
        if (!r.ok) throw new Error(out.detail || "compact failed");
        status.textContent = out.reclaimed_bytes > 0
          ? `${fmtBytes(out.after_bytes)} of recordings · `
            + `${fmtBytes(out.reclaimed_bytes)} reclaimed`
          : `${fmtBytes(out.after_bytes)} of recordings · nothing to reclaim`;
      } catch (e) {
        status.textContent = e.message || "compact failed";
      }
      btn.disabled = false;
    };
  };
  storageRow();

  const actions = document.createElement("div");
  actions.className = "modal-actions";
  box.appendChild(actions);
  const ok = document.createElement("button");
  ok.className = "modal-ok primary";
  ok.textContent = "Done";
  const close = () => { box.close(); box.remove(); };
  ok.onclick = close;
  actions.appendChild(ok);

  box.addEventListener("cancel", (e) => { e.preventDefault(); close(); });
  box.addEventListener("pointerdown", (e) => { if (e.target === box) close(); });

  document.body.appendChild(box);
  box.showModal();
  ok.focus();
}

/* Wire the header gear (present on both pages). */
(function bindSettingsButton() {
  const btn = document.getElementById("settings-btn");
  if (btn) btn.onclick = openSettings;
})();
