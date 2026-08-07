"""Track catalogue (app/tracks.py): download, validate, persist, hot-reload,
and the fingerprint match that turns an entry into a route name.

No network: ``tracks.SOURCE_URL`` is pointed at ``file://`` URLs, which
``urllib.request.urlopen`` serves natively — the exact code path minus the
socket. Same zero-dependency footprint as the rest of the tests.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import tracks
from app.recorder.store import Store


def _catalog(n: int = 25, **override) -> dict:
    """A plausible catalogue, big enough to clear MIN_ENTRIES. Start points are
    10 km apart so no two entries can match the same lap."""
    return {"version": tracks.CATALOG_VERSION, "game": "fh6", "routes": [
        {"key": f"test-{i}", "name": f"Test Route {i}", "kind": "sprint",
         "start": [100000.0 + i * 10000, 200000.0], "length": 5951.0,
         "span": [800.0, 900.0]}
        for i in range(n)
    ], **override}


GOOD = _catalog()


@pytest.fixture()
def track_state(tmp_path, monkeypatch):
    """Point the module at a scratch DATA_DIR and restore the pristine
    bundled-only state afterwards (tracks.py is module-global on purpose)."""
    tracks.load(str(tmp_path))
    yield tmp_path
    monkeypatch.setattr(tracks, "_data_dir", None)
    tracks.load()


def _source(tmp_path, payload, monkeypatch, name="upstream.json") -> Path:
    src = tmp_path / name
    src.write_text(payload if isinstance(payload, str) else json.dumps(payload),
                   encoding="utf-8")
    monkeypatch.setattr(tracks, "SOURCE_URL", src.as_uri())
    return src


def _bundled_entry(kind: str | None = None) -> dict:
    """An entry from the shipped catalogue - picked dynamically so these tests
    don't break when a track is renamed or the export is regenerated."""
    entries = sorted(tracks.TRACKS.values(), key=lambda e: e["key"])
    return next(e for e in entries if kind is None or e["kind"] == kind)


def _fingerprint(entry: dict) -> tuple[float, float, float, float, float]:
    return (entry["start_x"], entry["start_z"], entry["length"],
            entry["span_x"], entry["span_z"])


# ---------- refresh layer ----------

def test_refresh_downloads_persists_and_hot_swaps(track_state, monkeypatch):
    _source(track_state, GOOD, monkeypatch)
    bundled = _bundled_entry()
    assert "test-0" not in tracks.TRACKS

    total, added = tracks.refresh()

    assert tracks.TRACKS["test-0"]["name"] == "Test Route 0"  # no restart needed
    assert added == len(GOOD["routes"])
    assert total == len(tracks.TRACKS)
    # persisted under DATA_DIR so the next start picks it up offline
    on_disk = json.loads((track_state / tracks.DOWNLOAD_NAME).read_text(encoding="utf-8"))
    assert on_disk == GOOD
    # bundled entries survive: the download overlays, never replaces
    assert bundled["key"] in tracks.TRACKS


def test_refresh_overlays_by_key_and_recounts(track_state, monkeypatch):
    """A corrected name for a known key wins over the bundled one; a second
    refresh with the same payload adds nothing."""
    bundled = _bundled_entry()
    fixed = dict(bundled, name="Corrected Name")
    payload = _catalog()
    payload["routes"].append({
        "key": fixed["key"], "name": fixed["name"], "kind": fixed["kind"],
        "start": [fixed["start_x"], fixed["start_z"]], "length": fixed["length"],
        "span": [fixed["span_x"], fixed["span_z"]]})
    _source(track_state, payload, monkeypatch)

    _, added_first = tracks.refresh()
    assert added_first == len(GOOD["routes"])  # the bundled key was already known
    assert tracks.TRACKS[fixed["key"]]["name"] == "Corrected Name"

    _, added_again = tracks.refresh()
    assert added_again == 0


@pytest.mark.parametrize("payload", [
    "{not json",                                             # unparseable
    json.dumps(_catalog()["routes"]),                        # wrong shape
    json.dumps(_catalog(2)),                                 # truncated
    json.dumps(_catalog(version=99)),                        # newer schema
    json.dumps(_catalog(routes=[{**r, "kind": "hillclimb"}   # unknown kind
                                for r in _catalog()["routes"]])),
    json.dumps(_catalog(routes=[{**r, "name": ""}            # empty name
                                for r in _catalog()["routes"]])),
    json.dumps(_catalog(routes=[{**r, "span": [0.0, 900.0]}  # degenerate box
                                for r in _catalog()["routes"]])),
    json.dumps(_catalog(routes=[{**r, "key": ""}             # keyless entry
                                for r in _catalog()["routes"]])),
    json.dumps(_catalog(routes=[dict(r, key="same")          # duplicate keys
                                for r in _catalog()["routes"]])),
])
def test_refresh_rejects_bad_payloads(track_state, monkeypatch, payload):
    _source(track_state, payload, monkeypatch)
    before = dict(tracks.TRACKS)

    with pytest.raises(tracks.RefreshError):
        tracks.refresh()

    assert tracks.TRACKS == before                            # state untouched
    assert not (track_state / tracks.DOWNLOAD_NAME).exists()   # nothing persisted


def test_refresh_download_failure_keeps_current_catalogue(track_state, monkeypatch):
    monkeypatch.setattr(tracks, "SOURCE_URL",
                        (track_state / "no-such-file.json").as_uri())
    before = dict(tracks.TRACKS)
    with pytest.raises(tracks.RefreshError):
        tracks.refresh()
    assert tracks.TRACKS == before


def test_refresh_persist_failure_is_a_refresh_error(track_state, monkeypatch):
    """A filesystem failure while persisting the validated catalogue must
    surface as RefreshError - the endpoint's readable 502 - not escape as a
    bare 500 (same contract as the car list, issue #42)."""
    _source(track_state, GOOD, monkeypatch)
    monkeypatch.setattr(tracks, "_data_dir", str(track_state / "gone" / "deeper"))
    before = dict(tracks.TRACKS)

    with pytest.raises(tracks.RefreshError):
        tracks.refresh()

    assert tracks.TRACKS == before


def test_load_ignores_corrupt_downloaded_copy(track_state):
    (track_state / tracks.DOWNLOAD_NAME).write_text("{corrupt", encoding="utf-8")
    tracks.load(str(track_state))
    assert _bundled_entry()["key"] in tracks.TRACKS  # bundled layer still resolves


# ---------- matching ----------

def test_bundled_catalogue_is_unambiguous():
    """Every shipped entry must be identifiable by its own fingerprint. If two
    entries were close enough to both match, `match` returns None and both
    courses would go unnamed - `tools/export_track_catalog.py` refuses to emit
    such a pair, and this is the check that it stays that way."""
    for entry in tracks.TRACKS.values():
        assert tracks.match(*_fingerprint(entry)) is entry, entry["name"]


def test_match_returns_none_rather_than_guessing(track_state, monkeypatch):
    """A hand-edited or downgraded overlay could put two courses on one
    fingerprint. An unnamed route beats a confidently wrong name."""
    twins = _catalog()
    twins["routes"][1]["start"] = list(twins["routes"][0]["start"])
    twins["routes"][1]["span"] = list(twins["routes"][0]["span"])
    _source(track_state, twins, monkeypatch)
    tracks.refresh()

    assert tracks.match(100000.0, 200000.0, 5951.0, 800.0, 900.0) is None
    # ...while the untouched entries still resolve
    assert tracks.match(120000.0, 200000.0, 5951.0, 800.0, 900.0)["key"] == "test-2"


def test_match_uses_the_recorder_tolerances(track_state, monkeypatch):
    """Laps of one course drift; the catalogue has to accept the same drift the
    route fingerprint does, or a course would be recognized on one lap and not
    the next."""
    _source(track_state, GOOD, monkeypatch)
    tracks.refresh()
    base = tracks.TRACKS["test-0"]

    drifted = tracks.match(base["start_x"] + 100, base["start_z"] - 60,
                           base["length"] * 1.02,
                           base["span_x"] * 1.03, base["span_z"] * 0.97)
    assert drifted is base
    # a different course off the same start line is still refused
    assert tracks.match(base["start_x"], base["start_z"], base["length"],
                        base["span_x"] * 2, base["span_z"]) is None


# ---------- naming routes ----------

def test_route_is_named_when_it_is_created(tmp_path):
    """The whole point: a fresh install's first completed lap on an official
    course produces a route that already carries its name."""
    entry = _bundled_entry(kind="circuit")
    store = Store(str(tmp_path / "telemetry.db"))

    # the recorder's own guess is the weak one; the catalogue promotes it
    rid = store.match_or_create_route(*_fingerprint(entry), kind="sprint")

    row = store.get_route(rid)
    assert row["name"] == entry["name"]
    assert row["catalog_key"] == entry["key"]
    assert row["kind"] == "circuit"
    store.close()


def test_catalogue_never_downgrades_a_lapped_course(tmp_path):
    """`kind` still goes through the promotion ladder: a catalogue entry marked
    sprint can't unmake a course the recorder has watched being lapped."""
    entry = _bundled_entry(kind="sprint")
    store = Store(str(tmp_path / "telemetry.db"))

    rid = store.match_or_create_route(*_fingerprint(entry), kind="circuit")

    assert store.get_route(rid)["kind"] == "circuit"
    store.close()


def test_unknown_course_stays_unnamed(tmp_path):
    """A blueprint, a custom route, another game's map: no entry, no name. An
    unnamed route is the honest answer."""
    store = Store(str(tmp_path / "telemetry.db"))
    rid = store.match_or_create_route(999999.0, 999999.0, 4000.0, 500.0, 500.0)
    row = store.get_route(rid)
    assert row["name"] is None and row["catalog_key"] is None
    store.close()


def test_user_rename_outranks_the_catalogue(tmp_path):
    """Once a human has had an opinion about a route's name, nothing the
    catalogue does later may overwrite it - that is what clearing catalog_key
    buys, and it has to survive both the backfill and a name correction."""
    entry = _bundled_entry()
    store = Store(str(tmp_path / "telemetry.db"))
    rid = store.match_or_create_route(*_fingerprint(entry))

    store.rename_route(rid, "My Own Name")
    assert store.get_route(rid)["catalog_key"] is None

    assert store.backfill_route_names() == 0
    # another lap on the same course must not re-stamp it either
    store.match_or_create_route(*_fingerprint(entry))
    assert store.get_route(rid)["name"] == "My Own Name"
    store.close()


def test_backfill_names_routes_recorded_before_the_catalogue(tmp_path):
    """An existing database is the common case: routes were driven, the user
    never got round to naming them, and the catalogue arrives in an update."""
    entry = _bundled_entry()
    store = Store(str(tmp_path / "telemetry.db"))
    store.db.execute(
        "INSERT INTO routes (id, start_x, start_z, lap_length, span_x, span_z)"
        " VALUES (7, ?, ?, ?, ?, ?)", _fingerprint(entry))
    # no span: nothing to fingerprint with, so it has to be left alone
    store.db.execute(
        "INSERT INTO routes (id, start_x, start_z, lap_length)"
        " VALUES (8, ?, ?, ?)", _fingerprint(entry)[:3])
    store.db.commit()

    assert store.backfill_route_names() == 1
    assert store.get_route(7)["name"] == entry["name"]
    assert store.get_route(8)["name"] is None
    # idempotent: a second start changes nothing
    assert store.backfill_route_names() == 0
    store.close()


def test_legacy_route_is_named_when_it_adopts_a_span(tmp_path):
    """A pre-#53 row can't be identified until a lap gives it a shape. The
    naming runs on that path too, so the route is named the moment it becomes
    identifiable rather than waiting for the next restart."""
    entry = _bundled_entry()
    store = Store(str(tmp_path / "telemetry.db"))
    store.db.execute(
        "INSERT INTO routes (id, start_x, start_z, lap_length) VALUES (3, ?, ?, ?)",
        _fingerprint(entry)[:3])
    store.db.commit()

    assert store.match_or_create_route(*_fingerprint(entry)) == 3
    assert store.get_route(3)["name"] == entry["name"]
    store.close()


def test_catalogue_correction_reaches_routes_it_named(track_state, tmp_path,
                                                      monkeypatch):
    """A typo fixed upstream should reach installs that already took the bad
    name from the catalogue - and stop at the ones a user has renamed."""
    entry = _bundled_entry()
    store = Store(str(tmp_path / "telemetry.db"))
    auto = store.match_or_create_route(*_fingerprint(entry))
    mine = store.match_or_create_route(50000.0, 60000.0, 5951.0, 700.0, 800.0)
    store.rename_route(mine, "Mine")

    payload = _catalog()
    payload["routes"].append({
        "key": entry["key"], "name": "Corrected Name", "kind": entry["kind"],
        "start": [entry["start_x"], entry["start_z"]], "length": entry["length"],
        "span": [entry["span_x"], entry["span_z"]]})
    _source(track_state, payload, monkeypatch)
    tracks.refresh()

    assert store.backfill_route_names() == 1
    assert store.get_route(auto)["name"] == "Corrected Name"
    assert store.get_route(mine)["name"] == "Mine"
    store.close()


# ---------- endpoints ----------

def test_refresh_endpoint_maps_failure_to_502(track_state, tmp_path, monkeypatch):
    from app.api.routes import refresh_tracks, tracks_info

    store = Store(str(tmp_path / "telemetry.db"))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(store=store)))

    monkeypatch.setattr(tracks, "SOURCE_URL",
                        (track_state / "no-such-file.json").as_uri())
    with pytest.raises(HTTPException) as exc:
        asyncio.run(refresh_tracks(request))
    assert exc.value.status_code == 502
    assert tracks_info()["fetched_at"] is None  # nothing was persisted

    _source(track_state, GOOD, monkeypatch)
    out = asyncio.run(refresh_tracks(request))
    assert out["ok"] and out["added"] == len(GOOD["routes"])
    assert tracks_info() == {"total": out["total"],
                             "fetched_at": pytest.approx(
                                 (track_state / tracks.DOWNLOAD_NAME).stat().st_mtime,
                                 abs=2)}
    store.close()


def test_refresh_endpoint_names_routes_already_in_the_database(track_state, tmp_path,
                                                               monkeypatch):
    """The number the user cares about after a refresh: not how many entries
    the catalogue gained, but how many of *their* routes it just named."""
    from app.api.routes import refresh_tracks

    store = Store(str(tmp_path / "telemetry.db"))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(store=store)))
    # driven before the catalogue knew about it, so it is sitting there unnamed
    rid = store.match_or_create_route(100000.0, 200000.0, 5951.0, 800.0, 900.0)
    assert store.get_route(rid)["name"] is None

    _source(track_state, GOOD, monkeypatch)
    out = asyncio.run(refresh_tracks(request))

    assert out["named"] == 1
    assert store.get_route(rid)["name"] == "Test Route 0"
    store.close()
