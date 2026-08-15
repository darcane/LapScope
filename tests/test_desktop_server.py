"""The real server, started and stopped the way the window does it.

Not a mock: this runs the actual ASGI app on a background thread, asks it a
question over HTTP with urllib, and stops it - the same zero-dependency
footprint as the rest of the tests, no httpx. Everything that could only break
in the frozen exe is out of reach here, but the lifecycle is not, and the
lifecycle is where the lap in progress can be lost.
"""

from __future__ import annotations

import json
import logging
import socket
import time
import urllib.request

import pytest

from app import cars, tracks
from app.main import app
from desktop.server import ServerController
from desktop.state import ServerPhase


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def wait_for(controller: ServerController, phase: ServerPhase, timeout: float = 30.0) -> bool:
    """Drive poll() the way the UI's tick does."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        controller.poll()
        if controller.phase is phase:
            return True
        if controller.phase is ServerPhase.FAILED and phase is not ServerPhase.FAILED:
            pytest.fail(f"server failed to start: {controller.last_error}")
        time.sleep(0.02)
    return False


def get_json(port: int, path: str = "/api/status") -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as resp:
        assert resp.status == 200
        return json.loads(resp.read())


@pytest.fixture()
def controller(tmp_path, monkeypatch):
    """A controller on a scratch database and an ephemeral UDP port.

    Both are read inside the app's lifespan rather than at import, so setting
    them here is enough - no import-order dance. UDP port 0 means a real
    LapScope (or another test) on 9999 never collides with this.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TELEMETRY_UDP_PORT", "0")
    c = ServerController(app, "127.0.0.1", free_port(), "9.9.9")
    yield c
    c.stop(timeout=20)
    # The lifespan points the module-global car/track catalogues at tmp_path,
    # which is about to be deleted. Put them back on the bundled lists.
    cars._data_dir = None
    tracks._data_dir = None
    cars.load()
    tracks.load()


def test_it_starts_and_serves(controller):
    controller.start()
    assert wait_for(controller, ServerPhase.RUNNING), "never reached RUNNING"
    body = get_json(controller.port)
    assert body["version"] == "0.0.0"  # the app's own, not the launcher's label
    # A 200 alone doesn't prove the lifespan ran - the store is what proves it.
    assert hasattr(app.state, "store")


def test_stopping_runs_the_lifespan_teardown(controller, caplog):
    """The teardown is what calls SessionTracker.shutdown() to finalise the lap
    in progress. If a stop ever skipped it, closing the window would start
    losing laps again - silently, which is why this is asserted explicitly."""
    caplog.set_level(logging.INFO)
    controller.start()
    assert wait_for(controller, ServerPhase.RUNNING)
    assert controller.stop(timeout=20) is True
    assert controller.phase is ServerPhase.STOPPED
    assert "Application shutdown complete" in caplog.text


def test_it_can_be_started_again_after_stopping(controller):
    """Restart is the real regression risk: uvicorn closes the socket it was
    handed, Config.loaded is one-shot, and Server carries started/should_exit
    as instance state, so all three have to be rebuilt."""
    controller.start()
    assert wait_for(controller, ServerPhase.RUNNING)
    assert controller.stop(timeout=20) is True

    controller.start()
    assert wait_for(controller, ServerPhase.RUNNING), "second start never came up"
    assert get_json(controller.port)["version"] == "0.0.0"


def test_a_busy_port_raises_on_the_caller_rather_than_killing_the_thread(tmp_path, monkeypatch):
    """uvicorn's own bind calls sys.exit(), and threading.excepthook ignores
    SystemExit - on a background thread that means the server vanishes with no
    error at all. Binding on the caller's thread is what makes it reportable."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TELEMETRY_UDP_PORT", "0")
    port = free_port()
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", port))
    try:
        c = ServerController(app, "127.0.0.1", port, "9.9.9")
        with pytest.raises(OSError):
            c.start()
        assert c.phase is ServerPhase.STOPPED
    finally:
        blocker.close()


def test_the_port_is_held_while_the_server_is_stopped(tmp_path, monkeypatch):
    """A window sitting with the server stopped still owns the port, so a
    second LapScope cannot start and open the same telemetry.db with its own
    SessionTracker - two writers, one database."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    port = free_port()
    first = ServerController(app, "127.0.0.1", port, "9.9.9")
    try:
        second = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with pytest.raises(OSError):
                second.bind(("127.0.0.1", port))
        finally:
            second.close()
    finally:
        first._release_port()


def test_snapshot_reports_the_phase_before_the_lifespan_has_run(controller):
    """app.state is empty between the thread starting and the lifespan
    finishing, and Starlette's State raises on attribute access."""
    snap = controller.snapshot()
    assert snap.phase is ServerPhase.STOPPED
    assert snap.version == "9.9.9"


def test_snapshot_mirrors_the_status_endpoint(controller):
    """The window reads app.state directly instead of calling /api/status, so
    the two could drift apart. They describe the same thing to the same person."""
    controller.start()
    assert wait_for(controller, ServerPhase.RUNNING)
    snap = controller.snapshot()
    body = get_json(controller.port)
    for field in ("udp_port", "udp_error", "packets_total", "bad_packets",
                  "last_packet_size", "session_active", "session_id",
                  "session_best", "write_error", "frames_dropped"):
        assert getattr(snap, field) == body[field], f"{field} differs from /api/status"
