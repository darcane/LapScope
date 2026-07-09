"""Desktop entry point for the packaged LapScope.exe (PyInstaller onedir).

Runs the FastAPI app under uvicorn on localhost, points DATA_DIR at a stable
per-user location so the recorded telemetry survives re-downloading the exe, and
opens the dashboard in the default browser. Running from source works too:
``python run_desktop.py``.
"""

from __future__ import annotations

import os
import socket
import threading
import webbrowser

HTTP_HOST = "127.0.0.1"
HTTP_PORT = 8000


def _default_data_dir() -> str:
    """Per-user data dir: %LOCALAPPDATA%\\LapScope on Windows, ~/.lapscope elsewhere."""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "LapScope")


def _http_port_in_use() -> bool:
    """Pre-check the HTTP port the way app/main.py guards the UDP port: if
    uvicorn's bind fails it SystemExits, which slams the console shut on a
    double-clicked exe before the user can read anything."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((HTTP_HOST, HTTP_PORT))
        except OSError:
            return True
    return False


def main() -> None:
    os.environ.setdefault("DATA_DIR", _default_data_dir())
    os.makedirs(os.environ["DATA_DIR"], exist_ok=True)

    # Imported after DATA_DIR is set. The app object is passed to uvicorn by
    # reference (not as an "app.main:app" string) so PyInstaller statically
    # follows the import and bundles the whole app package; DATA_DIR is only read
    # later, inside the app's lifespan handler.
    import uvicorn

    from app import __version__
    from app.main import app

    url = f"http://{HTTP_HOST}:{HTTP_PORT}"
    if _http_port_in_use():
        print(f"LapScope v{__version__}: HTTP port {HTTP_PORT} is already in use - "
              "another program (or a second LapScope window) has it.")
        print(f"If LapScope is already running, its dashboard is at {url} - "
              "close this window and use that one.")
        input("Press Enter to close this window...")
        return

    threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    print(f"LapScope v{__version__} starting - dashboard at {url}")
    print(f"Recording telemetry to {os.environ['DATA_DIR']}")
    uvicorn.run(app, host=HTTP_HOST, port=HTTP_PORT, log_level="info")


if __name__ == "__main__":
    main()
