"""Official track catalogue (route fingerprint -> name) with a refresh layer.

The recorder can tell that two visits were the same course (see the route
fingerprint note in ``recorder/store.py``) but it has no idea what that course
is *called* - the packet carries no track name. Naming has always been manual,
so a fresh install shows "Unnamed route" for every official event until the
user types all of them in by hand.

This module closes that gap: a bundled table of fingerprint -> name for the
game's official routes, built by ``tools/export_track_catalog.py`` from a
database where they have all been driven and named. ``Store`` consults it when
it creates a route row, so the first completed lap on The Goliath produces a
route already called "The Goliath".

Layering mirrors ``cars.py`` exactly, and for the same reason - the game keeps
adding content: bundled ``app/track_catalog.json`` < downloaded
``DATA_DIR/track_catalog.json``, keyed by the entry's stable ``key`` so an
overlay entry replaces a bundled one. ``refresh()`` pulls the maintained copy
from this repo's main branch, so new tracks reach existing installs without a
release.

The user always outranks the catalogue: naming is guarded by ``name IS NULL``
and a manual rename clears ``routes.catalog_key``, which permanently exempts
that row from catalogue updates.
"""

from __future__ import annotations

import json
import logging
import math
import os
import urllib.request
from pathlib import Path

# The fingerprint's tolerances live with the recorder that measures them;
# matching a catalogue entry has to use exactly the same predicates as matching
# an existing route, or a course could be recognized by one and not the other.
# store.py deliberately does not import this module at module scope (it imports
# it inside the naming helper), so this direction stays a plain one-way edge.
from .recorder.store import (ROUTE_KINDS, ROUTE_LENGTH_TOLERANCE,
                             ROUTE_START_RADIUS_M, _spans_match)

log = logging.getLogger("lapscope.tracks")

BUNDLED_FILE = Path(__file__).parent / "track_catalog.json"
DOWNLOAD_NAME = "track_catalog.json"
# Canonical catalogue: this repo's main branch, same as the car list. Env
# override for forks and testing.
SOURCE_URL = os.environ.get(
    "LS_TRACK_LIST_URL",
    "https://raw.githubusercontent.com/darcane/LapScope/main/app/track_catalog.json",
)
FETCH_TIMEOUT_S = 10
# Sanity bounds on a fetched catalogue - reject a truncated or absurd payload
# instead of clobbering the good local copy.
MIN_ENTRIES, MAX_ENTRIES = 20, 2000
# Bumped only for a breaking change to the entry shape; a newer file is
# rejected rather than half-read.
CATALOG_VERSION = 1
# Matches the truncation in the rename endpoint (api/routes.py RoutePatch).
MAX_NAME_LEN = 80

# Read through the module (tracks.TRACKS) or via a from-import: load() mutates
# this dict in place, so both stay live across a refresh. key -> entry.
TRACKS: dict[str, dict] = {}
_data_dir: str | None = None


class RefreshError(Exception):
    """A refresh attempt failed; the message is user-readable."""


def _entry(key: str, raw: dict) -> dict:
    """Validate one catalogue entry and normalize it. Raises ValueError."""
    name = str(raw["name"]).strip()
    if not name or len(name) > MAX_NAME_LEN:
        raise ValueError(f"bad track name {name!r}")
    kind = raw.get("kind")
    if kind is not None and kind not in ROUTE_KINDS:
        raise ValueError(f"bad kind {kind!r} for {name!r}")
    start_x, start_z = (float(v) for v in raw["start"])
    span_x, span_z = (float(v) for v in raw["span"])
    length = float(raw["length"])
    for label, v in (("start_x", start_x), ("start_z", start_z),
                     ("length", length), ("span_x", span_x), ("span_z", span_z)):
        if not math.isfinite(v):
            raise ValueError(f"non-finite {label} for {name!r}")
    if length <= 0 or span_x <= 0 or span_z <= 0:
        raise ValueError(f"non-positive length/span for {name!r}")
    return {"key": key, "name": name, "kind": kind, "start_x": start_x,
            "start_z": start_z, "length": length,
            "span_x": span_x, "span_z": span_z}


def _parse(text: str) -> dict[str, dict]:
    """Validate the on-disk shape and index it by key. Raises ValueError on
    anything off, so a bad payload never replaces a working catalogue."""
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    version = data.get("version")
    if version != CATALOG_VERSION:
        raise ValueError(f"unsupported catalogue version {version!r}")
    routes = data.get("routes")
    if not isinstance(routes, list):
        raise ValueError("expected a 'routes' list")
    if not MIN_ENTRIES <= len(routes) <= MAX_ENTRIES:
        raise ValueError(f"expected {MIN_ENTRIES}-{MAX_ENTRIES} routes, got {len(routes)}")
    out: dict[str, dict] = {}
    for raw in routes:
        key = str(raw.get("key", "")).strip()
        if not key:
            raise ValueError(f"entry without a key: {raw!r}")
        if key in out:
            raise ValueError(f"duplicate key {key!r}")
        out[key] = _entry(key, raw)
    return out


def _downloaded_file() -> Path | None:
    return None if _data_dir is None else Path(_data_dir) / DOWNLOAD_NAME


def load(data_dir: str | None = None) -> None:
    """(Re)build TRACKS: the bundled catalogue, then the downloaded copy on
    top. Fail-soft per layer - a missing/corrupt file never takes names away
    that the other layer provides. Called at import (bundled only) and from the
    app lifespan once DATA_DIR is known."""
    global _data_dir
    if data_dir is not None:
        _data_dir = data_dir
    entries: dict[str, dict] = {}
    try:
        entries.update(_parse(BUNDLED_FILE.read_text(encoding="utf-8")))
    except FileNotFoundError:
        log.info("no bundled track catalogue; routes stay unnamed until renamed")
    except Exception:
        log.warning("bundled track_catalog.json unreadable; routes stay unnamed")
    downloaded = _downloaded_file()
    if downloaded is not None and downloaded.exists():
        try:
            entries.update(_parse(downloaded.read_text(encoding="utf-8")))
        except Exception:
            log.warning("downloaded track catalogue %s unreadable; ignoring it", downloaded)
    TRACKS.clear()
    TRACKS.update(entries)


def refresh() -> tuple[int, int]:
    """Download the catalogue, validate it, persist it under DATA_DIR, and
    reload. Returns (total, added) where added counts keys that were unknown
    before. Raises RefreshError on any failure - the current in-memory and
    on-disk state is left untouched."""
    if _data_dir is None:
        raise RefreshError("no data directory configured yet")
    try:
        with urllib.request.urlopen(SOURCE_URL, timeout=FETCH_TIMEOUT_S) as resp:
            text = resp.read().decode("utf-8")
    except Exception as exc:
        raise RefreshError(f"could not download the track catalogue: {exc}") from exc
    try:
        fetched = _parse(text)
    except Exception as exc:
        raise RefreshError(f"fetched track catalogue looks wrong ({exc}); "
                           "keeping the current one") from exc
    added = len(fetched.keys() - TRACKS.keys())
    dest = _downloaded_file()
    tmp = dest.with_name(dest.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, dest)  # atomic: never leaves a half-written file behind
    except OSError as exc:
        raise RefreshError(f"could not save the track catalogue: {exc}") from exc
    load()
    log.info("track catalogue refreshed: %d routes (%d new) from %s",
             len(TRACKS), added, SOURCE_URL)
    return len(TRACKS), added


def info() -> dict:
    """Metadata for the Settings panel: catalogue size and when it was last
    refreshed (mtime of the downloaded copy; None = still on the bundled one)."""
    downloaded = _downloaded_file()
    fetched_at = None
    if downloaded is not None and downloaded.exists():
        fetched_at = int(downloaded.stat().st_mtime)
    return {"total": len(TRACKS), "fetched_at": fetched_at}


def match(start_x: float, start_z: float, lap_length: float,
          span_x: float, span_z: float) -> dict | None:
    """The official route this fingerprint identifies, or None.

    Same three predicates as `Store.match_or_create_route`: start point within
    the radius, length within tolerance of the catalogue's, and both bounding
    box dimensions in agreement. Ambiguity returns None rather than a guess -
    two entries close enough to both match means the catalogue is wrong about
    one of them, and an unnamed route is far better than a confidently wrong
    name. `tools/export_track_catalog.py` refuses to emit such a pair, so this
    should only ever fire on a hand-edited or downgraded overlay."""
    hit = None
    for entry in TRACKS.values():
        if (math.hypot(start_x - entry["start_x"], start_z - entry["start_z"])
                > ROUTE_START_RADIUS_M):
            continue
        if abs(lap_length - entry["length"]) > ROUTE_LENGTH_TOLERANCE * entry["length"]:
            continue
        if not _spans_match(span_x, span_z, entry["span_x"], entry["span_z"]):
            continue
        if hit is not None:
            log.warning("track catalogue is ambiguous at (%.1f, %.1f): %s vs %s",
                        start_x, start_z, hit["name"], entry["name"])
            return None
        hit = entry
    return hit


load()
