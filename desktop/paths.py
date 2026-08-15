"""Where things live, running frozen or from source.

The rest of the app resolves its files with ``Path(__file__).parent``, which
happens to work under PyInstaller's onedir layout because the app package is
copied in whole. Assets are different: they are bundled by an explicit `datas`
entry in LapScope.spec and land under sys._MEIPASS, nowhere near this file. So
this is the repo's first (and, so far, only) frozen-path helper.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def bundle_root() -> Path:
    """Root for bundled read-only data: sys._MEIPASS when frozen (onedir puts
    that at dist/LapScope/_internal), the repo root when running from source."""
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        return Path(meipass) if meipass else Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def asset(name: str) -> Path:
    """A file from assets/. Anything read through here needs a matching `datas`
    entry in LapScope.spec or it will be missing from the exe (ARCHITECTURE.md,
    "Cross-file invariants")."""
    return bundle_root() / "assets" / name


def default_data_dir() -> str:
    """Per-user data dir: %LOCALAPPDATA%\\LapScope on Windows, ~/LapScope elsewhere.

    Stable across re-downloads of the exe, so recorded telemetry survives an
    upgrade."""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "LapScope")


def log_dir() -> Path:
    """Where the rotating log file goes. Under DATA_DIR rather than next to the
    exe: the exe may sit in a read-only or roaming location, and a bug report
    wants the log beside the database it describes."""
    return Path(os.environ.get("DATA_DIR") or default_data_dir()) / "logs"
