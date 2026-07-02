"""REST API: stream status, recorded sessions, laps, routes, cars, lap channel data."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from ..recorder.reprocess import reprocess_session
from ..telemetry.packet import parse

log = logging.getLogger("forzacalibrator.api")
router = APIRouter()

# FH6 CarClass indices; 6 = R (new class, 901-998 PI), 7 = X (999 only).
# Verified on a real R-class car: PI 998 reports CarClass 6.
CAR_CLASSES = ["D", "C", "B", "A", "S1", "S2", "R", "X"]
CONDITIONS = {"dry", "wet", "snow", "dirt"}
TRACK_TYPES = {"road", "street", "touge", "dirt", "cross", "drag", "wtc"}
DRIVETRAINS = ["FWD", "RWD", "AWD"]

# community-maintained FH6 list, {"1987 Porsche 959": "269", ...} -> {269: name}
_CAR_FILE = Path(__file__).parent.parent / "car_ordinals.json"
try:
    CAR_NAMES: dict[int, str] = {
        int(v): k for k, v in json.loads(_CAR_FILE.read_text(encoding="utf-8")).items()
    }
except Exception:
    log.warning("car_ordinals.json missing or unreadable; falling back to Car #<id>")
    CAR_NAMES = {}

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
    row["car_name"] = _car_name(row.get("car_ordinal"), row.pop("car_name_override", None))
    row["conditions"] = row.get("conditions") or "dry"
    row["track_type"] = row.get("track_type") or "road"
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
        "udp_port": request.app.state.udp_port,
        "packets_total": hub.packets_total,
        "bad_packets": hub.bad_packets,
        "last_packet_age": None if hub.last_packet_time is None else round(now - hub.last_packet_time, 3),
        "last_packet_size": hub.last_packet_size,
        "session_active": tracker.session_id is not None,
        "session_id": tracker.session_id,
        "session_best": tracker.best_lap_time,
    }


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
    if body.name is not None and body.name.strip():
        store.rename_session(session_id, body.name.strip()[:80])
    if body.conditions is not None:
        if body.conditions not in CONDITIONS:
            raise HTTPException(400, f"conditions must be one of {sorted(CONDITIONS)}")
        store.set_session_conditions(session_id, body.conditions)
    if body.track_type is not None:
        if body.track_type not in TRACK_TYPES:
            raise HTTPException(400, f"track_type must be one of {sorted(TRACK_TYPES)}")
        store.set_session_track_type(session_id, body.track_type)
    return {"ok": True}


@router.post("/sessions/{session_id}/reprocess")
async def reprocess(session_id: int, request: Request):
    """Rebuild the session's laps from its stored frames with the current
    detection logic. async on purpose: the replay writes laps through the
    Store's event-loop connection."""
    store = request.app.state.store
    if store.get_session(session_id) is None:
        raise HTTPException(404, "session not found")
    if request.app.state.tracker.session_id == session_id:
        raise HTTPException(409, "session is currently recording")
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


@router.patch("/routes/{route_id}")
def rename_route(route_id: int, body: NameBody, request: Request):
    if not body.name.strip():
        raise HTTPException(400, "name must not be empty")
    if not request.app.state.store.rename_route(route_id, body.name.strip()[:80]):
        raise HTTPException(404, "route not found")
    return {"ok": True}


@router.get("/cars/{ordinal}")
def car_name(ordinal: int, request: Request):
    override = request.app.state.store.get_car_override(ordinal)
    return {"ordinal": ordinal, "name": _car_name(ordinal, override),
            "known": override is not None or ordinal in CAR_NAMES}


@router.patch("/cars/{ordinal}")
def set_car_name(ordinal: int, body: NameBody, request: Request):
    if not body.name.strip():
        raise HTTPException(400, "name must not be empty")
    request.app.state.store.set_car_name(ordinal, body.name.strip()[:80])
    return {"ok": True}


@router.get("/sessions/{session_id}/laps")
def session_laps(session_id: int, request: Request):
    store = request.app.state.store
    session = store.get_session(session_id)
    if session is None:
        raise HTTPException(404, "session not found")
    laps = store.session_laps(session_id)
    best = min((l["lap_time"] for l in laps if l["lap_time"]), default=None)
    for l in laps:
        l["is_best"] = bool(l["lap_time"]) and l["lap_time"] == best
        l["gap_to_best"] = (l["lap_time"] - best) if l["lap_time"] and best else None
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

    return {"lap": lap, "n_frames": len(rows), "dist": dist, "t": t_rel, "channels": out}
