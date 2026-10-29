/* Page chrome both live pages share (index.html, cockpit.html), all plain
   HTML around whatever draws the gauges: the car chip, the write-failure
   banner and the raw-telemetry panel. Each page supplies the elements
   (#car-chip, #raw-panel / #raw-body / #raw-hold); this file fills them.
   Load after live.js. */

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

/* /api/status every 2 s — via setVisibleInterval, so a hidden tab stops
   polling — for the write-failure banner, which every live page must show;
   `onStatus` gets the same payload for anything page-specific. */
function watchStatus(onStatus) {
  const poll = async () => {
    try {
      const st = await (await fetch("/api/status")).json();
      showWriteError(st);
      if (onStatus) onStatus(st);
    } catch { /* server briefly unavailable */ }
  };
  setVisibleInterval(poll, 2000);
  poll();
}

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
  const chip = document.getElementById("car-chip");
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

/* ---------- raw telemetry panel (Settings → Raw data) ----------
   Every packet field of the live WS frame, verbatim — RAW_FIELDS (common.js)
   mirrors packet.py FIELDS — plus the tracker extras the listener merges in.
   The DOM is built once; each frame rewrites only the cells whose text
   changed, so 60 Hz updates stay cheap. Hold freezes the display only —
   recording and the rest of the page keep running. */
const RAW_EXTRAS = [
  ["session_id", "", 0], ["race_mode", "", 0], ["delta", "s", 3],
  ["session_best", "s", 3], ["lap_elapsed", "s", 3],
];
const rawPanel = { cells: [], held: false };

function initRawPanel() {
  const body = document.getElementById("raw-body");

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

  const hold = document.getElementById("raw-hold");
  hold.onclick = () => {
    rawPanel.held = !rawPanel.held;
    hold.classList.toggle("on", rawPanel.held);
    hold.setAttribute("aria-pressed", String(rawPanel.held));
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
  document.getElementById("raw-panel").style.display = getSettings().rawLive ? "" : "none";
}

/* Per drawn frame: the car chip and, when Settings shows it and it isn't
   held, the raw panel. */
function updateLiveChrome(f) {
  updateCarChip(f);
  if (getSettings().rawLive && !rawPanel.held) updateRawPanel(f);
}
