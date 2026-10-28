# Requirements — #89 Refresh the project's media

**Status:** locked 2026-10-26 · Issue: [#89](https://github.com/darcane/LapScope/issues/89)

## Problem

The README/wiki images no longer match the app. The hero GIF, `track-map.png` and
`fh6-settings.png` date from the first public README (#3, 2026-07-06); the rest from
#47 (2026-07-23). Since then the Analysis header, the browse bar, car/route chips,
the paused chip, CSV export and the desktop launcher window (#91) have changed or
appeared — several with no image at all. The hero GIF is 60 MB (Git LFS). Every
past refresh was done by hand, so there is no repeatable way to do it again.

## Goals

1. A committed, re-runnable capture tool that regenerates every LapScope-UI image in
   the docs from a real recorded `telemetry.db` — the next refresh is one command.
2. A shot list that covers the current UI, including features with no image today.
3. A hero clip small enough to stop bloating the repo (≤ 10 MB).
4. `docs/media/` holds exactly what the docs show.

## Non-goals

- Committing the recaptured media in this PR — the owner runs the tool on the gaming
  PC (where the real DB lives) and commits the media afterwards.
- Recapturing `fh6-settings.png` (in-game menu, unchanged; it stays as-is).
- Running the tool in CI or on Linux/macOS — a local, Windows maintainer tool.
- Any change to app runtime behavior, runtime dependencies, CI dependencies or the exe.
- Simulator-generated imagery in the docs — published media shows real sessions.

## Functional requirements

### Capture tool

- FR1. Lives under `tools/`, run by hand from a source checkout on Windows.
- FR2. Reads a real `telemetry.db`; default `%LOCALAPPDATA%\LapScope\telemetry.db`
  (the exe's data dir), overridable by a path argument.
- FR3. Never writes to the source DB or touches a running LapScope: it works on a
  temporary copy, runs its own server against a temporary data dir on ports that do
  not clash with a default install (never 5200–5300), and runs that server with
  `LS_OFFLINE` so no outbound call (update banner etc.) happens or shows in a shot.
- FR4. Live-dashboard shots are produced by replaying a stored session's raw frames
  (`frames.raw`, the original 324-byte packets) over UDP to that server with their
  original timing.
- FR5. Browser shots use Playwright for Python (headless Chromium); the hero and the
  animated 3D map are encoded as GIFs with Pillow.
- FR6. The desktop launcher window (#91) is captured as a real window grab.
- FR7. Media-only dependencies (Playwright, Pillow) go in a new
  `requirements-media.txt`; `requirements.txt` and `requirements-dev.txt` are unchanged.

### Shot manifest

- FR8. A committed manifest the owner edits lists every shot: output file, the
  session/lap ids it shows, viewport/crop, per-shot page settings (e.g. the Sunset
  accent, units), and for clips the replay time window.
- FR9. Ships with placeholder ids (the owner fills them from their DB); the tool
  refuses to capture a shot whose ids are still placeholders, naming the shot.

### Shot list

| File | Kind | Status |
|---|---|---|
| `hero.gif` | ~10–15 s of a real circuit race segment on the live dashboard (gauges, friction circle, delta, map drawing), **≤ 10 MB** | recapture |
| `analysis-compare.png` | lap comparison | recapture |
| `track-map.png` | 2D map colored by speed | recapture |
| `track-map-3d-animated.gif` | 3D map, rotating | recapture |
| `track-map-3d-jumps.png` | jump glyphs (README **and** wiki Event-Detection) | recapture |
| `session-list.png` | lap list with dirty-lap flags | recapture |
| `session-sidebar.png` | session list ribbons | recapture |
| `settings.png` | settings panel | recapture |
| `accent-live-sunset.png` | live dashboard in Sunset | recapture |
| launcher window | desktop exe launcher (#91) | **new** |
| Analysis browse bar + ⋯ menu | search/facets/sort + header overflow menu | **new** |
| paused chip | live dashboard's amber paused chip | **new** |
| CSV export choice | curated-vs-everything export dialog (#90) | **new** |
| `fh6-settings.png` | in-game Data Out menu | **kept, not captured** |

### Repository/doc changes in this PR

- FR10. Delete the 7 unreferenced files: `accent-analysis-violet.png`, `hero.png`,
  `live-map-jumps.png`, `live-map-pause-kept.png`, `map-zoom-highlight.png`,
  `track-map-3d.png`, `track-map-jumps.png`.
- FR11. Wire the 4 new shots into the README now, next to their feature text:
  launcher → Windows quick start; paused chip → "Works when things go wrong";
  browse bar/⋯ menu and CSV export → the Analysis showcase. Their images are broken
  until the owner runs the tool and commits the media (accepted by the owner).
- FR12. New `docs/MEDIA.md` (maintainer-facing): setup (`requirements-media.txt`,
  `playwright install chromium`), filling the manifest, running the tool,
  checking sizes, committing the media.

## Non-functional requirements

- NFR1. Hero GIF ≤ 10 MB; GIFs remain under Git LFS (`*.gif` rule in `.gitattributes`).
- NFR2. The tool passes `ruff check .` like the rest of the repo.
- NFR3. Nothing on the exe's import path or the app's runtime imports the tool.

## Edge cases / failure handling

- No DB at the default path and no override → clear error naming the path looked for.
- A manifest id not in the DB → error naming the shot and the missing id.
- A placeholder id still in the manifest → that shot is refused (FR9).
- A shot fails → its existing file in `docs/media/` is left untouched; a file is only
  overwritten when that shot captured successfully.
- Hero GIF would exceed 10 MB → reported, not silently written over budget.

## Approved assumptions

- A1. Proof in this PR is an end-to-end run against a throwaway, simulator-recorded
  DB (outputs inspected, not committed) plus unit tests for the non-browser logic.
- A2. The temporary-copy / isolated-server / `LS_OFFLINE` behavior in FR3.
- A3. The failure handling above.

## Acceptance criteria

1. With `requirements-media.txt` installed and a filled manifest, one command
   regenerates every shot in the table (except `fh6-settings.png`) into `docs/media/`.
2. The source DB is byte-identical after a run; a running LapScope is unaffected.
3. An end-to-end run against a simulator-recorded DB produces every shot, and the
   hero GIF is ≤ 10 MB.
4. The 7 unreferenced files are gone; no doc references a deleted file.
5. README references the 4 new shots at the stated places; `docs/MEDIA.md` exists
   and describes the full refresh workflow.
6. `pytest -q` and `ruff check .` pass; no new runtime/CI dependency.

## Codebase findings

- Frames are stored as raw packets: `frames(session_id, t, raw BLOB)` in
  `app/recorder/store.py` — enables byte-exact replay.
- `tools/simulator.py` sends UDP to `127.0.0.1:9999` by default (`--host`/`--port`).
- `desktop/paths.py`: exe data dir is `%LOCALAPPDATA%\LapScope`.
- `.gitattributes`: `*.gif` tracked via Git LFS; `*.png` binary.
- Image references: README lines ~17, 101–131, 187; wiki
  `docs/wiki/Event-Detection.md:171` (absolute raw URL to `track-map-3d-jumps.png`).
- Outbound calls are gated on `onlineAllowed()` / `LS_OFFLINE` (common.js).
