# Plan — #89 Refresh the project's media

The plan is a single step: every file the work touches (tool, manifest, tests, requirements, README, docs, deleted media) is part of one coherent change, and the hard format rule caps the plan at one step.

## Step 1 — Media capture tool, manifest, docs and cleanup

**Goal**: Ship a committed, re-runnable Windows maintainer tool (`tools/capture_media.py` + manifest) that regenerates every LapScope-UI image in the docs from a real `telemetry.db`, plus the surrounding repo/doc changes (FR1-FR12, NFR1-NFR3, AC1-AC6).

**Requirements**

Pure-logic layer (stdlib only, importable without Playwright/Pillow; Playwright and Pillow are imported lazily inside the functions that need them):
- Manifest: `tools/media_shots.json` (JSON, stdlib-parsable). One entry per shot: `output`, `kind` (`still|clip|window`), `session`, `laps` (list, comparison takes two), `viewport`, optional `crop`, `settings` (partial `ls_settings`, e.g. `{"accent":"sunset"}`, units, map mode), optional `replay` (`start_s`, `end_s` offsets into the session frames), plus per-shot tunables (stale wait, rotation steps, etc.). A top-level `_comment` documents the placeholder sentinel; every id field ships as the sentinel string `"REPLACE_ME"`.
- Shots (names fixed here, used identically in manifest and README): `hero.gif`, `analysis-compare.png`, `track-map.png`, `track-map-3d-jumps.png`, `track-map-3d-animated.gif`, `session-list.png`, `session-sidebar.png`, `settings.png`, `accent-live-sunset.png`, and the four new ones `paused-chip.png`, `browse-bar-menu.png`, `csv-export-choice.png`, `launcher-window.png`. `fh6-settings.png` is not in the manifest.
- `load_manifest(path)` validates shape (known kinds, required fields, unique outputs, output basename only, no path separators) and raises a manifest error naming the shot.
- `validate_shots(shots, db_ids)` returns per-shot `ok | refused(reason)`: placeholder id present -> refused naming the shot; id absent from the DB (session or lap) -> refused naming the shot and the missing id. Other shots proceed.
- `resolve_db_path(arg)` -> explicit `--db`, else the exe's data dir `%LOCALAPPDATA%\LapScope\telemetry.db` (reuse `desktop.paths.default_data_dir()` — `paths.py` is tkinter-free — or mirror its logic: `LOCALAPPDATA` or `~`, then `LapScope`); if missing, the whole run aborts with a message naming the path (checked before any server/browser starts).
- Replay helpers: `read_replay(conn, session_id, start_s, end_s)` yields `(t, raw)` ordered by `t` with only 324-byte blobs; `pace(frames)` yields sleep deltas from stored `t` differences (clamped to >= 0 and capped for stream gaps).
- Budget helpers: `pick_gif_ladder(...)` generates the degradation order (lower fps, then scale, then colors) and `fits_budget(size, limit=10*1024*1024)`.
- CLI: `--db`, `--out` (default `docs/media/`, overridable so the E2E run writes to a scratch dir), `--manifest`, `--only NAME [NAME...]` (shot filter by output name), `--list`.

I/O layer:
- Source DB opened read-only (`file:...?mode=ro` URI) and copied once into a temp dir with `sqlite3.Connection.backup` as `telemetry.db`; the source is never written (byte-identical afterwards).
- Capture server: separate `uvicorn app.main:app` subprocess (`sys.executable -m uvicorn`), env `DATA_DIR=<temp>`, `LS_OFFLINE=1`, `TELEMETRY_UDP_PORT=<free>`, HTTP bound to 127.0.0.1 on a free port; ports picked from a range excluding 5200-5300, 8000 and 9999; waits for `/api/status` (or equivalent) readiness; terminated and scratch dir removed in a `finally` block, including on failure.
- Playwright Chromium contexts block every non-loopback request via `context.route`; per-shot `ls_settings` is injected with `add_init_script` before page load; viewport and optional clip come from the manifest; stills wait for a render-ready condition (canvas / uPlot present, network idle) before `screenshot`.
- Stills (analysis page): `analysis-compare`, `track-map`, `track-map-3d-jumps`, `session-list`, `session-sidebar`, `settings` (opened via `#settings-btn`), `browse-bar-menu` (header ⋯ menu opened via `#header-menu`, cropped to header + browse bar), `csv-export-choice` (the #90 export dialog opened by clicking through the UI). Selectors live in one named constants block at the top of the file (referenced from MEDIA.md).
- Live shots: replay `frames.raw` from the scratch DB to the capture server's UDP port with `pace`; `accent-live-sunset` screenshots the dashboard mid-replay; `paused-chip` stops sending, waits past the dashboard's 2.5 s stale threshold (manifest-tunable) until the amber PAUSED chip is visible, then screenshots. Replay-created sessions live only in the scratch DB; analysis shots use the original ids.
- Clips: `hero.gif` = successive screenshots while the replay window runs; `track-map-3d-animated.gif` = analysis 3D map with rotation stepped programmatically; encoded with Pillow, trying the degradation ladder until <= 10 MB; if no rung fits the shot is reported `failed (over budget, N MB)` and nothing is written.
- Window shot: `launcher-window.png` starts `run_desktop.py` as a subprocess with scratch `DATA_DIR` and `TELEMETRY_UDP_PORT`, never imports `desktop/ui.py`; if port 8000 is already bound the shot is refused naming that cause; otherwise makes the process DPI-aware, finds the top-level window by title, raises it, and grabs its rectangle with `PIL.ImageGrab`; the subprocess is always terminated.
- Atomic writes: every output is written to a temp file beside its target and `os.replace`d only after capture and size checks pass; a failure leaves the existing media untouched and the run continues.
- Per-shot report (ok / refused / failed, size) printed at the end; exit code non-zero if any shot failed or was refused. Missing Playwright/Pillow gives a clear "pip install -r requirements-media.txt && playwright install chromium" message only when a shot needing them is selected.
- Tool uses `logging`/`print` freely (it is not on the exe import path); nothing under `app/` or `desktop/` imports it.

Dependencies and files:
- New `requirements-media.txt` with lower-bounded `playwright` and `Pillow`; `requirements.txt` / `requirements-dev.txt` unchanged.
- Delete the 7 unreferenced files in `docs/media/`: `accent-analysis-violet.png`, `hero.png`, `live-map-jumps.png`, `live-map-pause-kept.png`, `map-zoom-highlight.png`, `track-map-3d.png`, `track-map-jumps.png`. No recaptured media is committed; `fh6-settings.png` and the 9 referenced files stay. Afterwards grep confirms nothing in README, `docs/wiki/` or `docs/` references a removed file; `track-map-3d-jumps.png` keeps its name.
- `.gitattributes` unchanged (`*.gif` stays on LFS).
- `README.md`: four new image references with alt text and the fixed names: `docs/media/launcher-window.png` in the Windows quick start, `docs/media/paused-chip.png` in "Works when things go wrong", `docs/media/browse-bar-menu.png` and `docs/media/csv-export-choice.png` in the Analysis showcase.
- New `docs/MEDIA.md`: setup (`pip install -r requirements-media.txt`, `playwright install chromium`, Git LFS needed where media is committed), filling the manifest (placeholder sentinel, how to find session/lap ids with `tools/inspect_session.py --list`, replay windows, per-shot settings), running (`python tools/capture_media.py [--db ...] [--only ...] [--out ...]`), the launcher-shot constraints (port 8000 must be free, window unobscured, Windows only), the selector constants to update when the UI drifts, checking sizes (hero <= 10 MB), inspecting outputs for personal text (car/route/custom names) before committing, and committing.
- `AGENTS.md` documentation map: one pointer to `docs/MEDIA.md`. `ARCHITECTURE.md` Tools section: new entry for `tools/capture_media.py`, noting it is the exception to "stdlib only" (media deps from `requirements-media.txt`, maintainer-only, not imported by app/CI).

**Tests**

Unit (pytest, `tests/test_capture_media.py`, headless, no Playwright/Pillow import; the tool is loaded via `importlib` from `tools/` the way other tool tests do, or via a `sys.path` insert):
- `test_shipped_manifest_parses`: committed manifest loads, every shot-list output is present (and `fh6-settings.png` is not), outputs unique, all four new names present, and every id field is the placeholder.
- `test_manifest_rejects_bad_shape`: unknown kind, missing `output`, path-separator in `output`, duplicate outputs each raise a manifest error naming the shot.
- `test_placeholder_shot_refused_others_proceed`: with a fixture DB, a placeholder shot is refused (named) while a valid shot is ok.
- `test_missing_session_and_lap_id_refused`: refusal names the shot and the missing id.
- `test_no_db_aborts_naming_path`: `resolve_db_path` / run entry raises/returns abort naming the default path, before any server start (server starter monkeypatched to fail if called).
- `test_replay_reads_in_t_order_and_window`: fixture DB with synthetic 324-byte frames; `read_replay` honours start/end offsets and ordering; non-324-byte blobs skipped.
- `test_pace_uses_stored_deltas`: deltas match `t` differences, gaps capped, negatives clamped.
- `test_gif_ladder_order_and_budget`: ladder order is fps, then scale, then colors; `fits_budget` boundary at 10 MB.
- `test_free_port_excludes_reserved`: chosen ports are never in 5200-5300, 8000 or 9999 and are distinct.
- `test_atomic_write_keeps_existing_on_failure`: a failing capture leaves the existing target bytes unchanged and removes the temp file; success replaces it.
- `test_source_db_untouched_by_snapshot`: after `snapshot_db`, the source file's bytes/hash are unchanged and the copy has the same row counts (WAL-mode source).
- `test_no_app_imports_tool`: grep-style test that nothing under `app/` or `desktop/` references `capture_media`; `requirements.txt` and `requirements-dev.txt` contain no playwright/pillow.
- `test_docs_references_resolve`: every `docs/media/...` path referenced in README.md and `docs/wiki/*.md` that exists on disk is not one of the deleted seven, and none of the deleted seven are referenced anywhere.

E2E proof (A1, executed by implement; not committed, outputs to a scratch dir): record simulator sessions into a throwaway `DATA_DIR` (`--race 3`, `--dirt 60 --jumps`, `--duration 180 --dirty`), install `requirements-media.txt` + `playwright install chromium` into `.venv`, fill a throwaway manifest with those ids, run `python tools/capture_media.py --db <throwaway> --manifest <throwaway> --out <scratch>`; confirm all selected shots are produced (launcher shot reported ok or refused with its cause), the hero GIF is <= 10 MB, the source DB hash is unchanged, no stray scratch dir or server process remains, and `docs/media/` is untouched. Report the shot list and hero size. Delete any `./data` left over.

**Touches**: `tools/capture_media.py` (new), `tools/media_shots.json` (new), `requirements-media.txt` (new), `tests/test_capture_media.py` (new), `docs/MEDIA.md` (new), `docs/media/` (7 deletions), `README.md`, `AGENTS.md`, `ARCHITECTURE.md`.
