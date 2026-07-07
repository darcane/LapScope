/* Shared UI helpers: Forza-colored class badges and track-condition ribbons. */

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

/* course/track type is not in the packet - manual tag like snow/dirt */
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

/* ---------- themed modal dialogs (replace window.prompt / confirm / alert) ---------- */

function showModal({ title, message = "", value = null, placeholder = "",
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
function uiPrompt(title, { value = "", message = "", placeholder = "", okText = "Save" } = {}) {
  return showModal({ title, message, value, placeholder, okText });
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

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", checkForUpdate);
} else {
  checkForUpdate();
}
