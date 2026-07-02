"""Rebuild a session's laps by replaying its stored raw frames through a
fresh SessionTracker.

Recordings are lossless (every 324-byte packet is kept), so any session can
be re-segmented after a detection fix without redriving the event - e.g.
World Time Attack sessions captured before geometric lap detection existed,
or races recorded before final-lap finish detection.
"""

from __future__ import annotations

import logging

from ..telemetry.packet import parse
from .laps import RACE_OFF_GRACE, SessionTracker

log = logging.getLogger("forzacalibrator.recorder")


class _ReplayStore:
    """Store facade for replays: laps and routes are written for real, but
    the session row itself is left alone - no create/end/discard, and the
    frames are not rewritten."""

    def __init__(self, store, session_id: int) -> None:
        self._store = store
        self._sid = session_id
        self._starts = 0

    def create_session(self, started_at: float, frame: dict) -> int:
        self._starts += 1
        if self._starts > 1:
            log.warning("Replay of session %d wanted to split into a new"
                        " session; keeping everything in one", self._sid)
        return self._sid

    def add_frames(self, session_id: int, frames) -> None:
        pass

    def end_session(self, session_id: int, ended_at: float, frame_count: int,
                    conditions: str | None = None) -> None:
        pass

    def discard_session(self, session_id: int) -> None:
        pass  # never delete the real session from a replay

    def mark_session_kept(self, session_id: int) -> None:
        pass  # handled once by reprocess_session

    def __getattr__(self, name):
        # add_lap / complete_lap / restart_lap / delete_lap / routes...
        return getattr(self._store, name)


def reprocess_session(store, session_id: int) -> int:
    """Replay stored frames through current lap detection; returns the
    number of completed laps found. Existing lap rows are replaced.

    Must run on the event-loop thread: lap/route writes go through the
    Store's main connection.
    """
    frames = store.session_frames(session_id)
    if not frames:
        return 0
    store.delete_session_laps(session_id)
    tracker = SessionTracker(_ReplayStore(store, session_id))
    last_t = frames[0][0]
    for t, raw in frames:
        try:
            frame = parse(raw)
        except Exception:
            continue  # tolerate a corrupt frame rather than losing the replay
        tracker.on_frame(t, raw, frame)
        last_t = t
    tracker.shutdown(last_t + RACE_OFF_GRACE + 1.0)
    laps = sum(1 for l in store.session_laps(session_id) if l["lap_time"])
    store.mark_session_kept(session_id)  # survives cleanup even with 0 laps
    log.info("Session %d reprocessed: %d completed laps", session_id, laps)
    return laps
