# ForzaCalibrator

A self-hosted telemetry dashboard for **Forza Horizon 6**. Runs in Docker on your PC,
receives the game's official "Data Out" UDP stream, and serves a web UI with:

- **Live dashboard** — speed/RPM/gear gauges, friction circle (G-forces), per-tire grip
  panel (combined slip: green = grip, red = sliding), understeer/oversteer indicator,
  throttle/brake/steering traces, lap timer with **live delta vs. your session-best lap**,
  and a **live track map** that draws the circuit as you drive. The map and lap timer
  only run in **race mode** (a RACE MODE / FREE ROAM chip shows which): IsRaceOn is 1
  even in free roam, so the recorder detects events itself — a grid position, live lap
  fields, or the odometer reset that marks a World Time Attack / point-to-point launch.
- **Recording & analysis** — timed drives are stored in SQLite. Browse sessions, see lap
  times, draw your **racing line on a track map** colored by speed or tire slip, and
  compare two laps (A vs. B) with distance-aligned charts: time delta, speed, inputs,
  steering, and tire slip. The map has a **2D/3D toggle** — 3D uses the packet's
  elevation (PositionY) and is drag-to-rotate.
- **Dirty-lap flags** — the packet has no official "lap invalidated" field, so the
  recorder infers it: ⏪ **rewind** (the lap clock ran backwards — reversing on track
  never does that) and 💥 **contact** (a ground-plane G-spike beyond anything tires can
  generate). Rewound stretches are also cut from charts and the map, so only the
  finally-driven line is shown.
- **Session metadata** — Forza-colored class/PI ribbons (FH6 classes, including
  the new **R class**: 901–998 PI, with X reserved for 999), drivetrain badges
  (FWD/RWD/AWD, straight from the packet), car names (bundled community FH6 ordinal
  list + your own overrides), track conditions (wet is auto-detected from puddle
  telemetry; snow/dirt are one-click manual tags), and a track-type tag
  (road/street/touge/dirt/cross-country/drag/WTC — not in the packet, so it's
  a dropdown).
- **Races and point-to-point events** — the game ends an event *without* counting
  the last lap, so the recorder watches for two finish signals: `LastLap` changing
  while `LapNumber` stands still (final lap of a circuit race), and the race clock
  freezing during the finish cinematic. Sprints, drags, street races, and other
  point-to-point events (no laps at all) are captured as a single timed run.
- **World Time Attack** — WTA broadcasts *no lap fields at all* (verified on a real
  capture: LapNumber, CurrentLap, LastLap and BestLap stay 0 the whole event), so
  laps are detected geometrically: the launch point becomes the anchor and a lap
  completes on each same-direction return to it. The run finish is spotted by the
  game hard-resetting `DistanceTraveled`. Sessions recorded *before* this support
  existed can be recovered with the **Reprocess** button.
- **Routes** — the game never broadcasts route names, so circuits are fingerprinted
  from the lap start position + lap length. Name a route once ("Name route" button)
  and every past and future session on it picks the name up automatically. Free-roam
  **time-attack circuits** are captured too: the server starts a new session whenever
  the race clock resets or the lap timer starts counting mid-drive.
- **No junk entries** — sessions that end without a single completed lap (free-roam
  cruising, menu blips) are discarded automatically.

The game only broadcasts *your* car (no rival data), so driving quality is measured
against your own best lap and the tires' grip limit — which is what actually makes
you faster in Rivals.

## Quick start

```bash
docker compose up --build -d
```

Open **http://localhost:8000** — you'll see "Waiting for telemetry…" until the game sends data.

### In Forza Horizon 6

`Settings → HUD and Gameplay`:

| Setting             | Value       |
|---------------------|-------------|
| Data Out            | `ON`        |
| Data Out IP Address | `127.0.0.1` |
| Data Out IP Port    | `9999`      |

Then just drive. Telemetry is only sent while driving (not in menus). Timed events
(Rivals, races, time trials) get automatic lap detection; free roam is recorded as a
plain session.

> Do **not** use ports 5200–5300 — the game binds its own socket in that range.

## Test without the game

```bash
python tools/simulator.py                                   # ~3.5 laps, 1 event
python tools/simulator.py --freeroam 20 --events 2 --wet    # full feature test
python tools/simulator.py --duration 180 --dirty            # wall contact + rewind flags
python tools/simulator.py --race 3 --duration 200           # race with a real finish
python tools/simulator.py --sprint 75 --jumps               # point-to-point + jumps
```

The live dashboard should move immediately, and a session with laps appears on the
Analysis page ~15 s after the simulator finishes.

## Troubleshooting: no packets arriving (Xbox app / Microsoft Store version)

Store (UWP) builds of games can be blocked from sending to `127.0.0.1`. In order:

1. Try `127.0.0.1:9999` first — it is officially supported by FH6.
2. Use your PC's **LAN IP** instead (find it with `ipconfig`, e.g. `192.168.1.20`),
   keeping port `9999`. Docker publishes the port on all interfaces, and this
   bypasses UWP loopback isolation.
3. Check that nothing else on the host has stolen UDP 9999 — another app can grab
   the port while the container is being recreated, after which Docker's proxy
   silently binds only IPv6 and `/api/status` shows 0 packets forever:

   ```powershell
   Get-NetUDPEndpoint -LocalPort 9999 | Format-Table LocalAddress, OwningProcess
   ```

   If a process other than `com.docker.backend` owns `0.0.0.0:9999`, close it (or
   move ForzaCalibrator to a free port), then `docker compose down && docker compose up -d`.

4. Last resort — add a one-time loopback exemption (admin PowerShell):

   ```powershell
   Get-AppxPackage *Forza* | Select-Object PackageFamilyName
   CheckNetIsolation.exe LoopbackExempt -a -n=<PackageFamilyName>
   ```

Check what the server sees at any time: **http://localhost:8000/api/status**
(packet counters, last-packet age, wrong-size packet warnings) or
`docker compose logs -f`.

## Troubleshooting: an event type isn't being recorded

Sessions without a single completed lap are normally discarded (that's what keeps
free-roam cruising out of the list) — but if the game signals some event type in a
way the recorder doesn't recognize yet, those sessions get discarded too. (World
Time Attack was found and fixed exactly this way.) Three tools to pin it down:

1. Every discard logs a one-line signal summary — after driving the event, look for
   it in `docker compose logs`:

   ```
   Session 12 discarded (no completed laps, 4210 frames) | diag: dur=70s rt=0.0..68.2 maxLapNumber=0 maxCurrentLap=0.0 finish_seen=False dist=+1893
   ```

2. To keep the session (raw frames included) instead of losing it, restart with
   `FC_KEEP_DISCARDED=1` and drive the event once:

   ```powershell
   $env:FC_KEEP_DISCARDED = "1"; docker compose up -d
   ```

   (Or put `FC_KEEP_DISCARDED=1` in a `.env` file next to `docker-compose.yml` —
   compose reads it automatically.) The session then shows up on the Analysis page
   (0 laps, but the driven line and an "incomplete" run are there) and the raw data
   is preserved for adding proper support. Unset the variable (or set `0`) and
   `docker compose up -d` again when done.

3. Dump every signal transition of a stored session (race-clock resets and
   freezes, distance resets, lap-field activity, teleports, stream gaps) straight
   from the database — no game or container needed:

   ```powershell
   python tools/inspect_session.py --list     # find the session id
   python tools/inspect_session.py 12
   ```

Once support lands, the **Reprocess** button on the session rebuilds its laps from
the stored frames — recordings are lossless, so nothing has to be redriven.

## Configuration

| Env var              | Default     | Meaning                                        |
|----------------------|-------------|------------------------------------------------|
| `TELEMETRY_UDP_PORT` | `9999`      | UDP port the listener binds                    |
| `DATA_DIR`           | `/app/data` | Where `telemetry.db` is written                |
| `FC_KEEP_DISCARDED`  | `0`         | `1` = keep sessions with no completed laps     |

Recordings are raw 324-byte packets (~70 MB per hour of driving) in `./data/telemetry.db`;
delete sessions from the Analysis page to reclaim space.

## How it works

```
FH6 ──UDP 9999──▶ asyncio listener ──▶ parser (324-byte Data Out packet)
                                      ├──▶ WebSocket /ws/live ──▶ live dashboard
                                      └──▶ session/lap tracker ──▶ SQLite ──▶ REST /api ──▶ analysis page
```

Packet layout reference: [FH6 Data Out documentation](https://support.forza.net/hc/en-us/articles/51744149102611-Forza-Horizon-6-Data-Out-Documentation).
If a title update ever changes the packet size, the server logs a warning with a hex
dump instead of crashing — check the logs if data stops parsing.

**Quirk found in real FH6 data:** on real circuits, `DistanceTraveled` is *not*
driven meters — it advances by the same fixed amount every lap of a given route
(a track-position parameter, ~2.4–2.5× the true driven length). That makes it
ideal for aligning two laps by track position (how the comparison charts use it),
but not a length. The "Driven" figure on the analysis page is integrated from
speed instead.
