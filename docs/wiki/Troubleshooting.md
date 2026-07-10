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

**Where the logs are:** the console window for the Windows exe,
`docker compose logs -f` for Docker. Every recorder decision (session opened,
lap completed, session discarded + why) is logged there.

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
says so, in the console and in `/api/status` → `udp_error`.

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
  sessions still work) but shows the problem in the console and in
  `/api/status` → `udp_error`. Close the other program (often a second LapScope
  window, or another telemetry tool), or set `TELEMETRY_UDP_PORT` to a free
  port, then restart — and remember to change the port in the game too.
- **HTTP 8000 already in use** (exe) — the exe checks before starting and
  explains instead of crash-closing the console. Usually it's an
  already-running LapScope: open http://127.0.0.1:8000 — if the dashboard
  loads, use that window. To find another culprit:
  `Get-NetTCPConnection -LocalPort 8000 | Format-Table OwningProcess`.

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

## Still stuck?

[Open a bug report](https://github.com/darcane/LapScope/issues/new?template=bug_report.yml)
with your `/api/status` output and the relevant log lines.
