"""The structural lock that keeps the launcher testable.

Every desktop module except ui.py has to stay importable without tkinter, which
is what lets the tests above run on the Linux CI box. That rule is easy to
break by accident - one convenience re-export in desktop/__init__.py would do
it - and the breakage would otherwise only show up when someone built the exe,
or never, since CI has no display to notice with.

So: import them with tkinter genuinely unavailable.
"""

from __future__ import annotations

import importlib
import sys

import pytest

TKINTER_FREE = ["desktop", "desktop.paths", "desktop.state", "desktop.logs", "desktop.server"]


class NoTkinter:
    """A meta-path finder that refuses tkinter, the way a Python built without
    it would."""

    def find_spec(self, fullname, path=None, target=None):
        if fullname == "tkinter" or fullname.startswith("tkinter."):
            raise ImportError("No module named 'tkinter' (blocked by this test)")
        return None


@pytest.fixture()
def without_tkinter():
    purged = {name: mod for name, mod in sys.modules.items()
              if name in TKINTER_FREE or name == "desktop.ui"
              or name == "tkinter" or name.startswith("tkinter.")}
    for name in purged:
        del sys.modules[name]
    blocker = NoTkinter()
    sys.meta_path.insert(0, blocker)
    try:
        yield
    finally:
        sys.meta_path.remove(blocker)
        for name in list(sys.modules):
            if name in TKINTER_FREE or name == "desktop.ui":
                del sys.modules[name]
        sys.modules.update(purged)


@pytest.mark.parametrize("name", TKINTER_FREE)
def test_module_imports_without_tkinter(without_tkinter, name):
    assert importlib.import_module(name) is not None


def test_the_blocker_actually_blocks(without_tkinter):
    """Guards the guard: if tkinter became importable through some other path,
    every test above would pass without proving anything."""
    with pytest.raises(ImportError):
        importlib.import_module("tkinter")


def test_the_package_does_not_re_export_the_window(without_tkinter):
    """desktop/__init__.py is empty on purpose. A `from .ui import ...` here
    would drag tkinter into every import above."""
    desktop = importlib.import_module("desktop")
    assert not hasattr(desktop, "LauncherWindow")
    assert "desktop.ui" not in sys.modules


def test_ui_is_the_only_module_that_imports_tkinter():
    """Read rather than imported, so this holds even where tkinter exists."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "desktop"
    offenders = [p.name for p in root.glob("*.py")
                 if p.name != "ui.py" and "import tkinter" in p.read_text(encoding="utf-8")]
    assert not offenders, f"tkinter imported outside ui.py: {offenders}"
