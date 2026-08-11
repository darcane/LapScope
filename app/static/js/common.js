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

/* A point-to-point sprint produces exactly one timed run per visit, so
   calling it a "lap" is wrong. The noun comes from the route's kind
   (routes.kind / kind_user, see store.py) — a NULL kind means the route was
   never identified, or the session was imported, so fall back to "lap"
   rather than guess. Keep the two branches in step with ROUTE_KINDS. */
function lapWord(kind, n = 1) {
  const w = kind === "sprint" ? "run" : "lap";
  return n === 1 ? w : `${w}s`;
}

/* "Lap 3" / "Run 2" — a specific numbered entry in a lap table or tray */
function lapLabel(kind, n) {
  return `${kind === "sprint" ? "Run" : "Lap"} ${n}`;
}

/* ---------- route outlines (the course, drawn small) ----------

   A route is far easier to recognize by its shape than by a name someone
   typed once, so anywhere a route is listed gets a thumbnail of it. The
   server sends a normalized polyline (GET /api/routes/{id}/outline, filled
   from one lap's frames the first time it is asked for), so the only work
   here is turning it into a path and fitting the tight bbox to the box —
   the same projection the big 2D map uses, so both are the same way up.

   Thumbnails load lazily: a facet menu can list sixty routes and only ever
   show eight of them, and the first request for a route reads a lap's worth
   of frames. */

const outlineCache = new Map();    // route_id -> points | null (null = none)
const outlineInFlight = new Map(); // route_id -> Promise, so one route is
                                   // never fetched twice at once
let outlineObserver = null;

function drawOutline(host, pts) {
  host.classList.toggle("empty", !(pts && pts.length >= 4));
  if (!pts || pts.length < 4) return;
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  for (let i = 0; i < pts.length; i += 2) {
    minX = Math.min(minX, pts[i]); maxX = Math.max(maxX, pts[i]);
    minY = Math.min(minY, pts[i + 1]); maxY = Math.max(maxY, pts[i + 1]);
  }
  const pad = 30;  // room for the stroke; the box is 0..1000 (ROUTE_OUTLINE_BOX)
  const d = [];
  for (let i = 0; i < pts.length; i += 2)
    d.push(`${i ? "L" : "M"}${pts[i]} ${pts[i + 1]}`);
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox",
    `${minX - pad} ${minY - pad} ${maxX - minX + 2 * pad} ${maxY - minY + 2 * pad}`);
  svg.setAttribute("preserveAspectRatio", "xMidYMid meet");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", d.join(""));
  // non-scaling-stroke: a 3 km route and a 300 m one are drawn at wildly
  // different scales, and a scaled stroke would render one hairline and the
  // other a blob
  path.setAttribute("vector-effect", "non-scaling-stroke");
  svg.appendChild(path);
  host.replaceChildren(svg);
}

function fetchOutline(routeId) {
  if (outlineCache.has(routeId)) return Promise.resolve(outlineCache.get(routeId));
  if (outlineInFlight.has(routeId)) return outlineInFlight.get(routeId);
  const p = fetch(`/api/routes/${routeId}/outline`)
    .then((r) => (r.ok ? r.json() : { outline: null }))
    .then((body) => body.outline || null)
    .catch(() => null)
    .then((pts) => {
      outlineCache.set(routeId, pts);
      outlineInFlight.delete(routeId);
      return pts;
    });
  outlineInFlight.set(routeId, p);
  return p;
}

/* A thumbnail element for a route. `lazy` defers the fetch until it scrolls
   into view — right for a long menu, pointless for a dialog. */
function routeOutline(routeId, { lazy = true } = {}) {
  const host = document.createElement("span");
  host.className = "route-outline empty";  // drawOutline clears it on arrival
  host.dataset.route = routeId;
  const cached = outlineCache.get(routeId);
  if (cached !== undefined) {
    drawOutline(host, cached);
    return host;
  }
  const load = () => fetchOutline(routeId).then((pts) => drawOutline(host, pts));
  if (!lazy) { load(); return host; }
  if (!outlineObserver) {
    // the intersection rect is clipped by scrollable ancestors, so rows
    // parked below a menu's scroll never fire
    outlineObserver = new IntersectionObserver((entries, obs) => {
      for (const e of entries) {
        if (!e.isIntersecting) continue;
        obs.unobserve(e.target);
        fetchOutline(Number(e.target.dataset.route))
          .then((pts) => drawOutline(e.target, pts));
      }
    }, { rootMargin: "120px" });
  }
  outlineObserver.observe(host);
  return host;
}

/* DrivetrainType is in every packet: 0=FWD 1=RWD 2=AWD */
const DRIVETRAINS = ["FWD", "RWD", "AWD"];

function dtBadge(dt) {
  return `<span class="dt-badge dt-${dt}">${dt}</span>`;
}

/* ---------- car identity ----------

   Class, PI, name and drivetrain are four facts about one car, and the
   Analysis page used to scatter them: the two badges sat beside the session
   title while the name was a muted 0.8rem line below the whole button row,
   so the one part a human actually reads was also the faintest thing on the
   page. They travel as one chip now — built here, used by the session
   header and both sidebar card types, so the shape only has to change once.

   `edit` makes the NAME (and only the name) a button: a car name is edited
   where it is shown, which is what stops "Rename / Name route / Name car"
   from being three similar verbs to choose between. */
function carChip(s, { edit = null } = {}) {
  const host = document.createElement("span");
  host.className = "car-chip";
  host.innerHTML = classBadge(s.car_class_letter, s.car_pi);
  const nm = document.createElement(edit ? "button" : "span");
  nm.className = "car-nm";
  nm.textContent = s.car_name || "Unknown car";
  if (edit) {
    nm.type = "button";
    nm.classList.add("name-edit");
    nm.setAttribute("aria-label", "Name this car");
    nm.title = "Name this car — applies everywhere this car appears";
    nm.onclick = edit;
  }
  // an ordinal the community list doesn't know yet: the name shown is a
  // placeholder, and saying so is what gets it reported (unknownCarIssueUrl)
  if (s.car_known === false) {
    nm.classList.add("car-unknown");
    nm.title = "Unknown car — not in the community list yet."
      + (edit ? " Name it here, and report it so everyone gets the name." : "");
  }
  host.appendChild(nm);
  // a session recorded before the field existed has no drivetrain; an empty
  // badge reading "undefined" is worse than no badge
  if (s.drivetrain) host.insertAdjacentHTML("beforeend", dtBadge(s.drivetrain));
  return host;
}

/* The place, named. Deliberately text only — route outlines belong to the
   browse facets, where you are picking between routes you can't name; here
   the route is already identified and a thumbnail is just noise. Same
   `edit` rule as carChip. */
function routeChip(s, { edit = null } = {}) {
  const host = document.createElement("span");
  host.className = "route-chip";
  const nm = document.createElement(edit ? "button" : "span");
  nm.className = "route-nm";
  nm.textContent = s.route_name
    || (s.route_id ? "Unnamed route" : "Route not identified yet");
  if (!s.route_name) nm.classList.add("route-untitled");
  if (edit) {
    nm.type = "button";
    nm.classList.add("name-edit");
    nm.setAttribute("aria-label", "Name this route");
    nm.title = "Name this route — applies to every session driven here";
    nm.onclick = edit;
  }
  host.appendChild(nm);
  return host;
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

/* showModal's third answer. A dialog offering two opposite verdicts over one
   selection ("merge these" / "dismiss these") would otherwise have to send the
   second one through a second visit. Never collides with a prompt's result:
   the alt button is only ever paired with a dialog that has no text input. */
const MODAL_ALT = Symbol("modal-alt");

/* <dialog> + showModal(), not a hand-rolled backdrop: it brings the focus
   trap (38 tabbable elements behind the old backdrop stayed reachable —
   Tab walked straight out of the dialog), Escape, the ::backdrop, and
   restoring focus to whatever opened it, all of which this used to lack
   (issue #70). */
let modalSeq = 0;

function showModal({ title, message = "", extra = null, value = null, placeholder = "",
                     okText = "OK", cancelText = "Cancel", altText = "",
                     danger = false, showCancel = true, wide = false }) {
  return new Promise((resolve) => {
    const box = document.createElement("dialog");
    box.className = "modal" + (danger ? " danger" : "") + (wide ? " modal-wide" : "");

    const h = document.createElement("h3");
    h.id = `modal-title-${++modalSeq}`;
    h.textContent = title;
    box.setAttribute("aria-labelledby", h.id);
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

    let settled = false;
    const done = (result) => {
      if (settled) return;  // Escape fires cancel and then close
      settled = true;
      box.close();
      box.remove();
      resolve(result);
    };
    if (altText) {
      // first in the row so its margin-right:auto pushes the cancel/OK pair
      // away: a bulk dismissal should not sit under the thumb aiming for Merge
      const alt = document.createElement("button");
      alt.className = "modal-alt";
      alt.textContent = altText;
      alt.onclick = () => done(MODAL_ALT);
      actions.appendChild(alt);
    }
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

    box.addEventListener("cancel", (e) => { e.preventDefault(); done(null); });
    if (inputEl) inputEl.addEventListener("keydown", (e) => {
      if (e.key !== "Enter") return;
      e.preventDefault();
      done(inputEl.value);
    });
    // a click outside the box lands on the dialog element itself (the
    // ::backdrop is not an element of its own)
    box.addEventListener("pointerdown", (e) => { if (e.target === box) done(null); });

    document.body.appendChild(box);
    box.showModal();
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

/* ---------- overflow menu ----------

   The Analysis header grew one button per feature until twelve controls
   shared one wrapping row, and Delete ended up wherever the title's length
   happened to push it. Actions live in here instead.

   Hand-rolled rather than reusing <dialog>: a menu that dims the page and
   traps focus is heavier than the choice it offers, and it can't be
   dismissed by clicking the thing behind it. So it brings its own keyboard
   contract — arrows move, Home/End jump, Escape and Tab close, focus goes
   back to the trigger — which is the contract issue #70 asked of every
   other control on the page.

   Each item may carry a `hint`: the muted second line is what tells apart
   three actions whose names all start with a naming verb but whose blast
   radius runs from one session to every session ever driven in that car. */

let openMenu = null;   // at most one, page-wide

function closeMenu({ focusTrigger = false } = {}) {
  if (!openMenu) return;
  const { wrap, trigger, menu } = openMenu;
  openMenu = null;
  menu.hidden = true;
  wrap.classList.remove("open");
  trigger.setAttribute("aria-expanded", "false");
  // only when the keyboard closed it: stealing focus back on an outside
  // click would yank it off whatever the click was aimed at
  if (focusTrigger && trigger.isConnected) trigger.focus();
}

document.addEventListener("pointerdown", (e) => {
  if (openMenu && !openMenu.wrap.contains(e.target)) closeMenu();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && openMenu) closeMenu({ focusTrigger: true });
});

/* sections: [{ heading, items: [{ label, hint, onSelect, danger } | falsy] }]
   — falsy items and emptied sections drop out, so a caller can write
   `edits && { label: "Reset edits", … }` instead of branching. */
function menuButton({ label, title = "", ariaLabel = "", items }) {
  const wrap = document.createElement("span");
  wrap.className = "menu-wrap";

  const trigger = document.createElement("button");
  trigger.type = "button";
  trigger.className = "menu-trigger";
  trigger.textContent = label;
  if (title) trigger.title = title;
  if (ariaLabel) trigger.setAttribute("aria-label", ariaLabel);
  trigger.setAttribute("aria-haspopup", "menu");
  trigger.setAttribute("aria-expanded", "false");

  const menu = document.createElement("div");
  menu.className = "menu";
  menu.setAttribute("role", "menu");
  menu.hidden = true;

  const itemEls = [];
  for (const sec of items) {
    const live = (sec.items || []).filter(Boolean);
    if (!live.length) continue;
    const group = document.createElement("div");
    group.className = "menu-group";
    group.setAttribute("role", "group");
    if (sec.heading) {
      group.setAttribute("aria-label", sec.heading);
      const h = document.createElement("span");
      h.className = "menu-heading";
      h.textContent = sec.heading;
      h.setAttribute("aria-hidden", "true");  // the group label already says it
      group.appendChild(h);
    }
    for (const it of live) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "menu-item" + (it.danger ? " danger" : "");
      b.setAttribute("role", "menuitem");
      b.tabIndex = -1;                        // roving: the menu owns Tab
      const lab = document.createElement("span");
      lab.className = "menu-label";
      lab.textContent = it.label;
      b.appendChild(lab);
      if (it.hint) {
        const hint = document.createElement("span");
        hint.className = "menu-hint";
        hint.textContent = it.hint;
        b.appendChild(hint);
      }
      // close first, restoring focus to the trigger, THEN act: a dialog
      // opened from here hands focus back to whatever had it, and that
      // should be the ⋯ button rather than a menu item that no longer exists
      b.onclick = () => { closeMenu({ focusTrigger: true }); it.onSelect(); };
      group.appendChild(b);
      itemEls.push(b);
    }
    menu.appendChild(group);
  }

  const move = (step) => {
    const i = itemEls.indexOf(document.activeElement);
    itemEls[(Math.max(0, i) + step + itemEls.length) % itemEls.length].focus();
  };
  menu.addEventListener("keydown", (e) => {
    const keys = {
      ArrowDown: () => move(1),
      ArrowUp: () => move(-1),
      Home: () => itemEls[0].focus(),
      End: () => itemEls[itemEls.length - 1].focus(),
    };
    if (keys[e.key]) { e.preventDefault(); keys[e.key](); }
    else if (e.key === "Tab") closeMenu();   // let focus leave naturally
  });

  trigger.onclick = () => {
    if (openMenu && openMenu.trigger === trigger) {
      closeMenu({ focusTrigger: true });
      return;
    }
    closeMenu();
    openMenu = { wrap, trigger, menu };
    menu.hidden = false;
    wrap.classList.add("open");
    trigger.setAttribute("aria-expanded", "true");
    itemEls[0].focus();
  };
  trigger.disabled = !itemEls.length;

  wrap.append(trigger, menu);
  return wrap;
}

/* ---------- API calls (issue #68) ----------

   Every write on the Analysis page used to be a bare `fetch` that checked
   neither `res.ok` nor a network failure, fired from an `onclick` that never
   awaited it. With the server closed, renames, tags, exclusions, flag edits
   and deletes all appeared to do nothing — forever, with nothing on screen
   to say why. This is the one funnel: it throws on any failure, reports it
   in a single modal, and tells the page whether the server answered at all.

   `quiet` is for the polls (loadSessions on its 15 s timer): they must not
   pop a dialog every interval while the server is down — the connection chip
   is what speaks for them. */

class ApiError extends Error {
  constructor(title, detail, offline) {
    super(`${title}: ${detail}`);
    this.name = "ApiError";
    this.title = title;
    this.detail = detail;
    this.offline = offline;  // the request never reached the server
    this.reported = false;   // a modal already showed this one
  }
}

/* Reachability is simply what the last call did: a 500 is a reachable server,
   a rejected fetch is not. The Analysis header chip subscribes to this.
   Starts null, not true, so the very first call resolves the chip out of its
   "connecting…" state either way. */
let serverReachable = null;
const reachListeners = new Set();

function onServerReachable(cb) {
  reachListeners.add(cb);
  return () => reachListeners.delete(cb);
}

function noteReachable(ok) {
  if (ok === serverReachable) return;
  serverReachable = ok;
  for (const cb of reachListeners) {
    try { cb(ok); } catch { /* a bad listener must not block the rest */ }
  }
}

/* One dialog at a time: a failed write usually drags its follow-up reload
   down with it, and two stacked backdrops for one cause read as two faults. */
let apiAlertOpen = false;

function reportApiError(err, quiet) {
  err.reported = true;
  if (quiet || apiAlertOpen) return err;
  apiAlertOpen = true;
  uiAlert(err.title, err.detail).then(() => { apiAlertOpen = false; });
  return err;
}

/* `what` completes "Couldn't …" in the failure dialog. Resolves to the parsed
   JSON body, or null for an empty one. */
async function apiFetch(url, { what = "do that", quiet = false, ...init } = {}) {
  let res;
  try {
    res = await fetch(url, init);
  } catch {
    noteReachable(false);
    throw reportApiError(new ApiError("Can't reach LapScope",
      "The server didn't answer — it may have been closed or restarted."
      + " Nothing was changed; the page picks up again on its own once it's back.",
      true), quiet);
  }
  noteReachable(true);
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try { detail = (await res.json()).detail || detail; } catch { /* not JSON */ }
    throw reportApiError(new ApiError(`Couldn't ${what}`, detail, false), quiet);
  }
  try { return await res.json(); } catch { return null; }
}

/* Writes hang off bare `onclick`s that cannot await, so an ApiError already
   shown in a modal would still surface as an unhandled rejection. Swallow
   exactly those — every other rejection stays loud. */
window.addEventListener("unhandledrejection", (e) => {
  if (e.reason instanceof ApiError && e.reason.reported) e.preventDefault();
});

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

/* ---------- track-catalogue auto-refresh (fail-soft, once a day) ----------
   Same deal as the car list, for the bundled table of official routes that
   names a course on its first completed lap (see app/tracks.py). A refresh
   also re-runs the naming backfill server-side, so a route the user drove
   before the catalogue knew about it gets named here rather than never. */

const TRACKDB_CHECK_KEY = "ls_trackdb_check";    // ts of the last attempt
const TRACKDB_CHECK_TTL = 24 * 60 * 60 * 1000;   // 1 day

async function maybeRefreshTrackList() {
  const last = parseInt(localStorage.getItem(TRACKDB_CHECK_KEY) || "0", 10);
  if (Date.now() - last < TRACKDB_CHECK_TTL) return;
  localStorage.setItem(TRACKDB_CHECK_KEY, String(Date.now()));  // even on failure: don't hammer
  try {
    const r = await fetch("/api/tracks/refresh", { method: "POST" });
    if (!r.ok) return;
    const { named } = await r.json();
    // newly named routes show up as route names on the cards: redraw
    if (named > 0 && typeof loadSessions === "function") loadSessions();
  } catch { /* offline / server restarting: bundled catalogue keeps working */ }
}

/* At most one call per animation frame (issue #71). `resize` fires far faster
   than the ~13 ms a full analysis redraw costs, and a map drag fires one
   pointermove per pointer sample — both saturate the main thread otherwise. */
function rafThrottle(fn) {
  let queued = false;
  return (...args) => {
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => { queued = false; fn(...args); });
  };
}

/* setInterval that skips hidden tabs (issue #73): a background tab nobody is
   looking at has no reason to keep polling the server, and browsers only
   throttle these timers rather than stopping them. Becoming visible runs the
   callback straight away, so the page is never a whole period out of date. */
function setVisibleInterval(fn, ms) {
  document.addEventListener("visibilitychange", () => { if (!document.hidden) fn(); });
  return setInterval(() => { if (!document.hidden) fn(); }, ms);
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
onReady(maybeRefreshTrackList);
