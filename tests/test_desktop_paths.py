"""Frozen-vs-source path resolution (desktop/paths.py).

The repo's first sys.frozen helper. Everything else finds its files with
Path(__file__).parent, which works under PyInstaller only because the app
package is copied in whole; assets are not, so they need this.
"""

from __future__ import annotations

import sys
from pathlib import Path

from desktop.paths import asset, bundle_root, default_data_dir, log_dir

ROOT = Path(__file__).resolve().parent.parent


def test_source_run_resolves_to_the_repo_root():
    assert bundle_root() == ROOT
    assert (bundle_root() / "app" / "main.py").exists()


def test_frozen_run_resolves_to_meipass(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert bundle_root() == tmp_path


def test_frozen_without_meipass_falls_back_to_the_exe_directory(monkeypatch):
    """onedir always sets _MEIPASS, but a onefile or a future PyInstaller
    change should degrade to something usable rather than raising."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    assert bundle_root() == Path(sys.executable).resolve().parent


def test_the_window_icon_is_present_in_a_source_checkout():
    """It also needs a `datas` entry in LapScope.spec, or it is missing from
    the exe - the source tree passing proves only half of that."""
    assert asset("lapscope.ico").exists()


def test_the_icon_has_a_datas_entry_in_the_spec():
    spec = (ROOT / "LapScope.spec").read_text(encoding="utf-8")
    assert "assets/lapscope.ico" in spec, "bundle the icon or the window cannot load it"


def test_data_dir_follows_localappdata(monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\someone\AppData\Local")
    assert default_data_dir().endswith("LapScope")
    assert "AppData" in default_data_dir()


def test_data_dir_falls_back_to_home_without_localappdata(monkeypatch):
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert default_data_dir().endswith("LapScope")


def test_logs_live_under_the_data_dir(monkeypatch, tmp_path):
    """Beside the database they describe, not beside the exe - which may sit
    somewhere read-only."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert log_dir() == tmp_path / "logs"
