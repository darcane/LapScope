"""Community car-name list (CarOrdinal -> name) with a refresh layer.

Name precedence, top wins: per-user DB override (``car_names`` table) >
downloaded community list (``DATA_DIR/car_ordinals.json``) > bundled
``app/car_ordinals.json`` > ``Car #<ordinal>``. The bundled copy ships with
the build and goes stale as the game adds cars; ``refresh()`` pulls the
maintained copy from this repo's main branch so installs pick up new names
without a release. The download is persisted under DATA_DIR (the writable,
volume-mounted home for user data) and overlaid on every load.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from pathlib import Path

log = logging.getLogger("lapscope.cars")

BUNDLED_FILE = Path(__file__).parent / "car_ordinals.json"
DOWNLOAD_NAME = "car_ordinals.json"
# Canonical community list: this repo's main branch, so merged name reports
# reach every install within a day. Env override for forks and testing.
SOURCE_URL = os.environ.get(
    "LS_CAR_LIST_URL",
    "https://raw.githubusercontent.com/darcane/LapScope/main/app/car_ordinals.json",
)
FETCH_TIMEOUT_S = 10
# Sanity bounds on a fetched list - FH6 ships mid-hundreds of cars; reject a
# truncated or absurd payload instead of clobbering the good local copy.
MIN_ENTRIES, MAX_ENTRIES = 100, 10_000

# Read through the module (cars.CAR_NAMES) or via a from-import: load() mutates
# this dict in place, so both stay live across a refresh.
CAR_NAMES: dict[int, str] = {}
_data_dir: str | None = None


class RefreshError(Exception):
    """A refresh attempt failed; the message is user-readable."""


def _parse(text: str) -> dict[int, str]:
    """Validate the on-disk shape {"1987 Porsche 959": "269", ...} and invert
    it to {269: "1987 Porsche 959"}. Raises ValueError on anything off."""
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object of name -> ordinal")
    if not MIN_ENTRIES <= len(data) <= MAX_ENTRIES:
        raise ValueError(f"expected {MIN_ENTRIES}-{MAX_ENTRIES} entries, got {len(data)}")
    out: dict[int, str] = {}
    for name, ordinal in data.items():
        if not name.strip() or len(name) > 80:
            raise ValueError(f"bad car name {name!r}")
        out[int(ordinal)] = name.strip()
    return out


def _downloaded_file() -> Path | None:
    return None if _data_dir is None else Path(_data_dir) / DOWNLOAD_NAME


def load(data_dir: str | None = None) -> None:
    """(Re)build CAR_NAMES: the bundled list, then the downloaded copy on top.
    Fail-soft per layer - a missing/corrupt file never takes names away that
    the other layer provides. Called at import (bundled only) and from the
    app lifespan once DATA_DIR is known."""
    global _data_dir
    if data_dir is not None:
        _data_dir = data_dir
    names: dict[int, str] = {}
    try:
        names.update(_parse(BUNDLED_FILE.read_text(encoding="utf-8")))
    except Exception:
        log.warning("bundled car_ordinals.json missing or unreadable; "
                    "falling back to Car #<id>")
    downloaded = _downloaded_file()
    if downloaded is not None and downloaded.exists():
        try:
            names.update(_parse(downloaded.read_text(encoding="utf-8")))
        except Exception:
            log.warning("downloaded car list %s unreadable; ignoring it", downloaded)
    CAR_NAMES.clear()
    CAR_NAMES.update(names)


def refresh() -> tuple[int, int]:
    """Download the community list, validate it, persist it under DATA_DIR,
    and reload. Returns (total, added) where added counts ordinals that were
    unknown before. Raises RefreshError on any failure - the current in-memory
    and on-disk state is left untouched."""
    if _data_dir is None:
        raise RefreshError("no data directory configured yet")
    try:
        with urllib.request.urlopen(SOURCE_URL, timeout=FETCH_TIMEOUT_S) as resp:
            text = resp.read().decode("utf-8")
    except Exception as exc:
        raise RefreshError(f"could not download the car list: {exc}") from exc
    try:
        fetched = _parse(text)
    except Exception as exc:
        raise RefreshError(f"fetched car list looks wrong ({exc}); "
                           "keeping the current one") from exc
    added = len(fetched.keys() - CAR_NAMES.keys())
    dest = _downloaded_file()
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, dest)  # atomic: never leaves a half-written list behind
    load()
    log.info("car list refreshed: %d cars (%d new) from %s",
             len(CAR_NAMES), added, SOURCE_URL)
    return len(CAR_NAMES), added


def info() -> dict:
    """Metadata for the Settings panel: list size and when it was last
    refreshed (mtime of the downloaded copy; None = still on the bundled list)."""
    downloaded = _downloaded_file()
    fetched_at = None
    if downloaded is not None and downloaded.exists():
        fetched_at = int(downloaded.stat().st_mtime)
    return {"total": len(CAR_NAMES), "fetched_at": fetched_at}


load()
