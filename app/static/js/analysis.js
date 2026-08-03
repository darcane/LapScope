/* Session browser, track map, and multi-lap comparison charts (issue #30). */

const LAP_CHANNELS = "speed_kmh,throttle,brake,steer,slip_front,slip_rear,lap_time,pos_x,pos_y,pos_z";

/* raw_* channel names (the backend generates the same set from packet.FIELDS);
   appended to lap fetches only while the Settings raw-data toggle is on */
const RAW_CHANNELS = RAW_FIELDS.flatMap(([name, count]) =>
  count === 1 ? [`raw_${name}`] : RAW_WHEELS.map((w) => `raw_${name}_${w}`)).join(",");
const channelList = () =>
  getSettings().rawAnalysis ? `${LAP_CHANNELS},${RAW_CHANNELS}` : LAP_CHANNELS;

/* overlay palette: distinct on the dark theme and identical between map and
   charts. Entry 0 follows the Settings accent so a single picked lap matches
   the UI; the base color nearest the chosen accent is swapped for cyan
   (ACCENTS[..].clash, settings.js) so six overlaid laps stay tellable-apart. */
const BASE_PICK_COLORS = ["#22d3ee", "#f59e0b", "#a78bfa", "#34d399", "#f472b6", "#94a3b8"];
const MAX_PICKS = BASE_PICK_COLORS.length;

function accentPickPalette() {
  const a = accentDef();
  const pal = [...BASE_PICK_COLORS];
  pal[0] = a.pick;
  if (a.clash > 0) pal[a.clash] = BASE_PICK_COLORS[0];
  return pal;
}
let PICK_COLORS = accentPickPalette();

const state = {
  // exactly one of sessionId / groupId is set: the detail pane shows a single
  // session or a merged run group, and the lap table, header and picks all
  // key off which
  sessionId: null,
  groupId: null,
  session: null,  // resolved session object of the *displayed* session
  group: null,
  allSessions: [],  // last /api/sessions payload; browse.js filters over it
  sessionsById: {},  // every session the displayed laps came from, by id
  laps: [],
  // comparison tray, ordered; picks[0] is the reference lap ("A") the Δ chart,
  // zoom window and map extras are based on. Picks persist across session
  // switches (cross-session overlay), each carrying its own lap/session meta.
  // pick = { lapId, lap, session, color, data, auto }
  picks: [],
  colorMode: getSettings().defaultColor,
  mapMode: getSettings().defaultMapMode,
  charts: [],
  zoomRange: null,  // [distLo, distHi] while the charts are drag-zoomed
};

const pickOf = (lapId) => state.picks.find((p) => p.lapId === lapId);
const refPick = () => (state.picks[0] && state.picks[0].data ? state.picks[0] : null);
const pickLetter = (pick) => String.fromCharCode(65 + state.picks.indexOf(pick)); // A–F
const nextColor = () => PICK_COLORS.find((c) => !state.picks.some((p) => p.color === c));
function shortName(session) {
  const n = displayName(session);
  return n.length > 14 ? n.slice(0, 13) + "…" : n;
}
/* tray chips / captions: the plain lap number is enough while every pick is
   from the displayed session; session names only appear once picks cross */
function chipLabel(pick, crossSession) {
  // run_index numbers a pick within a merged group, where every sprint
  // member's own lap_number is 0
  const n = pick.lap.run_index ?? pick.lap.lap_number + 1;
  return crossSession
    ? `${shortName(pick.session)} · ${pick.session.route_kind === "sprint" ? "R" : "L"}${n}`
    : lapLabel(pick.session.route_kind, n);
}

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

/* Overlapping polls can land out of order and paint a stale list (the same
   rule selectSession applies to its payload). */
let sessionsSeq = 0;

async function loadSessions() {
  const seq = ++sessionsSeq;
  let list;
  try {
    list = await (await fetch("/api/sessions")).json();
  } catch { return; }  // server briefly away (restart): keep the current list
  if (seq !== sessionsSeq) return;  // an older poll landed late
  state.allSessions = list;
  browseIndex(list);   // the facet menus read the unfiltered list
  renderSessionList();
}

/* Exactly the fields a card renders — nothing else may enter the signature.
   state.sessionId in particular must stay out: selection is a class toggle
   in markActive(), and folding it in here would rebuild (and so scroll to
   the top of) the list on every click. */
const cardSig = (s) =>
  [s.id, s.name, s.route_name, s.lap_count, s.best_lap, s.car_name, s.car_known,
   s.car_class_letter, s.car_pi, s.drivetrain, s.track_type, s.conditions,
   s.route_kind, s.group_id, s.group_name].join("|");

let lastListSig = null;

/* Rebuilding #session-list empties a scrolled overflow container, which
   clamps scrollTop to 0 — and this runs on a 15 s poll plus after every
   edit. Rebuild only when the rendered content actually changed, and keep
   the scroll offset when it does. */
function renderSessionList() {
  const el = $("#session-list");
  if (!el) return;
  const total = state.allSessions.length;
  const rows = browseApply(state.allSessions);
  browseStatus(rows.length, total);
  const sig = rows.map(cardSig).join("/");
  if (sig === lastListSig) { markActive(el); return; }
  lastListSig = sig;
  const top = el.scrollTop;
  el.replaceChildren(...(rows.length ? collapseGroups(rows) : [emptyHint(total)]));
  el.scrollTop = top;
  markActive(el);
  renderMergeHint();
}

/* A merged group is one card where its members would have been several, at
   the position of its most recent member. Built from the FILTERED rows, so a
   filter matching 3 of 5 members says so rather than misreporting the group. */
function collapseGroups(rows) {
  const seen = new Set();
  const out = [];
  for (const s of rows) {
    if (s.group_id == null) { out.push(sessionCard(s)); continue; }
    if (seen.has(s.group_id)) continue;
    seen.add(s.group_id);
    out.push(groupCard(rows.filter((x) => x.group_id === s.group_id),
                       state.allSessions.filter((x) => x.group_id === s.group_id)));
  }
  return out;
}

function groupCard(shown, all) {
  const lead = shown[0];
  const runs = all.reduce((n, s) => n + s.lap_count, 0);
  const bests = all.filter((s) => s.best_lap).map((s) => s.best_lap);
  const times = all.map((s) => s.started_at);
  const card = document.createElement("div");
  card.className = "session-card group-card";
  card.dataset.gid = lead.group_id;
  card.innerHTML = `
    <div class="title"><span class="group-mark" title="merged run group">⛓</span><span></span></div>
    <div class="meta-row">${classBadge(lead.car_class_letter, lead.car_pi)}${dtBadge(lead.drivetrain)}${trackBadge(lead.track_type)}${condBadge(lead.conditions)}</div>
    <div class="car-line"></div>
    <div class="sub"></div>`;
  $(".title span:last-child", card).textContent =
    lead.group_name || lead.route_name || fmtDate(Math.min(...times));
  $(".car-line", card).textContent = lead.car_name;
  const partial = shown.length < all.length
    ? ` (${shown.length} match)` : "";
  $(".sub", card).textContent =
    `${fmtDate(Math.max(...times))} · ${all.length} sessions${partial}`
    + ` · ${runs} ${lapWord(lead.route_kind, runs)}`
    + ` · best ${fmtLap(bests.length ? Math.min(...bests) : null)}`;
  card.onclick = () => selectGroup(lead.group_id);
  return card;
}

/* Sittings worth merging: two or more ungrouped attempts at one route in one
   car, close together in time. Offered, never applied — dismissals are
   remembered per cluster so a decision doesn't come back every 15 s. */
function mergeSuggestions() {
  const dismissed = new Set(JSON.parse(
    localStorage.getItem("ls_merge_dismissed") || "[]"));
  const byPair = {};
  for (const s of state.allSessions) {
    if (s.group_id != null || !s.route_id || s.car_ordinal == null) continue;
    (byPair[`${s.route_id}:${s.car_ordinal}`] ||= []).push(s);
  }
  const out = [];
  for (const rows of Object.values(byPair))
    for (const cluster of timeClusters(rows)) {
      const sig = cluster.map((s) => s.id).join(",");
      if (!dismissed.has(sig)) out.push({ sig, cluster });
    }
  // sprint routes first (a circuit session already holds several laps), then
  // the biggest sitting
  return out.sort((a, b) =>
    (b.cluster[0].route_kind === "sprint") - (a.cluster[0].route_kind === "sprint")
    || b.cluster.length - a.cluster.length);
}

function dismissMerges(sigs) {
  const seen = JSON.parse(localStorage.getItem("ls_merge_dismissed") || "[]");
  localStorage.setItem("ls_merge_dismissed",
                       JSON.stringify([...new Set([...seen, ...sigs])].slice(-200)));
}

function renderMergeHint() {
  const host = $("#merge-hint");
  if (!host) return;
  const all = mergeSuggestions();
  const [top] = all;
  host.innerHTML = "";
  host.style.display = top ? "" : "none";
  if (!top) return;
  const { cluster, sig } = top;
  const s = cluster[0];
  const text = document.createElement("div");
  // laps, not sessions: on a circuit two sessions are rarely two laps, and the
  // review list right behind this banner prints the real count
  const runs = cluster.reduce((n, x) => n + x.lap_count, 0);
  text.textContent =
    `${runs} ${lapWord(s.route_kind, runs)} on `
    + `${s.route_name || "this route"} in one sitting — merge them?`;
  host.appendChild(text);
  // the banner speaks for the newest sitting; the queue behind it is the
  // reason the review list exists, so say how deep it is
  if (all.length > 1) {
    const more = document.createElement("div");
    more.className = "merge-hint-more";
    more.textContent = `and ${all.length - 1} more sitting`
      + (all.length > 2 ? "s" : "");
    host.appendChild(more);
  }
  const row = document.createElement("div");
  row.className = "merge-hint-actions";
  const go = document.createElement("button");
  go.textContent = all.length > 1 ? "Review all" : "Review";
  go.title = "Go through every suggestion in one list";
  go.onclick = reviewMerges;
  const no = document.createElement("button");
  no.textContent = "Not now";
  no.title = all.length > 1
    ? "Hide this suggestion — the others stay" : "Hide this suggestion";
  no.onclick = () => { dismissMerges([sig]); renderMergeHint(); };
  row.append(go, no);
  host.append(row);
}

/* Every pending suggestion in one list, with the two verdicts applied to
   whatever is ticked: everything starts ticked, so the buttons read "merge
   all" / "dismiss all" until you say otherwise. Unticking is neither — that
   sitting stays pending and comes back. */
async function reviewMerges() {
  const all = mergeSuggestions();
  if (!all.length) return;
  const chosen = new Set(all.map((x) => x.sig));
  let handOff = null;   // set by a row's Edit button, opened after we close

  const extra = document.createElement("div");
  extra.className = "merge-review";

  const head = document.createElement("label");
  head.className = "rv-all";
  const headBox = document.createElement("input");
  headBox.type = "checkbox";
  headBox.checked = true;
  const headText = document.createElement("span");
  head.append(headBox, headText);
  extra.appendChild(head);

  /* showModal owns its buttons, so relabel them through the dialog we were
     mounted into rather than growing a callback API for one caller */
  const sync = () => {
    const box = extra.closest(".modal");
    const n = chosen.size;
    headBox.checked = n > 0;
    headBox.indeterminate = n > 0 && n < all.length;
    headText.textContent = `All ${all.length} sitting`
      + (all.length > 1 ? "s" : "") + (n < all.length ? ` (${n} picked)` : "");
    if (!box) return;
    $(".modal-ok", box).textContent = n ? `Merge ${n}` : "Merge";
    $(".modal-alt", box).textContent = n ? `Dismiss ${n}` : "Dismiss";
    for (const b of [$(".modal-ok", box), $(".modal-alt", box)]) b.disabled = !n;
  };
  headBox.onchange = () => {
    chosen.clear();
    if (headBox.checked) for (const x of all) chosen.add(x.sig);
    for (const cb of extra.querySelectorAll(".rv-row input"))
      cb.checked = headBox.checked;
    sync();
  };

  for (const { sig, cluster } of all) {
    const s = cluster[0];
    const runs = cluster.reduce((n, x) => n + x.lap_count, 0);
    const bests = cluster.filter((x) => x.best_lap).map((x) => x.best_lap);
    const label = document.createElement("label");
    label.className = "rv-row";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = true;
    cb.onchange = () => {
      cb.checked ? chosen.add(sig) : chosen.delete(sig);
      sync();
    };
    label.appendChild(cb);
    label.appendChild(routeOutline(s.route_id));   // never null: see the filter
    const text = document.createElement("span");
    text.className = "rv-text";
    const title = document.createElement("span");
    title.className = "rv-title";
    const name = document.createElement("span");
    name.textContent = s.route_name || "Unnamed route";
    const count = document.createElement("span");
    count.className = "rv-count";
    count.textContent = `${cluster.length} sessions`;
    title.append(name, count);
    const sub = document.createElement("span");
    sub.className = "rv-sub";
    sub.textContent = `${fmtDate(Math.min(...cluster.map((x) => x.started_at)))}`
      + ` · ${s.car_name} · ${runs} ${lapWord(s.route_kind, runs)}`
      + ` · best ${fmtLap(bests.length ? Math.min(...bests) : null)}`;
    text.append(title, sub);
    label.append(text);
    // the way back to the per-route dialog, which is where naming a group and
    // pulling in an attempt from another sitting still live
    const edit = document.createElement("button");
    edit.className = "rv-edit";
    edit.textContent = "Edit";
    edit.title = "Merge this one by hand: pick the sessions and name the group";
    edit.onclick = (e) => {
      e.preventDefault();
      handOff = s;
      $(".modal-cancel", extra.closest(".modal")).click();
    };
    label.appendChild(edit);
    extra.appendChild(label);
  }
  sync();

  const verdict = await showModal({
    title: "Merge suggestions",
    message: "Each row is several attempts at one route in one car, driven in"
      + " one sitting. Merging groups them into a single card — nothing is"
      + " rewritten, and you can ungroup at any time.",
    extra,
    // sync() can only relabel these once the dialog exists, so they have to
    // start out already counting
    okText: `Merge ${all.length}`,
    altText: `Dismiss ${all.length}`,
    cancelText: "Close",
    wide: true,
  });
  if (handOff) { await mergeRuns(handOff); return; }
  const picked = all.filter((x) => chosen.has(x.sig));
  if (verdict === null || !picked.length) return;
  if (verdict === MODAL_ALT) {
    dismissMerges(picked.map((x) => x.sig));
    renderMergeHint();
    return;
  }
  // one POST per sitting: /api/groups takes one group at a time, and a failure
  // on the third must not cost the two that already landed
  const failed = [];
  for (const { cluster } of picked) {
    const res = await fetch("/api/groups", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_ids: cluster.map((s) => s.id) }),
    });
    if (!res.ok) {
      const why = await res.json().catch(() => ({}));
      failed.push(`${cluster[0].route_name || "Unnamed route"}: `
        + (why.detail || `HTTP ${res.status}`));
    }
  }
  await loadSessions();
  if (failed.length)
    await uiAlert(failed.length === picked.length
      ? "Couldn't merge" : "Some sittings weren't merged",
      `Merged ${picked.length - failed.length} of ${picked.length}. ${failed[0]}`
      + (failed.length > 1 ? ` (and ${failed.length - 1} more)` : ""));
}

function emptyHint(total) {
  const el = document.createElement("div");
  el.className = "empty-hint";
  el.textContent = total
    ? "No sessions match these filters."
    : "No sessions recorded yet.";
  return el;
}

function markActive(el) {
  for (const card of el.querySelectorAll(".session-card"))
    card.classList.toggle("active", card.dataset.gid
      ? Number(card.dataset.gid) === state.groupId
      : Number(card.dataset.sid) === state.sessionId);
}

function sessionCard(s) {
  const card = document.createElement("div");
  card.className = "session-card";
  card.dataset.sid = s.id;
  card.innerHTML = `
    <div class="title"></div>
    <div class="meta-row">${classBadge(s.car_class_letter, s.car_pi)}${dtBadge(s.drivetrain)}${trackBadge(s.track_type)}${condBadge(s.conditions)}</div>
    <div class="car-line"></div>
    <div class="sub">${fmtDate(s.started_at)} · ${s.lap_count} ${lapWord(s.route_kind, s.lap_count)} · best ${fmtLap(s.best_lap)}</div>`;
  $(".title", card).textContent = displayName(s);
  $(".car-line", card).textContent = s.car_name;
  if (!s.car_known) {
    $(".car-line", card).classList.add("car-unknown");
    $(".car-line", card).title = "Unknown car — open the session to name or report it";
  }
  card.onclick = () => selectSession(s.id);
  if (s.best_lap) {
    // grind workflow: build the overlay straight from the sidebar, one
    // best lap per attempt, without opening each session (issue #30)
    const add = document.createElement("button");
    add.className = "card-add";
    add.title = `Add this session's best ${lapWord(s.route_kind)} to the comparison (click again to remove)`;
    add.textContent = "＋";
    add.onclick = async (e) => {
      e.stopPropagation();
      try {
        const payload = await (await fetch(`/api/sessions/${s.id}/laps`)).json();
        const best = payload.laps.find((l) => l.is_best);
        if (!best) return;
        const existing = pickOf(best.id);
        if (existing) { promoteManual(); removePick(existing); }
        else await addPick(best, payload.session);
      } catch (err) {
        uiAlert("Couldn't load session", String(err.message || err));
      }
    };
    card.appendChild(add);
  }
  return card;
}

/* rapid clicks race their fetches: only the latest selection may render its
   payload, or a slower response would show session A while state.sessionId
   is already B (same rule addPick applies to lap-data fetches) */
/* one counter for both entry points: a slow group payload must not paint
   over a session the user has already clicked, and vice versa */
let selectSeq = 0;

async function selectView(seq, url, onError) {
  try {
    const res = await fetch(url);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const payload = await res.json();
    return seq === selectSeq ? payload : null;  // newer selection landed
  } catch (err) {
    if (seq === selectSeq) uiAlert(onError, String(err.message || err));
    return null;
  }
}

/* Clone the detail pane and wire everything that doesn't care whether it is
   showing one session or a merged group: the map, its controls, and the
   lap-table header. */
function mountDetail() {
  const detail = $("#detail");
  detail.innerHTML = "";
  detail.appendChild($("#detail-template").content.cloneNode(true));
  $("#btn-map-png").onclick = exportMapPng;
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
      resetMapView(); // a 2D pan/zoom makes no sense under the 3D projection
      updateMapHint();
      drawMap();
    };
  }
  updateMapHint();
  bindMapDrag($("#trackmap"));
  bindMapContext($("#trackmap"));
}

/* Laps + the sessions they came from (one for a session view, several for a
   group). Every pick carries its own session, so this is what lets the tray,
   map, charts and PNG caption work identically either way. */
function applyPayload(laps, sessions) {
  state.laps = laps;
  state.sessionsById = Object.fromEntries(sessions.map((s) => [s.id, s]));
  resetMapView();  // stale zoom/pan aimed at the old track's geometry (#48)
  renderLapHeader();
  renderSessionList();  // move the active highlight; no refetch, no scroll jump

  // preselect: best lap as A — but only while the tray holds no deliberate
  // picks. A manually built comparison must survive browsing other sessions
  // (the whole point of the cross-session overlay); the lone auto pick from
  // casual browsing is replaced like before.
  if (!state.picks.some((p) => !p.auto)) {
    state.picks.length = 0;
    const best = laps.find((l) => l.is_best);
    if (best) {
      addPick(best, state.sessionsById[best.session_id], { auto: true });
      return;  // renders itself
    }
  }
  renderPicks();
}

async function selectSession(id) {
  const seq = ++selectSeq;
  state.sessionId = id;
  state.groupId = null;
  const payload = await selectView(
    seq, `/api/sessions/${id}/laps`, "Couldn't load session");
  if (!payload) return;
  state.session = payload.session;  // PNG export captions from it
  mountDetail();
  wireSessionHeader(payload.session);
  applyPayload(payload.laps, [payload.session]);
}

function wireSessionHeader(s) {
  $("#session-title").textContent = displayName(s);
  $("#header-badges").innerHTML = classBadge(s.car_class_letter, s.car_pi) + dtBadge(s.drivetrain);
  // route_id, not route_name: routes are created nameless, so an
  // identified-but-unnamed route must not read as "not identified"
  $("#header-car").textContent = s.car_name + (s.route_id ? "" : "  ·  route not identified yet (complete a lap)");
  if (!s.car_known) {
    // community list doesn't know this ordinal yet: make it one click to fix
    const help = document.createElement("button");
    help.type = "button";
    help.className = "car-unknown-hint";
    help.textContent = "unknown car — help name it";
    help.onclick = () => renameCar(s);
    $("#header-car").appendChild(help);
  }
  const patch = (body) => fetch(`/api/sessions/${s.id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  $("#cond-select").value = s.conditions || "";
  $("#cond-select").onchange = async (e) => { await patch({ conditions: e.target.value }); loadSessions(); };
  $("#track-select").value = s.track_type || "";
  $("#track-select").onchange = async (e) => {
    await patch({ track_type: e.target.value });
    await maybeRetagRoute(s, e.target.value);
    loadSessions();
  };
  $("#btn-rename").onclick = () => renameSession(s);
  $("#btn-route").onclick = () => renameRoute(s);
  $("#btn-car").onclick = () => renameCar(s);
  $("#btn-reprocess").onclick = () => reprocessSession(s);
  $("#btn-reset-edits").style.display = s.edit_count ? "" : "none";
  $("#btn-reset-edits").onclick = () => resetEdits(s);
  $("#btn-export").onclick = () => { window.location = `/api/sessions/${s.id}/export.csv`; };
  $("#btn-delete").onclick = () => deleteSession(s);
  $("#btn-merge").onclick = () => mergeRuns(s);
  $("#btn-merge").style.display = mergeCandidates(s).length ? "" : "none";
}

/* A merged group: several attempts at one route, browsed and scored as one.
   The session-scoped controls (type/conditions tags, Reprocess, Reset edits,
   Export CSV, Delete) act on exactly one session, so they are hidden here —
   each run row opens its own session for those. */
async function selectGroup(id) {
  const seq = ++selectSeq;
  state.sessionId = null;
  state.groupId = id;
  const payload = await selectView(
    seq, `/api/groups/${id}/laps`, "Couldn't load group");
  if (!payload) return;
  const g = payload.group;
  state.group = g;
  state.session = payload.sessions[0];  // PNG export captions from it
  mountDetail();
  wireGroupHeader(g, payload.sessions);
  applyPayload(payload.laps, payload.sessions);
}

function wireGroupHeader(g, members) {
  $("#session-title").textContent = g.display_name;
  $("#header-badges").innerHTML =
    classBadge(g.car_class_letter, g.car_pi) + dtBadge(g.drivetrain)
    + `<span class="group-badge" title="${members.length} sessions merged into one run group">⛓ merged</span>`;
  $("#header-car").textContent =
    `${g.car_name}  ·  ${members.length} sessions, ${g.run_count} ${lapWord(g.route_kind, g.run_count)}`;
  if (g.mixed) {
    const warn = document.createElement("span");
    warn.className = "tray-warn";
    warn.textContent = "⚠ members disagree on route or car";
    warn.title = "A reprocess re-fingerprinted a member onto another route."
      + " Times across it aren't comparable — remove it from the group.";
    $("#header-car").appendChild(warn);
  }
  for (const sel of ["#track-select", "#cond-select"]) $(sel).style.display = "none";
  for (const btn of ["#btn-car", "#btn-reprocess", "#btn-reset-edits",
                     "#btn-export", "#btn-merge"])
    $(btn).style.display = "none";
  $("#btn-rename").onclick = () => renameGroup(g);
  $("#btn-route").onclick = () => renameRoute(members[0]);
  $("#btn-delete").textContent = "Ungroup";
  $("#btn-delete").classList.remove("danger");
  $("#btn-delete").title = "Split back into separate sessions — nothing is deleted";
  $("#btn-delete").onclick = () => ungroup(g);
}

const cap = (s) => s.replace(/^./, (c) => c.toUpperCase());

/* Sprint routes produce one timed run per visit, so the whole lap panel
   renames itself off the route's kind; a group view gains a column naming
   the session each run came from. Runs on mount, before the first
   renderLapRows / drawMap. */
function renderLapHeader() {
  const kind = state.session && state.session.route_kind;
  const one = lapWord(kind), many = lapWord(kind, 2);
  const group = state.groupId !== null;
  // timed rows only, so the heading agrees with the sidebar card's count
  // (the table also lists incomplete laps, which never score)
  const timed = state.laps.filter((l) => l.lap_time).length;
  const n = Object.keys(state.sessionsById).length;
  $("#laps-heading").textContent = group
    ? `${cap(many)} · ${timed} across ${n} session${n === 1 ? "" : "s"}`
    : cap(many);
  const head = $("#lap-head");
  head.innerHTML = "";
  const cols = ["Compare", cap(one), ...(group ? ["Recorded"] : []),
                "Time", "Gap", "", ""];
  for (const label of cols) {
    const th = document.createElement("th");
    th.textContent = label;
    head.appendChild(th);
  }
  const side = $("#map-side");
  if (side.classList.contains("empty-hint"))
    side.textContent =
      `Tag a ${one} to draw its racing line — add more to overlay them.`;
}

function renderLapRows() {
  const tbody = $("#lap-rows");
  if (!tbody) return;
  tbody.innerHTML = "";
  const word = lapWord(state.session && state.session.route_kind);
  const group = state.groupId !== null;
  for (const l of state.laps) {
    const tr = document.createElement("tr");
    if (l.is_best) tr.className = "best";
    if (l.excluded) tr.className = "excluded";
    // an override never changes the icons' meaning, just their source
    const edited = (l.flags || "") !== (l.flags_auto || "");
    const pick = pickOf(l.id);
    // one toggle per lap; a picked lap wears its overlay letter + color
    const pickChip = pick
      ? `<span class="pick on" style="color:${pick.color};border-color:${pick.color};background:${pick.color}26" title="Remove from the comparison">${pickLetter(pick)}</span>`
      : `<span class="pick" title="Add to the comparison (up to ${MAX_PICKS} laps, across sessions)">+</span>`;
    // in a group every sprint member's own lap_number is 0, so the number
    // has to come from the group's ordering
    tr.innerHTML = `
      <td>${pickChip}</td>
      <td>${group ? l.run_index : l.lap_number + 1}</td>
      ${group ? `<td class="run-when"><button class="lap-act act-open" title="Open this session on its own">${fmtDate(state.sessionsById[l.session_id].started_at)}</button></td>` : ""}
      <td class="lap-time">${fmtLap(l.lap_time)}</td>
      <td>${l.gap_to_best != null && l.gap_to_best > 0 ? "+" + l.gap_to_best.toFixed(3) : (l.is_best ? "best" : "")}</td>
      <td style="color:var(--muted)">${flagIcons(l.flags)}${edited ? `<span class="lap-flag" title="flags edited by you — ✎ to change, Reset edits to undo">✎</span>` : ""}${l.lap_time ? "" : " incomplete"}${l.excluded ? " excluded" : ""}</td>
      <td class="lap-actions">
        <button class="lap-act act-csv" title="Download this ${word}'s telemetry as CSV">⬇</button>
        <button class="lap-act act-flags" title="Edit this ${word}'s flags">✎</button>
        <button class="lap-act act-exclude" title="${l.excluded ? `Restore the ${word} into bests and counts` : `Exclude the ${word} from bests and counts`}">${l.excluded ? "↩" : "🗑"}</button>
        ${group ? `<button class="lap-act act-unmerge" title="Take this session back out of the group">⛓</button>` : ""}
      </td>`;
    $(".pick", tr).onclick = () => pickLap(l.id);
    $(".act-csv", tr).onclick = () => { window.location = `/api/laps/${l.id}/export.csv`; };
    $(".act-flags", tr).onclick = () => editLapFlags(l);
    $(".act-exclude", tr).onclick = () => toggleLapExcluded(l);
    if (group) {
      $(".act-open", tr).onclick = () => selectSession(l.session_id);
      $(".act-unmerge", tr).onclick = () => removeFromGroup(l.session_id);
    }
    tbody.appendChild(tr);
  }
}

/* Re-fetch every picked lap's channel data (after a manual edit, or because
   the raw-data toggle changed the channel list). */
async function refetchPickData() {
  for (const p of state.picks) {
    const res = await fetch(
      `/api/laps/${p.lapId}/data?channels=${channelList()}&max_points=1500`);
    if (res.ok) p.data = await res.json();
  }
}

/* Re-fetch the session's laps + every picked lap's data after a manual edit,
   keeping the comparison tray (unlike selectSession's auto-pick reset). */
async function reloadSession() {
  const group = state.groupId !== null;
  const payload = await (await fetch(group
    ? `/api/groups/${state.groupId}/laps`
    : `/api/sessions/${state.sessionId}/laps`)).json();
  const sessions = group ? payload.sessions : [payload.session];
  state.laps = payload.laps;
  state.sessionsById = Object.fromEntries(sessions.map((x) => [x.id, x]));
  state.session = sessions[0];
  if (group) state.group = payload.group;
  const reset = $("#btn-reset-edits");
  if (reset) reset.style.display =
    (group ? payload.group.edit_count : payload.session.edit_count) ? "" : "none";
  for (const p of state.picks) {
    // lap meta (flags / excluded / is_best) moved for picks of the shown view
    const fresh = state.sessionsById[p.session.id];
    if (fresh) {
      p.session = fresh;
      const lap = payload.laps.find((l) => l.id === p.lapId);
      if (lap) p.lap = lap;
    }
  }
  // channel data refreshed for all: a map right-click can dismiss a contact
  // on a pick from a session other than the displayed one
  await refetchPickData();
  renderPicks();
  loadSessions();  // lap counts / best on the session cards may have moved
}

const lapPatch = (lapId, body) => fetch(`/api/laps/${lapId}`, {
  method: "PATCH",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

/* checkbox editor over the recorder's dirty-lap markers; stored as a
   read-time override, so Reprocess keeps it and Reset edits undoes it */
async function editLapFlags(lap) {
  const kind = state.session && state.session.route_kind;
  const extra = document.createElement("div");
  extra.className = "flag-editor";
  const current = new Set((lap.flags || "").split(",").filter(Boolean));
  const boxes = {};
  for (const [flag, [icon, desc]] of Object.entries(FLAG_META)) {
    const label = document.createElement("label");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = current.has(flag);
    boxes[flag] = cb;
    label.append(cb, ` ${icon} ${flag}`);
    label.title = desc;
    extra.appendChild(label);
  }
  const detected = (lap.flags_auto || "").split(",").filter(Boolean)
    .map((f) => (FLAG_META[f] ? `${FLAG_META[f][0]} ${f}` : f)).join(", ") || "none";
  const hint = document.createElement("p");
  hint.className = "modal-hint";
  hint.textContent = `Detected by the recorder: ${detected}. Matching it removes your override.`;
  extra.appendChild(hint);
  const ok = await showModal({
    title: `${lapLabel(kind, lap.lap_number + 1)} — flags`,
    message: `Detection is heuristic; correct this ${lapWord(kind)}'s markers here.`
      + " Reprocess keeps your choice.",
    extra, okText: "Save",
  });
  if (!ok) return;
  const flags = Object.keys(boxes).filter((f) => boxes[f].checked).join(",");
  await lapPatch(lap.id, { flags });
  reloadSession();
}

/* excluded laps stay listed (grayed) but drop out of best/gap and the
   session card's lap count - reversible, so no confirm */
async function toggleLapExcluded(lap) {
  await lapPatch(lap.id, { excluded: !lap.excluded });
  reloadSession();
}

async function resetEdits(session) {
  const sure = await uiConfirm("Reset edits",
    "Remove every manual edit on this session — dismissed contact markers, lap flag "
    + "overrides, and excluded laps — and show exactly what the recorder detected?",
    { okText: "Reset", danger: true });
  if (!sure) return;
  await fetch(`/api/sessions/${session.id}/edits`, { method: "DELETE" });
  reloadSession();
}

/* ---------------- comparison tray ---------------- */

/* every deliberate pick action turns the whole tray into user intent: from
   then on selectSession stops replacing it (see the auto-pick rule there) */
function promoteManual() {
  for (const p of state.picks) p.auto = false;
}

async function addPick(lap, session, { auto = false } = {}) {
  if (pickOf(lap.id)) return;
  if (state.picks.length >= MAX_PICKS) {
    uiAlert("Comparison full",
      `Up to ${MAX_PICKS} laps can be overlaid — remove one from the tray first.`);
    return;
  }
  const pick = { lapId: lap.id, lap, session, color: nextColor(), data: null, auto };
  state.picks.push(pick);
  if (!auto) promoteManual();
  renderPicks();  // the chip/row shows up right away, data follows
  try {
    const res = await fetch(
      `/api/laps/${lap.id}/data?channels=${channelList()}&max_points=1500`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    // rapid clicks race their fetches: only a pick still in the tray may
    // land its data - a removed pick's late response must be dropped
    if (!state.picks.includes(pick)) return;
    pick.data = data;
  } catch (err) {
    if (!state.picks.includes(pick)) return; // stale failure; nothing to untag
    removePick(pick);                        // untag so the row doesn't lie
    uiAlert("Couldn't load lap data", String(err.message || err));
    return;
  }
  renderPicks();
}

function removePick(pick) {
  const i = state.picks.indexOf(pick);
  if (i >= 0) state.picks.splice(i, 1);
  renderPicks();
}

/* make a pick the reference lap ("A"): Δ, slip, zoom and map extras re-base */
function promoteRef(pick) {
  const i = state.picks.indexOf(pick);
  if (i <= 0) return;
  state.picks.splice(i, 1);
  state.picks.unshift(pick);
  promoteManual();
  renderPicks();
}

function pickLap(lapId) {
  const existing = pickOf(lapId);
  if (existing) {
    promoteManual();  // dropping a lap is deliberate too
    removePick(existing);
    return;
  }
  const lap = state.laps.find((l) => l.id === lapId);
  // the lap's OWN session, not the displayed one: in a group view they differ
  // per row, and the wrong one gives a wrong PNG caption and a bogus
  // cross-route warning
  if (lap) addPick(lap, state.sessionsById[lap.session_id] || state.session);
}

/* overlaying laps from different routes is allowed (route_id null = each
   unidentified session counts as its own route) but the traces live in each
   route's own world coordinates, so nothing will line up. Warn with a modal
   the moment an overlay first crosses routes — once per episode, re-armed
   when the tray is back to a single route — on top of the tray badge. */
let warnedCrossRoute = false;
function crossRoute() {
  return new Set(state.picks.map((p) => p.session.route_id ?? `s${p.session.id}`)).size > 1;
}
function maybeWarnCrossRoute() {
  if (!crossRoute()) { warnedCrossRoute = false; return; }
  if (warnedCrossRoute) return;
  warnedCrossRoute = true;
  uiAlert("⚠ Comparing laps from different routes",
    "The picked laps were recorded on different routes. Positions are world "
    + "coordinates, so the map overlay and the distance-aligned charts will "
    + "not line up — Δ time and racing-line spread are only meaningful "
    + "between laps of the same route.");
}

/* single funnel for "the pick set changed": re-render rows, tray, map, charts.
   A reference change invalidates the zoom window and the hover index — both
   live on the reference lap's distance axis. */
let lastRefId = null;
function renderPicks() {
  const ref = state.picks[0] || null;
  if ((ref ? ref.lapId : null) !== lastRefId) {
    lastRefId = ref ? ref.lapId : null;
    state.zoomRange = null;
    mapCursor.idx = null;
    mapCursor.pin = null;  // the pin is an index into the old reference's arrays
  }
  renderLapRows();
  renderTray();
  drawMap();
  drawCharts();
  renderRawSection();
  maybeWarnCrossRoute();
}

function renderTray() {
  const el = $("#cmp-tray");
  if (!el) return;
  el.innerHTML = "";
  el.style.display = state.picks.length ? "" : "none";
  if (!state.picks.length) return;
  const label = document.createElement("span");
  label.className = "tray-label";
  label.textContent = "Comparing";
  el.appendChild(label);
  // a group's own members are "this view", so their chips keep the short run
  // label — labelling them by session would print the same route name on
  // every chip
  const crossSession = state.picks.some((p) => !state.sessionsById[p.session.id]);
  for (const p of state.picks) {
    const chip = document.createElement("span");
    chip.className = "tray-chip";
    chip.style.borderColor = p.color;
    const sw = document.createElement("span");
    sw.className = "swatch";
    sw.style.background = p.color;
    chip.appendChild(sw);
    const txt = document.createElement("span");
    txt.textContent = `${pickLetter(p)} · ${chipLabel(p, crossSession)} — ${fmtLap(p.lap.lap_time)}`;
    chip.appendChild(txt);
    if (p === state.picks[0]) {
      const tag = document.createElement("span");
      tag.className = "ref-tag";
      tag.title = "Reference lap: Δ time, slip, zoom and map extras are based on it";
      tag.textContent = "ref";
      chip.appendChild(tag);
    } else {
      const star = document.createElement("button");
      star.title = "Make this the reference lap";
      star.textContent = "★";
      star.onclick = () => promoteRef(p);
      chip.appendChild(star);
    }
    const x = document.createElement("button");
    x.title = "Remove from the comparison";
    x.textContent = "×";
    x.onclick = () => { promoteManual(); removePick(p); };
    chip.appendChild(x);
    el.appendChild(chip);
  }
  // overlaying different routes is allowed but rarely what you want
  if (crossRoute()) {
    const warn = document.createElement("span");
    warn.className = "tray-warn";
    warn.textContent = "⚠ laps from different routes — the overlay will not align";
    el.appendChild(warn);
  }
  const clear = document.createElement("button");
  clear.className = "tray-clear";
  clear.textContent = "Clear";
  clear.title = "Empty the comparison tray";
  clear.onclick = () => { state.picks.length = 0; renderPicks(); };
  el.appendChild(clear);
}

/* ---------------- merged run groups ---------------- */

/* Ungrouped sessions this one could be merged with: same route, same car,
   both known. That is the whole rule — the time clustering below only decides
   what gets *suggested*. */
function mergeCandidates(session) {
  if (!session.route_id || session.car_ordinal == null) return [];
  return state.allSessions.filter(
    (s) => s.group_id == null && s.route_id === session.route_id
      && s.car_ordinal === session.car_ordinal);
}

/* One sitting. Measured over a real database, every gap between consecutive
   attempts at the same route in the same car was under 19 minutes, and the
   next gap up was 22 hours — anywhere in that gulf gives identical clusters,
   so this is the value that reads sensibly to a human rather than a fitted
   one. */
const MERGE_GAP_S = 2 * 3600;

function timeClusters(rows) {
  const out = [];
  let cur = [];
  for (const s of [...rows].sort((a, b) => a.started_at - b.started_at)) {
    if (cur.length && s.started_at - cur[cur.length - 1].started_at > MERGE_GAP_S) {
      if (cur.length > 1) out.push(cur);
      cur = [];
    }
    cur.push(s);
  }
  if (cur.length > 1) out.push(cur);
  return out;
}

async function mergeRuns(session) {
  const candidates = mergeCandidates(session);
  if (candidates.length < 2) {
    await uiAlert("Nothing to merge",
      "Merging needs at least two ungrouped sessions on this route in this car.");
    return;
  }
  // preselect the sitting this session belongs to; everything else is listed
  // and one click away
  const sitting = timeClusters(candidates).find(
    (c) => c.some((s) => s.id === session.id)) || [session];
  const chosen = new Set(sitting.map((s) => s.id));

  const extra = document.createElement("div");
  extra.className = "merge-list";
  for (const s of [...candidates].sort((a, b) => b.started_at - a.started_at)) {
    const label = document.createElement("label");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = chosen.has(s.id);
    cb.onchange = () => (cb.checked ? chosen.add(s.id) : chosen.delete(s.id));
    const text = document.createElement("span");
    text.textContent =
      `${fmtDate(s.started_at)} — ${s.lap_count} ${lapWord(s.route_kind, s.lap_count)}, best ${fmtLap(s.best_lap)}`;
    label.append(cb, text);
    extra.appendChild(label);
  }
  // "runs" as in attempts at the route, matching the header button — NOT
  // lapWord(), which would title a circuit's dialog "Merge laps" and promise
  // something this does not do
  const name = await uiPrompt("Merge runs", {
    value: "",
    placeholder: session.route_name || displayName(session),
    message: "Merged sessions share one card and one table, scored against"
      + " each other. Nothing is rewritten — you can ungroup at any time.",
    extra,
    okText: "Merge",
  });
  if (name === null) return;
  if (chosen.size < 2) {
    await uiAlert("Nothing to merge", "Pick at least two sessions.");
    return;
  }
  const res = await fetch("/api/groups", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: name.trim(), session_ids: [...chosen] }),
  });
  if (!res.ok) {
    await uiAlert("Couldn't merge", (await res.json()).detail || "failed");
    return;
  }
  const { group } = await res.json();
  await loadSessions();
  selectGroup(group.id);
}

async function renameGroup(group) {
  const name = await uiPrompt("Rename group", {
    value: group.name || "",
    placeholder: group.display_name,
    message: "Shown on the merged card. Leave empty to fall back to the route name.",
  });
  if (name === null) return;
  await fetch(`/api/groups/${group.id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: name.trim() }),
  });
  await loadSessions();
  selectGroup(group.id);
}

async function ungroup(group) {
  const sure = await uiConfirm("Ungroup",
    "Split this group back into separate sessions? Nothing is deleted — the"
    + " sessions, their telemetry and every manual edit stay exactly as they are.",
    { okText: "Ungroup" });
  if (!sure) return;
  await fetch(`/api/groups/${group.id}`, { method: "DELETE" });
  await loadSessions();
  $("#detail").innerHTML =
    `<div class="empty-hint">Ungrouped — the sessions are back in the list.</div>`;
  state.groupId = null;
  state.sessionId = null;
  renderSessionList();
}

async function removeFromGroup(sessionId) {
  const gid = state.groupId;
  const res = await fetch(`/api/groups/${gid}/sessions/${sessionId}`,
                          { method: "DELETE" });
  const out = await res.json();
  await loadSessions();
  if (out.pruned) { state.groupId = null; selectSession(sessionId); }
  else selectGroup(gid);
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

/* Name the route and correct its shape in one dialog. The shape decides
   whether the whole page says "lap" or "run", and the recorder's guess can
   be wrong (a circuit you only ever drove one lap of reads as a sprint until
   you drive two), so an override belongs next to the name. showModal renders
   `extra` above its input, so the picker is built here and read from the
   closure — same pattern as the lap flags editor. */
async function renameRoute(session) {
  if (!session.route_id) {
    await uiAlert("No route identified yet",
      "Routes are fingerprinted from the first completed lap — finish a lap on this route first.");
    return;
  }
  const detected = session.route_kind_auto;
  let kind = session.route_kind || "";
  const extra = document.createElement("div");
  // which course this actually is, drawn from its fastest recorded lap —
  // the one thing a name-this-route dialog was missing
  const map = routeOutline(session.route_id, { lazy: false });
  map.classList.add("big");
  extra.appendChild(map);
  const label = document.createElement("div");
  label.className = "modal-hint";
  label.textContent = detected
    ? `Shape (detected: ${detected === "sprint" ? "sprint" : "circuit"})`
    : "Shape";
  const seg = document.createElement("span");
  seg.className = "seg route-kind";
  for (const [value, text] of [["circuit", "Circuit — laps"],
                               ["sprint", "Sprint — runs"]]) {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = text;
    b.classList.toggle("active", kind === value);
    b.onclick = () => {
      // clicking the active choice clears the override back to the guess
      kind = kind === value ? "" : value;
      for (const x of seg.children) x.classList.toggle("active", x === b && kind);
    };
    seg.appendChild(b);
  }
  extra.append(label, seg);

  const name = await uiPrompt("Name route / circuit", {
    value: session.route_name || "",
    message: "Applies to every session recorded on this route.",
    extra,
  });
  if (name === null) return;  // cancelled: neither the name nor the shape changes
  const body = { kind };  // "" clears the override back to the detected shape
  if (name.trim()) body.name = name.trim();
  await fetch(`/api/routes/${session.route_id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  await loadSessions();  // the shape renames every card on this route
  selectSession(session.id);
}

/* a route's surface doesn't change: offer to apply a manually chosen track
   type to every session recorded on the same (fingerprint-recognized) route */
async function maybeRetagRoute(session, type) {
  if (!type || !session.route_id) return;
  const all = await (await fetch("/api/sessions")).json();
  const others = all.filter((x) => x.route_id === session.route_id && x.id !== session.id);
  if (!others.length) return;
  const [icon, label] = TRACK_META[type];
  const route = session.route_name || "this route";
  const ok = await uiConfirm(`Tag every session on ${route} as ${icon} ${label}?`,
    `${others.length} other session${others.length > 1 ? "s" : ""} recorded on the same route`
    + " will be retagged too (future sessions inherit it automatically).");
  if (!ok) return;
  await fetch(`/api/routes/${session.route_id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ track_type: type }),
  });
}

async function renameCar(session) {
  let extra = null;
  if (!session.car_known) {
    // unknown ordinal: naming it locally helps this install, reporting it
    // upstream puts the name in the community list for everyone
    extra = document.createElement("p");
    const a = document.createElement("a");
    a.href = unknownCarIssueUrl(session.car_ordinal);
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    a.textContent = "report it on GitHub";
    extra.append("This car isn't in the community list yet — also ", a,
      " so everyone gets the name.");
  }
  const name = await uiPrompt("Name car", {
    value: session.car_name || "",
    message: "Applies everywhere this car appears. Leave empty to reset to the built-in name.",
    extra,
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
    + "recorded before a fix, e.g. World Time Attack). Manual edits — dismissed "
    + "contacts, flag overrides, excluded laps — are kept.",
    { okText: "Reprocess" });
  if (!sure) return;
  const res = await fetch(`/api/sessions/${session.id}/reprocess`, { method: "POST" });
  if (!res.ok) {
    await uiAlert("Reprocess failed", (await res.json()).detail || "failed");
    return;
  }
  const { laps } = await res.json();
  await uiAlert("Reprocess complete",
    `${laps} completed ${lapWord(session.route_kind, laps)} found.`);
  // the replay recreates lap rows under recycled rowids: a kept pick could
  // silently point at a different lap - drop this session's picks instead
  state.picks = state.picks.filter((p) => p.session.id !== session.id);
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
  state.picks = state.picks.filter((p) => p.session.id !== session.id);
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
const map3d = { yaw: -0.9, drag: null };

/* map viewport on top of the fit-to-canvas projection, shared by 2D and 3D:
   wheel zooms about the cursor, dragging pans (2D always, 3D with Shift held —
   a plain 3D drag keeps rotating). zoom 1 = the classic full-fit frame. */
const mapView = { zoom: 1, panX: 0, panY: 0 };

function resetMapView() {
  mapView.zoom = 1;
  mapView.panX = 0;
  mapView.panY = 0;
}

/* chart cursor -> map marker: drawMap caches its finished frame plus the
   world->canvas projection, so hovering a chart only blits the cache and
   paints a dot where lap A was at that track position. pin = a clicked-in
   position (same index space): the raw table and the map marker fall back to
   it whenever the cursor is off the charts; without one, the old behavior
   (values freeze at the last hovered spot) still applies. */
const mapCursor = { idx: null, pin: null, proj: null, snap: null, dpr: 1 };

function setMapCursor(idx) {
  if (idx === mapCursor.idx) return;
  mapCursor.idx = idx;
  drawMapMarker();
  updateRawTable(idx ?? mapCursor.pin);
}

/* chart click: pin the hovered spot (click it again to unpin) */
function togglePin(idx) {
  if (idx == null) return;
  mapCursor.pin = mapCursor.pin === idx ? null : idx;
  drawMapMarker();
  updateRawTable(mapCursor.idx ?? mapCursor.pin);
  for (const c of state.charts) c.redraw(); // pin line on every chart
}

function lowerBound(xs, x) {
  let lo = 0, hi = xs.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (xs[mid] < x) lo = mid + 1; else hi = mid;
  }
  return lo;
}

function drawMapMarker() {
  const canvas = $("#trackmap");
  const ref = refPick();
  if (!canvas || !mapCursor.snap || !mapCursor.proj) return;
  const ctx = canvas.getContext("2d");
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.drawImage(mapCursor.snap, 0, 0);
  ctx.setTransform(mapCursor.dpr, 0, 0, mapCursor.dpr, 0, 0);
  if (!ref) return;
  // an index lives on the reference lap's arrays; every other lap is marked
  // at the same track position (its own lower bound on dist), so the dots'
  // spread IS the racing-line difference at that spot. filled = live hover,
  // hollow ring = the pinned point (both when hovering the pinned spot).
  const markAt = (idx, filled) => {
    const ri = Math.min(idx, ref.data.channels.pos_x.length - 1);
    const dist = ref.data.dist[ri];
    const dot = (p, i, r) => {
      const c = p.data.channels;
      const j = Math.min(i, c.pos_x.length - 1);
      const [x, y] = mapCursor.proj(c.pos_x[j], c.pos_y ? c.pos_y[j] : 0, c.pos_z[j], false);
      ctx.save();
      ctx.shadowColor = p.color;
      ctx.shadowBlur = 10;
      ctx.fillStyle = filled ? p.color : "#0b1520";
      ctx.strokeStyle = filled ? "#fff" : p.color;
      ctx.lineWidth = filled ? 2 : 2.5;
      ctx.beginPath();
      ctx.arc(x, y, r, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();
      ctx.restore();
    };
    for (const p of state.picks)
      if (p.data && p !== ref) dot(p, lowerBound(p.data.dist, dist), 4.5);
    dot(ref, ri, 6);  // reference last, on top
  };
  if (mapCursor.pin != null) markAt(mapCursor.pin, false);
  if (mapCursor.idx != null) markAt(mapCursor.idx, true);
}

/* screen-space contact markers of the last drawMap, for right-click hit
   testing (dismiss flow); refilled on every draw */
const hitMarkers = [];
const HIT_RADIUS_PX = 12;

function nearestMarker(x, y) {
  let best = null, bestD = HIT_RADIUS_PX;
  for (const m of hitMarkers) {
    const d = Math.hypot(m.x - x, m.y - y);
    if (d <= bestD) { best = m; bestD = d; }
  }
  return best;
}

/* right-click a contact spark -> "not a contact": the marker stops counting
   and the lap's 💥 flag lifts once no real contact remains (see issue #26) */
function bindMapContext(canvas) {
  canvas.addEventListener("contextmenu", async (e) => {
    const hit = nearestMarker(e.offsetX, e.offsetY);
    if (!hit) return;  // not on a marker: leave the browser menu alone
    e.preventDefault();
    const ok = await uiConfirm("Not a contact?",
      "Dismiss this contact marker? It stops counting toward Contacts (and the lap's "
      + "💥 flag lifts once no real contact remains). Reset edits brings it back.",
      { okText: "Dismiss" });
    if (!ok) return;
    const res = await fetch(`/api/laps/${hit.lapId}/dismiss_contact`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ t: hit.t }),
    });
    if (!res.ok) {
      await uiAlert("Dismiss failed", (await res.json()).detail || "failed");
      return;
    }
    reloadSession();
  });
}

function updateMapHint() {
  const el = $("#map-hint");
  if (!el) return;
  el.style.display = "";
  el.textContent = state.mapMode === "3d"
    ? "↔ drag to rotate · scroll to zoom · shift-drag to pan · double-click resets"
    : "scroll to zoom · drag to pan · double-click resets";
}

function bindMapDrag(canvas) {
  canvas.addEventListener("pointerdown", (e) => {
    const rotate = state.mapMode === "3d" && !e.shiftKey;
    if (!rotate && mapView.zoom <= 1) return; // nothing to pan at full fit
    map3d.drag = {
      rotate, x: e.clientX, y: e.clientY,
      yaw: map3d.yaw, panX: mapView.panX, panY: mapView.panY,
    };
    canvas.setPointerCapture(e.pointerId);
  });
  canvas.addEventListener("pointermove", (e) => {
    const d = map3d.drag;
    if (!d) return;
    if (d.rotate) {
      map3d.yaw = d.yaw + (e.clientX - d.x) * 0.008;
    } else {
      mapView.panX = d.panX + (e.clientX - d.x);
      mapView.panY = d.panY + (e.clientY - d.y);
    }
    drawMap();
  });
  const stop = () => { map3d.drag = null; };
  canvas.addEventListener("pointerup", stop);
  canvas.addEventListener("pointercancel", stop);
  // wheel: zoom about the cursor, so what you point at stays put
  canvas.addEventListener("wheel", (e) => {
    e.preventDefault();
    const zoom = Math.min(12, Math.max(1, mapView.zoom * Math.exp(-e.deltaY * 0.002)));
    if (zoom === mapView.zoom) return;
    const k = zoom / mapView.zoom;
    mapView.panX = e.offsetX - (e.offsetX - mapView.panX) * k;
    mapView.panY = e.offsetY - (e.offsetY - mapView.panY) * k;
    mapView.zoom = zoom;
    if (zoom === 1) { mapView.panX = 0; mapView.panY = 0; }
    drawMap();
  }, { passive: false });
  canvas.addEventListener("dblclick", () => { resetMapView(); drawMap(); });
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
  hitMarkers.length = 0; // refilled by drawHits below

  const loaded = state.picks.filter((p) => p.data);
  const ref = refPick();
  const multi = state.picks.length >= 2;  // overlay mode: solid distinct colors
  const three = state.mapMode === "3d";
  canvas.style.cursor = three || mapView.zoom > 1 ? "grab" : "default";
  // the speed/slip gradient only exists for a lone lap; in an overlay the
  // colors identify laps (issue #30), so the select disappears entirely — a
  // merely-disabled control still looked clickable and confused people
  const colorWrap = $("#color-wrap");
  if (colorWrap) colorWrap.style.display = multi ? "none" : "";
  if (!ref) { // the side stats and color legend describe the reference lap -
    $("#map-side").innerHTML = "";      // don't let them show a lap that was untagged
    $("#legend-scale").innerHTML = "";
  }
  if (!loaded.length) return;

  // world extent (elevation exaggeration is derived from it)
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity,
      minZ = Infinity, maxZ = -Infinity;
  for (const { data: d } of loaded) {
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
    for (const { data: d } of loaded) if (d.channels.pos_y) ys.push(...d.channels.pos_y);
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
  for (const { data: d } of loaded) {
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
    // the user viewport (wheel zoom + pan) sits on top of the full fit
    return [(sx * scale + offX) * mapView.zoom + mapView.panX,
            (sy * scale + offY) * mapView.zoom + mapView.panY];
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
    // ground shadow + elevation posts first, so the ribbon reads as floating;
    // the reference gets the full treatment, the rest a faint ground line
    for (const p of loaded) if (p !== ref)
      polyline(p.data, true, "rgba(91,102,117,0.22)", 1.2);
    if (ref) {
      polyline(ref.data, true, "rgba(0,0,0,0.5)", 3);
      ctx.strokeStyle = "rgba(123,135,148,0.18)";
      ctx.lineWidth = 1;
      for (let i = 0; i < ref.data.channels.pos_x.length; i += 14) {
        const [gx, gy] = at(ref.data, i, true), [ax, ay] = at(ref.data, i, false);
        ctx.beginPath(); ctx.moveTo(gx, gy); ctx.lineTo(ax, ay); ctx.stroke();
      }
    }
  }

  // non-reference traces: each in its tray color, under the reference
  for (const p of loaded) if (p !== ref) polyline(p.data, false, p.color, 2);

  if (ref) {
    const A = ref.data;
    // chart drag-zoom window -> the matching index range on the reference
    // trace (dist is the charts' shared x-array, ascending by construction)
    let zi = null;
    if (state.zoomRange && A.dist && A.dist.length) {
      const [lo, hi] = state.zoomRange;
      let i0 = 0, i1 = A.dist.length - 1;
      while (i0 < i1 && A.dist[i0] < lo) i0++;
      while (i1 > i0 && A.dist[i1] > hi) i1--;
      if (i0 < i1) zi = [i0, i1];
    }
    if (zi) {  // halo under the zoomed stretch so it pops against any colors
      ctx.save();
      ctx.strokeStyle = hexRgba(PICK_COLORS[0], 0.30);
      ctx.lineWidth = 11;
      ctx.lineJoin = "round";
      ctx.lineCap = "round";
      ctx.beginPath();
      for (let i = zi[0]; i <= zi[1]; i++) {
        const [X, Y] = at(A, i, false);
        i === zi[0] ? ctx.moveTo(X, Y) : ctx.lineTo(X, Y);
      }
      ctx.stroke();
      ctx.restore();
    }
    if (!multi) {
      // a lone lap keeps the telemetry gradient (speed / slip)
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
        // everything outside the zoom window fades so the span reads instantly
        ctx.globalAlpha = zi && (i <= zi[0] || i > zi[1]) ? 0.22 : 1;
        ctx.strokeStyle = colorFn(vals[i]);
        ctx.beginPath();
        ctx.moveTo(prev[0], prev[1]);
        ctx.lineTo(cur[0], cur[1]);
        ctx.stroke();
        prev = cur;
      }
      ctx.globalAlpha = 1;
    } else {
      // overlay: the reference wears its solid tray color, on top and wider
      $("#legend-scale").innerHTML = "";
      polyline(A, false, ref.color, 3);
    }
    if (zi) {  // span endpoints: filled = where the window starts, hollow = ends
      const zc = PICK_COLORS[0];
      for (const [idx, fill, ring] of [[zi[0], zc, "#fff"], [zi[1], "#0b1520", zc]]) {
        const [X, Y] = at(A, idx, false);
        ctx.save();
        ctx.shadowColor = zc;
        ctx.shadowBlur = 8;
        ctx.fillStyle = fill;
        ctx.strokeStyle = ring;
        ctx.lineWidth = 2.5;
        ctx.beginPath();
        ctx.arc(X, Y, 5.5, 0, Math.PI * 2);
        ctx.fill();
        ctx.stroke();
        ctx.restore();
      }
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
    // landings (jump touchdowns) and user-dismissed spikes don't count
    const contacts = (A.collisions || []).filter((h) => !h.landing && !h.dismissed).length;
    const jumps = A.jumps || [];
    const hard = jumps.filter((j) => j.hard).length;
    // one cell per picked lap (letter + color match tray, map and charts);
    // letters and numbers only - session names stay in the tray, which
    // renders them via textContent (user-named sessions must not hit innerHTML)
    const lapCells = state.picks.map((p) => `
      <div><div class="label"><span class="swatch" style="background:${p.color}"></span> ${pickLetter(p)} · ${lapLabel(p.session.route_kind, p.lap.lap_number + 1)}</div>
      <div class="value">${fmtLap(p.lap.lap_time)}</div></div>`).join("");
    side.innerHTML = `<div class="lap-grid" style="text-align:left">
      ${lapCells}
      <div><div class="label">Samples</div><div class="value">${A.n_frames}</div></div>
      <div><div class="label">Driven</div><div class="value">${distFromM(drivenM).toFixed(2)} ${distUnit()}</div></div>
      <div><div class="label">Elevation range</div><div class="value">${yRange > 0.3 ? yRange.toFixed(0) + " m" : "flat"}</div></div>
      <div><div class="label">Contacts</div><div class="value">${contacts ? `<span style="color:#ef4444">${contacts}</span>` : "0"}</div></div>
      ${jumps.length ? `<div><div class="label">Jumps</div><div class="value">${jumps.length}${hard ? `<span style="color:#f59e0b;font-size:0.9rem" title="${hard} hard landing${hard > 1 ? "s" : ""} — not contact"> (${hard} 🛬)</span>` : ""}</div></div>` : ""}
    </div>`;
  }

  // jump flights (takeoff -> touchdown, detected server-side) with the shared
  // glyph, then collision points (contact spikes) as red bursts - both over
  // the ribbon so they read against any coloring. The reference lap's are
  // full-strength, every other pick's dimmer (they'd drown the overlay in
  // sparks otherwise); all of them land in hitMarkers, so right-click
  // dismissal works on any picked lap. Landing spikes are part of the jump
  // glyph (hard = glow + impact ring), not their own spark - not contact.
  const drawJumps = (d, color) => {
    if (!d || !d.jumps) return;
    for (const j of d.jumps) {
      const [X0, Y0] = P(j.x0, j.y0 ?? 0, j.z0, false);
      const [X1, Y1] = P(j.x1, j.y1 ?? 0, j.z1, false);
      drawJump(ctx, X0, Y0, X1, Y1, { color, hard: j.hard });
    }
  };
  const drawHits = (d, fill, r) => {
    if (!d || !d.collisions) return;
    for (const h of d.collisions) {
      if (h.landing) continue;   // drawn as its jump's touchdown, not a spark
      if (h.dismissed) continue; // user said "not a contact"
      const [X, Y] = P(h.x, h.y ?? 0, h.z, false);
      hitMarkers.push({ x: X, y: Y, lapId: d.lap.id, t: h.t });
      ctx.save();
      ctx.strokeStyle = "#fff";
      ctx.lineWidth = 1.5;
      ctx.fillStyle = fill;
      ctx.shadowColor = fill;
      ctx.shadowBlur = 8;
      // 4-point spark
      ctx.beginPath();
      for (let k = 0; k < 8; k++) {
        const a = (k * Math.PI) / 4;
        const rr = k % 2 ? r * 0.4 : r;
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
    for (const p of loaded) if (p !== ref) drawJumps(p.data, "#7f6f4b");
    if (ref) drawJumps(ref.data, "#f59e0b");
    for (const p of loaded) if (p !== ref) drawHits(p.data, "#7f5b5b", 6);
    if (ref) drawHits(ref.data, "#ef4444", 7);
  }

  // cache the finished frame + projection for the chart-cursor marker
  mapCursor.proj = P;
  mapCursor.dpr = dpr;
  if (!mapCursor.snap) mapCursor.snap = document.createElement("canvas");
  mapCursor.snap.width = canvas.width;
  mapCursor.snap.height = canvas.height;
  mapCursor.snap.getContext("2d").drawImage(canvas, 0, 0);
  if (mapCursor.idx != null || mapCursor.pin != null) drawMapMarker();
}

/* ---------------- export (issue #29) ---------------- */

/* mirror of the backend's _safe_filename, so a PNG and its lap's CSV sort
   together in a download folder */
function safeFilename(name) {
  return name.replace(/[^A-Za-z0-9._ -]+/g, "_").replace(/^[ .]+|[ .]+$/g, "") || "export";
}

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/* the map canvas is already the shareable artifact - snapshot the cached
   clean frame (mapCursor.snap: no hover dot, dismissed contacts already
   absent) and composite a caption bar under it. The canvas itself is
   transparent over the page background, so the PNG needs its own fill. */
function exportMapPng() {
  const ref = refPick();
  if (!ref) {
    uiAlert("Nothing to export", "Tag a lap to draw its racing line first.");
    return;
  }
  const s = ref.session;      // the caption belongs to the reference lap's
  const lapMeta = ref.lap;    // own session, not the one being browsed
  if (!mapCursor.snap) drawMap();
  const snap = mapCursor.snap;
  const dpr = mapCursor.dpr || 1;
  const css = getComputedStyle(document.documentElement);
  const color = (name, fallback) => (css.getPropertyValue(name) || fallback).trim();

  const pad = 12, lineH = 21, capH = pad + 2 * lineH + pad / 2;
  const out = document.createElement("canvas");
  out.width = snap.width;
  out.height = snap.height + Math.round(capH * dpr);
  const ctx = out.getContext("2d");
  ctx.fillStyle = color("--bg", "#06080c");
  ctx.fillRect(0, 0, out.width, out.height);
  ctx.drawImage(snap, 0, 0);

  ctx.scale(dpr, dpr);
  ctx.textBaseline = "top";
  const yCap = snap.height / dpr + pad;
  const pi = s.car_pi ? ` ${s.car_class_letter} ${s.car_pi}` : "";
  ctx.fillStyle = color("--text", "#e8eef6");
  ctx.font = "600 15px 'Segoe UI', system-ui, sans-serif";
  ctx.fillText(`${displayName(s)} — ${s.car_name}${pi}`, pad, yCap);
  const tags = [TRACK_META[s.track_type]?.[1], CONDITION_META[s.conditions]?.[1]]
    .filter(Boolean).join(" · ");
  // every picked lap, in tray order; laps from other sessions carry their
  // session's name so a cross-session overlay stays readable
  const lapsTxt = state.picks.map((p) => {
    const t = `${lapLabel(p.session.route_kind, p.lap.lap_number + 1)} — ${fmtLap(p.lap.lap_time)}`;
    return p.session.id !== s.id ? `${shortName(p.session)} ${t}` : t;
  }).join("  ·  ");
  ctx.fillStyle = color("--muted", "#8494a7");
  ctx.font = "13px 'Segoe UI', system-ui, sans-serif";
  ctx.fillText(lapsTxt + (tags ? `  ·  ${tags}` : ""), pad, yCap + lineH);

  const time = lapMeta.lap_time ? `_${fmtLap(lapMeta.lap_time).replace(":", "-")}` : "";
  const name = `lapscope_${safeFilename(displayName(s))}_lap${lapMeta.lap_number + 1}${time}`
    + (state.picks.length > 1 ? `_overlay${state.picks.length}` : "") + ".png";
  out.toBlob((blob) => { if (blob) downloadBlob(blob, name); }, "image/png");
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
    else {
      const span = xs[i + 1] - xs[i];  // 0 on plateaued dist (parked frames):
      out[k] = span > 0                // 0/0 would put a NaN gap in the chart
        ? ys[i] + (ys[i + 1] - ys[i]) * ((x - xs[i]) / span)
        : ys[i];
    }
  }
  return out;
}

const AXIS = { stroke: "#7b8794", grid: { stroke: "#242e3b" }, ticks: { stroke: "#242e3b" } };

/* drag-zoom on any chart -> the same x-window on every chart, and the zoomed
   stretch of track highlighted on the map (double-click resets both). uPlot
   fires setScale for our own propagation too, hence the re-entry guard. */
let zoomSyncing = false;
function syncZoom(u) {
  const ref = refPick();
  if (zoomSyncing || !ref) return;
  const x = ref.data.dist;
  const { min, max } = u.scales.x;
  if (min == null || max == null || !x.length) return;
  zoomSyncing = true;
  for (const c of state.charts) if (c !== u) c.setScale("x", { min, max });
  zoomSyncing = false;
  const range = min <= x[0] && max >= x[x.length - 1] ? null : [min, max];
  if (JSON.stringify(range) !== JSON.stringify(state.zoomRange)) {
    state.zoomRange = range;
    drawMap();
  }
}

/* dashed vertical marker on every chart at the pinned track position */
function drawPinLine(u) {
  const ref = refPick();
  if (mapCursor.pin == null || !ref || !ref.data.dist.length) return;
  const x = ref.data.dist[Math.min(mapCursor.pin, ref.data.dist.length - 1)];
  const cx = u.valToPos(x, "x", true);
  if (cx < u.bbox.left || cx > u.bbox.left + u.bbox.width) return; // zoomed out of view
  const ctx = u.ctx;
  ctx.save();
  ctx.strokeStyle = "rgba(255,255,255,0.55)";
  ctx.setLineDash([4, 4]);
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(cx, u.bbox.top);
  ctx.lineTo(cx, u.bbox.top + u.bbox.height);
  ctx.stroke();
  ctx.restore();
}

function makeChart(el, title, xVals, seriesDefs, height = 150) {
  const opts = {
    title, width: el.clientWidth, height,
    cursor: { sync: { key: "fc" } },
    // hovering any chart marks the matching spot on the track map (the
    // charts all share lap A's x-array, so the cursor idx maps 1:1)
    hooks: {
      setCursor: [(u) => setMapCursor(u.cursor.idx ?? null)],
      setScale: [(u, key) => { if (key === "x") syncZoom(u); }],
      draw: [drawPinLine],
      ready: [(u) => {
        // click pins the hovered spot; a drag is uPlot's zoom, not a click,
        // and the second click of a double is skipped (dblclick = reset both)
        let downX = null;
        u.over.addEventListener("mousedown", (e) => { downX = e.clientX; });
        u.over.addEventListener("click", (e) => {
          if (e.detail > 1) return;
          if (downX != null && Math.abs(e.clientX - downX) > 3) return;
          togglePin(u.cursor.idx ?? null);
        });
        u.over.addEventListener("dblclick", () => {
          if (mapCursor.pin == null) return;
          mapCursor.pin = null;
          drawMapMarker();
          for (const c of state.charts) c.redraw();
        });
      }],
    },
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

  const ref = refPick();
  if (!ref) {
    holder.innerHTML =
      `<div class="empty-hint">Tag laps in the table above (or ＋ on a session card) to compare.</div>`;
    $("#cmp-label").textContent = "";
    return;
  }
  const A = ref.data;
  // every non-reference pick, interpolated onto the reference's
  // track-position axis once (the charts all share that x-array)
  const others = state.picks.slice(1).filter((p) => p.data);
  const multi = others.length > 0;
  const cache = new Map();
  for (const p of others) {
    const c = {};
    for (const name of ["speed_kmh", "throttle", "brake", "steer", "lap_time"])
      c[name] = interp(p.data.dist, p.data.channels[name], A.dist);
    cache.set(p, c);
  }
  $("#cmp-label").textContent = multi ? "— Δ and slip vs A (reference)" : "";

  const x = A.dist;
  const onA = (name) => A.channels[name];

  const div = () => {
    const el = document.createElement("div");
    holder.appendChild(el);
    return el;
  };

  if (multi) {
    const tA = onA("lap_time");
    makeChart(div(), "Δ time vs A — above 0 = behind A", x, others.map((p) => ({
      label: `Δ ${pickLetter(p)}`, stroke: p.color,
      fill: others.length === 1 ? `${p.color}14` : undefined,
      _vals: cache.get(p).lap_time.map((tP, i) => tP - tA[i]),
    })), 170);
  }

  const toSpeed = (arr) => arr.map(speedFromKmh);
  makeChart(div(), `Speed (${speedUnit()})`, x, [
    { label: "A", stroke: ref.color, _vals: toSpeed(onA("speed_kmh")) },
    ...others.map((p) => ({
      label: pickLetter(p), stroke: p.color, width: 1.2, _vals: toSpeed(cache.get(p).speed_kmh),
    })),
  ]);

  // with several laps the channel colors (green/red) would collide with the
  // lap colors, so the overlay encodes the channel in the line style instead
  makeChart(div(),
    multi ? "Throttle (solid) / Brake (dashed) (%)" : "Throttle / Brake (%)", x,
    multi
      ? [ref, ...others].flatMap((p) => {
          const thr = p === ref ? onA("throttle") : cache.get(p).throttle;
          const brk = p === ref ? onA("brake") : cache.get(p).brake;
          return [
            { label: `thr ${pickLetter(p)}`, stroke: p.color, width: 1.2, _vals: thr },
            { label: `brk ${pickLetter(p)}`, stroke: p.color, dash: [5, 5], width: 1.1, _vals: brk },
          ];
        })
      : [
          { label: "thr A", stroke: "#34d399", _vals: onA("throttle") },
          { label: "brk A", stroke: "#f87171", _vals: onA("brake") },
        ]);

  makeChart(div(), "Steering (%)", x, [
    { label: "A", stroke: multi ? ref.color : "#93a3b8", _vals: onA("steer") },
    ...others.map((p) => ({
      label: pickLetter(p), stroke: p.color, width: 1.2, _vals: cache.get(p).steer,
    })),
  ], 120);

  makeChart(div(),
    `Tire combined slip (front / rear${multi ? ", A only" : ""}) — 1.0 = grip limit`, x, [
    { label: "front", stroke: "#fbbf24", _vals: onA("slip_front") },
    { label: "rear", stroke: "#f87171", _vals: onA("slip_rear") },
  ], 140);
}

/* ---------------- raw data at cursor (Settings → Raw data) ---------------- */

/* One table row per raw_* channel (RAW_FIELDS in common.js mirrors the
   packet), one value column per picked lap in tray order/colors. The table is
   rebuilt when the pick set changes; cells fill on chart hover. */
const rawView = { rows: [], cols: [], lastIdx: null };

const rawLoaded = (p) => p.data && p.data.channels.raw_speed;

function renderRawSection() {
  const sec = $("#raw-section");
  if (!sec) return;
  rawView.rows = [];
  rawView.cols = getSettings().rawAnalysis ? state.picks.filter(rawLoaded) : [];
  rawView.lastIdx = null;
  sec.style.display = rawView.cols.length ? "" : "none";
  const holder = $("#raw-table");
  $("#raw-pos").textContent = "";
  holder.innerHTML = "";
  if (!rawView.cols.length) return;

  const table = document.createElement("table");
  const head = table.insertRow();
  head.appendChild(document.createElement("th"));
  for (const p of rawView.cols) {
    const th = document.createElement("th");
    th.textContent = `${pickLetter(p)} · ${lapLabel(p.session.route_kind, p.lap.lap_number + 1)}`;
    th.style.color = p.color;
    head.appendChild(th);
  }
  for (const [name, count, unit, dec] of RAW_FIELDS) {
    const rows = count === 1 ? [[`raw_${name}`, name]]
      : RAW_WHEELS.map((w) => [`raw_${name}_${w}`, `${name} ${w.toUpperCase()}`]);
    for (const [ch, label] of rows) {
      const tr = table.insertRow();
      const lab = tr.insertCell();
      lab.textContent = label;
      if (unit) {
        const u = document.createElement("em");
        u.textContent = unit;
        lab.appendChild(u);
      }
      const cells = rawView.cols.map(() => {
        const td = tr.insertCell();
        td.textContent = "—";
        return td;
      });
      rawView.rows.push({ ch, dec, cells });
    }
  }
  holder.appendChild(table);
  const idx = mapCursor.idx ?? mapCursor.pin;
  if (idx != null) updateRawTable(idx);
}

/* Fill every cell with the values at the hovered track position. The cursor
   index lives on the reference lap's arrays; other laps are read at their own
   nearest sample of that dist (same rule as drawMapMarker's dots). idx null =
   the cursor left the charts: keep the last values on screen. */
function updateRawTable(idx) {
  if (idx == null || idx === rawView.lastIdx || !rawView.rows.length) return;
  const ref = refPick();
  if (!ref || !rawLoaded(ref)) return;
  rawView.lastIdx = idx;
  const ri = Math.min(idx, ref.data.dist.length - 1);
  const dist = ref.data.dist[ri];
  const pinned = mapCursor.idx == null && idx === mapCursor.pin;
  $("#raw-pos").textContent = `— @ track position ${Math.round(dist)}${pinned ? " · pinned" : ""}`;
  const at = rawView.cols.map((p) =>
    p === ref ? ri : Math.min(lowerBound(p.data.dist, dist), p.data.dist.length - 1));
  for (const row of rawView.rows) {
    for (let c = 0; c < rawView.cols.length; c++) {
      const arr = rawView.cols[c].data.channels[row.ch];
      row.cells[c].textContent = arr ? fmtRaw(arr[at[c]], row.dec) : "—";
    }
  }
}

/* The toggle can arrive when the picks were fetched without raw channels —
   refetch once, then build the table. */
async function syncRawSection() {
  if (getSettings().rawAnalysis && state.picks.some((p) => p.data && !rawLoaded(p)))
    await refetchPickData();
  renderRawSection();
}

/* Import CSV: the file's raw text is the request body (text/csv - no
   multipart, matching the backend's no-new-dependency route); the rebuilt
   session is selected as soon as the server is done */
function bindImport() {
  const input = $("#import-file");
  $("#btn-import").onclick = () => input.click();
  input.onchange = async () => {
    const file = input.files[0];
    input.value = ""; // so the same file can be re-imported
    if (!file) return;
    const res = await fetch(
      `/api/import/csv?name=${encodeURIComponent(file.name.replace(/\.csv$/i, ""))}`, {
        method: "POST",
        headers: { "Content-Type": "text/csv" },
        body: await file.text(),
      });
    if (!res.ok) {
      let detail = `HTTP ${res.status}`;
      try { detail = (await res.json()).detail || detail; } catch { /* not JSON */ }
      uiAlert("Couldn't import", detail);
      return;
    }
    const out = await res.json();
    await loadSessions();
    selectSession(out.session_id);
  };
}

window.addEventListener("resize", () => { drawMap(); drawCharts(); });
// live-apply unit / layer / accent changes from the settings panel
onSettingsChange(() => {
  const pal = accentPickPalette();
  if (pal.join() !== PICK_COLORS.join()) { // accent switch: recolor picks in place
    for (const p of state.picks) {
      const i = PICK_COLORS.indexOf(p.color);
      if (i >= 0) p.color = pal[i];
    }
    PICK_COLORS = pal;
    renderLapRows();
    renderTray();
  }
  drawMap(); drawCharts(); syncRawSection();
});
bindImport();
bindBrowse();
loadSessions();
setInterval(loadSessions, 15000); // pick up newly finished sessions
