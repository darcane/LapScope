"""Headless smoke test for the frozen build, run by the release workflow.

CI has never built the exe - it lints and tests on Linux - so until now the
only thing that ever ran a LapScope.exe was a person downloading it. That was
survivable while the entry point was 60 lines of uvicorn.run(); it is not now
that a whole window sits in front of it, and the windowed build has failure
modes source runs cannot have (no sys.stdout, assets resolved out of _MEIPASS).

This starts the real server out of the real bundle, asks it a question over
HTTP, and stops it. It never creates a Tk window: it has to pass on a runner,
and a GUI failure here would be indistinguishable from a headless one.
"""

from __future__ import annotations

import json
import socket
import time
import urllib.request
from pathlib import Path

from .paths import log_dir

RESULT_NAME = "selftest.txt"
START_TIMEOUT_S = 30.0
STOP_TIMEOUT_S = 20.0


def _free_port() -> int:
    """An ephemeral port, not the shipping 8000. What is being proved here is
    that the bundle works, not that a CI runner has 8000 free - a machine with
    something else on that port should not fail a release."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _report(ok: bool, message: str) -> int:
    text = f"{'PASS' if ok else 'FAIL'} {message}".strip()
    # Written to a file because a windowed exe has no stdout for the workflow
    # to capture, and the exit code alone doesn't say what broke.
    try:
        path = Path(log_dir())
        path.mkdir(parents=True, exist_ok=True)
        (path / RESULT_NAME).write_text(text + "\n", encoding="utf-8")
    except OSError:
        pass
    return 0 if ok else 1


def run_headless_check(app, version: str) -> int:
    """Start, query, stop. Returns a process exit code."""
    from .server import ServerController
    from .state import ServerPhase

    port = _free_port()
    controller = ServerController(app, "127.0.0.1", port, version)
    try:
        controller.start()
        deadline = time.monotonic() + START_TIMEOUT_S
        while time.monotonic() < deadline:
            controller.poll()
            if controller.phase is ServerPhase.RUNNING:
                break
            if controller.phase is ServerPhase.FAILED:
                return _report(False, f"startup failed: {controller.last_error}")
            time.sleep(0.05)
        else:
            return _report(False, f"server never started (phase {controller.phase.value})")

        url = f"http://127.0.0.1:{port}/api/status"
        with urllib.request.urlopen(url, timeout=10) as resp:
            if resp.status != 200:
                return _report(False, f"/api/status returned {resp.status}")
            body = json.loads(resp.read())
        if "version" not in body:
            return _report(False, f"/api/status missing 'version': {sorted(body)}")
        # The lifespan really ran, i.e. the database opened and the catalogues
        # loaded out of the bundle. A 200 alone would not prove that.
        if not hasattr(app.state, "store"):
            return _report(False, "lifespan did not complete: no app.state.store")

        if not controller.stop(timeout=STOP_TIMEOUT_S):
            return _report(False, "server did not stop within the timeout")
    except Exception as exc:
        return _report(False, f"{type(exc).__name__}: {exc}")
    return _report(True, f"v{body['version']} served /api/status on port {port} and stopped cleanly")
