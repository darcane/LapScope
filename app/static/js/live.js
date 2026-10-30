/* Live feed shared by the live pages (index.html, classic.html): the
   WebSocket, the connection chip, the friction-trail and input-trace buffers,
   and the live track map's path + contact/jump detection. Pages only read
   `live` and `liveMap` and draw them — nothing here touches a canvas.
   Load after common.js + settings.js, before the page's own script. */

const STALE_MS = 2500;   // stream silent this long → chip says "paused"
const G = 9.80665;
const TRAIL_LEN = 90;    // friction-circle history, ~1.5 s at 60 Hz
const STRIP_LEN = 12 * 60;  // input trace, ~12 s at 60 Hz

const live = {
  frame: null,
  lastMsg: 0,
  connected: false, // WebSocket open (distinct from the stream being stale)
  connLabel: null,  // last connection chip text (avoids per-frame DOM thrash)
  trail: [],        // [latG, lonG] history for the friction circle
  strip: [],        // input history: {th (0-100), br (0-100), st (-100..100)}
  stripN: 0,        // samples ever pushed: a renderer can redraw only when this moves
};

/* live track map: path of the current session, thinned adaptively so long
   drives stay cheap to redraw at display refresh rate */
const LIVEMAP_CAP = 4000;
// ground-plane acceleration (m/s^2) that counts as a contact/collision, plus
// the airborne/jump-landing discrimination (a spike while airborne or right
// after touchdown is a landing, not contact) and the impulse test that keeps
// a downforce car's cornering load from reading as a wall (issue #49);
// mirrors IMPACT_* / AIRBORNE_* / LANDING_GRACE_S and impulsive() in
// app/recorder/laps.py (keep in lockstep).
const IMPACT_ACCEL = 45;
const IMPACT_JERK = 30;
const IMPACT_JERK_DT_MAX = 0.05;
const IMPACT_PEAK = 98;
const AIRBORNE_SUSP_MAX = 0.15;
const AIRBORNE_SLIP_MAX = 0.05;
const AIRBORNE_MIN_S = 0.12;
const LANDING_GRACE_S = 0.35;

// mirrors impulsive() in laps.py: an impact either jumps IMPACT_JERK in one
// frame (only trustworthy across a sane frame gap) or reaches IMPACT_PEAK,
// which no measured aero cornering approaches. prev = [t, g] or null.
function impulsive(t, g, prev) {
  if (g >= IMPACT_PEAK) return true;
  if (!prev) return false;
  const dt = t - prev[0];
  return dt > 0 && dt <= IMPACT_JERK_DT_MAX && g - prev[1] >= IMPACT_JERK;
}

const liveMap = {
  pts: [],         // [x, z] world points
  last: null,      // last stored point
  minDist: 3,      // m between stored points; doubles when thinned
  session: null,
  gen: 0,          // bumped whenever pts is replaced, not appended to (reset, thinning)
  minX: Infinity, maxX: -Infinity, minZ: Infinity, maxZ: -Infinity,
  hits: [],        // [x, z] world points where a contact spike fired
  jumps: [],       // {x0, z0, x1, z1, hard} takeoff -> touchdown segments
  marks: 0,        // bumped on any change to hits/jumps (incl. a jump turning hard)
  overImpact: false, // above the threshold last frame (edge-detect one hit/impact)
  burstSparked: false, // this burst already pushed its marker (one per impact)
  prevG: null,     // previous frame's [t, ground-plane g], for the impulse test
  airSince: null,  // when the current all-wheels-unloaded stretch began
  airStart: null,  // [x, z] where that stretch began (the takeoff point)
  graceUntil: 0,   // spikes before this time are jump landings, not contact
  pendingHard: false, // mid-flight spike waiting for its jump (pushed at touchdown)
};

function resetLiveMap(sessionId) {
  Object.assign(liveMap, {
    session: sessionId, pts: [], last: null, minDist: 3,
    minX: Infinity, maxX: -Infinity, minZ: Infinity, maxZ: -Infinity,
    hits: [], jumps: [], overImpact: false, burstSparked: false, prevG: null,
    airSince: null, airStart: null, graceUntil: 0, pendingHard: false,
  });
  liveMap.gen++;
  liveMap.marks++;
}

function mapActive(f) {
  // races/time-attacks always; free roam only when the user opts in
  return f.race_mode || getSettings().freeroamMap;
}

// One marker per impact: register on the rising edge only, so grinding a wall
// (many frames over the threshold) leaves a single dot. Gated exactly like the
// map path — races/time-attacks only, same session — and runs after
// feedLiveMap so a session change / grid snap has already cleared old hits.
function feedCollision(f) {
  if (f.session_id == null || !mapActive(f) || f.session_id !== liveMap.session) {
    liveMap.overImpact = false;
    liveMap.burstSparked = false;
    liveMap.prevG = null;
    liveMap.airSince = null;
    liveMap.airStart = null;
    liveMap.pendingHard = false;
    return;
  }
  const t = f._t;
  const airborne = f.norm_susp_travel.every((s) => s < AIRBORNE_SUSP_MAX)
    && f.tire_combined_slip.every((s) => s < AIRBORNE_SLIP_MAX);
  if (airborne) {
    if (liveMap.airSince == null) {
      liveMap.airSince = t;
      liveMap.airStart = [f.pos_x, f.pos_z];
    }
  } else {
    if (liveMap.airSince != null && t - liveMap.airSince >= AIRBORNE_MIN_S) {
      liveMap.graceUntil = t + LANDING_GRACE_S;
      // a real flight just ended: this frame is the touchdown; a spike seen
      // mid-flight belongs to THIS jump (issue #41)
      liveMap.jumps.push({ x0: liveMap.airStart[0], z0: liveMap.airStart[1],
                           x1: f.pos_x, z1: f.pos_z, hard: liveMap.pendingHard });
      liveMap.pendingHard = false;
      liveMap.marks++;
    }
    liveMap.airSince = null;
  }
  const flying = liveMap.airSince != null && t - liveMap.airSince >= AIRBORNE_MIN_S;
  const g = Math.hypot(f.accel_x, f.accel_z);
  if (g >= IMPACT_ACCEL) {
    if (!liveMap.overImpact) {
      liveMap.burstSparked = false;
      if (flying) {
        // mid-flight spike: its jump isn't pushed until touchdown - hold it
        // instead of marking the previous jump hard (issue #41)
        liveMap.pendingHard = true;
        liveMap.burstSparked = true;   // a landing never sparks
      } else if (t < liveMap.graceUntil) {
        // the landing of the jump that just ended: mark its glyph hard, no spark
        const j = liveMap.jumps[liveMap.jumps.length - 1];
        if (j && !j.hard) { j.hard = true; liveMap.marks++; }
        liveMap.burstSparked = true;
      }
    }
    // the spark waits for the frame that actually looks like an impact, which
    // may not be the burst's first: a hit taken mid-corner starts out looking
    // like the aero load it is riding on top of (issue #49)
    if (!liveMap.burstSparked && impulsive(t, g, liveMap.prevG)) {
      liveMap.hits.push([f.pos_x, f.pos_z]);
      liveMap.burstSparked = true;
      liveMap.marks++;
    }
    liveMap.overImpact = true;
  } else {
    liveMap.overImpact = false;
  }
  liveMap.prevG = [t, g];
}

function feedLiveMap(f) {
  // only draw during an actual event (race / time attack / point-to-point):
  // IsRaceOn is 1 in free roam too, so fast-travel sprawl would wreck the
  // map - race_mode comes from the recorder, which knows the difference.
  // The finished track stays on screen until the next event starts.
  if (f.session_id == null || !mapActive(f)) return;
  if (f.session_id !== liveMap.session) {
    // A pause longer than the recorder's grace (photo mode, a long sit in the
    // pause menu) splits the recording into a new session id, but the race
    // itself resumes exactly where it stopped. Keep drawing in that case:
    // same place (within the 250 m teleport rule below) AND the race clock
    // kept its value - a restart or a genuinely new event resets it to ~0,
    // which is the very signal the recorder splits sessions on.
    const resumed = liveMap.last != null
      && Math.hypot(f.pos_x - liveMap.last[0], f.pos_z - liveMap.last[1]) < 250
      && f.current_race_time > 5;
    if (resumed) liveMap.session = f.session_id;
    else resetLiveMap(f.session_id); // new event -> fresh track
  }
  const x = f.pos_x, z = f.pos_z;
  if (liveMap.last) {
    const jump = Math.hypot(x - liveMap.last[0], z - liveMap.last[1]);
    if (jump < liveMap.minDist) return;
    // a car can't move 250 m in one frame: that's a grid snap / event
    // restart. Start the track fresh from here - keeping the old points
    // would wreck the scale (the bounds span both places) and overlay
    // two different pieces of world on one map.
    if (jump > 250) resetLiveMap(f.session_id);
  }
  liveMap.last = [x, z];
  liveMap.pts.push(liveMap.last);
  liveMap.minX = Math.min(liveMap.minX, x); liveMap.maxX = Math.max(liveMap.maxX, x);
  liveMap.minZ = Math.min(liveMap.minZ, z); liveMap.maxZ = Math.max(liveMap.maxZ, z);
  if (liveMap.pts.length > LIVEMAP_CAP) { // free roam can sprawl: thin + relax
    liveMap.pts = liveMap.pts.filter((_, i) => i % 2 === 0);
    liveMap.minDist *= 2;
    liveMap.gen++;
  }
}

/* Reconnect backoff: a server that is gone for good was being retried every
   1.5 s forever. Doubles up to RECONNECT_MAX, resets on a successful open
   (issue #73). */
const RECONNECT_MIN = 1500;
const RECONNECT_MAX = 15000;
let reconnectDelay = RECONNECT_MIN;

function connect() {
  // wss when the page itself is served over TLS: a hardcoded ws:// behind a
  // reverse proxy is blocked as mixed content, and the socket never opens
  const scheme = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${scheme}//${location.host}/ws/live`);
  ws.onmessage = (ev) => {
    // A hidden tab renders nothing (rAF is paused by the browser), so
    // parsing 64-field frames at 60 Hz into buffers nobody will see is
    // pure CPU. Keep the socket open — reconnecting on every tab switch is
    // worse — and drop the frames instead.
    if (document.hidden) return;
    const f = JSON.parse(ev.data);
    live.frame = f;
    live.lastMsg = performance.now();
    if (f.is_race_on) {
      live.trail.push([f.accel_x / G, f.accel_z / G]);
      if (live.trail.length > TRAIL_LEN) live.trail.shift();
      live.strip.push({ th: f.accel / 2.55, br: f.brake / 2.55, st: f.steer / 1.27 });
      if (live.strip.length > STRIP_LEN) live.strip.shift();
      live.stripN++;
    }
    feedLiveMap(f);
    feedCollision(f);
  };
  ws.onopen = () => {
    live.connected = true;
    reconnectDelay = RECONNECT_MIN;
    setConn("live", "ok");
  };
  ws.onclose = () => {
    live.connected = false;
    setConn("reconnecting…", "err");
    setTimeout(connect, reconnectDelay);
    reconnectDelay = Math.min(RECONNECT_MAX, reconnectDelay * 2);
  };
  ws.onerror = () => ws.close();
}

/* Coming back to the tab after frames were dropped: the last one on screen is
   however old, so re-arm the staleness clock from now rather than showing a
   PAUSED chip for a stream that is running fine. */
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && live.frame) live.lastMsg = performance.now();
});

function setConn(text, cls) {
  if (live.connLabel === text) return; // idempotent: skip redundant DOM writes
  live.connLabel = text;
  const el = document.getElementById("conn");
  el.querySelector("span").textContent = text;
  el.className = `chip ${cls}`;
}

/* Per render frame: is the stream stale, and keep the chip saying so. */
function syncConn() {
  const stale = performance.now() - live.lastMsg > STALE_MS;
  if (live.frame && live.connected) setConn(stale ? "paused" : "live", stale ? "warn" : "ok");
  return stale;
}

// toggling the free-roam-map setting off mid-drive: drop the accumulated path
// so it doesn't linger on screen (races clear it on their own on session change)
onSettingsChange(() => {
  if (!getSettings().freeroamMap) resetLiveMap(liveMap.session);
});
