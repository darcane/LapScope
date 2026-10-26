"""Regenerate the LapScope screenshots/GIFs in docs/media/ from a real telemetry.db.

Maintainer tool, Windows-first (the launcher-window shot grabs a real desktop
window). It is the one exception to "tools/ is stdlib only": the browser and the
GIF encoder come from requirements-media.txt. Nothing under app/ or desktop/
imports this file, CI never installs those packages, and Playwright / Pillow are
imported lazily inside the functions that need them so the pure-logic half
(manifest, validation, replay pacing, GIF budget) is testable on a bare runner.

Usage (from the repo root, see docs/MEDIA.md):
    python tools/capture_media.py [--db PATH] [--out DIR] [--manifest FILE]
                                  [--only NAME [NAME ...]] [--list]

How it stays safe:
  * the source database is opened read-only and copied once into a temp dir; the
    capture server runs against the copy, so your real data is never written;
  * the server runs with LS_OFFLINE=1 and the browser refuses every
    non-loopback request, so the shots cannot reach out anywhere;
  * every output is written beside its target and os.replace()d only after the
    capture and the size check passed - a failed shot leaves the old file alone.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import logging
import os
import random
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from desktop.paths import default_data_dir  # noqa: E402  (tkinter-free by contract)

log = logging.getLogger("capture_media")

# --- selectors: the one place to touch when the UI drifts (docs/MEDIA.md) -----
SEL_SESSION_CARD = "#session-list .session-card"
SEL_LAP_ROWS = "#lap-rows tr"
SEL_LAP_CSV_BUTTON = "#lap-rows .act-csv"
SEL_SETTINGS_BUTTON = "#settings-btn"
SEL_HEADER_MENU_TRIGGER = "#header-menu .menu-trigger"
SEL_OPEN_MENU = "#header-menu .menu:not([hidden])"
SEL_BROWSE_BAR = ".browse-bar"
SEL_MODAL_OPEN = "dialog.modal[open]"
SEL_EXPORT_CHOICE = "dialog.modal[open] .export-choice"
SEL_CHARTS_READY = "#charts .uplot"
SEL_TRACKMAP = "#trackmap"
SEL_CONN_CHIP = "#conn"
SEL_DASH_READY = "#rpm"
PAGE_ANALYSIS = "/analysis.html"
PAGE_LIVE = "/index.html"
LAUNCHER_TITLE_PREFIX = "LapScope"   # desktop/ui.py: "LapScope" / "LapScope — <state>"
LAUNCHER_WINDOW_CLASS = "TkTopLevel"

# --- constants ---------------------------------------------------------------
SENTINEL = "REPLACE_ME"
KINDS = ("still", "clip", "window")
PACKET_SIZE = 324
GIF_LIMIT = 10 * 1024 * 1024
GAP_CAP_S = 0.25                      # a stream gap must not stall the replay
RESERVED_PORTS = frozenset({8000, 9999, *range(5200, 5301)})
PORT_RANGE = (30000, 39999)
INSTALL_HINT = ("pip install -r requirements-media.txt && playwright install chromium")
DEFAULT_MANIFEST = ROOT / "tools" / "media_shots.json"
DEFAULT_OUT = ROOT / "docs" / "media"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}


class ManifestError(ValueError):
    """The manifest is malformed; the message names the offending shot."""


class ShotRefused(Exception):
    """A shot that cannot run (not a failure of the tool)."""


class AbortRun(Exception):
    """Nothing can run at all (e.g. the database does not exist)."""


# --- manifest ----------------------------------------------------------------
def _shot_label(raw: object, index: int) -> str:
    if isinstance(raw, dict) and isinstance(raw.get("output"), str) and raw["output"]:
        return raw["output"]
    return f"#{index + 1}"


def load_manifest(path: str | os.PathLike) -> list[dict]:
    """Parse and shape-check the manifest; returns the shot list."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ManifestError(f"cannot read manifest {path}: {e}") from e
    shots = data.get("shots") if isinstance(data, dict) else None
    if not isinstance(shots, list) or not shots:
        raise ManifestError("manifest needs a non-empty top-level 'shots' list")
    seen: set[str] = set()
    for i, shot in enumerate(shots):
        label = _shot_label(shot, i)
        if not isinstance(shot, dict):
            raise ManifestError(f"shot {label}: must be an object")
        out = shot.get("output")
        if not isinstance(out, str) or not out:
            raise ManifestError(f"shot {label}: missing 'output'")
        if "/" in out or "\\" in out or out != Path(out).name or out in (".", ".."):
            raise ManifestError(f"shot {out}: 'output' must be a bare file name, no path separators")
        if out in seen:
            raise ManifestError(f"shot {out}: duplicate output name")
        seen.add(out)
        kind = shot.get("kind")
        if kind not in KINDS:
            raise ManifestError(f"shot {out}: unknown kind {kind!r} (expected one of {', '.join(KINDS)})")
        if kind != "window":
            for key in ("session", "laps", "viewport"):
                if key not in shot:
                    raise ManifestError(f"shot {out}: missing '{key}'")
            if not isinstance(shot["laps"], list):
                raise ManifestError(f"shot {out}: 'laps' must be a list")
            vp = shot["viewport"]
            if not (isinstance(vp, dict) and {"width", "height"} <= vp.keys()):
                raise ManifestError(f"shot {out}: 'viewport' needs width and height")
        if not isinstance(shot.get("settings", {}), dict):
            raise ManifestError(f"shot {out}: 'settings' must be an object")
        replay = shot.get("replay")
        if replay is not None and not (isinstance(replay, dict) and "start_s" in replay):
            raise ManifestError(f"shot {out}: 'replay' needs start_s (and optionally end_s)")
    return shots


def shot_ids(shot: dict) -> list:
    """Every id field of a shot, in one list (session first, then laps)."""
    ids = []
    if "session" in shot:
        ids.append(shot["session"])
    ids.extend(shot.get("laps") or [])
    return ids


@dataclass(frozen=True)
class Verdict:
    status: str            # "ok" | "refused"
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def validate_shots(shots: Iterable[dict], db_ids: dict) -> dict[str, Verdict]:
    """Per-shot verdicts. db_ids = {"sessions": set[int], "laps": set[int]}.
    A refused shot never stops the others."""
    sessions, laps = db_ids["sessions"], db_ids["laps"]
    out: dict[str, Verdict] = {}
    for shot in shots:
        name = shot["output"]
        verdict = Verdict("ok")
        if SENTINEL in shot_ids(shot):
            verdict = Verdict("refused", f"{name}: still has the {SENTINEL} placeholder - fill in real ids")
        elif "session" in shot and shot["session"] not in sessions:
            verdict = Verdict("refused", f"{name}: session {shot['session']} is not in the database")
        else:
            for lap in shot.get("laps") or []:
                if lap not in laps:
                    verdict = Verdict("refused", f"{name}: lap {lap} is not in the database")
                    break
        out[name] = verdict
    return out


def collect_db_ids(conn: sqlite3.Connection) -> dict:
    return {
        "sessions": {r[0] for r in conn.execute("SELECT id FROM sessions")},
        "laps": {r[0] for r in conn.execute("SELECT id FROM laps")},
    }


# --- database ----------------------------------------------------------------
def resolve_db_path(arg: str | None) -> Path:
    """--db, else the exe's data dir. Aborts (naming the path) when it is missing."""
    path = Path(arg) if arg else Path(default_data_dir()) / "telemetry.db"
    if not path.is_file():
        raise AbortRun(f"telemetry database not found at {path} - pass --db to point at one")
    return path


def snapshot_db(src: str | os.PathLike, dest_dir: str | os.PathLike) -> Path:
    """Copy the source into dest_dir/telemetry.db through SQLite's backup API.
    The source is opened read-only; it is never written."""
    dest = Path(dest_dir) / "telemetry.db"
    uri = Path(src).resolve().as_uri() + "?mode=ro"
    source = sqlite3.connect(uri, uri=True)
    try:
        target = sqlite3.connect(dest)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    return dest


# --- replay ------------------------------------------------------------------
def read_replay(conn: sqlite3.Connection, session_id: int,
                start_s: float = 0.0, end_s: float | None = None) -> Iterator[tuple[float, bytes]]:
    """(t, raw) for one session ordered by t; start_s/end_s are offsets from the
    session's first frame. Anything that is not a full 324-byte packet is skipped."""
    row = conn.execute("SELECT MIN(t) FROM frames WHERE session_id = ?", (session_id,)).fetchone()
    if row is None or row[0] is None:
        return
    t0 = row[0]
    lo = t0 + float(start_s or 0)
    hi = t0 + float(end_s) if end_s is not None else float("inf")
    cur = conn.execute(
        "SELECT t, raw FROM frames WHERE session_id = ? AND t >= ? ORDER BY t", (session_id, lo))
    for t, raw in cur:
        if t > hi:
            break
        if isinstance(raw, (bytes, bytearray, memoryview)) and len(raw) == PACKET_SIZE:
            yield t, bytes(raw)


def pace(frames: Iterable[tuple[float, bytes]], gap_cap: float = GAP_CAP_S) -> Iterator[float]:
    """How long to sleep *before* sending each frame: the stored t difference,
    never negative, never longer than gap_cap (a results-cinematic gap would
    otherwise freeze the replay). The first frame goes out immediately."""
    prev = None
    for t, _raw in frames:
        yield 0.0 if prev is None else min(max(t - prev, 0.0), gap_cap)
        prev = t


def replay_to_udp(frames: Iterable[tuple[float, bytes]], port: int,
                  stop: threading.Event | None = None) -> None:
    """Send frames to the capture server in (stored) real time."""
    items = list(frames)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for delay, (_t, raw) in zip(pace(items), items):
            if stop is not None and stop.is_set():
                return
            if delay:
                time.sleep(delay)
            sock.sendto(raw, ("127.0.0.1", port))
    finally:
        sock.close()


# --- GIF budget --------------------------------------------------------------
def pick_gif_ladder(fps: int = 12, scale: float = 1.0, colors: int = 256,
                    min_fps: int = 6, min_scale: float = 0.5, min_colors: int = 64) -> list[dict]:
    """Rungs to try in order: lower the frame rate first, then shrink, then
    cut the palette. The first rung is the requested quality."""
    rungs = [{"fps": fps, "scale": scale, "colors": colors}]
    f = fps
    while f > min_fps:
        f = max(min_fps, f - 3)
        rungs.append({"fps": f, "scale": scale, "colors": colors})
    s = scale
    while s > min_scale + 1e-9:
        s = max(min_scale, round(s - 0.15, 2))
        rungs.append({"fps": f, "scale": s, "colors": colors})
    c = colors
    while c > min_colors:
        c = max(min_colors, c // 2)
        rungs.append({"fps": f, "scale": s, "colors": c})
    return rungs


def fits_budget(size: int, limit: int = GIF_LIMIT) -> bool:
    return size <= limit


def _require_pillow():
    try:
        from PIL import Image
    except ImportError as e:
        raise ShotRefused(f"Pillow is not installed - run: {INSTALL_HINT}") from e
    return Image


def encode_gif(frames: list[tuple[float, bytes]], rung: dict) -> bytes:
    """frames = [(seconds_since_start, png_bytes)]; resampled to the rung's fps."""
    Image = _require_pillow()
    step = 1.0 / rung["fps"]
    picked, next_t = [], 0.0
    for t, png in frames:
        if t + 1e-9 >= next_t:
            picked.append(png)
            next_t = t + step
    images = []
    for png in picked:
        im = Image.open(io.BytesIO(png)).convert("RGB")
        if rung["scale"] != 1.0:
            im = im.resize((max(1, int(im.width * rung["scale"])),
                            max(1, int(im.height * rung["scale"]))), Image.LANCZOS)
        images.append(im.quantize(colors=rung["colors"], method=Image.MEDIANCUT))
    buf = io.BytesIO()
    images[0].save(buf, format="GIF", save_all=True, append_images=images[1:],
                   duration=int(1000 / rung["fps"]), loop=0, optimize=True, disposal=1)
    return buf.getvalue()


def encode_within_budget(frames: list[tuple[float, bytes]], shot: dict) -> bytes:
    ladder = pick_gif_ladder(fps=int(shot.get("fps", 12)), scale=float(shot.get("scale", 1.0)))
    best = None
    for rung in ladder:
        data = encode_gif(frames, rung)
        best = len(data)
        if fits_budget(len(data)):
            log.info("%s: %d frames at %s -> %.1f MB", shot["output"], len(frames), rung, len(data) / 1e6)
            return data
    raise RuntimeError(f"over budget, {best / 1e6:.1f} MB")


# --- atomic output -----------------------------------------------------------
def atomic_write(target: str | os.PathLike, produce: Callable[[Path], None]) -> int:
    """produce(tmp) writes the file; only if it returns normally does tmp replace
    the target. Returns the size. A failure leaves the existing target intact."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.tmp")
    try:
        produce(tmp)
        if not tmp.is_file() or tmp.stat().st_size == 0:
            raise RuntimeError("capture produced no data")
        size = tmp.stat().st_size
        os.replace(tmp, target)
        return size
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


# --- ports + server ----------------------------------------------------------
def pick_free_ports(n: int = 2) -> list[int]:
    """n distinct free ports, never the game's 5200-5300, nor 8000 or 9999."""
    chosen: list[int] = []
    for _ in range(2000):
        port = random.randint(*PORT_RANGE)
        if port in RESERVED_PORTS or port in chosen:
            continue
        with contextlib.ExitStack() as stack:
            try:
                tcp = stack.enter_context(contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)))
                udp = stack.enter_context(contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_DGRAM)))
                tcp.bind(("127.0.0.1", port))
                udp.bind(("127.0.0.1", port))
            except OSError:
                continue
        chosen.append(port)
        if len(chosen) == n:
            return chosen
    raise RuntimeError("could not find free ports")


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        return s.connect_ex((host, port)) == 0


def _stop(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


class CaptureServer:
    """uvicorn on a scratch copy of the database: loopback only, offline."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.http_port, self.udp_port = pick_free_ports(2)
        self.proc: subprocess.Popen | None = None
        self._log = None

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.http_port}"

    def __enter__(self) -> CaptureServer:
        env = {**os.environ, "DATA_DIR": str(self.data_dir), "LS_OFFLINE": "1",
               "TELEMETRY_UDP_PORT": str(self.udp_port), "PYTHONUNBUFFERED": "1"}
        self._log = open(self.data_dir / "server.log", "wb")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
             "--port", str(self.http_port)],
            cwd=ROOT, env=env, stdout=self._log, stderr=subprocess.STDOUT)
        try:
            self._wait_ready()
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def _wait_ready(self, timeout: float = 60.0) -> None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.proc.poll() is not None:
                raise RuntimeError(f"capture server exited early (code {self.proc.returncode}); "
                                   f"see {self.data_dir / 'server.log'}")
            try:
                with urllib.request.urlopen(f"{self.base}/api/status", timeout=2) as r:
                    if r.status == 200:
                        return
            except OSError:
                time.sleep(0.3)
        raise RuntimeError("capture server did not become ready")

    def __exit__(self, *exc) -> None:
        _stop(self.proc)
        if self._log:
            self._log.close()


# --- browser helpers ---------------------------------------------------------
def _require_playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise ShotRefused(f"Playwright is not installed - run: {INSTALL_HINT}") from e
    return sync_playwright


def _loopback_only(route) -> None:
    from urllib.parse import urlparse
    host = urlparse(route.request.url).hostname or ""
    if host in LOOPBACK_HOSTS or route.request.url.startswith(("data:", "blob:")):
        route.continue_()
    else:
        route.abort()


def new_page(browser, shot: dict):
    """A fresh context per shot: loopback-only, per-shot settings pre-seeded."""
    vp = shot.get("viewport") or {"width": 1440, "height": 900}
    ctx = browser.new_context(viewport={"width": vp["width"], "height": vp["height"]},
                              device_scale_factor=float(shot.get("scale_factor", 1)),
                              accept_downloads=False)
    ctx.route("**/*", _loopback_only)
    settings = json.dumps(shot.get("settings") or {})
    ctx.add_init_script(
        "try{const k='ls_settings';const s=JSON.parse(localStorage.getItem(k)||'{}')||{};"
        f"localStorage.setItem(k,JSON.stringify(Object.assign({{v:1}},s,{settings})));"
        "localStorage.setItem('ls_online','0');}catch(e){}")
    return ctx, ctx.new_page()


def _clip(shot: dict) -> dict | None:
    c = shot.get("crop")
    return {k: c[k] for k in ("x", "y", "width", "height")} if c else None


def _shoot(page, shot: dict, clip: dict | None = None) -> bytes:
    return page.screenshot(clip=clip or _clip(shot), type="png")


def _write_png(data: bytes) -> Callable[[Path], None]:
    return lambda tmp: tmp.write_bytes(data)


def open_analysis(browser, base: str, shot: dict):
    """Open the analysis page, select the shot's session, tag its laps."""
    ctx, page = new_page(browser, shot)
    page.goto(base + PAGE_ANALYSIS, wait_until="networkidle")
    page.wait_for_selector(SEL_SESSION_CARD, timeout=30000)
    if shot.get("select_session", True):
        page.evaluate("id => selectSession(id)", shot["session"])
        page.wait_for_selector(SEL_LAP_ROWS, timeout=30000)
        # selectSession auto-tags the best lap, and pickLap toggles: start from
        # an empty tray so the shot shows exactly the laps the manifest names
        page.evaluate("() => state.picks.slice().forEach(removePick)")
        for lap in shot.get("laps") or []:
            found = page.evaluate("id => state.laps.some(l => l.id === id)", lap)
            if not found:
                raise RuntimeError(f"lap {lap} is not in session {shot['session']}")
            page.evaluate("id => pickLap(id)", lap)
        if shot.get("laps"):
            page.wait_for_selector(SEL_CHARTS_READY, timeout=30000)
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(int(shot.get("settle_ms", 600)))
    return ctx, page


# --- recipes (one per output name) -------------------------------------------
def shot_analysis_still(browser, server, shot, _db) -> bytes:
    ctx, page = open_analysis(browser, server.base, shot)
    try:
        return _shoot(page, shot)
    finally:
        ctx.close()


def shot_settings(browser, server, shot, _db) -> bytes:
    ctx, page = open_analysis(browser, server.base, {**shot, "laps": []})
    try:
        page.click(SEL_SETTINGS_BUTTON)
        page.wait_for_selector(SEL_MODAL_OPEN)
        page.wait_for_timeout(300)
        return _shoot(page, shot)
    finally:
        ctx.close()


def shot_browse_bar_menu(browser, server, shot, _db) -> bytes:
    ctx, page = open_analysis(browser, server.base, {**shot, "laps": []})
    try:
        page.click(SEL_HEADER_MENU_TRIGGER)
        page.wait_for_selector(SEL_OPEN_MENU)
        page.wait_for_timeout(300)
        clip = _clip(shot)
        if clip is None:   # header through the lower of the browse bar and the open menu
            vp = page.viewport_size
            bottoms = [page.locator(s).first.bounding_box() for s in (SEL_BROWSE_BAR, SEL_OPEN_MENU)]
            low = max(b["y"] + b["height"] for b in bottoms if b)
            clip = {"x": 0, "y": 0, "width": vp["width"], "height": min(vp["height"], low + 16)}
        return _shoot(page, shot, clip)
    finally:
        ctx.close()


def shot_csv_export_choice(browser, server, shot, _db) -> bytes:
    ctx, page = open_analysis(browser, server.base, {**shot, "laps": []})
    try:
        page.locator(SEL_LAP_CSV_BUTTON).first.click()
        page.wait_for_selector(SEL_EXPORT_CHOICE)
        page.wait_for_timeout(300)
        return _shoot(page, shot)
    finally:
        ctx.close()


def _screens_while(page, shot, work: Callable[[], None], fps: float) -> list[tuple[float, bytes]]:
    """Screenshots at ~fps while work() runs in this thread's helper."""
    frames: list[tuple[float, bytes]] = []
    done = threading.Event()
    t = threading.Thread(target=lambda: (work(), done.set()), daemon=True)
    start = time.monotonic()
    t.start()
    while not done.is_set():
        frames.append((time.monotonic() - start, _shoot(page, shot)))
        time.sleep(max(0.0, 1.0 / fps - 0.05))
    t.join()
    return frames


def _replay_thread_work(db_conn, server, shot) -> Callable[[], None]:
    rp = shot.get("replay") or {"start_s": 0}
    start = float(rp["start_s"])
    end = float(rp.get("end_s", start + 10))
    frames = list(read_replay(db_conn, shot["session"], start, end))
    if not frames:
        raise RuntimeError(f"session {shot['session']} has no frames in {start}-{end}s")
    return lambda: replay_to_udp(frames, server.udp_port)


def _open_live(browser, server, shot):
    ctx, page = new_page(browser, shot)
    page.goto(server.base + PAGE_LIVE, wait_until="domcontentloaded")
    page.wait_for_selector(SEL_DASH_READY, timeout=30000)
    return ctx, page


def shot_accent_live(browser, server, shot, db_conn) -> bytes:
    ctx, page = _open_live(browser, server, shot)
    try:
        work = _replay_thread_work(db_conn, server, shot)
        stop_at = float(shot.get("shoot_after_s", 3.0))
        t = threading.Thread(target=work, daemon=True)
        t.start()
        page.wait_for_timeout(int(stop_at * 1000))
        data = _shoot(page, shot)
        t.join(timeout=60)
        return data
    finally:
        ctx.close()


def shot_paused_chip(browser, server, shot, db_conn) -> bytes:
    ctx, page = _open_live(browser, server, shot)
    try:
        rp = shot.get("replay") or {"start_s": 0}
        short = {**shot, "replay": {"start_s": rp["start_s"],
                                    "end_s": rp.get("end_s", float(rp["start_s"]) + 3)}}
        _replay_thread_work(db_conn, server, short)()   # synchronous: then silence
        page.wait_for_timeout(int(float(shot.get("stale_wait_s", 3.5)) * 1000))
        page.wait_for_function(
            f"document.querySelector('{SEL_CONN_CHIP}').innerText.toLowerCase().includes('paused')",
            timeout=15000)
        return _shoot(page, shot)
    finally:
        ctx.close()


def shot_hero(browser, server, shot, db_conn) -> bytes:
    ctx, page = _open_live(browser, server, shot)
    try:
        frames = _screens_while(page, shot, _replay_thread_work(db_conn, server, shot),
                                float(shot.get("capture_fps", 8)))
        return encode_within_budget(frames, shot)
    finally:
        ctx.close()


def shot_map_animated(browser, server, shot, _db) -> bytes:
    ctx, page = open_analysis(browser, server.base, shot)
    try:
        steps = int(shot.get("rotation_steps", 36))
        frames = []
        for i in range(steps):
            page.evaluate("d => { map3d.yaw += d; drawMap(); }", 6.283185307 / steps)
            frames.append((i / float(shot.get("capture_fps", 12)), _shoot(page, shot)))
        return encode_within_budget(frames, shot)
    finally:
        ctx.close()


RECIPES: dict[str, Callable] = {
    "hero": shot_hero,
    "analysis-compare": shot_analysis_still,
    "track-map": shot_analysis_still,
    "track-map-3d-jumps": shot_analysis_still,
    "track-map-3d-animated": shot_map_animated,
    "session-list": shot_analysis_still,
    "session-sidebar": shot_analysis_still,
    "settings": shot_settings,
    "accent-live-sunset": shot_accent_live,
    "paused-chip": shot_paused_chip,
    "browse-bar-menu": shot_browse_bar_menu,
    "csv-export-choice": shot_csv_export_choice,
}


# --- launcher window ---------------------------------------------------------
def capture_launcher_window(shot: dict, scratch: Path) -> bytes:
    """Run the real launcher as a subprocess and grab its window rectangle."""
    if sys.platform != "win32":
        raise ShotRefused("the launcher-window shot needs Windows")
    if port_in_use(8000):
        raise ShotRefused("port 8000 is already in use (an installed LapScope or docker?) - "
                          "the launcher always binds 8000; stop that and retry")
    try:
        from PIL import ImageGrab
    except ImportError as e:
        raise ShotRefused(f"Pillow is not installed - run: {INSTALL_HINT}") from e
    import ctypes
    from ctypes import wintypes

    with contextlib.suppress(Exception):
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    data_dir = scratch / "launcher-data"
    data_dir.mkdir()
    env = {**os.environ, "DATA_DIR": str(data_dir), "LS_OFFLINE": "1",
           "TELEMETRY_UDP_PORT": str(pick_free_ports(1)[0])}
    proc = subprocess.Popen([sys.executable, str(ROOT / "run_desktop.py")], cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        user32 = ctypes.windll.user32
        found: list[int] = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def visit(hwnd, _lp):
            if not user32.IsWindowVisible(hwnd):
                return True
            cls = ctypes.create_unicode_buffer(64)
            title = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls, 64)
            user32.GetWindowTextW(hwnd, title, 256)
            if cls.value == LAUNCHER_WINDOW_CLASS and title.value.startswith(LAUNCHER_TITLE_PREFIX):
                found.append(hwnd)
            return True

        end = time.monotonic() + float(shot.get("window_wait_s", 40))
        while not found and time.monotonic() < end:
            if proc.poll() is not None:
                raise RuntimeError(f"launcher exited early (code {proc.returncode})")
            user32.EnumWindows(visit, 0)
            time.sleep(0.5)
        if not found:
            raise RuntimeError("launcher window never appeared")
        hwnd = found[0]
        user32.ShowWindow(hwnd, 9)   # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        time.sleep(float(shot.get("settle_s", 3.0)))   # let the server reach "running"
        rect = wintypes.RECT()
        try:   # the visible frame, without the invisible resize border
            ctypes.windll.dwmapi.DwmGetWindowAttribute(
                hwnd, 9, ctypes.byref(rect), ctypes.sizeof(rect))
        except Exception:
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
        img = ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom), all_screens=True)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    finally:
        _stop(proc)


# --- orchestration -----------------------------------------------------------
@dataclass
class Result:
    name: str
    status: str            # ok | refused | failed
    detail: str = ""
    size: int = 0


def run_shot(shot: dict, out_dir: Path, ctx: dict) -> Result:
    name = shot["output"]
    stem = Path(name).stem
    try:
        if shot["kind"] == "window":
            data_fn = lambda: capture_launcher_window(shot, ctx["scratch"])  # noqa: E731
        else:
            recipe = RECIPES.get(stem)
            if recipe is None:
                raise RuntimeError(f"no capture recipe for '{stem}'")
            browser = ctx["get_browser"]()
            data_fn = lambda: recipe(browser, ctx["server"](), shot, ctx["conn"]())  # noqa: E731
        size = atomic_write(out_dir / name, lambda tmp: tmp.write_bytes(data_fn()))
        return Result(name, "ok", size=size)
    except ShotRefused as e:
        return Result(name, "refused", str(e))
    except Exception as e:   # one bad shot must not stop the others
        log.debug("shot %s failed", name, exc_info=True)
        return Result(name, "failed", str(e))


def needs_browser(shots: list[dict]) -> bool:
    return any(s["kind"] != "window" for s in shots)


def run(args: argparse.Namespace) -> int:
    shots = load_manifest(args.manifest)
    if args.only:
        wanted = set(args.only)
        unknown = wanted - {s["output"] for s in shots}
        if unknown:
            raise AbortRun(f"--only names not in the manifest: {', '.join(sorted(unknown))}")
        shots = [s for s in shots if s["output"] in wanted]
    if args.list:
        for s in shots:
            print(f"{s['output']:28s} {s['kind']}")
        return 0

    db_path = resolve_db_path(args.db)      # before anything is started
    out_dir = Path(args.out)
    uri = db_path.resolve().as_uri() + "?mode=ro"
    probe = sqlite3.connect(uri, uri=True)
    try:
        verdicts = validate_shots(shots, collect_db_ids(probe))
    finally:
        probe.close()

    results: list[Result] = []
    runnable = []
    for s in shots:
        v = verdicts[s["output"]]
        if v.ok:
            runnable.append(s)
        else:
            results.append(Result(s["output"], "refused", v.reason))

    scratch = Path(tempfile.mkdtemp(prefix="lapscope-media-"))
    stack = contextlib.ExitStack()
    try:
        if runnable:
            state: dict = {"scratch": scratch}
            if needs_browser(runnable):
                snapshot_db(db_path, scratch)
                state["conn"] = lambda c=sqlite3.connect(scratch / "telemetry.db"): c
                state["server"] = lambda s=stack.enter_context(CaptureServer(scratch)): s
                state["get_browser"] = _browser_factory(stack)
            for s in runnable:
                if s["kind"] != "window":
                    try:
                        state["get_browser"]()   # surfaces a missing Playwright once, per shot
                    except ShotRefused as e:
                        results.append(Result(s["output"], "refused", str(e)))
                        continue
                results.append(run_shot(s, out_dir, state))
            if "conn" in state:
                state["conn"]().close()
    finally:
        stack.close()
        for _ in range(5):   # Windows can hold a file for a moment after its process dies
            shutil.rmtree(scratch, ignore_errors=True)
            if not scratch.exists():
                break
            time.sleep(0.5)
    return report(results)


def _browser_factory(stack: contextlib.ExitStack) -> Callable:
    cache: dict = {}

    def get():
        if "b" not in cache:
            sync_playwright = _require_playwright()
            pw = stack.enter_context(sync_playwright())
            cache["b"] = pw.chromium.launch()
            stack.callback(cache["b"].close)
        return cache["b"]
    return get


def report(results: list[Result]) -> int:
    print("\nLapScope media capture")
    bad = 0
    for r in results:
        size = f"  {r.size / 1024:.0f} KB" if r.status == "ok" else ""
        detail = f"  {r.detail}" if r.detail else ""
        label = f"failed ({r.detail})" if r.status == "failed" else r.status
        print(f"  {r.name:28s} {label}{size}" + ("" if r.status == "failed" else detail))
        bad += r.status != "ok"
    print(f"{len(results) - bad} ok, {bad} not ok")
    return 1 if bad else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Regenerate docs/media from a real telemetry.db")
    p.add_argument("--db", help="telemetry.db to read (default: the exe's %%LOCALAPPDATA%%\\LapScope)")
    p.add_argument("--out", default=str(DEFAULT_OUT), help="output directory (default docs/media/)")
    p.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    p.add_argument("--only", nargs="+", metavar="NAME", help="only these output names")
    p.add_argument("--list", action="store_true", help="list the shots and exit")
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):   # a cp1254/cp1252 console must not crash the report
        with contextlib.suppress(Exception):
            stream.reconfigure(errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except (AbortRun, ManifestError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
