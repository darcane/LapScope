"""API-level checks: the real endpoint functions run against a store the
harness produced. The handlers are plain functions reading
``request.app.state``, so a stub request object is enough - no HTTP server,
no httpx, same zero-dependency footprint as the rest of the tests.
"""

from __future__ import annotations

from types import SimpleNamespace

from harness import completed_laps, run, sessions


def _request_for(store):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(store=store)))


def test_lap_time_channel_falls_back_when_lap_clock_dead(tmp_path):
    """World Time Attack broadcasts CurrentLap 0 for the whole event; the
    lap_time channel must fall back to time-since-lap-start so the A/B
    delta-time chart isn't a flat zero line."""
    from app.api.routes import lap_data

    def scenario(sim):
        sim.wta(2)
        sim.race_off()

    store = run(scenario, tmp_path)
    lap = completed_laps(store, sessions(store)[0]["id"])[0]
    data = lap_data(lap["id"], _request_for(store), "lap_time,speed_kmh", 500)

    lt = data["channels"]["lap_time"]
    assert lt == data["t"]  # substituted with time since the lap's first frame
    assert lt[-1] > 10.0    # and it actually counts across the lap
    assert any(v > 1.0 for v in data["channels"]["speed_kmh"])  # others untouched


def test_lap_time_channel_kept_when_lap_clock_alive(tmp_path):
    """A circuit lap's CurrentLap is real telemetry and must pass through
    (alive channel, no zeroing, no substitution surprises)."""
    from app.api.routes import lap_data

    def scenario(sim):
        sim.event(120, "event")
        sim.race_off()

    store = run(scenario, tmp_path)
    lap = completed_laps(store, sessions(store)[0]["id"])[0]
    data = lap_data(lap["id"], _request_for(store), "lap_time", 500)
    assert any(v > 0.5 for v in data["channels"]["lap_time"])
