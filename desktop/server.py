"""Start and stop the ASGI server from a GUI, without losing the lap in progress.

uvicorn.run() blocks forever and owns the process, which is fine for a console
and useless behind a window. This hosts uvicorn.Server on a background thread
instead, leaving the main thread to tkinter, and stops it the way the recorder
needs: by asking, then waiting.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time

import uvicorn

from .state import ServerPhase, StatusSnapshot

log = logging.getLogger("lapscope.desktop")

# How long uvicorn may spend draining open connections before it moves on to
# the lifespan teardown. The default is None, meaning wait forever, and the
# dashboard's open /ws/live socket is exactly the connection that would hold it.
GRACEFUL_DRAIN_S = 5

# How long we wait for the whole stop - drain, then the lifespan teardown that
# finalises the session - before offering to give up. Comfortably more than
# GRACEFUL_DRAIN_S, with headroom for a large final flush.
STOP_TIMEOUT_S = 12.0


class ServerController:
    """Owns the uvicorn thread and the HTTP port.

    All methods are called from the UI thread. Nothing here blocks on the
    server: a blocking join would freeze the window, and Windows would paint it
    grey and offer to kill it - which is the failure mode this whole window
    exists to remove.
    """

    def __init__(self, app, host: str = "127.0.0.1", port: int = 8000,
                 version: str = "0.0.0") -> None:
        self._app = app
        self.host = host
        self.port = port
        self.version = version
        self.phase = ServerPhase.STOPPED
        self.last_error: str | None = None
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._stop_deadline: float | None = None
        self._holder: socket.socket | None = None
        self._restart_pending = False
        self._hold_port()

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    # ---- the port as a mutex ------------------------------------------------
    # While the server runs, binding the port is what stops a second LapScope.
    # But a window sitting with the server stopped leaves the port free, and a
    # second instance would then open the same telemetry.db with its own
    # SessionTracker - two writers, one database. So the launcher holds the port
    # for its whole life and only lets go for the moment it hands a listening
    # socket to uvicorn.

    def _hold_port(self) -> None:
        if self._holder is not None:
            return
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind((self.host, self.port))
        except OSError:
            s.close()
            return  # someone else has it; start() reports it properly
        self._holder = s

    def _release_port(self) -> None:
        if self._holder is not None:
            self._holder.close()
            self._holder = None

    # ---- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """Bind and launch. Raises OSError if the HTTP port is taken.

        The socket is bound here, on the caller's thread, rather than left to
        uvicorn. Config.bind_socket() calls sys.exit() on a busy port, and
        threading.excepthook ignores SystemExit - on a background thread that
        means the thread vanishes with no traceback and no error to show. It
        also closes the gap between checking a port and claiming it.
        """
        if self.phase in (ServerPhase.STARTING, ServerPhase.RUNNING, ServerPhase.STOPPING):
            return
        self._release_port()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # Deliberately no SO_REUSEADDR: on Windows it lets you bind a port
        # another process is already holding, which would turn the conflict we
        # want to report into two servers quietly fighting over one database.
        try:
            sock.bind((self.host, self.port))
        except OSError:
            sock.close()
            self._hold_port()
            raise
        self.last_error = None
        self.phase = ServerPhase.STARTING
        cfg = uvicorn.Config(
            self._app,  # the object by reference, so PyInstaller follows the import
            host=self.host,
            port=self.port,
            # Without this the windowed exe dies here, in the constructor:
            # Config.__init__ configures logging, whose formatter calls
            # sys.stdout.isatty(), and a windowed build has no sys.stdout.
            log_config=None,
            log_level=None,  # the launcher's root logger owns levels
            # ~1800 lines/hour of "GET /api/status 200" would push every
            # recorder decision out of the log pane long before anyone read it.
            # Read here rather than at import so Restart picks up a change.
            access_log=os.environ.get("LS_ACCESS_LOG", "0").lower() not in ("", "0", "false", "no"),
            # "auto" catches lifespan exceptions and logs a misleading
            # "protocol appears unsupported" at INFO, discarding the traceback,
            # then serves an app with no app.state.store where every request
            # 500s. "on" makes a failed startup loud and fatal.
            lifespan="on",
            timeout_graceful_shutdown=GRACEFUL_DRAIN_S,
        )
        self._server = uvicorn.Server(cfg)
        self._thread = threading.Thread(
            target=self._run, args=(sock,), name="lapscope-uvicorn", daemon=True)
        # daemon: a window that has gone away must not leave an invisible
        # process holding port 8000. The graceful stop always runs first, and
        # the timeout path is explicit, so this never costs a lap.
        self._thread.start()

    def _run(self, sock: socket.socket) -> None:
        assert self._server is not None
        try:
            self._server.run(sockets=[sock])
        except BaseException as exc:
            # BaseException, not Exception: uvicorn raises SystemExit from
            # Server.startup when a lifespan="on" startup fails (a locked or
            # unmigratable database, most likely). threading.excepthook ignores
            # SystemExit, so without this the thread would disappear silently.
            self.phase = ServerPhase.FAILED
            self.last_error = str(exc) or exc.__class__.__name__
            log.critical("The LapScope server stopped unexpectedly.", exc_info=True)
        else:
            self.phase = ServerPhase.STOPPED
        finally:
            try:
                sock.close()  # a no-op after a clean shutdown, which closes it
            except OSError:
                pass

    def request_stop(self) -> None:
        """Ask the server to stop. Returns immediately; watch phase via poll().

        should_exit is a plain bool read by Server.on_tick every 100 ms on its
        own loop, so setting it across threads is the intended mechanism. Never
        force_exit: that skips the lifespan shutdown, and the lifespan shutdown
        is what calls SessionTracker.shutdown() to finalise the open lap.
        """
        if self.phase not in (ServerPhase.STARTING, ServerPhase.RUNNING):
            return
        self.phase = ServerPhase.STOPPING
        self._stop_deadline = time.monotonic() + STOP_TIMEOUT_S
        if self._server is not None:
            self._server.should_exit = True

    def poll(self) -> None:
        """Advance the state machine. Cheap; call it from the UI's tick."""
        srv, thread = self._server, self._thread
        if self.phase is ServerPhase.STARTING and srv is not None and srv.started:
            self.phase = ServerPhase.RUNNING
        if thread is not None and not thread.is_alive():
            self._reap()

    def _reap(self) -> None:
        """Clean up after the thread has exited. _run has already set the phase."""
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        self._thread = None
        self._server = None
        self._stop_deadline = None
        self._hold_port()

    def stop_overdue(self) -> bool:
        """True once a stop has taken longer than it should. The UI offers to
        give up; nothing here acts on it by itself."""
        return (self.phase is ServerPhase.STOPPING
                and self._stop_deadline is not None
                and time.monotonic() > self._stop_deadline)

    def extend_stop_deadline(self) -> None:
        self._stop_deadline = time.monotonic() + STOP_TIMEOUT_S

    def restart(self) -> None:
        """Stop, then start again once the thread is gone.

        Needs a fresh Config, Server and socket every time: uvicorn closes the
        socket it was handed, Config.loaded is one-shot, and Server carries
        started/should_exit as instance state. Re-entering the app's lifespan is
        safe - app.state is just a namespace, the static mount is module-level,
        and cars.load/tracks.load mutate globals in place.

        Note what a restart can and cannot change. DATA_DIR and
        TELEMETRY_UDP_PORT are read inside the lifespan, so they take effect.
        LS_OFFLINE, LS_KEEP_DISCARDED and LS_ALLOWED_HOSTS are read at module
        import, before the app object existed, and need the whole process
        restarted.
        """
        self.request_stop()
        self._restart_pending = True

    def poll_restart(self) -> bool:
        """Finish a pending restart once the old thread is gone. Returns True
        if it started the server this tick."""
        if not self._restart_pending:
            return False
        if self._thread is not None:
            return False
        self._restart_pending = False
        self.start()
        return True

    def stop(self, timeout: float = STOP_TIMEOUT_S) -> bool:
        """Blocking stop. For tests and the headless self-test - never call this
        from the UI thread, it would freeze the window."""
        self.request_stop()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
            if thread.is_alive():
                return False
        self._reap()
        return True

    # ---- status -------------------------------------------------------------

    def snapshot(self) -> StatusSnapshot:
        """The same fields GET /api/status reports, read straight off app.state.

        In-process rather than over HTTP: an HTTP call from the UI thread would
        block the window on any hiccup, and it would make the launcher a client
        of its own check_host middleware for data that is already in memory.
        It also keeps working when HTTP is wedged, which is when the status
        matters most.

        Only counters and small scalars are touched here. app.state.store must
        never be read from this thread - its sqlite connection belongs to the
        event loop.
        """
        if self.phase is not ServerPhase.RUNNING:
            return StatusSnapshot(phase=self.phase, version=self.version,
                                  startup_error=self.last_error)
        state = self._app.state
        hub = getattr(state, "hub", None)
        tracker = getattr(state, "tracker", None)
        if hub is None or tracker is None:
            # Between the thread starting and the lifespan finishing, app.state
            # is empty and Starlette's State raises on attribute access.
            return StatusSnapshot(phase=ServerPhase.STARTING, version=self.version)
        last = hub.last_packet_time
        return StatusSnapshot(
            phase=ServerPhase.RUNNING,
            version=self.version,
            udp_port=getattr(state, "udp_port", None),
            udp_error=getattr(state, "udp_error", None),
            packets_total=hub.packets_total,
            bad_packets=hub.bad_packets,
            last_packet_age=None if last is None else round(time.time() - last, 3),
            last_packet_size=hub.last_packet_size,
            session_active=tracker.session_id is not None,
            session_id=tracker.session_id,
            session_best=tracker.best_lap_time,
            write_error=tracker.write_error,
            frames_dropped=tracker.frames_dropped,
        )
