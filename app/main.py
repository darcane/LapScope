"""LapScope server: UDP telemetry in, web dashboard + API out."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Response, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from . import cars, tracks
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

    # overlay a previously downloaded community car list (see app/cars.py) and
    # track catalogue (app/tracks.py). Both before the Store: its startup
    # backfill names routes straight out of the catalogue.
    cars.load(data_dir)
    tracks.load(data_dir)
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


# extra Host names to answer to, comma-separated (a reverse proxy, a hostname
# the box is known by). Read once at import, like the app object itself.
ALLOWED_HOSTS = {h.strip().lower()
                 for h in os.environ.get("LS_ALLOWED_HOSTS", "").split(",")
                 if h.strip()}


def _hostname(host_header: str) -> str:
    """The name out of a Host header, port and IPv6 brackets removed."""
    h = host_header.strip()
    if h.startswith("["):                      # [::1]:8000
        return h[1:h.index("]")] if "]" in h else h[1:]
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


def host_allowed(host_header: str) -> bool:
    """Is this Host header one LapScope should answer to?

    The threat is DNS rebinding: a page the user visits points its own domain
    at 127.0.0.1 (or their LAN address) and then talks to LapScope as if it
    were same-origin - reaching the whole API, `DELETE /api/sessions/{id}`
    included. That attack always leaves the attacker's *name* in the Host
    header; it cannot make a browser send a bare address it never resolved.

    So: localhost and any IP literal are fine - loopback, the exe, a phone
    hitting the Docker host by LAN IP - and any other name is refused unless
    the operator listed it in LS_ALLOWED_HOSTS (issue #67)."""
    name = _hostname(host_header).lower()
    if not name:
        return False
    # .local is mDNS - resolved on the LAN, not by a nameserver the attacker
    # can point anywhere - so a NAS or Pi reached at `box.local:8000` stays
    # reachable without opening the door rebinding needs
    if (name == "localhost" or name.endswith((".localhost", ".local"))
            or name in ALLOWED_HOSTS):
        return True
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return False
    return True


@app.middleware("http")
async def revalidate_static(request, call_next):
    """Make browsers revalidate dashboard assets so UI updates apply on reload
    (cheap 304s on localhost; without this, heuristic caching serves stale JS/CSS)."""
    response = await call_next(request)
    if not request.url.path.startswith("/api"):
        response.headers.setdefault("Cache-Control", "no-cache")
    return response


# registered last, so it wraps everything above: a request for a host this
# server has no business answering is refused before any handler sees it
@app.middleware("http")
async def check_host(request, call_next):
    if not host_allowed(request.headers.get("host", "")):
        return Response("Invalid host header", status_code=400,
                        media_type="text/plain")
    return await call_next(request)


def origin_allowed(origin: str | None, host_header: str) -> bool:
    """WebSocket handshakes are exempt from CORS, so nothing stopped a page
    the user happened to visit from opening /ws/live and streaming their live
    position, speed and car while they drove (issue #67). The dashboard
    connects to its own origin, so requiring exactly that costs it nothing.
    A missing Origin is a non-browser client (a script, a CLI tool) and is
    allowed: the header is what browsers attach, and forging it isn't the
    threat - a program that can set headers can already reach the API."""
    if origin is None:
        return True
    netloc = urlsplit(origin).netloc.lower()
    return bool(netloc) and netloc == host_header.strip().lower()


@app.websocket("/ws/live")
async def ws_live(ws: WebSocket) -> None:
    if not origin_allowed(ws.headers.get("origin"), ws.headers.get("host", "")):
        log.warning("Rejected /ws/live handshake from origin %s",
                    ws.headers.get("origin"))
        await ws.close(code=1008)  # policy violation
        return
    await ws.accept()
    hub: Hub = ws.app.state.hub
    q = hub.subscribe()
    # Watch the socket as well as the queue. Nothing is ever sent *to* this
    # endpoint, so this only completes on a disconnect - the tab closing, or
    # the server asking the connection to shut down. Waiting on q.get() alone
    # cannot see either: FH6 stops publishing the moment you pause, so a
    # handler with no frames to send would block until something arrived.
    # That made shutdown wait out its whole grace period on a client that had
    # already gone away, then log the cancellation as an ASGI error.
    watcher = asyncio.ensure_future(ws.receive())
    try:
        while True:
            getter = asyncio.ensure_future(q.get())
            done, _ = await asyncio.wait({getter, watcher},
                                         return_when=asyncio.FIRST_COMPLETED)
            if getter in done:
                await ws.send_json(getter.result())
            else:
                getter.cancel()
            if watcher in done:
                if watcher.result().get("type") == "websocket.disconnect":
                    break
                watcher = asyncio.ensure_future(ws.receive())  # ignore stray frames
    except WebSocketDisconnect:
        pass
    finally:
        watcher.cancel()
        hub.unsubscribe(q)


app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")
