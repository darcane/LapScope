/* Shared UI helpers: Forza-colored class badges and track-condition ribbons. */

const CLASS_LETTERS = ["D", "C", "B", "A", "S1", "S2", "X", "X"];

/* Forza Horizon PI badge colors */
const CLASS_COLORS = {
  D: "#41c7e0",   // light blue
  C: "#f2d21f",   // yellow
  B: "#f7941e",   // orange
  A: "#e63946",   // red
  S1: "#b750e0",  // purple
  S2: "#2f6df6",  // blue
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
  dirt: ["🟤", "Dirt"],
};

function condBadge(cond) {
  const [icon, label] = CONDITION_META[cond] || CONDITION_META.dry;
  return `<span class="cond-badge cond-${cond}">${icon} ${label}</span>`;
}

/* course/track type is not in the packet - manual tag like snow/dirt */
const TRACK_META = {
  road: ["🛣️", "Road"],
  street: ["🏙️", "Street"],
  dirt: ["🟫", "Dirt"],
  cross: ["🏞️", "Cross-Country"],
  drag: ["🏁", "Drag"],
};

function trackBadge(type) {
  const [icon, label] = TRACK_META[type] || TRACK_META.road;
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
