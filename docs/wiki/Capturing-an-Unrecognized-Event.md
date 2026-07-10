# Capturing an unrecognized event

FH6 never says "an event started" or "the race is over" — LapScope infers every
session and lap boundary from packet behavior (see
[Event Detection](Event-Detection)). When the game signals some event type in a
way the inference doesn't recognize yet, the drive is either **discarded**,
**mis-timed**, or **missing laps**. The only way LapScope learns a new event
type is from a real capture — World Time Attack, dirt sprints, touge, and
cross-country were each added exactly this way.

**This is the most valuable kind of contribution.** Here's the workflow.

## Why drives disappear in the first place

Sessions that end without a single completed lap or run are discarded — that's
the feature that keeps free-roam cruising and menu blips out of your session
list. An event the inference doesn't recognize ends up in the same bucket: no
lap was ever "completed", so the session is dropped at close (and stored
leftovers are cleaned again at startup).

## Step 1 — read the discard line

Every discard logs a one-line signal summary. After driving the event, look in
the console (exe) or `docker compose logs` (Docker):

```
Session 12 discarded (no completed laps, 4210 frames) | diag: dur=70s rt=0.0..68.2 maxLapNumber=0 maxCurrentLap=0.0 finish_seen=False gridded=True launch=True lastSpeed=51.3 dist=+1893
```

How to read the `diag:` fields:

| Field | Meaning |
|---|---|
| `dur` | Session length in seconds. |
| `rt` | `CurrentRaceTime` range — did the race clock run, and from where to where? |
| `maxLapNumber` | Highest `LapNumber` seen. 0 = the game never counted a lap (normal for point-to-point and World Time Attack). |
| `maxCurrentLap` | Highest `CurrentLap` clock seen. 0 = the per-lap clock never ran either. |
| `finish_seen` | Whether any finish signal fired (lap-time change, odometer reset, clock freeze). |
| `gridded` | Whether `RacePosition > 0` was ever seen — true means a real gridded event, never free roam. |
| `launch` | Whether a launch anchor was armed (a `DistanceTraveled` reset followed by movement — the point-to-point/WTA start signature). |
| `lastSpeed` | Speed on the final frame — high speed at cutoff usually means the stream was cut dead at a finish line. |
| `dist` | How much `DistanceTraveled` grew over the session. |

That one line often already narrows down what the inference missed.

## Step 2 — keep the session: `LS_KEEP_DISCARDED=1`

To preserve the session (raw frames included) instead of losing it, set
`LS_KEEP_DISCARDED=1`, restart LapScope, and drive the event **once**:

- **Docker** — put `LS_KEEP_DISCARDED=1` in a `.env` file next to
  `docker-compose.yml` (compose reads it automatically), or set it inline:

  ```powershell
  $env:LS_KEEP_DISCARDED = "1"; docker compose up -d
  ```

- **Windows exe** — start it from a shell with the variable set:

  ```powershell
  cd <your LapScope folder>
  $env:LS_KEEP_DISCARDED = "1"; .\LapScope.exe
  ```

  (Or `setx LS_KEEP_DISCARDED 1`, then double-click as usual — but remember to
  `setx LS_KEEP_DISCARDED 0` afterwards, since that variant persists.)

- **Running from source** — same variable, before `python run_desktop.py`.

The kept session then shows up on the Analysis page (0 laps, but the driven
line and an "incomplete" run are there), is exempt from the startup cleanup,
and its raw frames are preserved for adding proper support. Set the variable
back to `0` when you're done, or every free-roam cruise gets kept too.

## Step 3 — dump the signals: `inspect_session.py`

With a source checkout (plain stdlib — no container, no game, and the database
is opened read-only):

```powershell
python tools/inspect_session.py --list                 # find the session id
python tools/inspect_session.py 12
```

The default database path is `data/telemetry.db` (the Docker bind mount, run
from the repo root). The Windows exe stores its database elsewhere — point the
tool at it:

```powershell
python tools/inspect_session.py --list --db "$env:LOCALAPPDATA\LapScope\telemetry.db"
```

It prints one line per transition the segmentation cares about — `IsRaceOn`
flips, race-clock resets and freezes, `DistanceTraveled` resets, lap-field
activity, `RacePosition` changes, stream gaps, single-frame teleports — plus
the first/last frame and any stored laps. This is the exact evidence needed to
see what the event's signature looks like.

## Step 4 — file the capture issue

Open an
[unrecognized event capture](https://github.com/darcane/LapScope/issues/new?template=unrecognized_event.yml)
with:

- what the event was and what you expected (e.g. "one run of ~92 s"),
- the full `inspect_session.py` output,
- your LapScope version,
- whether you can share the session's `telemetry.db` — the raw frames are what
  let a regression test be added, so support never breaks again.

## Step 5 — after support lands: Reprocess

Recordings are lossless raw packets, so once a fix ships, the **Reprocess**
button on the session rebuilds its laps from the stored frames — nothing has
to be re-driven. (Reprocess is refused with a 409 while any recording is in
progress; wait ~15 s after you stop driving.)
