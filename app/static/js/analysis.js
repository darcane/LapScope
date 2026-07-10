/* Session browser, track map, and A/B lap comparison charts. */

const LAP_CHANNELS = "speed_kmh,throttle,brake,steer,slip_front,slip_rear,lap_time,pos_x,pos_y,pos_z";

const state = {
  sessionId: null,
  laps: [],
  lapA: null, lapB: null,     // lap ids
  dataA: null, dataB: null,   // /api/laps/{id}/data payloads
  colorMode: getSettings().defaultColor,
  mapMode: getSettings().defaultMapMode,
  charts: [],
};

/* dirty-lap markers inferred by the recorder (the game sends no official flag) */
const FLAG_META = {
  rewind: ["⏪", "rewind used during this lap"],
  contact: ["💥", "hard contact (wall / obstacle) during this lap"],
  cutoff: ["🏁", "time inferred — telemetry cut off at the finish with no finish signal"],
};

function flagIcons(flags) {
  if (!flags) return "";
  return flags.split(",")
    .map((f) => FLAG_META[f] ? `<span class="lap-flag" title="${FLAG_META[f][1]}">${FLAG_META[f][0]}</span>` : "")
    .join("");
}

const $ = (sel, root = document) => root.querySelector(sel);

function fmtLap(s) {
  if (!s || s <= 0) return "—";
  const m = Math.floor(s / 60);
  return `${m}:${(s - m * 60).toFixed(3).padStart(6, "0")}`;
}
function fmtDate(ts) {
  return new Date(ts * 1000).toLocaleString([], {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
}
/* like the API's display_name, but with the timestamp fallback rendered in
   the browser's timezone (the container runs UTC) */
function displayName(s) {
  return s.name || s.route_name || fmtDate(s.started_at);
}

/* ---------------- sessions sidebar ---------------- */

async function loadSessions() {
  const list = await (await fetch("/api/sessions")).json();
  const el = $("#session-list");
  el.innerHTML = "";
  if (!list.length) {
    el.innerHTML = `<div class="empty-hint">No sessions recorded yet.</div>`;
    return;
  }
  for (const s of list) {
    const card = document.createElement("div");
    card.className = "session-card" + (s.id === state.sessionId ? " active" : "");
    card.innerHTML = `
      <div class="title"></div>
      <div class="meta-row">${classBadge(s.car_class_letter, s.car_pi)}${dtBadge(s.drivetrain)}${trackBadge(s.track_type)}${condBadge(s.conditions)}</div>
      <div class="car-line"></div>
      <div class="sub">${fmtDate(s.started_at)} · ${s.lap_count} laps · best ${fmtLap(s.best_lap)}</div>`;
    $(".title", card).textContent = displayName(s);
    $(".car-line", card).textContent = s.car_name;
    card.onclick = () => selectSession(s.id);
    el.appendChild(card);
  }
}

async function selectSession(id) {
  state.sessionId = id;
  state.lapA = state.lapB = null;
  state.dataA = state.dataB = null;
  const payload = await (await fetch(`/api/sessions/${id}/laps`)).json();
  state.laps = payload.laps;

  const detail = $("#detail");
  detail.innerHTML = "";
  detail.appendChild($("#detail-template").content.cloneNode(true));
  const s = payload.session;
  $("#session-title").textContent = displayName(s);
  $("#header-badges").innerHTML = classBadge(s.car_class_letter, s.car_pi) + dtBadge(s.drivetrain);
  $("#header-car").textContent = s.car_name + (s.route_name ? "" : "  ·  route not identified yet (complete a lap)");
  const patch = (body) => fetch(`/api/sessions/${s.id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  $("#cond-select").value = s.conditions || "";
  $("#cond-select").onchange = async (e) => { await patch({ conditions: e.target.value }); loadSessions(); };
  $("#track-select").value = s.track_type || "";
  $("#track-select").onchange = async (e) => { await patch({ track_type: e.target.value }); loadSessions(); };
  $("#btn-rename").onclick = () => renameSession(s);
  $("#btn-route").onclick = () => renameRoute(s);
  $("#btn-car").onclick = () => renameCar(s);
  $("#btn-reprocess").onclick = () => reprocessSession(s);
  $("#btn-delete").onclick = () => deleteSession(s);
  $("#color-mode").value = state.colorMode;
  $("#color-mode").onchange = (e) => {
    state.colorMode = e.target.value;
    saveSettings({ defaultColor: state.colorMode });
    drawMap();
  };
  for (const b of document.querySelectorAll("#map-mode button")) {
    b.classList.toggle("active", b.dataset.mode === state.mapMode);
    b.onclick = () => {
      state.mapMode = b.dataset.mode;
      saveSettings({ defaultMapMode: state.mapMode });
      for (const x of document.querySelectorAll("#map-mode button"))
        x.classList.toggle("active", x === b);
      $("#map-hint").style.display = state.mapMode === "3d" ? "" : "none";
      drawMap();
    };
  }
  $("#map-hint").style.display = state.mapMode === "3d" ? "" : "none";
  bindMapDrag($("#trackmap"));

  renderLapRows();
  loadSessions();

  // preselect: best lap as A
  const best = payload.laps.find((l) => l.is_best);
  if (best) pickLap(best.id, "a");
}

function renderLapRows() {
  const tbody = $("#lap-rows");
  if (!tbody) return;
  tbody.innerHTML = "";
  for (const l of state.laps) {
    const tr = document.createElement("tr");
    if (l.is_best) tr.className = "best";
    tr.innerHTML = `
      <td><span class="pick ${state.lapA === l.id ? "a" : ""}" data-slot="a">A</span>
          <span class="pick ${state.lapB === l.id ? "b" : ""}" data-slot="b">B</span></td>
      <td>${l.lap_number + 1}</td>
      <td>${fmtLap(l.lap_time)}</td>
      <td>${l.gap_to_best != null && l.gap_to_best > 0 ? "+" + l.gap_to_best.toFixed(3) : (l.is_best ? "best" : "")}</td>
      <td style="color:var(--muted)">${flagIcons(l.flags)}${l.lap_time ? "" : " incomplete"}</td>`;
    for (const pick of tr.querySelectorAll(".pick"))
      pick.onclick = () => pickLap(l.id, pick.dataset.slot);
    tbody.appendChild(tr);
  }
}

async function pickLap(lapId, slot) {
  const key = slot === "a" ? "lapA" : "lapB";
  const dataKey = slot === "a" ? "dataA" : "dataB";
  if (state[key] === lapId) {           // toggle off
    state[key] = null;
    state[dataKey] = null;
  } else {
    state[key] = lapId;
    state[dataKey] = null;
    renderLapRows();
    try {
      const res = await fetch(
        `/api/laps/${lapId}/data?channels=${LAP_CHANNELS}&max_points=1500`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      // rapid clicks race their fetches: only the pick that still owns the
      // slot may write it - a stale response must not overwrite a newer one
      if (state[key] !== lapId) return;
      state[dataKey] = data;
    } catch (err) {
      if (state[key] !== lapId) return; // stale failure; the newer pick renders
      state[key] = null;                // untag so the row doesn't lie
      uiAlert("Couldn't load lap data", String(err.message || err));
    }
  }
  renderLapRows();
  drawMap();
  drawCharts();
}

async function renameSession(session) {
  const name = await uiPrompt("Rename session", {
    value: session.name || "",
    placeholder: displayName(session),
    message: "Shown in the session list on the left. Leave empty to reset.",
  });
  if (name === null) return;  // cancelled; "" clears back to the fallback name
  await fetch(`/api/sessions/${session.id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: name.trim() }),
  });
  selectSession(session.id);
}

async function renameRoute(session) {
  if (!session.route_id) {
    await uiAlert("No route identified yet",
      "Routes are fingerprinted from the first completed lap — finish a lap on this route first.");
    return;
  }
  const name = await uiPrompt("Name route / circuit", {
    value: session.route_name || "",
    message: "Applies to every session recorded on this route.",
  });
  if (name === null || !name.trim()) return;
  await fetch(`/api/routes/${session.route_id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: name.trim() }),
  });
  selectSession(session.id);
}

async function renameCar(session) {
  const name = await uiPrompt("Name car", {
    value: session.car_name || "",
    message: "Applies everywhere this car appears. Leave empty to reset to the built-in name.",
  });
  if (name === null) return;  // cancelled; "" reverts to the bundled name
  await fetch(`/api/cars/${session.car_ordinal}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: name.trim() }),
  });
  selectSession(session.id);
}

async function reprocessSession(session) {
  const sure = await uiConfirm("Reprocess session",
    "Re-run lap detection over this session's stored telemetry? The lap list "
    + "is rebuilt with the current detection logic (recovers laps from events "
    + "recorded before a fix, e.g. World Time Attack).",
    { okText: "Reprocess" });
  if (!sure) return;
  const res = await fetch(`/api/sessions/${session.id}/reprocess`, { method: "POST" });
  if (!res.ok) {
    await uiAlert("Reprocess failed", (await res.json()).detail || "failed");
    return;
  }
  const { laps } = await res.json();
  await uiAlert("Reprocess complete", `${laps} completed lap${laps === 1 ? "" : "s"} found.`);
  selectSession(session.id);
  loadSessions();
}

async function deleteSession(session) {
  const sure = await uiConfirm("Delete session",
    `Delete "${displayName(session)}" and all of its telemetry? This cannot be undone.`,
    { okText: "Delete", danger: true });
  if (!sure) return;
  const res = await fetch(`/api/sessions/${session.id}`, { method: "DELETE" });
  if (!res.ok) {
    await uiAlert("Delete failed", (await res.json()).detail || "delete failed");
    return;
  }
  state.sessionId = null;
  $("#detail").innerHTML = `<div class="empty-hint">Session deleted.</div>`;
  loadSessions();
}

/* ---------------- track map ---------------- */

function slipColor(s) {
  const hue = Math.max(0, Math.min(120, 120 * (1.15 - s) / 1.15));
  return `hsl(${hue}, 75%, 55%)`;
}
function speedColor(v, lo, hi) {
  const t = hi > lo ? (v - lo) / (hi - lo) : 0;
  return `hsl(${220 - 220 * t}, 80%, 55%)`;
}

/* 3D view state: yaw is user-draggable; the tilt is a fixed axonometric angle */
const map3d = { yaw: -0.9, dragging: false, dragX: 0, dragYaw: 0 };

/* chart cursor -> map marker: drawMap caches its finished frame plus the
   world->canvas projection, so hovering a chart only blits the cache and
   paints a dot where lap A was at that track position */
const mapCursor = { idx: null, proj: null, snap: null, dpr: 1 };

function setMapCursor(idx) {
  if (idx === mapCursor.idx) return;
  mapCursor.idx = idx;
  drawMapMarker();
}

function drawMapMarker() {
  const canvas = $("#trackmap");
  const A = state.dataA;
  if (!canvas || !mapCursor.snap || !mapCursor.proj) return;
  const ctx = canvas.getContext("2d");
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.drawImage(mapCursor.snap, 0, 0);
  ctx.setTransform(mapCursor.dpr, 0, 0, mapCursor.dpr, 0, 0);
  if (mapCursor.idx == null || !A) return;
  const c = A.channels;
  const i = Math.min(mapCursor.idx, c.pos_x.length - 1);
  const [x, y] = mapCursor.proj(c.pos_x[i], c.pos_y ? c.pos_y[i] : 0, c.pos_z[i], false);
  ctx.save();
  ctx.shadowColor = "#22d3ee";
  ctx.shadowBlur = 10;
  ctx.fillStyle = "#22d3ee";
  ctx.strokeStyle = "#fff";
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.arc(x, y, 6, 0, Math.PI * 2);
  ctx.fill();
  ctx.stroke();
  ctx.restore();
}

function bindMapDrag(canvas) {
  canvas.addEventListener("pointerdown", (e) => {
    if (state.mapMode !== "3d") return;
    map3d.dragging = true;
    map3d.dragX = e.clientX;
    map3d.dragYaw = map3d.yaw;
    canvas.setPointerCapture(e.pointerId);
  });
  canvas.addEventListener("pointermove", (e) => {
    if (!map3d.dragging) return;
    map3d.yaw = map3d.dragYaw + (e.clientX - map3d.dragX) * 0.008;
    drawMap();
  });
  const stop = () => { map3d.dragging = false; };
  canvas.addEventListener("pointerup", stop);
  canvas.addEventListener("pointercancel", stop);
}

function drawMap() {
  const canvas = $("#trackmap");
  if (!canvas) return;
  const cssW = canvas.parentElement.clientWidth - 4, cssH = 420;
  const dpr = window.devicePixelRatio || 1;
  canvas.width = cssW * dpr; canvas.height = cssH * dpr;
  canvas.style.width = cssW + "px";
  canvas.style.height = cssH + "px";
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, cssW, cssH);

  mapCursor.proj = null; // stale until this draw completes

  const A = state.dataA, B = state.dataB;
  const three = state.mapMode === "3d";
  canvas.style.cursor = three ? "grab" : "default";
  if (!A) { // the side stats and color legend describe lap A - don't let
    $("#map-side").innerHTML = "";      // them show a lap that was untagged
    $("#legend-scale").innerHTML = "";
  }
  if (!A && !B) return;

  // world extent (elevation exaggeration is derived from it)
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity,
      minZ = Infinity, maxZ = -Infinity;
  for (const d of [A, B]) if (d) {
    const c = d.channels;
    for (let i = 0; i < c.pos_x.length; i++) {
      minX = Math.min(minX, c.pos_x[i]); maxX = Math.max(maxX, c.pos_x[i]);
      minZ = Math.min(minZ, c.pos_z[i]); maxZ = Math.max(maxZ, c.pos_z[i]);
      const y = c.pos_y ? c.pos_y[i] : 0;
      minY = Math.min(minY, y); maxY = Math.max(maxY, y);
    }
  }
  const horiz = Math.max(maxX - minX, maxZ - minZ, 1);
  const yRange = maxY - minY;
  // robust elevation band: airborne spikes (cross-country jumps) must not
  // dictate the vertical scale, so exaggeration comes from the p5-p95 band
  // and anything above it is capped at 1.8x the band height
  let yLo = minY, yBand = yRange;
  if (three) {
    const ys = [];
    for (const d of [A, B]) if (d && d.channels.pos_y) ys.push(...d.channels.pos_y);
    if (ys.length) {
      ys.sort((a, b) => a - b);
      yLo = ys[Math.floor(0.05 * (ys.length - 1))];
      yBand = ys[Math.floor(0.95 * (ys.length - 1))] - yLo;
    }
    if (yBand <= 0.3 && yRange > 1) { yLo = minY; yBand = yRange; } // flat + jumps
  }
  // scale elevation so its display span reads at ~18% of the track extent
  const exag = yBand > 0.3 ? (0.18 * horiz) / yBand : 0;
  const yDisp = (y) => Math.min(Math.max(0, y - yLo), yBand * 1.8) * exag;

  const cosY = Math.cos(map3d.yaw), sinY = Math.sin(map3d.yaw);
  const raw = three
    ? (x, y, z, ground) => {
        // rotate the 2D map plane (x, -z) so the 3D view keeps the same
        // handedness as 2D - rotating (x, z) would mirror the track
        const u = x, v = -z;
        const xr = u * cosY - v * sinY, zr = u * sinY + v * cosY;
        return [xr, zr * 0.52 - (ground ? 0 : yDisp(y)) * 0.9];
      }
    : (x, y, z) => [x, -z];

  // fit-to-canvas over every point that may be drawn (ground + elevated in 3D)
  let pMinX = Infinity, pMaxX = -Infinity, pMinY = Infinity, pMaxY = -Infinity;
  for (const d of [A, B]) if (d) {
    const c = d.channels;
    for (let i = 0; i < c.pos_x.length; i++) {
      const y = c.pos_y ? c.pos_y[i] : 0;
      for (const g of three ? [true, false] : [false]) {
        const [sx, sy] = raw(c.pos_x[i], y, c.pos_z[i], g);
        pMinX = Math.min(pMinX, sx); pMaxX = Math.max(pMaxX, sx);
        pMinY = Math.min(pMinY, sy); pMaxY = Math.max(pMaxY, sy);
      }
    }
  }
  const pad = 24;
  const scale = Math.min((cssW - 2 * pad) / Math.max(1e-6, pMaxX - pMinX),
                         (cssH - 2 * pad) / Math.max(1e-6, pMaxY - pMinY));
  const offX = (cssW - (pMaxX - pMinX) * scale) / 2 - pMinX * scale;
  const offY = (cssH - (pMaxY - pMinY) * scale) / 2 - pMinY * scale;
  const P = (x, y, z, ground) => {
    const [sx, sy] = raw(x, y, z, ground);
    return [sx * scale + offX, sy * scale + offY];
  };
  const at = (d, i, ground) => {
    const c = d.channels;
    return P(c.pos_x[i], c.pos_y ? c.pos_y[i] : 0, c.pos_z[i], ground);
  };

  const polyline = (d, ground, stroke, width) => {
    ctx.strokeStyle = stroke;
    ctx.lineWidth = width;
    ctx.beginPath();
    for (let i = 0; i < d.channels.pos_x.length; i++) {
      const [X, Y] = at(d, i, ground);
      i === 0 ? ctx.moveTo(X, Y) : ctx.lineTo(X, Y);
    }
    ctx.stroke();
  };

  if (three) {
    // ground shadow + elevation posts first, so the ribbon reads as floating
    if (B) polyline(B, true, "rgba(91,102,117,0.22)", 1.2);
    if (A) {
      polyline(A, true, "rgba(0,0,0,0.5)", 3);
      ctx.strokeStyle = "rgba(123,135,148,0.18)";
      ctx.lineWidth = 1;
      for (let i = 0; i < A.channels.pos_x.length; i += 14) {
        const [gx, gy] = at(A, i, true), [ax, ay] = at(A, i, false);
        ctx.beginPath(); ctx.moveTo(gx, gy); ctx.lineTo(ax, ay); ctx.stroke();
      }
    }
  }

  if (B) polyline(B, false, "#5b6675", 1.5);

  if (A) {
    let vals, lo = 0, hi = 1, colorFn;
    if (state.colorMode === "speed") {
      vals = A.channels.speed_kmh;
      lo = Math.min(...vals); hi = Math.max(...vals);
      colorFn = (v) => speedColor(v, lo, hi);
      $("#legend-scale").innerHTML =
        `<span class="swatch" style="background:hsl(220,80%,55%)"></span> ${speedFromKmh(lo).toFixed(0)} ` +
        `→ <span class="swatch" style="background:hsl(0,80%,55%)"></span> ${speedFromKmh(hi).toFixed(0)} ${speedUnit()}`;
    } else {
      vals = A.channels.slip_front.map((f, i) => Math.max(f, A.channels.slip_rear[i]));
      colorFn = slipColor;
      $("#legend-scale").innerHTML =
        `<span class="swatch" style="background:hsl(120,75%,55%)"></span> grip ` +
        `→ <span class="swatch" style="background:hsl(0,75%,55%)"></span> sliding`;
    }
    ctx.lineWidth = 3;
    ctx.lineCap = "round";
    let prev = at(A, 0, false);
    for (let i = 1; i < A.channels.pos_x.length; i++) {
      const cur = at(A, i, false);
      ctx.strokeStyle = colorFn(vals[i]);
      ctx.beginPath();
      ctx.moveTo(prev[0], prev[1]);
      ctx.lineTo(cur[0], cur[1]);
      ctx.stroke();
      prev = cur;
    }
    const [sx, sy] = at(A, 0, false);
    ctx.fillStyle = "#34d399";
    ctx.beginPath();
    ctx.arc(sx, sy, 6, 0, Math.PI * 2);
    ctx.fill();

    // drive direction: stem + arrowhead pointing along the first stretch of
    // the lap (sampled in screen space, so it follows the 3D projection too)
    const nPts = A.channels.pos_x.length;
    let j = 1;
    while (j < nPts - 1) {
      const [px, py] = at(A, j, false);
      if (Math.hypot(px - sx, py - sy) >= 14) break;
      j++;
    }
    const [hx, hy] = at(A, j, false);
    if (Math.hypot(hx - sx, hy - sy) > 2) {
      const ang = Math.atan2(hy - sy, hx - sx);
      const ax = sx + Math.cos(ang) * 24, ay = sy + Math.sin(ang) * 24;
      ctx.strokeStyle = "#34d399";
      ctx.lineWidth = 2.5;
      ctx.lineCap = "round";
      ctx.beginPath();
      ctx.moveTo(sx + Math.cos(ang) * 9, sy + Math.sin(ang) * 9);
      ctx.lineTo(ax, ay);
      ctx.stroke();
      ctx.beginPath();
      ctx.moveTo(ax + Math.cos(ang) * 9, ay + Math.sin(ang) * 9);
      ctx.lineTo(ax + Math.cos(ang + 2.4) * 6.5, ay + Math.sin(ang + 2.4) * 6.5);
      ctx.lineTo(ax + Math.cos(ang - 2.4) * 6.5, ay + Math.sin(ang - 2.4) * 6.5);
      ctx.closePath();
      ctx.fill();
    }

    // driven length from integrated speed - FH6's DistanceTraveled is a
    // track-position parameter on real circuits, not meters
    let drivenM = 0;
    for (let i = 1; i < A.t.length; i++)
      drivenM += (A.channels.speed_kmh[i] + A.channels.speed_kmh[i - 1]) / 7.2 * (A.t[i] - A.t[i - 1]);

    const side = $("#map-side");
    const lapMeta = state.laps.find((l) => l.id === state.lapA);
    // landings (spikes on jump touchdowns) are shown but not counted as contact
    const contacts = (A.collisions || []).filter((h) => !h.landing).length;
    const landings = (A.collisions?.length || 0) - contacts;
    side.innerHTML = `<div class="lap-grid" style="text-align:left">
      <div><div class="label">Lap A</div><div class="value">${fmtLap(lapMeta?.lap_time)}</div></div>
      <div><div class="label">Lap B</div><div class="value">${fmtLap(state.laps.find((l) => l.id === state.lapB)?.lap_time)}</div></div>
      <div><div class="label">Samples</div><div class="value">${A.n_frames}</div></div>
      <div><div class="label">Driven</div><div class="value">${distFromM(drivenM).toFixed(2)} ${distUnit()}</div></div>
      <div><div class="label">Elevation range</div><div class="value">${yRange > 0.3 ? yRange.toFixed(0) + " m" : "flat"}</div></div>
      <div><div class="label">Contacts</div><div class="value">${contacts ? `<span style="color:#ef4444">${contacts}</span>` : "0"}${landings ? `<span style="color:#f59e0b;font-size:0.9rem" title="${landings} hard jump landing${landings > 1 ? "s" : ""} — not contact"> +${landings} 🛬</span>` : ""}</div></div>
    </div>`;
  }

  // collision points (contact spikes) as red bursts, over the ribbon so
  // they read against any speed/slip color. B's are dimmer, like its line.
  // Jump landings (h.landing, classified server-side) draw amber and smaller:
  // worth seeing where a jump bottomed out, but they aren't contact.
  const drawHits = (d, fill, landFill, r) => {
    if (!d || !d.collisions) return;
    for (const h of [...d.collisions].sort((a, b) => (b.landing ? 1 : 0) - (a.landing ? 1 : 0))) {
      const [X, Y] = P(h.x, h.y ?? 0, h.z, false);
      const color = h.landing ? landFill : fill;
      const rad = h.landing ? r * 0.75 : r;
      ctx.save();
      ctx.strokeStyle = "#fff";
      ctx.lineWidth = 1.5;
      ctx.fillStyle = color;
      ctx.shadowColor = color;
      ctx.shadowBlur = 8;
      // 4-point spark
      ctx.beginPath();
      for (let k = 0; k < 8; k++) {
        const a = (k * Math.PI) / 4;
        const rr = k % 2 ? rad * 0.4 : rad;
        const px = X + Math.cos(a) * rr, py = Y + Math.sin(a) * rr;
        k === 0 ? ctx.moveTo(px, py) : ctx.lineTo(px, py);
      }
      ctx.closePath();
      ctx.fill();
      ctx.stroke();
      ctx.restore();
    }
  };
  if (getSettings().contactLayer) {
    drawHits(B, "#7f5b5b", "#7f6f4b", 6);
    drawHits(A, "#ef4444", "#f59e0b", 7);
  }

  // cache the finished frame + projection for the chart-cursor marker
  mapCursor.proj = P;
  mapCursor.dpr = dpr;
  if (!mapCursor.snap) mapCursor.snap = document.createElement("canvas");
  mapCursor.snap.width = canvas.width;
  mapCursor.snap.height = canvas.height;
  mapCursor.snap.getContext("2d").drawImage(canvas, 0, 0);
  if (mapCursor.idx != null) drawMapMarker();
}

/* ---------------- comparison charts ---------------- */

function interp(xs, ys, xq) {
  const out = new Array(xq.length);
  let i = 0;
  for (let k = 0; k < xq.length; k++) {
    const x = xq[k];
    while (i < xs.length - 2 && xs[i + 1] < x) i++;
    if (x <= xs[0]) out[k] = ys[0];
    else if (x >= xs[xs.length - 1]) out[k] = ys[ys.length - 1];
    else out[k] = ys[i] + (ys[i + 1] - ys[i]) * ((x - xs[i]) / (xs[i + 1] - xs[i]));
  }
  return out;
}

const AXIS = { stroke: "#7b8794", grid: { stroke: "#242e3b" }, ticks: { stroke: "#242e3b" } };

function makeChart(el, title, xVals, seriesDefs, height = 150) {
  const opts = {
    title, width: el.clientWidth, height,
    cursor: { sync: { key: "fc" } },
    // hovering any chart marks the matching spot on the track map (the
    // charts all share lap A's x-array, so the cursor idx maps 1:1)
    hooks: { setCursor: [(u) => setMapCursor(u.cursor.idx ?? null)] },
    scales: { x: { time: false } },
    // x is DistanceTraveled progress: on real FH6 circuits it is a per-route
    // track-position parameter, not literal meters - ideal for aligning laps,
    // wrong to label as a length
    axes: [
      { ...AXIS, label: "track position" },
      { ...AXIS },
    ],
    series: [
      { label: "dist" },
      ...seriesDefs.map((s) => ({ width: 1.6, ...s })),
    ],
  };
  const data = [xVals, ...seriesDefs.map((s) => s._vals)];
  state.charts.push(new uPlot(opts, data, el));
}

function drawCharts() {
  const holder = $("#charts");
  if (!holder) return;
  for (const c of state.charts) c.destroy();
  state.charts = [];
  holder.innerHTML = "";

  const A = state.dataA;
  if (!A) {
    holder.innerHTML = `<div class="empty-hint">Tag lap A (and optionally B) above.</div>`;
    $("#cmp-label").textContent = "";
    return;
  }
  const B = state.dataB;
  $("#cmp-label").textContent = B ? "— A colored, B gray" : "";

  const x = A.dist;
  const chan = (d, name) => d.channels[name];
  const onA = (name) => chan(A, name);
  const onB = (name) => (B ? interp(B.dist, chan(B, name), x) : null);

  const div = () => {
    const el = document.createElement("div");
    holder.appendChild(el);
    return el;
  };

  // cache B interpolations once
  const onBcache = {};
  if (B) for (const name of ["speed_kmh", "throttle", "brake", "steer", "lap_time"])
    onBcache[name] = onB(name);

  if (B) {
    const dt = onA("lap_time").map((tA, i) => tA - onBcache.lap_time[i]);
    makeChart(div(), "Δ time (A − B), negative = A ahead", x,
      [{ label: "Δs", stroke: "#22d3ee", fill: "rgba(34,211,238,0.08)", _vals: dt }], 170);
  }

  const toSpeed = (arr) => arr.map(speedFromKmh);
  makeChart(div(), `Speed (${speedUnit()})`, x, [
    { label: "A", stroke: "#22d3ee", _vals: toSpeed(onA("speed_kmh")) },
    ...(B ? [{ label: "B", stroke: "#5b6675", _vals: toSpeed(onBcache.speed_kmh) }] : []),
  ]);

  makeChart(div(), "Throttle / Brake (%)", x, [
    { label: "thr A", stroke: "#34d399", _vals: onA("throttle") },
    { label: "brk A", stroke: "#f87171", _vals: onA("brake") },
    ...(B ? [
      { label: "thr B", stroke: "#34d399", dash: [5, 5], width: 1, _vals: onBcache.throttle },
      { label: "brk B", stroke: "#f87171", dash: [5, 5], width: 1, _vals: onBcache.brake },
    ] : []),
  ]);

  makeChart(div(), "Steering (%)", x, [
    { label: "A", stroke: "#93a3b8", _vals: onA("steer") },
    ...(B ? [{ label: "B", stroke: "#5b6675", dash: [5, 5], width: 1, _vals: onBcache.steer }] : []),
  ], 120);

  makeChart(div(), "Tire combined slip (front / rear) — 1.0 = grip limit", x, [
    { label: "front", stroke: "#fbbf24", _vals: onA("slip_front") },
    { label: "rear", stroke: "#f87171", _vals: onA("slip_rear") },
  ], 140);
}

window.addEventListener("resize", () => { drawMap(); drawCharts(); });
// live-apply unit / layer changes from the settings panel
onSettingsChange(() => { drawMap(); drawCharts(); });
loadSessions();
setInterval(loadSessions, 15000); // pick up newly finished sessions
