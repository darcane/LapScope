# LapScope Wiki

LapScope is a self-hosted telemetry dashboard and lap analyzer for **Forza
Horizon 6**. The [README](https://github.com/darcane/LapScope#readme) covers
what most people need — installing, the in-game Data Out settings, a one-line
feature list, and quick troubleshooting. **This wiki is the deep end:** the material
that would bloat the README but matters when something misbehaves or you want to
know how the inference actually works.

## Using LapScope

- **[Feature Tour](Feature-Tour)** — every screen with what each part does: the
  live dashboard, lap comparison, 2D/3D maps, jumps, dirty-lap flags and edits,
  routes, CSV export/import, settings, the Windows launcher.
- **[Configuration](Configuration)** — environment variables, offline mode,
  host names behind a reverse proxy, Docker network exposure, storage and
  compacting, where the database and logs live.
- **[Troubleshooting](Troubleshooting)** — the full no-packets diagnosis flow:
  UWP/Microsoft-Store loopback isolation, the LAN-IP fallback, the silent
  Docker IPv6-proxy port bug, loopback exemptions, busy ports, and what
  `/api/status` tells you.
- **[Capturing an Unrecognized Event](Capturing-an-Unrecognized-Event)** — what
  to do when a drive doesn't show up or is timed wrong: keep the discarded
  session with `LS_KEEP_DISCARDED=1`, dump its signals with
  `tools/inspect_session.py`, and file a capture issue so support can be added.
  **This is the most valuable way to contribute.**

## Internals

- **[FH6 Data Out Packet](FH6-Data-Out-Packet)** — the 324-byte packet: layout,
  what's *not* in it, and the field quirks that shaped the app
  (`DistanceTraveled` isn't meters, `Velocity` is car-local, `IsRaceOn` is 1 in
  free roam, …).
- **[Event Detection](Event-Detection)** — how LapScope infers sessions, laps,
  finishes, race mode, dirty-lap flags, and routes from a stream that never
  announces any of them. Includes every finish signal and the real captures
  that proved them.

## In the repo

- [README](https://github.com/darcane/LapScope#readme) — install, in-game
  setup, features at a glance.
- [CONTRIBUTING.md](https://github.com/darcane/LapScope/blob/main/CONTRIBUTING.md)
  — workflow, branch rules, how to get changes in.
- [ARCHITECTURE.md](https://github.com/darcane/LapScope/blob/main/ARCHITECTURE.md)
  — code map: files, data flow, DB schema, API surface.
- [AGENTS.md](https://github.com/darcane/LapScope/blob/main/AGENTS.md) — the
  dev-facing knowledge base the recorder is built on.
- [docs/BUILDING.md](https://github.com/darcane/LapScope/blob/main/docs/BUILDING.md)
  — building the Windows exe from source and verifying a release download.
