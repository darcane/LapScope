/* Page chrome both live pages share (index.html, classic.html), all plain
   HTML around whatever draws the gauges: the first-run setup overlay, the car
   chip, the write-failure banner and the raw-telemetry panel. Each page
   supplies the elements (#nodata / #nd-port / #nd-stat, #car-chip,
   #raw-panel / #raw-body / #raw-hold); this file fills them.
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

/* The full-screen "waiting for telemetry" setup steps (#nodata) are for the
   first-run case only: no frame ever. Once data has arrived, a stall must NOT
   re-cover the page: FH6 stops Data Out whenever it loses focus, so clicking
   away from the game to read the dashboard on a second screen would otherwise
   slam an opaque overlay — with the wrong diagnosis — over everything (issue
   #69). A stale-but-connected stream just flips the chip to PAUSED. Cheap to
   call per drawn frame: the DOM is touched only when the answer changes. */
let noDataShown = null;
function syncNoData(f) {
  if (noDataShown === !f) return;
  noDataShown = !f;
  document.getElementById("nodata").classList.toggle("hidden", !!f);
}

/* the overlay's stats line, while it is visible: what the server has seen */
function updateNoDataStats(st) {
  if (!noDataShown) return;
  document.getElementById("nd-port").textContent = st.udp_port;
  const stat = document.getElementById("nd-stat");
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
}

/* /api/status every 2 s — via setVisibleInterval, so a hidden tab stops
   polling — for the write-failure banner and the first-run overlay's stats
   line, which every live page must show. */
function watchStatus() {
  const poll = async () => {
    try {
      const st = await (await fetch("/api/status")).json();
      showWriteError(st);
      updateNoDataStats(st);
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
