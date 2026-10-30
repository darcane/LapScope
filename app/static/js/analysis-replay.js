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

/* fractional index of x in an ascending array (clamped to its ends) */
function fracIndex(xs, x) {
  const n = xs.length;
  if (!n || x <= xs[0]) return 0;
  if (x >= xs[n - 1]) return n - 1;
  const i = lowerBound(xs, x);
  const span = xs[i] - xs[i - 1];
  return span > 0 ? i - 1 + (x - xs[i - 1]) / span : i;
}
function lerpAt(arr, k) {
  const i = Math.floor(k), f = k - i;
  return f && i + 1 < arr.length ? arr[i] + (arr[i + 1] - arr[i]) * f : arr[i];
}

/* A lap's timeline as the game counts it, strictly increasing: the samples it
   kept and the lap clock at each. Neither `t` nor the raw lap_time channel
   will do — `t` keeps counting through a rewind (the 6 s a rewind took back
   are in it, and the replay would drive that stretch twice), and lap_time
   drops back at a rewind and to ~0 on the crossing frame, the last sample.
   So: a rewind discards the samples it took back (the clock dropped below
   them), and a reset to ~0 in the last few samples continues the clock by
   elapsed time instead. `dist` is the kept samples' track position. Cached
   per data object (a refetch is a new one). */
const lapClocks = new WeakMap();
function lapClock(d) {
  let lc = lapClocks.get(d);
  if (lc) return lc;
  const lt = d.channels.lap_time, n = lt.length;
  const idx = [], c = [];
  for (let i = 0; i < n; i++) {
    let v = lt[i];
    const prev = c[c.length - 1];
    if (c.length && i >= n - 3 && v < 1 && prev > 5)  // the line crossing
      v = prev + (d.t[i] - d.t[idx[idx.length - 1]]);
    while (c.length && c[c.length - 1] >= v) { c.pop(); idx.pop(); }  // rewound
    idx.push(i);
    c.push(v);
  }
  lc = { idx, c, dist: idx.map((i) => d.dist[i]), end: c[c.length - 1] };
  lapClocks.set(d, lc);
  return lc;
}
/* a fractional position on the kept timeline -> the sample index and the
   two samples to interpolate between */
function keptSample(lc, j) {
  const i = Math.floor(j), f = j - i;
  return { a: lc.idx[i], b: lc.idx[Math.min(i + 1, lc.idx.length - 1)], f };
}
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

/* screen-space ghosts for the renderer: { x, y, color, letter, done } */
function replayGhosts() {
  if (!replay.engaged || !mapCursor.proj) return [];
  return replayPositions().map(({ pick, j, done }) => {
    const c = pick.data.channels;
    const { a, b, f } = keptSample(lapClock(pick.data), j);
    const at = (arr) => arr[a] + (arr[b] - arr[a]) * f;
    const [x, y] = mapCursor.proj(at(c.pos_x), c.pos_y ? at(c.pos_y) : 0, at(c.pos_z), false);
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
      syncReplayControls();
    };
  }
  setReplayPlaying(false);
  syncReplayControls();
}

/* Space on the focused map toggles playback (the map canvas is focusable) */
function bindReplayKeys(canvas) {
  canvas.addEventListener("keydown", (e) => {
    if (e.key !== " " || !pixiMap || !refPick()) return;
    e.preventDefault();
    setReplayPlaying(!replay.playing);
  });
}

document.addEventListener("visibilitychange", () => {
  if (document.hidden) setReplayPlaying(false);
});
