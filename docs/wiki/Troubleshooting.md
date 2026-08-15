# Troubleshooting

The [README](https://github.com/darcane/LapScope#readme) has the short version.
This page is the full diagnosis flow, in the order that finds the problem
fastest.

## First stop: `/api/status`

Open **http://localhost:8000/api/status** (or `127.0.0.1:8000` for the exe). It
answers most questions in one glance:

| Field | Meaning |
|---|---|
| `udp_error` | Non-null = LapScope **could not bind its UDP port** (another program has it). Nothing will arrive until that's fixed — see [Busy ports](#busy-ports) below. |
| `packets_total` | Total telemetry packets received since start. `0` while driving = the game's packets aren't reaching LapScope — see [No packets arriving](#no-packets-arriving). |
| `bad_packets` / `last_packet_size` | Packets of the wrong size. Non-zero = something else is sending to the port, or a game update changed the packet — see [Wrong-size packets](#wrong-size-packets). |
| `last_packet_age` | Seconds since the last packet. Remember: FH6 only sends **while you're driving**, not in menus or the pause screen. |
| `session_active` / `session_id` / `session_best` | What the recorder is doing right now. |
| `version` | The running build (`0.0.0` = unversioned source run). |

**Where the logs are:** the log pane in the LapScope window for the Windows exe,
`docker compose logs -f` for Docker. Every recorder decision (session opened,
lap completed, session discarded + why) is logged there.

The exe also writes the same lines to a file, which is the one to attach to a
bug report — the window's **Open log folder** button goes straight to it:

```
%LOCALAPPDATA%\LapScope\logs\lapscope.log
```

It rotates at 2 MB and keeps four files, so the last few runs are always there.
**Copy** puts the visible pane on the clipboard if that's quicker.

The window itself answers the same questions as `/api/status` in one line —
*Waiting for telemetry*, *Recording — session 12*, *Telemetry port blocked*,
*Recording — NOT saving* — so check it before opening anything.

## No packets arriving

`packets_total` stays 0 while you drive. Most common with **Microsoft Store /
Xbox-app (UWP) builds** of the game, which can be blocked from sending UDP to
`127.0.0.1`. Work through these in order:

### 1. Try plain loopback first

In FH6 under **Settings → HUD and Gameplay**: Data Out `ON`, IP `127.0.0.1`,
port `9999`. This is officially supported and works for most installs. Telemetry
only flows while driving, so be on the road when you check.

> ⚠️ Never use ports **5200–5300** — the game binds its own socket in that
> range, and the packets will go to the game instead of LapScope.

### 2. Use your PC's LAN IP instead

Find it with `ipconfig` (e.g. `192.168.1.20`) and put **that** as the Data Out
IP, keeping port `9999`. This bypasses UWP loopback isolation: LapScope listens
on all interfaces (both the exe and the Docker container), so packets addressed
to your LAN IP land in the same place. If Windows Firewall prompts when LapScope
first starts, allow it — a blocked inbound rule looks exactly like "no packets".

### 3. Check nothing stole the UDP port (Docker's silent failure mode)

Another app can grab UDP 9999 — and with Docker specifically it can happen
*silently*: if the port is taken while the container is being recreated,
Docker's proxy binds **only IPv6**, everything looks up and running, and
`/api/status` shows 0 packets forever. Check who owns the port:

```powershell
Get-NetUDPEndpoint -LocalPort 9999 | Format-Table LocalAddress, OwningProcess
Get-Process -Id <OwningProcess>
```

If a process other than `com.docker.backend` (Docker) or `LapScope`/`python`
(exe/source) owns `0.0.0.0:9999`, close it — or move LapScope to a free port
with `TELEMETRY_UDP_PORT` — then restart:
`docker compose down && docker compose up -d`.

The native exe doesn't have this failure mode: if it can't bind the port it
says so — *Telemetry port blocked* in the LapScope window, and
`/api/status` → `udp_error`.

### 4. Last resort: a UWP loopback exemption

Tell Windows to let the Store version of Forza send to loopback (one-time,
admin PowerShell):

```powershell
Get-AppxPackage *Forza* | Select-Object PackageFamilyName
CheckNetIsolation.exe LoopbackExempt -a -n=<PackageFamilyName>
```

Then set the Data Out IP back to `127.0.0.1`.

## Busy ports

- **UDP 9999 already in use** — LapScope starts anyway (the dashboard and past
  sessions still work) but shows the problem as *Telemetry port blocked* in the
  window and in `/api/status` → `udp_error`. Close the other program (often a
  second LapScope window, or another telemetry tool), or set
  `TELEMETRY_UDP_PORT` to a free port, then restart — and remember to change
  the port in the game too. On the exe the **Restart** button is enough for
  this one.
- **HTTP 8000 already in use** (exe) — the window opens as normal and shows an
  amber row explaining the conflict, with **Open dashboard** and **Retry**
  buttons; nothing crash-closes. Usually it's an already-running LapScope: open
  http://127.0.0.1:8000 — if the dashboard loads, use that one and close this
  window. Otherwise free the port and press **Retry**. To find the culprit:
  `Get-NetTCPConnection -LocalPort 8000 | Format-Table OwningProcess`.

  Note that a LapScope window keeps hold of port 8000 even while its server is
  stopped. That is deliberate: it is what stops a second copy starting up and
  writing to the same database.

## Wrong-size packets

LapScope expects exactly **324 bytes** per packet. On the first wrong-size
packet it logs a warning with the received size and a hex dump, and counts the
rest in `bad_packets`:

- **Wrong sender**: something other than FH6 is transmitting to the port —
  a different Forza title whose packet is another size (only FH4/FH5's "Dash"
  format matches FH6's 324 bytes), or another telemetry tool's forwarder.
- **A game update changed the layout**: if this starts right after an FH6 title
  update and the size is new, the packet probably grew. Check for a LapScope
  update, and if there is none yet, please
  [open a bug report](https://github.com/darcane/LapScope/issues/new?template=bug_report.yml)
  with the logged size — that warning line is exactly what's needed to adapt
  the parser. Details of the current layout:
  [FH6 Data Out Packet](FH6-Data-Out-Packet).

## A session or lap is missing / timed wrong

That's not a connectivity problem — it's the event-detection inference not
recognizing something. See
[Capturing an Unrecognized Event](Capturing-an-Unrecognized-Event) for the
capture workflow, and [Event Detection](Event-Detection) for how the inference
works.

## A trailing lap with no time, after a crash or power cut

Normal. If LapScope is killed mid-lap (`docker kill`, Task Manager, the machine
losing power), the lap that was in progress is left open — it never crossed a
finish line, so it has no time. The next startup closes it at the last frame
that was recorded, and the session keeps every lap it did finish. Excluding it
with 🗑 in the lap table is safe.

**Closing the LapScope window is not one of these cases.** It stops the server
properly — the window stays up saying *Stopping — saving the session in
progress…* until the recorder has finalised the session — so the normal way of
quitting doesn't leave a trailing lap behind.

## "Not recording — the database write failed"

A red bar across the top of the Live page, and `write_error` in
`/api/status`. Telemetry is still arriving and the gauges still move, but
nothing is being stored — almost always a **full disk**, occasionally a
database file that has been made read-only or locked by another program.

LapScope keeps buffering while this lasts (about a minute of telemetry,
oldest frames dropped first — `frames_dropped` counts what was lost) and
retries every second, so freeing space is enough: the bar disappears on its
own and recording carries on, no restart needed. The failure is logged once
when it starts and once when it recovers, not per packet.

Where the space went: recordings are raw packets, roughly **70 MB per hour of
driving**. Delete sessions you don't need from the Analysis page, then
**Settings → Storage → Compact now** — deleting alone frees space inside the
database file without giving it back to the drive.

## "400 Invalid host header"

LapScope answers to `localhost`, any `.local` name, and any IP address. It
refuses other hostnames, which is what stops a web page you visit from
pointing its own domain at your machine and using the API from there.

You'll only hit this if you reach LapScope by some other name — through a
reverse proxy, say. List the names you use in the `LS_ALLOWED_HOSTS` env var
(comma-separated) and restart.

## The Analysis page says "server not answering"

The chip next to the ⚙ button on the Analysis page is red, or a dialog says
"Can't reach LapScope". The page is fine; the server isn't answering. Usually
it was closed, restarted, or the container stopped.

Nothing you clicked was saved — renames, tags, exclusions and deletes all
report the failure rather than pretending to work. Start LapScope again and
the page picks up on its own within 15 seconds; the chip goes green and the
session list refills. If it stays red with LapScope running, check that
nothing else grabbed its port (see **Busy ports** above).

## The dashboard says "paused"

An amber `paused` chip on the Live page means the socket is connected but no
telemetry has arrived for a few seconds. That is normal and expected:
**Forza Horizon 6 stops sending Data Out whenever the game loses focus**, so
alt-tabbing to read the dashboard on a second screen pauses the stream. Every
gauge holds its last value and the page stays usable; click back into the game
and it goes green again within a frame.

A red `reconnecting…` chip is different — that is the WebSocket itself
dropping, which means the server went away. Retries back off from 1.5 s to
15 s and continue indefinitely.

## Still stuck?

[Open a bug report](https://github.com/darcane/LapScope/issues/new?template=bug_report.yml)
with your `/api/status` output and the relevant log lines.
