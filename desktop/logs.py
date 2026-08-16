"""Log plumbing for the launcher: one handler feeding the window, one feeding a file.

Replaces the console the exe used to keep open. The console was the only place
logs existed - there was no file handler anywhere - so a bug report meant a
screenshot. Now the same lines go to both the pane and a rotating file that can
be attached to an issue.
"""

from __future__ import annotations

import collections
import logging
import logging.handlers

from .paths import log_dir

# Must match app/main.py:38. That call still runs (Docker relies on it) and
# would install a second, differently formatted handler if this drifted.
LOG_FORMAT = "%(asctime)s %(name)s %(levelname)s %(message)s"

# 2 MB x 4 files. docker-compose.yml caps its logs for a specific reason - a
# recorder that starts failing logs at telemetry rate and can fill a disk - and
# the exe had no equivalent guard at all. laps.py's log-once-on-failure contract
# is the real defence; this is the backstop for whatever the next one is.
LOG_MAX_BYTES = 2_000_000
LOG_BACKUP_COUNT = 3


class LogSink(logging.Handler):
    """Formatted lines on their way to the window.

    A deque rather than a Queue: append and popleft are atomic, and a bounded
    deque sheds its oldest line instead of raising when the UI falls behind -
    the same backpressure Hub.publish applies to a slow WebSocket client. A
    handler that can raise turns a diagnostic into a second failure, and this
    one runs on the server thread where nothing would catch it. Shed lines are
    still in the log file, so nothing is actually lost.
    """

    def __init__(self, maxlen: int = 2000) -> None:
        super().__init__()
        self._lines: collections.deque[str] = collections.deque(maxlen=maxlen)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._lines.append(self.format(record))
        except Exception:
            # handleError no-ops when sys.stderr is None, which under a windowed
            # build is always. Never let a log line kill the recorder.
            self.handleError(record)

    def drain(self, limit: int = 200) -> list[str]:
        """Take up to `limit` lines. Bounded so one burst can't stall the UI
        thread for a whole frame."""
        out: list[str] = []
        while len(out) < limit:
            try:
                out.append(self._lines.popleft())
            except IndexError:
                break
        return out


class LogRing:
    """The pane's line budget, as arithmetic rather than widget calls.

    tkinter's Text has no maxlen, so something has to decide how many lines to
    delete off the top after an append. Keeping that here means it can be
    tested without a display.
    """

    def __init__(self, maxlen: int = 500) -> None:
        self.maxlen = maxlen
        self.count = 0

    def add(self, n: int) -> int:
        """Record `n` new lines; return how many to delete from the top."""
        self.count += n
        if self.count <= self.maxlen:
            return 0
        drop = self.count - self.maxlen
        self.count = self.maxlen
        return drop

    def add_text(self, text: str) -> int:
        """Record an insertion, counted in the lines the widget will show.

        One log record is not one line: anything logged with a traceback
        arrives as a single formatted string containing dozens of them. The
        widget's delete is by line index, so counting records would under-count
        every exception and let the pane grow past its budget forever - which
        is exactly the situation (something is going wrong, repeatedly) where
        it matters.
        """
        return self.add(text.count("\n"))


def install_logging(maxlen: int = 2000) -> tuple[LogSink, object | None, str | None]:
    """Wire up root logging before the server starts. Returns (sink, file
    handler or None, warning or None).

    This has to make the basicConfig call itself, not leave it to app/main.py:38.
    CPython puts root.setLevel inside basicConfig's `if len(root.handlers) == 0`
    branch, so once these handlers exist that call returns early and the level
    is never set - root stays at WARNING and every recorder INFO line vanishes.
    Passing level and handlers together here does both, and main.py's call then
    harmlessly no-ops.
    """
    sink = LogSink(maxlen)
    handlers: list[logging.Handler] = [sink]
    file_handler = None
    warning = None
    try:
        d = log_dir()
        d.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            d / "lapscope.log", maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT, encoding="utf-8")
        handlers.append(file_handler)
    except OSError as exc:
        # Read-only or missing DATA_DIR. The window is still perfectly usable
        # without a file, so degrade instead of refusing to start - but say so,
        # or the first bug report will ask for a log that was never written.
        warning = f"Could not open the log file in {log_dir()} ({exc}). Logs are in this window only."

    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, handlers=handlers)
    # DeprecationWarnings and friends would otherwise go to a stderr that does
    # not exist in a windowed build.
    logging.captureWarnings(True)
    return sink, file_handler, warning
