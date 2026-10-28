# Regenerating the project media

Every LapScope-UI image in the README and wiki (`docs/media/`) is produced by
[tools/capture_media.py](../tools/capture_media.py) from **your real
`telemetry.db`**, so the screenshots always show the current UI. Maintainer-only;
Windows (the launcher shot grabs a real desktop window).

## Setup

```
pip install -r requirements-media.txt
playwright install chromium
```

Committing the regenerated media needs **Git LFS** (`*.gif` is tracked by LFS, see
`.gitattributes`).

## Fill the manifest

[tools/media_shots.json](../tools/media_shots.json) has one entry per output. The
committed ids point at sessions in the maintainer's own database; on any other
database they are refused as missing, so swap in your own. Set an id to
`"REPLACE_ME"` to park a shot: it is *refused* and the others run.

- `session`: a session id; `laps`: lap ids **of that session** (`analysis-compare`
  takes two, the map shots one). Find both with
  `python tools/inspect_session.py --list --db <path>` (laps are rows in `laps.id`;
  the analysis page's lap table order is the same).
- `replay`: `start_s` / `end_s`, offsets into the session's frames, for the live
  shots (`hero.gif`, `accent-live-sunset.png`, `paused-chip.png`). Pick a stretch
  with the car moving. Frames are replayed in stored real time, so a window that
  covers one finished lap and part of the next (for a live delta and a drawn live
  map) costs a couple of minutes. `capture_after_s` (hero) and `shoot_after_s`
  (sunset) say how far into the replay to start filming. Replay shots run after
  the analysis shots, because each replay records a session of its own into the
  scratch copy and would otherwise top the session list.
- `settings`: a partial `ls_settings` (accent, units, `defaultMapMode`...) injected
  before the page loads.
- `element`: a CSS selector to frame instead of the viewport (one panel, the
  sidebar, an open dialog), with optional `pad` and `max_height` in px; it is shot
  from the full page, so a panel below the fold is fine.
- `viewport`, optional `crop` (`x`, `y`, `width`, `height`; wins over `element`),
  and per-shot tunables:
  `stale_wait_s` (paused chip), `rotation_steps` (3D GIF), `capture_fps`, `fps`,
  `scale`, `scale_factor` (device pixel ratio: 0.75 lays out at the full viewport
  but renders smaller, which keeps a whole-dashboard GIF under budget),
  `settle_ms`.
- `launcher-window.png` needs no ids.

## Run

```
python tools/capture_media.py                       # all shots, default DB, docs/media/
python tools/capture_media.py --db D:\telemetry.db --only hero.gif paused-chip.png
python tools/capture_media.py --out C:\temp\media   # write elsewhere
python tools/capture_media.py --list
```

The default DB is the exe's `%LOCALAPPDATA%\LapScope\telemetry.db`. It is opened
read-only and copied to a temp dir; the capture server (own port, `LS_OFFLINE=1`)
and a loopback-only browser run against the copy, so your data is never written and
nothing leaves the machine. Each file is replaced only after its shot succeeded; the
end-of-run report lists ok / refused / failed per shot and the exit code is non-zero
if any was not ok.

**Launcher shot**: it starts `run_desktop.py` and grabs its window, so port **8000
must be free** (close the exe/docker), the window must be **unobscured** while it
runs, and it only works on Windows.

## When the UI drifts

Every selector the tool relies on is in the constants block at the top of
`tools/capture_media.py` (`SEL_*`, `PAGE_*`, `LAUNCHER_*`). Update those, not the
recipes.

## Before committing

- `hero.gif` must be <= 10 MB. The tool lowers fps, then scale, then colors until it
  fits, and reports `failed (over budget, N MB)` (writing nothing) if nothing does.
- **Look at every image for personal text** — car names, route names, custom session
  names shown in lists, headers and the map caption. Rename in the app (or pick other
  sessions) rather than publishing them.
- `git add docs/media`, commit. `fh6-settings.png` is a photo of the game's menu and
  is not generated.
