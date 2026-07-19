"""Car-list refresh (app/cars.py): download, validate, persist, hot-reload.

No network: ``cars.SOURCE_URL`` is pointed at ``file://`` URLs, which
``urllib.request.urlopen`` serves natively — the exact code path minus the
socket. Same zero-dependency footprint as the rest of the tests.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import cars

# a plausible community list, big enough to clear MIN_ENTRIES
GOOD_LIST = {f"{2000 + i % 26} Test Car {i}": str(5000 + i) for i in range(150)}


@pytest.fixture()
def car_state(tmp_path, monkeypatch):
    """Point the module at a scratch DATA_DIR and restore the pristine
    bundled-only state afterwards (cars.py is module-global on purpose)."""
    cars.load(str(tmp_path))
    yield tmp_path
    monkeypatch.setattr(cars, "_data_dir", None)
    cars.load()


def _source(tmp_path, payload, monkeypatch, name="upstream.json") -> Path:
    src = tmp_path / name
    src.write_text(payload if isinstance(payload, str) else json.dumps(payload),
                   encoding="utf-8")
    monkeypatch.setattr(cars, "SOURCE_URL", src.as_uri())
    return src


def test_refresh_downloads_persists_and_hot_swaps(car_state, monkeypatch):
    _source(car_state, GOOD_LIST, monkeypatch)
    assert 5000 not in cars.CAR_NAMES

    total, added = cars.refresh()

    assert cars.CAR_NAMES[5000] == "2000 Test Car 0"   # visible without restart
    assert added == len(GOOD_LIST)                     # all new ordinals counted
    assert total == len(cars.CAR_NAMES)
    # persisted under DATA_DIR so the next start picks it up offline
    on_disk = json.loads((car_state / cars.DOWNLOAD_NAME).read_text(encoding="utf-8"))
    assert on_disk == GOOD_LIST
    # bundled names survive: the download overlays, never replaces
    assert 269 in cars.CAR_NAMES  # 1987 Porsche 959, from the bundled list


def test_refresh_overlays_and_recounts(car_state, monkeypatch):
    """A renamed known ordinal wins over the bundled name; a second refresh
    with the same payload adds nothing."""
    payload = dict(GOOD_LIST, **{"1987 Porsche 959 (renamed)": "269"})
    _source(car_state, payload, monkeypatch)

    _, added_first = cars.refresh()
    assert added_first == len(GOOD_LIST)  # 269 was already known
    assert cars.CAR_NAMES[269] == "1987 Porsche 959 (renamed)"

    _, added_again = cars.refresh()
    assert added_again == 0


@pytest.mark.parametrize("payload", [
    "{not json",                                          # unparseable
    json.dumps(list(GOOD_LIST)),                          # wrong shape
    json.dumps({"2020 Lone Car": "1"}),                   # truncated (< MIN_ENTRIES)
    json.dumps({f"Car {i}": "x" for i in range(150)}),    # non-numeric ordinals
    json.dumps({"": "1", **GOOD_LIST}),                   # empty name
])
def test_refresh_rejects_bad_payloads(car_state, monkeypatch, payload):
    _source(car_state, payload, monkeypatch)
    before = dict(cars.CAR_NAMES)

    with pytest.raises(cars.RefreshError):
        cars.refresh()

    assert cars.CAR_NAMES == before                          # state untouched
    assert not (car_state / cars.DOWNLOAD_NAME).exists()     # nothing persisted


def test_refresh_download_failure_keeps_current_list(car_state, monkeypatch):
    monkeypatch.setattr(cars, "SOURCE_URL",
                        (car_state / "no-such-file.json").as_uri())
    before = dict(cars.CAR_NAMES)
    with pytest.raises(cars.RefreshError):
        cars.refresh()
    assert cars.CAR_NAMES == before


def test_refresh_persist_failure_is_a_refresh_error(car_state, monkeypatch):
    """A filesystem failure while persisting the validated list (disk full,
    permissions, DATA_DIR gone) must surface as RefreshError - the endpoint's
    readable 502 - not escape as a bare 500 (issue #42). The in-memory list
    stays untouched."""
    _source(car_state, GOOD_LIST, monkeypatch)
    monkeypatch.setattr(cars, "_data_dir", str(car_state / "gone" / "deeper"))
    before = dict(cars.CAR_NAMES)

    with pytest.raises(cars.RefreshError):
        cars.refresh()

    assert cars.CAR_NAMES == before


def test_load_ignores_corrupt_downloaded_copy(car_state):
    (car_state / cars.DOWNLOAD_NAME).write_text("{corrupt", encoding="utf-8")
    cars.load(str(car_state))
    assert 269 in cars.CAR_NAMES  # bundled layer still resolves


def test_refresh_endpoint_maps_failure_to_502(car_state, monkeypatch):
    from app.api.routes import cars_info, refresh_cars

    monkeypatch.setattr(cars, "SOURCE_URL",
                        (car_state / "no-such-file.json").as_uri())
    with pytest.raises(HTTPException) as exc:
        asyncio.run(refresh_cars())
    assert exc.value.status_code == 502

    assert cars_info()["fetched_at"] is None  # nothing was persisted

    _source(car_state, GOOD_LIST, monkeypatch)
    out = asyncio.run(refresh_cars())
    assert out["ok"] and out["added"] == len(GOOD_LIST)
    assert cars_info() == {"total": out["total"],
                           "fetched_at": pytest.approx(
                               (car_state / cars.DOWNLOAD_NAME).stat().st_mtime, abs=2)}
