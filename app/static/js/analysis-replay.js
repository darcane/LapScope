/* Ghost lap replay on the Analysis map (issue #99; design and measurements in
   docs/spikes/analysis-replay.md): one marker per picked lap moving along its
   own line, in sync, with a live gap to the reference lap (A).

   One clock drives everything: `replay.t`, A's lap clock. The two alignments
   differ only in where the other laps stand at that clock:
     by time      each lap is where its own lap clock read the same — a real
                  race, the gap shows as distance between the markers;
     by distance  each lap is where A is on the track (the Δ chart's
                  alignment) — the markers overlap, the gap shows as time.
   A is at its own clock in both, so the chart cursor always follows A.

   The gap readout is the Δ chart's number under the cursor in both modes:
   each lap's clock minus A's at A's current track position.

   Renderer: the Pixi map draws the ghosts (analysis-map.js); without it the
   controls stay hidden and the canvas map behaves as before. Nothing runs
   while paused: the loop is a rAF chain that exists only while playing. */

const REPLAY_SPEEDS = [0.25, 0.5, 1, 2, 4];

const replay = {
  t: 0,               // A's lap clock, seconds
  mode: "time",       // "time" | "dist"
  speed: 1,
  playing: false,
  engaged: false,     // the map shows ghosts (play or scrub happened)
  driving: false,     // re-entry guard: our own chart-cursor moves
  last: 0,            // rAF timestamp of the previous tick
  said: 0,            // when the aria-label was last refreshed
};

/* fracIndex, lerpAt, lapClock and keptSample live in common.js: the Live
   page's ghost (issue #108) runs the same clock. */
const lapClockAtDist = (d, x) => { const lc = lapClock(d); return lerpAt(lc.c, fracIndex(lc.dist, x)); };

/* the playable window, in A's clock: the chart zoom span if there is one;
   else the whole of A by distance, and every lap's full length by time (so
   the slowest one gets to finish) */
function replayRange() {
  const ref = refPick();
  if (!ref) return [0, 0];
  const A = ref.data;
  if (state.zoomRange) return state.zoomRange.map((x) => lapClockAtDist(A, x));
  const lcA = lapClock(A);
  let end = lcA.end;
  if (replay.mode === "time")
    for (const p of state.picks) if (p.data) end = Math.max(end, lapClock(p.data).end);
  return [lcA.c[0], end];
}

/* where every pick stands at the current clock, in draw order (A last):
   { pick, j (fractional index into its kept timeline), done } */
function replayPositions() {
  const ref = refPick();
  if (!ref) return [];
  const lcA = lapClock(ref.data);
  const jA = fracIndex(lcA.c, replay.t);
  const dA = lerpAt(lcA.dist, jA);
  const out = [];
  for (const p of state.picks) {
    if (!p.data || p === ref) continue;
    const lc = lapClock(p.data);
    const j = replay.mode === "time" ? fracIndex(lc.c, replay.t) : fracIndex(lc.dist, dA);
    out.push({ pick: p, j, done: replay.mode === "time" && replay.t > lc.end });
  }
  out.push({ pick: ref, j: jA, done: replay.mode === "time" && replay.t > lcA.end });
  return out;
}

/* Δ clock vs A at A's current track position, per other pick (A's own
   clock there, not the replay's: past A's finish line they part ways) */
function replayGaps() {
  const ref = refPick();
  if (!ref) return [];
  const lcA = lapClock(ref.data);
  const jA = fracIndex(lcA.c, replay.t);
  const dA = lerpAt(lcA.dist, jA), tA = lerpAt(lcA.c, jA);
  return state.picks.filter((p) => p.data && p !== ref).map((p) => ({
    pick: p, gap: lapClockAtDist(p.data, dA) - tA,
  }));
}

/* a pick's world position at fractional index j of its kept timeline */
function pickWorldAt(pick, j) {
  const c = pick.data.channels;
  const { a, b, f } = keptSample(lapClock(pick.data), j);
  const at = (arr) => arr[a] + (arr[b] - arr[a]) * f;
  return [at(c.pos_x), c.pos_y ? at(c.pos_y) : 0, at(c.pos_z)];
}

/* screen-space ghosts for the renderer: { x, y, color, letter, done } */
function replayGhosts() {
  if (!replay.engaged || !mapCursor.proj) return [];
  return replayPositions().map(({ pick, j, done }) => {
    const [x, y] = mapCursor.proj(...pickWorldAt(pick, j), false);
    return { x, y, color: pick.color, letter: pickLetter(pick), done };
  });
}

/* A's sample index at the current clock: what the chart cursor shows */
function replayRefIndex() {
  const lc = lapClock(refPick().data);
  const { a, b, f } = keptSample(lc, fracIndex(lc.c, replay.t));
  return f < 0.5 ? a : b;
}

function fmtReplayClock(s) {
  const m = Math.floor(s / 60);
  return `${m}:${(s - m * 60).toFixed(1).padStart(4, "0")}`;
}

/* everything the clock moves: ghosts, scrubber, clock + gap text, the chart
   cursor (and with it the raw table), and — at a reading pace — the map's
   aria-label */
function replayUpdate({ drive = true } = {}) {
  const ref = refPick();
  if (!ref || !pixiMap) return;
  const scrub = $("#rp-scrub");
  if (scrub) scrub.value = replay.t;
  const clock = $("#rp-clock");
  if (clock) clock.textContent = fmtReplayClock(replay.t);
  const gaps = replayGaps();
  const gapEl = $("#rp-gap");
  if (gapEl) {
    // letters and numbers only: session names never reach innerHTML here
    gapEl.innerHTML = gaps.length
      ? `Δ vs ${pickLetter(ref)} ` + gaps.map(({ pick, gap }) =>
          `<span style="color:${pick.color}">${pickLetter(pick)} ${gap >= 0 ? "+" : "−"}${Math.abs(gap).toFixed(3)}</span>`).join(" ")
      : "";
  }
  if (drive) driveChartCursor(replayRefIndex());
  const now = performance.now();
  if (!replay.playing || now - replay.said > 1000) {
    replay.said = now;
    const canvas = $("#trackmap");
    if (canvas) canvas.setAttribute("aria-label", `Lap replay ${replay.mode === "time" ? "by time" : "by distance"} at `
      + `${fmtReplayClock(replay.t)}` + (gaps.length ? ", " + gaps.map(({ pick, gap }) =>
        `${pickLetter(pick)} ${Math.abs(gap).toFixed(1)} seconds ${gap >= 0 ? "behind" : "ahead of"} ${pickLetter(ref)}`).join(", ") : ""));
  }
  followUpdate();
  pixiMap.drawMarkers(replay.playing);
}

/* the replay moves every chart's cursor to A's position (uPlot's sync group
   only publishes moves made by the mouse, so each chart is set directly).
   The setCursor hooks call setMapCursor; `driving` tells it this is us. */
function driveChartCursor(idx) {
  const x = refPick().data.dist[idx];
  replay.driving = true;
  try {
    for (const c of state.charts) {
      const left = c.valToPos(x, "x");
      c.setCursor({ left, top: c.bbox.height / devicePixelRatio / 2 });
    }
    if (!state.charts.length) setMapCursor(idx);
  } finally {
    replay.driving = false;
  }
}

/* setMapCursor's first question: does the replay own the cursor? While it is
   engaged, our own moves only update the raw table; a hover on a chart pauses
   playback and scrubs the replay to that spot. */
function replayOnChartCursor(idx) {
  if (!replay.engaged) return false;
  if (replay.driving) {
    mapCursor.idx = idx;
    updateRawTable(idx ?? mapCursor.pin);
    return true;
  }
  if (idx != null) {
    const A = refPick().data;
    setReplayPlaying(false);
    replay.t = lapClockAtDist(A, A.dist[Math.min(idx, A.dist.length - 1)]);
    mapCursor.idx = idx;
    updateRawTable(idx);
    replayUpdate({ drive: false });
  }
  return true;
}

/* A pointer on a chart pauses playback. It has to be caught here, before
   uPlot: while playing, the replay sets every chart's cursor each frame, and
   that overwrites the mouse position uPlot is about to handle, so the hover
   would never reach setMapCursor. Once paused, uPlot's own hover scrubs. */
function replayChartHover() {
  if (replay.playing) setReplayPlaying(false);
}

function replayTick(now) {
  if (!replay.playing) return;
  const dt = replay.last ? Math.min(now - replay.last, 100) : 0;  // a stalled tab doesn't fast-forward
  replay.last = now;
  const [lo, hi] = replayRange();
  replay.t += (dt / 1000) * replay.speed;
  if (replay.t >= hi) {
    // a zoom span loops; the whole lap stops at the line
    if (state.zoomRange) replay.t = lo + (replay.t - hi) % Math.max(hi - lo, 1e-3);
    else { replay.t = hi; setReplayPlaying(false); }
  }
  replayUpdate();
  if (replay.playing) requestAnimationFrame(replayTick);
}

function setReplayPlaying(on) {
  if (on === replay.playing) return;
  replay.playing = on;
  const btn = $("#rp-play");
  if (btn) {
    btn.setAttribute("aria-pressed", String(on));
    btn.textContent = on ? "❚❚" : "▶";
    btn.title = on ? "Pause the replay (Space on the map)" : "Play the tagged laps against each other (Space on the map)";
  }
  if (!on) return;
  const [lo, hi] = replayRange();
  if (replay.t >= hi - 1e-3 || replay.t < lo) replay.t = lo;  // play at the end restarts
  replay.engaged = true;
  replay.last = 0;
  requestAnimationFrame(replayTick);
}

/* back to a plain map: a new pick set, a new session, a hidden tab */
function resetReplay() {
  setReplayPlaying(false);
  follow.on = false;  // it followed a ghost that is gone now
  follow.ease = null;
  replay.engaged = false;
  replay.t = 0;
  syncReplayControls();
}

/* the controls row: shown only where the Pixi map can draw ghosts, with a
   reference lap to replay; scrubber bounds follow the playable window */
function syncReplayControls() {
  const bar = $("#replay");
  if (!bar) return;
  const ref = refPick();
  bar.hidden = !pixiMap || !ref;
  if (bar.hidden) return;
  const [lo, hi] = replayRange();
  replay.t = Math.min(Math.max(replay.t, lo), hi);
  const scrub = $("#rp-scrub");
  scrub.min = lo;
  scrub.max = hi;
  scrub.value = replay.t;
  $("#rp-clock").textContent = fmtReplayClock(replay.t);
  for (const b of bar.querySelectorAll("#rp-align button"))
    b.setAttribute("aria-pressed", String(b.dataset.align === replay.mode));
  followSync();
  if (replay.engaged) replayUpdate({ drive: !replay.playing });
  else $("#rp-gap").textContent = "";
}

/* wired by mountDetail on every pane clone */
function bindReplayControls() {
  const bar = $("#replay");
  if (!bar) return;
  $("#rp-play").onclick = () => setReplayPlaying(!replay.playing);
  $("#rp-scrub").oninput = (e) => {
    replay.t = Number(e.target.value);
    replay.engaged = true;
    replayUpdate();
  };
  const speed = $("#rp-speed");
  speed.innerHTML = REPLAY_SPEEDS.map((s) => `<option value="${s}">${s}×</option>`).join("");
  speed.value = String(replay.speed);
  speed.onchange = (e) => { replay.speed = Number(e.target.value); };
  for (const b of bar.querySelectorAll("#rp-align button")) {
    b.onclick = () => {
      replay.mode = b.dataset.align;
      follow.leader = null;
      syncReplayControls();
    };
  }
  $("#mf-lock").onclick = () => setFollow(!follow.on);
  $("#mf-target").onchange = (e) => {
    const v = e.target.value;
    follow.target = v === "ref" || v === "leader" ? v : state.picks[Number(v)];
    follow.leader = null;
    followEase();
  };
  setReplayPlaying(false);
  syncReplayControls();
}

/* on the focused map (the canvas is focusable): Space toggles playback,
   F the follow camera */
function bindReplayKeys(canvas) {
  canvas.addEventListener("keydown", (e) => {
    if (!pixiMap || !refPick() || e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key === " ") setReplayPlaying(!replay.playing);
    else if (e.key === "f" || e.key === "F") setFollow(!follow.on);
    else return;
    e.preventDefault();
  });
}

/* ---------- follow camera (issue #106) ----------
   The lock button hands the map's pan to one car: on every update the pan
   is re-derived so that car's ghost sits at the map's centre. Derived, never
   accumulated, so the yaw and the zoom stay the user's — and a 3D rotate
   pivots about the car for free: the drag redraws under the new yaw, and
   drawMap centres the car again before anything is laid out. A pan drag
   takes the camera back (bindMapDrag).

   Moves are cuts by default: a scrub, a zoom span looping round, any jump
   simply lands centred. Only a change of *what* is followed (locking on,
   another target, a new leader) eases, by bleeding off over FOLLOW_EASE_MS
   the screen offset the new target started at — plus, locking on at full
   fit, the zoom in to FOLLOW_ZOOM (following a whole-track view would only
   slide the track around).

   Per frame the camera costs a transform: pixiMap.pan() moves what was last
   drawn, no projection and no glyph is recomputed. Only an easing zoom goes
   through drawMap, for its quarter second. Nothing runs while paused except
   a running ease, on its own rAF chain that ends with it. */

const FOLLOW_EASE_MS = 250;
const FOLLOW_ZOOM = 3;
const LEADER_HOLD_S = 0.5;  // replay seconds a new leader must hold the lead

const follow = {
  on: false,
  target: "ref",          // "ref" (lap A, whichever that is) | "leader" | a pick
  leader: null,           // the pick the camera currently treats as leading
  cand: null, candT: 0,   // a challenger, and the replay clock it took over at
  ease: null,             // { t0, dx, dy, z0, z1 }: what is left to bleed off
  raf: 0,
  w: 0, h: 0,             // the map's CSS size at the last draw
};

function setFollow(on) {
  if (!pixiMap || !refPick() || on === follow.on) return;
  follow.on = on;
  follow.leader = null;
  follow.ease = null;
  if (on) {
    replay.engaged = true;  // the camera follows a ghost: show them
    startEase(mapView.zoom < FOLLOW_ZOOM ? FOLLOW_ZOOM : null);
  }
  syncReplayControls();  // ...which runs the first step
}

/* ease toward the (new) target from wherever it is on screen now */
function startEase(zoomTo = null) {
  follow.ease = { t0: null, dx: null, dy: null, z0: mapView.zoom, z1: zoomTo };
}
function followEase() {
  startEase();
  if (replay.engaged) replayUpdate({ drive: !replay.playing });
}

/* the car with the most track behind it, held for LEADER_HOLD_S of replay
   time before the camera moves to it — two close cars would otherwise swap
   the camera back and forth. Paused, a scrub is a jump anyway: no hold. */
function followLeader() {
  let best = null, bestD = -Infinity;
  for (const { pick, j, done } of replayPositions()) {
    if (done) continue;
    const d = lerpAt(lapClock(pick.data).dist, j);
    if (d > bestD) { bestD = d; best = pick; }
  }
  best = best || refPick();  // everyone is home: back to A
  if (!follow.leader || !state.picks.includes(follow.leader) || !replay.playing) {
    follow.leader = best;
    follow.cand = null;
  } else if (best === follow.leader) {
    follow.cand = null;
  } else if (best !== follow.cand) {
    follow.cand = best;
    follow.candT = replay.t;
  } else if (Math.abs(replay.t - follow.candT) >= LEADER_HOLD_S) {
    follow.leader = best;
    follow.cand = null;
    startEase();
  }
  return follow.leader;
}

function followPick() {
  const ref = refPick();
  if (!ref || follow.target === "ref") return ref;
  // By distance stands every car on A's spot: there is no leader to follow
  if (follow.target === "leader") return replay.mode === "time" ? followLeader() : ref;
  return follow.target;
}

/* set mapView's pan so the followed car lands at the centre (or on its way
   there). `P` is the projection about to be drawn with; it reads mapView
   live, so the car is measured at the current pan and moved by the rest. */
function followAim(P, w, h) {
  follow.w = w;
  follow.h = h;
  if (!follow.on) return;
  const pick = followPick();
  if (!pick || !pick.data) return;
  const pos = replayPositions().find((r) => r.pick === pick);
  if (!pos) return;
  const e = follow.ease;
  let left = 0;
  if (e) {
    const now = performance.now();
    if (e.t0 == null) e.t0 = now;
    const k = Math.min(1, (now - e.t0) / FOLLOW_EASE_MS);
    left = (1 - k) ** 3;  // ease-out cubic: what is still to go
    if (e.z1 != null) mapView.zoom = e.z1 + (e.z0 - e.z1) * left;
    if (k >= 1) follow.ease = null;
  }
  const [sx, sy] = P(...pickWorldAt(pick, pos.j), false);
  if (e && e.dx == null) { e.dx = sx - w / 2; e.dy = sy - h / 2; }
  const tx = w / 2 + (e ? e.dx * left : 0), ty = h / 2 + (e ? e.dy * left : 0);
  mapView.panX += tx - sx;
  mapView.panY += ty - sy;
}

/* one camera step, from replayUpdate (the caller draws the markers) */
function followUpdate() {
  if (!follow.on || !mapCursor.proj) return;
  if (follow.ease && follow.ease.z1 != null) {
    drawMap();  // a zoom changes the layout, not just its position
  } else {
    followAim(mapCursor.proj, follow.w, follow.h);
    pixiMap.pan();
  }
  if (follow.ease && !replay.playing && !follow.raf) follow.raf = requestAnimationFrame(followTick);
}
function followTick() {
  follow.raf = 0;
  if (!follow.on || replay.playing) return;  // the replay loop has it now
  followUpdate();
  pixiMap.drawMarkers(true);
}

/* the lock button and its target picker: shown with the replay row */
function followSync() {
  const box = $("#map-follow");
  const ref = refPick();
  const t = follow.target;
  if (t !== "ref" && t !== "leader" && (t === ref || !t.data || !state.picks.includes(t))) {
    if (t !== ref) follow.on = false;  // the followed lap left the tray
    follow.target = "ref";
  }
  if (replay.mode === "dist" && follow.target === "leader") follow.target = "ref";
  if (!box) return;
  box.hidden = !pixiMap || !ref;
  if (box.hidden) {
    follow.on = false;
    return;
  }
  const btn = $("#mf-lock");
  btn.setAttribute("aria-pressed", String(follow.on));
  btn.title = follow.on
    ? "Following — click (or F on the map) for a free camera; dragging to pan frees it too"
    : "Follow a car: the map pans with it while you rotate and zoom (F on the map)";
  const sel = $("#mf-target");
  sel.hidden = !follow.on;
  const dist = replay.mode === "dist";
  sel.innerHTML = `<option value="ref">Lap ${pickLetter(ref)}</option>`
    + `<option value="leader"${dist ? ` disabled title="By distance, every lap stands where lap ${pickLetter(ref)} is: there is no leader"` : ""}>Leader</option>`
    + state.picks.filter((p) => p.data && p !== ref)
        .map((p) => `<option value="${state.picks.indexOf(p)}">Lap ${pickLetter(p)}</option>`).join("");
  sel.value = typeof follow.target === "string" ? follow.target : String(state.picks.indexOf(follow.target));
  updateMapHint();
}

document.addEventListener("visibilitychange", () => {
  if (document.hidden) setReplayPlaying(false);
});
