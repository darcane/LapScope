"""REST API: stream status, recorded sessions, laps, routes, cars, lap channel data."""

from __future__ import annotations

import logging
import math
import time

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from .. import __version__, cars
from ..cars import CAR_NAMES
from ..recorder.laps import (AIRBORNE_MIN_S, AIRBORNE_SLIP_MAX,
                             AIRBORNE_SUSP_MAX, IMPACT_ACCEL, LANDING_GRACE_S)
from ..recorder.reprocess import reprocess_session
from ..telemetry.packet import parse

log = logging.getLogger("lapscope.api")
router = APIRouter()

# FH6 CarClass indices; 6 = R (new class, 901-998 PI), 7 = X (999 only).
# Verified on a real R-class car: PI 998 reports CarClass 6.
CAR_CLASSES = ["D", "C", "B", "A", "S1", "S2", "R", "X"]
CONDITIONS = {"dry", "wet", "snow"}
TRACK_TYPES = {"road", "street", "touge", "dirt", "cross", "drag", "wtc"}
DRIVETRAINS = ["FWD", "RWD", "AWD"]

# channel name -> extractor over a parsed frame; used by /laps/{id}/data
CHANNELS = {
    "speed_kmh": lambda p: p["speed"] * 3.6,
    "rpm": lambda p: p["current_engine_rpm"],
    "gear": lambda p: p["gear"],
    "throttle": lambda p: p["accel"] / 2.55,
    "brake": lambda p: p["brake"] / 2.55,
    "steer": lambda p: p["steer"] / 1.27,
    "lat_g": lambda p: p["accel_x"] / 9.80665,
    "lon_g": lambda p: p["accel_z"] / 9.80665,
    "slip_front": lambda p: (p["tire_combined_slip"][0] + p["tire_combined_slip"][1]) / 2,
    "slip_rear": lambda p: (p["tire_combined_slip"][2] + p["tire_combined_slip"][3]) / 2,
    "slip_max": lambda p: max(p["tire_combined_slip"]),
    "boost": lambda p: p["boost"],
    # falls back to time-since-lap-start when the lap clock never ran (WTA /
    # bare sprints keep CurrentLap at 0) - see the post-pass in lap_data()
    "lap_time": lambda p: p["current_lap"],
    "pos_x": lambda p: p["pos_x"],
    "pos_y": lambda p: p["pos_y"],   # elevation (world up-axis, meters)
    "pos_z": lambda p: p["pos_z"],
}


def _class_letter(v) -> str:
    return CAR_CLASSES[v] if isinstance(v, int) and 0 <= v < len(CAR_CLASSES) else "?"


def _car_name(ordinal, override=None) -> str:
    return override or CAR_NAMES.get(ordinal) or f"Car #{ordinal}"


def _session_out(row: dict) -> dict:
    row["car_class_letter"] = _class_letter(row.get("car_class"))
    override = row.pop("car_name_override", None)
    row["car_name"] = _car_name(row.get("car_ordinal"), override)
    row["car_known"] = override is not None or row.get("car_ordinal") in CAR_NAMES
    # conditions / track_type stay None until tagged (or auto-detected):
    # defaulting them to dry/road made every untagged session look tagged
    dt = row.get("drivetrain_type")
    row["drivetrain"] = DRIVETRAINS[dt] if isinstance(dt, int) and 0 <= dt < 3 else "?"
    started = time.strftime("%Y-%m-%d %H:%M", time.localtime(row["started_at"]))
    row["display_name"] = row.get("name") or row.get("route_name") or started
    return row


@router.get("/status")
async def status(request: Request):
    hub = request.app.state.hub
    tracker = request.app.state.tracker
    now = time.time()
    return {
        "version": __version__,
        "udp_port": request.app.state.udp_port,
        "udp_error": getattr(request.app.state, "udp_error", None),
        "packets_total": hub.packets_total,
        "bad_packets": hub.bad_packets,
        "last_packet_age": None if hub.last_packet_time is None else round(now - hub.last_packet_time, 3),
        "last_packet_size": hub.last_packet_size,
        "session_active": tracker.session_id is not None,
        "session_id": tracker.session_id,
        "session_best": tracker.best_lap_time,
    }


@router.get("/version")
def version():
    """The running app version. The frontend compares this against the latest
    GitHub Release (client-side) to surface a dismissible update notice.
    "0.0.0" marks an unversioned dev/source run and suppresses the check."""
    return {"version": __version__}


@router.get("/sessions")
def sessions(request: Request):
    return [_session_out(s) for s in request.app.state.store.list_sessions()]


class SessionPatch(BaseModel):
    name: str | None = None
    conditions: str | None = None
    track_type: str | None = None


@router.patch("/sessions/{session_id}")
def patch_session(session_id: int, body: SessionPatch, request: Request):
    store = request.app.state.store
    if store.get_session(session_id) is None:
        raise HTTPException(404, "session not found")
    if body.name is not None:  # "" clears back to the route/date fallback
        store.rename_session(session_id, body.name.strip()[:80] or None)
    if body.conditions is not None:  # "" clears the tag back to not-set
        if body.conditions and body.conditions not in CONDITIONS:
            raise HTTPException(400, f"conditions must be one of {sorted(CONDITIONS)}")
        store.set_session_conditions(session_id, body.conditions or None)
    if body.track_type is not None:
        if body.track_type and body.track_type not in TRACK_TYPES:
            raise HTTPException(400, f"track_type must be one of {sorted(TRACK_TYPES)}")
        store.set_session_track_type(session_id, body.track_type or None)
    return {"ok": True}


@router.post("/sessions/{session_id}/reprocess")
async def reprocess(session_id: int, request: Request):
    """Rebuild the session's laps from its stored frames with the current
    detection logic. async on purpose: the replay writes laps through the
    Store's event-loop connection — which also means it blocks the loop for
    the whole replay, so it must not run while ANY session is recording
    (a long replay would freeze live telemetry and the dashboard mid-race)."""
    store = request.app.state.store
    if store.get_session(session_id) is None:
        raise HTTPException(404, "session not found")
    if request.app.state.tracker.session_id is not None:
        raise HTTPException(409, "a session is recording; retry after it ends")
    return {"ok": True, "laps": reprocess_session(store, session_id)}


@router.delete("/sessions/{session_id}")
def delete_session(session_id: int, request: Request):
    store = request.app.state.store
    if store.get_session(session_id) is None:
        raise HTTPException(404, "session not found")
    if request.app.state.tracker.session_id == session_id:
        raise HTTPException(409, "session is currently recording")
    store.delete_session(session_id)
    return {"ok": True}


class NameBody(BaseModel):
    name: str


class RoutePatch(BaseModel):
    name: str | None = None
    track_type: str | None = None


@router.patch("/routes/{route_id}")
def patch_route(route_id: int, body: RoutePatch, request: Request):
    """Rename a route and/or retag every session recorded on it in one go
    (the analysis page offers the retag when a session's type is changed -
    a route's surface doesn't change, so the tag belongs to all of them)."""
    store = request.app.state.store
    if not store.route_exists(route_id):
        raise HTTPException(404, "route not found")
    if body.name is not None:
        if not body.name.strip():
            raise HTTPException(400, "name must not be empty")
        store.rename_route(route_id, body.name.strip()[:80])
    if body.track_type is not None:  # "" clears the tag on every session
        if body.track_type and body.track_type not in TRACK_TYPES:
            raise HTTPException(400, f"track_type must be one of {sorted(TRACK_TYPES)}")
        store.set_route_sessions_track_type(route_id, body.track_type or None)
    return {"ok": True}


@router.get("/cars")
def cars_info():
    """Car-list metadata for the Settings panel: size + last refresh time
    (null while still on the bundled copy)."""
    return cars.info()


# NOTE: registered before /cars/{ordinal} so "refresh" isn't parsed as an ordinal.
@router.post("/cars/refresh")
async def refresh_cars():
    """Re-download the community car list from the repo's main branch and
    hot-swap it in (bundled copy stays as the offline fallback, per-user DB
    overrides always win). Blocking urllib fetch, hence the threadpool."""
    try:
        total, added = await run_in_threadpool(cars.refresh)
    except cars.RefreshError as exc:
        raise HTTPException(502, str(exc))
    return {"ok": True, "total": total, "added": added}


@router.get("/cars/{ordinal}")
def car_name(ordinal: int, request: Request):
    override = request.app.state.store.get_car_override(ordinal)
    return {"ordinal": ordinal, "name": _car_name(ordinal, override),
            "known": override is not None or ordinal in CAR_NAMES}


@router.patch("/cars/{ordinal}")
def set_car_name(ordinal: int, body: NameBody, request: Request):
    name = body.name.strip()
    if name:
        request.app.state.store.set_car_name(ordinal, name[:80])
    else:  # "" reverts to the bundled name (or "Car #<ordinal>")
        request.app.state.store.clear_car_name(ordinal)
    return {"ok": True}


@router.get("/sessions/{session_id}/laps")
def session_laps(session_id: int, request: Request):
    store = request.app.state.store
    session = store.get_session(session_id)
    if session is None:
        raise HTTPException(404, "session not found")
    laps = store.session_laps(session_id)
    best = min((lap["lap_time"] for lap in laps if lap["lap_time"]), default=None)
    for lap in laps:
        lap["is_best"] = bool(lap["lap_time"]) and lap["lap_time"] == best
        lap["gap_to_best"] = (lap["lap_time"] - best) if lap["lap_time"] and best else None
    return {"session": _session_out(session), "laps": laps}


@router.get("/laps/{lap_id}/data")
def lap_data(
    lap_id: int,
    request: Request,
    channels: str = Query("speed_kmh,throttle,brake"),
    max_points: int = Query(2000, ge=50, le=20000),
):
    store = request.app.state.store
    lap = store.get_lap(lap_id)
    if lap is None:
        raise HTTPException(404, "lap not found")

    names = [c.strip() for c in channels.split(",") if c.strip()]
    unknown = [n for n in names if n not in CHANNELS]
    if unknown:
        raise HTTPException(400, f"unknown channels: {unknown}; available: {sorted(CHANNELS)}")

    rows = store.lap_frames(lap)
    start_dist = lap["start_distance"] or 0.0

    # Rewind safety: when the in-game rewind scrubs DistanceTraveled backwards,
    # drop the samples it rewound over so only the finally-driven pass remains
    # (otherwise charts and the map draw the same stretch twice). This also
    # cleans up reversing after a spin.
    kept: list[tuple[float, float, dict]] = []  # (t, distance, parsed frame)
    for t, raw in rows:
        p = parse(raw)
        d = p["distance_traveled"]
        if kept and d < kept[-1][1] - 0.5:
            while kept and kept[-1][1] >= d:
                kept.pop()
        kept.append((t, d, p))

    # Collision points: ground-plane acceleration spikes past the contact
    # threshold (the same test the recorder uses for the per-lap "contact"
    # flag). A single impact spans several frames over the threshold, so group
    # consecutive over-threshold frames into one event and keep its peak. Run
    # on the full-resolution kept trace so a one-frame spike is never decimated
    # away. World coords are returned so the map projects them like any point.
    # Spikes while airborne or right after touchdown are jump landings, not
    # contact (same classification as the recorder) - tagged, not dropped, so
    # the map can still show where a jump bottomed out.
    #
    # Jump segments: the same airborne classifier also yields explicit flights
    # (every wheel unloaded for >= AIRBORNE_MIN_S). Each is returned as a
    # takeoff -> touchdown segment so the map can draw where the car left the
    # ground and where it came down; a landing-classified spike marks the
    # segment "hard" with its peak g.
    collisions: list[dict] = []
    jumps: list[dict] = []
    peak: tuple | None = None  # (g, d, frame) of the current impact burst
    burst_landing = True       # all frames of the burst classified as landing
    air_since: float | None = None
    air_start: tuple | None = None  # (t, d, frame) of the first airborne frame
    grace_until = 0.0

    def emit(peak: tuple, landing: bool) -> None:
        g0, d0, p0 = peak
        collisions.append({"x": round(p0["pos_x"], 2), "y": round(p0["pos_y"], 2),
                           "z": round(p0["pos_z"], 2), "dist": round(d0 - start_dist, 2),
                           "g": round(g0 / 9.80665, 2), "landing": landing})
        if landing and jumps:
            jumps[-1]["hard"] = True
            jumps[-1]["g"] = max(jumps[-1]["g"] or 0.0, round(g0 / 9.80665, 2))

    def emit_jump(start: tuple, land: tuple) -> None:
        (t0, d0, p0), (t1, d1, p1) = start, land
        jumps.append({"x0": round(p0["pos_x"], 2), "y0": round(p0["pos_y"], 2),
                      "z0": round(p0["pos_z"], 2), "dist0": round(d0 - start_dist, 2),
                      "x1": round(p1["pos_x"], 2), "y1": round(p1["pos_y"], 2),
                      "z1": round(p1["pos_z"], 2), "dist1": round(d1 - start_dist, 2),
                      "air_s": round(t1 - t0, 2), "hard": False, "g": None})

    for t, d, p in kept:
        airborne = (all(s < AIRBORNE_SUSP_MAX for s in p["norm_susp_travel"])
                    and all(s < AIRBORNE_SLIP_MAX for s in p["tire_combined_slip"]))
        if airborne:
            if air_since is None:
                air_since = t
                air_start = (t, d, p)
        else:
            if air_since is not None and t - air_since >= AIRBORNE_MIN_S:
                grace_until = t + LANDING_GRACE_S
                emit_jump(air_start, (t, d, p))  # this frame is the touchdown
            air_since = None
        flying = air_since is not None and t - air_since >= AIRBORNE_MIN_S
        g = math.hypot(p["accel_x"], p["accel_z"])
        if g >= IMPACT_ACCEL:
            if peak is None:
                peak, burst_landing = (g, d, p), True
            elif g > peak[0]:
                peak = (g, d, p)
            burst_landing = burst_landing and (flying or t < grace_until)
        elif peak is not None:
            emit(peak, burst_landing)
            peak = None
    if air_since is not None and kept and kept[-1][0] - air_since >= AIRBORNE_MIN_S:
        emit_jump(air_start, kept[-1])  # lap trace ended mid-flight
    if peak is not None:  # impact ran to the last kept frame
        emit(peak, burst_landing)

    stride = max(1, len(kept) // max_points)
    dist: list[float] = []
    t_rel: list[float] = []
    out: dict[str, list[float]] = {n: [] for n in names}
    t0 = kept[0][0] if kept else 0.0
    for i in range(0, len(kept), stride):
        t, d, p = kept[i]
        dist.append(round(d - start_dist, 2))
        t_rel.append(round(t - t0, 3))
        for n in names:
            out[n].append(round(CHANNELS[n](p), 4))

    # World Time Attack and bare sprints broadcast no lap clock at all
    # (CurrentLap stays 0 for the whole event), which made the A/B delta-time
    # chart a flat zero line for exactly those events. Fall back to time since
    # the lap's first frame - restart_lap re-anchors geometric laps at launch,
    # so it counts from the line like a live lap clock would.
    if "lap_time" in out and not any(v > 0.5 for v in out["lap_time"]):
        out["lap_time"] = list(t_rel)

    return {"lap": lap, "n_frames": len(rows), "dist": dist, "t": t_rel,
            "channels": out, "collisions": collisions, "jumps": jumps}
