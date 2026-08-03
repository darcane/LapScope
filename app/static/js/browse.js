/* Analysis browse bar: client-side session facets (issue #55).

   Every field the bar filters on is already in the /api/sessions payload, so
   there is no query API and no second fetch - browseApply() runs over the
   array the sidebar already holds. That stays true while the list is small
   (a few hundred rows, ~40 KB); past a few thousand sessions this wants
   ?since= / ?limit= on the endpoint rather than more facets here.

   Facets, the date range and the sort order persist across reloads; the
   search box deliberately does not - a restored query that hides most of the
   list on startup reads as data loss. */

const BROWSE_KEY = "ls_browse";

const DATE_RANGES = [
  ["", "Any time"], ["7", "Last 7 days"], ["30", "Last 30 days"],
  ["90", "Last 90 days"], ["365", "Last year"],
];
const BROWSE_SORTS = [
  ["new", "Newest first"], ["old", "Oldest first"],
  ["best", "Best time"], ["laps", "Most laps"],
];

/* Facet values are always strings: they round-trip through localStorage as
   JSON, where a numeric route_id would come back as a number and stop
   matching. "none" is the untagged/unidentified bucket for every facet. */
const FACETS = [
  {
    /* Only NAMED routes get a row of their own. A fingerprint you have never
       named says nothing a "Route #37" row could help you pick, and there are
       more of those than named ones - they all share one bucket instead. */
    key: "route", label: "Route", bar: true,
    value: (s) => (s.route_name ? String(s.route_id)
      : s.route_id ? "unnamed" : "none"),
    text: (s) => s.route_name
      || (s.route_id ? "Unnamed routes" : "No route identified"),
    outline: (s) => (s.route_name ? s.route_id : null),
    /* named routes first, then the two buckets, each by session count */
    rank: (s) => (s.route_name ? 0 : s.route_id ? 1 : 2),
  },
  {
    key: "cls", label: "Class", bar: true,
    value: (s) => s.car_class_letter || "none",
    text: (s) => s.car_class_letter === "?" ? "Unknown class" : s.car_class_letter,
    html: (s) => s.car_class_letter && s.car_class_letter !== "?"
      ? `<span class="cls-only" style="background:${CLASS_COLORS[s.car_class_letter] || "#7b8794"}">${s.car_class_letter}</span>`
      : null,
    rank: (s) => CLASS_LETTERS.indexOf(s.car_class_letter),
  },
  {
    key: "car", label: "Car", bar: true,
    value: (s) => String(s.car_ordinal ?? "none"),
    text: (s) => s.car_name,
  },
  {
    key: "track", label: "Type", bar: true,
    value: (s) => s.track_type || "none",
    text: (s) => TRACK_META[s.track_type] ? TRACK_META[s.track_type][1] : "Untagged",
    html: (s) => trackBadge(s.track_type) || null,
  },
  {
    key: "cond", label: "Conditions",
    value: (s) => s.conditions || "none",
    text: (s) => CONDITION_META[s.conditions] ? CONDITION_META[s.conditions][1] : "Untagged",
    html: (s) => condBadge(s.conditions) || null,
  },
  {
    key: "dt", label: "Drivetrain",
    value: (s) => s.drivetrain || "none",
    text: (s) => s.drivetrain === "?" ? "Unknown" : s.drivetrain,
  },
];

const facetByKey = Object.fromEntries(FACETS.map((f) => [f.key, f]));
/* everything not given its own button lives behind "More" */
const MORE_FACETS = FACETS.filter((f) => !f.bar);

const browse = (() => {
  const b = { q: "", since: "", sort: "new" };
  for (const f of FACETS) b[f.key] = new Set();
  try {
    const saved = JSON.parse(localStorage.getItem(BROWSE_KEY) || "{}");
    for (const f of FACETS)
      if (Array.isArray(saved[f.key])) b[f.key] = new Set(saved[f.key].map(String));
    if (DATE_RANGES.some(([v]) => v === saved.since)) b.since = saved.since;
    if (BROWSE_SORTS.some(([v]) => v === saved.sort)) b.sort = saved.sort;
  } catch { /* corrupt entry: start clean rather than break the page */ }
  return b;
})();

function saveBrowse() {
  const out = { since: browse.since, sort: browse.sort };  // q is not persisted
  for (const f of FACETS) out[f.key] = [...browse[f.key]];
  try { localStorage.setItem(BROWSE_KEY, JSON.stringify(out)); } catch { /* private mode */ }
}

let browseAll = [];  // the last fetched session list, for the facet menus

/* ---------------- matching ---------------- */

function browseMatchesText(s, q) {
  return [s.name, s.route_name, s.car_name, s.display_name]
    .some((v) => v && v.toLowerCase().includes(q));
}

/* `skip` leaves one facet out so its own menu can show how many sessions each
   of its values *would* add - the usual faceted-search count. */
function browsePasses(s, skip) {
  if (browse.q && !browseMatchesText(s, browse.q)) return false;
  if (browse.since
      && s.started_at < Date.now() / 1000 - Number(browse.since) * 86400) return false;
  for (const f of FACETS) {
    const set = browse[f.key];
    if (f.key !== skip && set.size && !set.has(f.value(s))) return false;
  }
  return true;
}

const browseActive = () =>
  FACETS.reduce((n, f) => n + browse[f.key].size, 0) + (browse.since ? 1 : 0);

function browseApply(list) {
  const rows = list.filter((s) => browsePasses(s, null));
  const by = {
    new: (a, b) => b.started_at - a.started_at,
    old: (a, b) => a.started_at - b.started_at,
    // sessions without a timed lap sort last rather than winning on null
    best: (a, b) => (a.best_lap || Infinity) - (b.best_lap || Infinity),
    laps: (a, b) => b.lap_count - a.lap_count || b.started_at - a.started_at,
  };
  return rows.sort(by[browse.sort] || by.new);
}

/* Distinct values of one facet over everything the *other* filters allow. */
function browseValues(f) {
  const seen = new Map();
  for (const s of browseAll) {
    if (!browsePasses(s, f.key)) continue;
    const v = f.value(s);
    const hit = seen.get(v);
    if (hit) { hit.count++; continue; }
    seen.set(v, {
      value: v, count: 1, text: f.text(s),
      html: f.html ? f.html(s) : null, rank: f.rank ? f.rank(s) : 0,
      outline: f.outline ? f.outline(s) : null,
    });
  }
  // a checked value whose sessions are all filtered out must stay visible,
  // or there is no way to uncheck it from the menu
  for (const v of browse[f.key]) {
    if (seen.has(v)) continue;
    const hit = browseSample(f, v);
    seen.set(v, {
      value: v, count: 0, rank: 99,
      text: hit ? f.text(hit) : v,
      html: hit && f.html ? f.html(hit) : null,
      outline: hit && f.outline ? f.outline(hit) : null,
    });
  }
  return [...seen.values()].sort(
    (a, b) => a.rank - b.rank || b.count - a.count || a.text.localeCompare(b.text));
}

/* Any session carrying a stored facet value, so a value still under filter
   can be labelled and drawn from the full list rather than from the rows
   that survived (a chip has to read "Seaside Park Sprint", not "19"). */
function browseSample(f, value) {
  return browseAll.find((s) => f.value(s) === value);
}

function browseLabel(f, value) {
  const hit = browseSample(f, value);
  return hit ? f.text(hit) : value;
}

/* ---------------- popovers ---------------- */

let openPop = null;

function closeFacetPop() {
  if (!openPop) return;
  const btn = openPop._btn;
  openPop.remove();
  openPop = null;
  if (btn) btn.classList.remove("open");
}

function placePop(pop, btn) {
  const r = btn.getBoundingClientRect();
  pop.style.top = `${r.bottom + 6}px`;
  pop.style.left =
    `${Math.max(8, Math.min(r.left, window.innerWidth - pop.offsetWidth - 8))}px`;
}

function facetPop(btn, key, fill) {
  if (openPop && openPop.dataset.key === key) { closeFacetPop(); return; }
  closeFacetPop();
  const pop = document.createElement("div");
  pop.className = "facet-pop";
  pop.dataset.key = key;
  pop._btn = btn;
  fill(pop);
  document.body.appendChild(pop);
  openPop = pop;
  btn.classList.add("open");
  placePop(pop, btn);
  const search = pop.querySelector("input[type=search]");
  if (search) search.focus();
}

/* One checkbox row; toggling re-renders the list and the row in place so the
   menu can stay open for a second pick. */
function facetRow(f, v, onToggle) {
  const row = document.createElement("div");
  row.className = "facet-row";
  const on = browse[f.key].has(v.value);
  row.classList.toggle("on", on);
  const label = document.createElement("span");
  label.className = "facet-label";
  if (v.html) label.innerHTML = v.html;         // badge markup, from common.js
  else label.textContent = v.text;              // route / car names are user data
  const count = document.createElement("span");
  count.className = "facet-count";
  count.textContent = v.count;
  // a named route is recognized by its shape long before its name
  if (v.outline) row.appendChild(routeOutline(v.outline));
  row.append(label, count);
  row.onclick = () => {
    if (on) browse[f.key].delete(v.value);
    else browse[f.key].add(v.value);
    saveBrowse();
    onToggle();
  };
  return row;
}

function fillFacetPop(pop, f) {
  const render = () => {
    pop.innerHTML = "";
    const values = browseValues(f);
    if (values.length > 8) {
      const wrap = document.createElement("label");
      wrap.className = "facet-search";
      const q = document.createElement("input");
      q.type = "search";
      q.placeholder = `Filter ${f.label.toLowerCase()}…`;
      q.autocomplete = "off";
      q.oninput = () => {
        const needle = q.value.trim().toLowerCase();
        for (const row of pop.querySelectorAll(".facet-row"))
          row.style.display =
            row.textContent.toLowerCase().includes(needle) ? "" : "none";
      };
      wrap.appendChild(q);
      pop.appendChild(wrap);
    }
    const body = document.createElement("div");
    body.className = "facet-body";
    for (const v of values) body.appendChild(facetRow(f, v, () => {
      renderBrowseBar();
      renderSessionList();
      render();
    }));
    pop.appendChild(body);
    if (browse[f.key].size) {
      const clear = document.createElement("button");
      clear.className = "facet-clear";
      clear.textContent = `Clear ${f.label.toLowerCase()}`;
      clear.onclick = () => {
        browse[f.key].clear();
        saveBrowse();
        renderBrowseBar();
        renderSessionList();
        render();
      };
      pop.appendChild(clear);
    }
  };
  render();
}

function fillMorePop(pop) {
  const render = () => {
    pop.innerHTML = "";
    for (const f of MORE_FACETS) {
      const head = document.createElement("div");
      head.className = "facet-group";
      head.textContent = f.label;
      pop.appendChild(head);
      const body = document.createElement("div");
      body.className = "facet-body";
      for (const v of browseValues(f)) body.appendChild(facetRow(f, v, () => {
        renderBrowseBar();
        renderSessionList();
        render();
      }));
      pop.appendChild(body);
    }
    const head = document.createElement("div");
    head.className = "facet-group";
    head.textContent = "Recorded";
    pop.appendChild(head);
    const body = document.createElement("div");
    body.className = "facet-body";
    for (const [value, label] of DATE_RANGES) {
      const row = document.createElement("div");
      row.className = "facet-row" + (browse.since === value ? " on" : "");
      row.innerHTML = `<span class="facet-label"></span>`;
      row.firstChild.textContent = label;
      row.onclick = () => {
        browse.since = value;
        saveBrowse();
        renderBrowseBar();
        renderSessionList();
        render();
      };
      body.appendChild(row);
    }
    pop.appendChild(body);
  };
  render();
}

/* ---------------- the bar ---------------- */

function clearBrowse() {
  browse.q = "";
  browse.since = "";
  for (const f of FACETS) browse[f.key].clear();
  const q = document.querySelector("#browse-q");
  if (q) q.value = "";
  saveBrowse();
  renderBrowseBar();
  renderSessionList();
}

function browseChip(text, onRemove) {
  const chip = document.createElement("span");
  chip.className = "fchip";
  const label = document.createElement("b");
  label.textContent = text;
  const x = document.createElement("button");
  x.type = "button";
  x.title = "Remove this filter";
  x.textContent = "×";
  x.onclick = onRemove;
  chip.append(label, x);
  return chip;
}

function renderBrowseBar() {
  const bar = document.querySelector("#browse-facets");
  if (!bar) return;
  for (const btn of bar.querySelectorAll("button[data-facet]")) {
    const key = btn.dataset.facet;
    const n = key === "more"
      ? MORE_FACETS.reduce((t, f) => t + browse[f.key].size, 0) + (browse.since ? 1 : 0)
      : browse[key].size;
    btn.classList.toggle("on", n > 0);
    btn.querySelector(".facet-n").textContent = n || "";
  }
  const chips = document.querySelector("#browse-chips");
  chips.innerHTML = "";
  for (const f of FACETS)
    for (const v of browse[f.key])
      chips.appendChild(browseChip(browseLabel(f, v), () => {
        browse[f.key].delete(v);
        saveBrowse();
        closeFacetPop();
        renderBrowseBar();
        renderSessionList();
      }));
  if (browse.since) {
    const label = (DATE_RANGES.find(([v]) => v === browse.since) || [, ""])[1];
    chips.appendChild(browseChip(label, () => {
      browse.since = "";
      saveBrowse();
      closeFacetPop();
      renderBrowseBar();
      renderSessionList();
    }));
  }
  if (browseActive() || browse.q) {
    const clear = document.createElement("button");
    clear.type = "button";
    clear.className = "browse-clear";
    clear.textContent = "Clear all";
    clear.onclick = clearBrowse;
    chips.appendChild(clear);
  }
  // an empty second row is a wasted line: the count lives up in the first one
  chips.style.display = chips.children.length ? "" : "none";
}

/* Called by renderSessionList once it knows how many rows survived. The
   count is always shown, filtered or not, so a short list is never mistaken
   for missing data. */
function browseStatus(shown, total) {
  const el = document.querySelector("#browse-status");
  if (!el) return;
  el.textContent = shown === total
    ? `${total} session${total === 1 ? "" : "s"}`
    : `${shown} of ${total} sessions`;
  // sorting by time across several tracks compares unrelated laps
  const cross = browse.sort === "best" && browse.route.size !== 1;
  el.classList.toggle("warn", cross);
  el.title = cross
    ? "Best time spans more than one route — pick a route to compare like for like"
    : "";
}

/* The bar is built before the first /api/sessions response lands, so a
   filter restored from localStorage starts out with nothing to resolve its
   label against ("19" instead of "Seaside Park Sprint"). Re-render the chips
   as soon as there is a list to name them from. */
function browseIndex(list) {
  browseAll = list;
  renderBrowseBar();
}

/* ---------------- wiring ---------------- */

function bindBrowse() {
  const bar = document.querySelector("#browse-facets");
  if (!bar) return;
  for (const f of FACETS.filter((x) => x.bar)) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.dataset.facet = f.key;
    btn.innerHTML = `${f.label}<span class="facet-n"></span><span class="caret">▾</span>`;
    btn.onclick = () => facetPop(btn, f.key, (pop) => fillFacetPop(pop, f));
    bar.appendChild(btn);
  }
  const more = document.createElement("button");
  more.type = "button";
  more.dataset.facet = "more";
  more.innerHTML = `More<span class="facet-n"></span><span class="caret">▾</span>`;
  more.onclick = () => facetPop(more, "more", fillMorePop);
  bar.appendChild(more);

  const sort = document.querySelector("#browse-sort");
  sort.innerHTML = BROWSE_SORTS
    .map(([v, label]) => `<option value="${v}">${label}</option>`).join("");
  sort.value = browse.sort;
  sort.onchange = () => {
    browse.sort = sort.value;
    saveBrowse();
    renderBrowseBar();
    renderSessionList();
  };

  const q = document.querySelector("#browse-q");
  let debounce;
  q.oninput = () => {
    clearTimeout(debounce);
    debounce = setTimeout(() => {
      browse.q = q.value.trim().toLowerCase();
      renderBrowseBar();
      renderSessionList();
    }, 120);
  };

  // same dismissal rules as the modals in common.js
  document.addEventListener("pointerdown", (e) => {
    if (openPop && !openPop.contains(e.target) && !e.target.closest("[data-facet]"))
      closeFacetPop();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeFacetPop();
  });
  window.addEventListener("resize", () => {
    if (openPop) placePop(openPop, openPop._btn);
  });

  // the sidebar's max-height is viewport-relative; the bar's own height has
  // to come out of it, and the chip row wraps
  const measure = () => document.documentElement.style.setProperty(
    "--browse-h", `${document.querySelector(".browse-bar").offsetHeight}px`);
  new ResizeObserver(measure).observe(document.querySelector(".browse-bar"));
  measure();

  renderBrowseBar();
}
