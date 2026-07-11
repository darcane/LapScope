"""LapScope server: UDP telemetry in, web dashboard + API out."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from . import cars
from .api.routes import router
from .recorder.laps import SessionTracker
from .recorder.store import Store
from .telemetry.hub import Hub
from .telemetry.listener import TelemetryProtocol

log = logging.getLogger("lapscope")


async def _watchdog(tracker: SessionTracker) -> None:
    while True:
        await asyncio.sleep(1.0)
        try:
            tracker.tick(time.time())
        except Exception:
            log.exception("watchdog tick failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    data_dir = os.environ.get("DATA_DIR", "./data")
    udp_port = int(os.environ.get("TELEMETRY_UDP_PORT", "9999"))

    # overlay a previously downloaded community car list (see app/cars.py)
    cars.load(data_dir)
    store = Store(os.path.join(data_dir, "telemetry.db"))
    removed = store.cleanup_sessions()
    if removed:
        log.info("Startup cleanup: removed %d session(s) without completed laps", removed)
    hub = Hub()
    tracker = SessionTracker(store)
    app.state.store, app.state.hub, app.state.tracker = store, hub, tracker
    app.state.udp_port = udp_port

    app.state.udp_error = None
    loop = asyncio.get_running_loop()
    transport = None
    try:
        transport, _ = await loop.create_datagram_endpoint(
            lambda: TelemetryProtocol(hub, tracker), local_addr=("0.0.0.0", udp_port)
        )
        log.info("Listening for FH6 Data Out on UDP %d; dashboard on HTTP 8000", udp_port)
    except OSError as exc:
        # Port already taken (another telemetry tool, or a second LapScope
        # window). Don't crash-exit — that slams the console shut on a
        # double-clicked exe before the user can read anything. Keep the
        # dashboard serving (past-session analysis still works) and surface an
        # actionable message here and in /api/status.
        app.state.udp_error = (
            f"UDP port {udp_port} is already in use - another program (or a second "
            "LapScope window) has it. Close that program, or set TELEMETRY_UDP_PORT to "
            "a free port, then restart LapScope."
        )
        log.error(
            "Could not bind UDP telemetry port %d (%s). %s "
            "The dashboard is still available on HTTP 8000, but no live telemetry "
            "will arrive until the port is free.",
            udp_port, exc, app.state.udp_error,
        )
    watchdog = asyncio.create_task(_watchdog(tracker))

    yield

    watchdog.cancel()
    if transport is not None:
        transport.close()
    tracker.shutdown(time.time())
    store.close()


app = FastAPI(title="LapScope", lifespan=lifespan)
app.include_router(router, prefix="/api")


@app.middleware("http")
async def revalidate_static(request, call_next):
    """Make browsers revalidate dashboard assets so UI updates apply on reload
    (cheap 304s on localhost; without this, heuristic caching serves stale JS/CSS)."""
    response = await call_next(request)
    if not request.url.path.startswith("/api"):
        response.headers.setdefault("Cache-Control", "no-cache")
    return response


@app.websocket("/ws/live")
async def ws_live(ws: WebSocket) -> None:
    await ws.accept()
    hub: Hub = ws.app.state.hub
    q = hub.subscribe()
    try:
        while True:
            await ws.send_json(await q.get())
    except WebSocketDisconnect:
        pass
    finally:
        hub.unsubscribe(q)


app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")
