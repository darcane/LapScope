"""Desktop entry point for the packaged LapScope.exe (PyInstaller onedir).

Points DATA_DIR at a stable per-user location so recorded telemetry survives
re-downloading the exe, then opens the launcher window, which owns the server
from there. Running from source works too: ``python run_desktop.py``.

The build is windowed (LapScope.spec sets console=False), so there is no
console to print to and nothing here may assume one.
"""

from __future__ import annotations

import logging
import os
import sys

HTTP_HOST = "127.0.0.1"
HTTP_PORT = 8000

log = logging.getLogger("lapscope.desktop")


def _guard_std_streams() -> None:
    """Give sys.stdout/sys.stderr somewhere to go in a windowed build.

    PyInstaller sets both to None when console=False, and anything that prints
    or writes a traceback then raises AttributeError. A real file object rather
    than a hand-rolled null writer, so isatty() and fileno() still work.

    This is NOT what keeps uvicorn alive - uvicorn's default logging config
    calls sys.stdout.isatty() while building its formatter, and if this shim
    let that succeed every server log line would go to NUL. desktop/server.py
    passes log_config=None for that. This only covers whatever prints next.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    null = open(os.devnull, "w", encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = null
    if sys.stderr is None:
        sys.stderr = null


def main() -> None:
    _guard_std_streams()

    from desktop.paths import default_data_dir

    os.environ.setdefault("DATA_DIR", default_data_dir())
    os.makedirs(os.environ["DATA_DIR"], exist_ok=True)

    # Before the app is imported, so the first thing the log file records is the
    # startup it is being asked about. Also sets the root level, which
    # app/main.py's own basicConfig can no longer do once handlers exist.
    from desktop.logs import install_logging

    sink, _log_file, log_warning = install_logging()

    # Imported after DATA_DIR is set. The app object is passed to the controller
    # by reference (not as an "app.main:app" string) so PyInstaller statically
    # follows the import and bundles the whole app package; DATA_DIR is only read
    # later, inside the app's lifespan handler.
    from app import __version__
    from app.main import app

    # The header the console used to print. It is the first thing anyone reads
    # off a bug report, so it belongs at the top of the log file.
    log.info("LapScope v%s starting - dashboard at http://%s:%d",
             __version__, HTTP_HOST, HTTP_PORT)
    log.info("Recording telemetry to %s", os.environ["DATA_DIR"])

    # An env-var switch rather than a CLI flag: a double-clicked exe has no
    # argv, and this only ever runs from the release workflow.
    if os.environ.get("LS_DESKTOP_SELFTEST"):
        from desktop.selftest import run_headless_check

        raise SystemExit(run_headless_check(app, __version__))

    from desktop import ui
    from desktop.server import ServerController

    controller = ServerController(app, HTTP_HOST, HTTP_PORT, __version__)
    ui.run(controller, sink, log_warning)


if __name__ == "__main__":
    main()
