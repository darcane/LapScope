"""What the launcher window says, as a pure function of what the server reports.

Kept apart from ui.py on purpose: this is the part with the interesting
decisions in it (which of several simultaneous problems to lead with), and
deriving it here means it can be tested without a display. The UI just paints
whatever comes back.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

# A stream quieter than this means the game stopped sending - paused, in a
# menu, or alt-tabbed. Mirrors the 2500 ms the dashboard uses to flip its chip
# to "paused" (app/static/js/dashboard.js:472); the two must not drift or the
# window and the browser will disagree about the same moment.
STALE_S = 2.5

# FH6's "Data Out" packet. Anything else on the port is another game's format
# or a version bump, and the parser rejects it (app/telemetry/listener.py).
GOOD_PACKET_SIZE = 324


class ServerPhase(str, Enum):
    """Where the uvicorn thread is. Distinct from what the *recorder* is doing,
    which only means anything once the phase is RUNNING."""

    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    FAILED = "failed"


@dataclass(frozen=True)
class StatusSnapshot:
    """One reading of the server, mirroring GET /api/status field-for-field
    (app/api/routes.py) plus the two things only the launcher knows: which
    phase the server thread is in, and why it died if it did."""

    phase: ServerPhase = ServerPhase.STOPPED
    version: str = "0.0.0"
    udp_port: int | None = None
    udp_error: str | None = None
    packets_total: int = 0
    bad_packets: int = 0
    last_packet_age: float | None = None
    last_packet_size: int | None = None
    session_active: bool = False
    session_id: int | None = None
    session_best: float | None = None
    write_error: str | None = None
    frames_dropped: int = 0
    startup_error: str | None = None


@dataclass(frozen=True)
class LauncherState:
    """What to paint. `tone` is a name, never a colour: the UI owns the palette,
    and `label` always carries the meaning in words so the state never depends
    on a user being able to tell red from green."""

    key: str
    label: str
    tone: str  # "ok" | "warn" | "error" | "neutral"
    detail: str


def fmt_lap(secs: float | None) -> str:
    """m:ss.mmm, matching fmtLap in app/static/js/analysis.js:90 so a best lap
    reads the same in the window and on the page."""
    if not secs or secs <= 0:
        return "—"
    m = int(secs // 60)
    return f"{m}:{secs - m * 60:06.3f}"


def _fresh(s: StatusSnapshot) -> bool:
    return s.last_packet_age is not None and s.last_packet_age <= STALE_S


def _stream_detail(s: StatusSnapshot) -> str:
    bits = []
    if s.udp_port is not None:
        bits.append(f"UDP {s.udp_port}")
    bits.append(f"{s.packets_total:,} packets")
    if s.last_packet_age is not None:
        bits.append(f"last {s.last_packet_age:.1f} s ago")
    return " · ".join(bits)


def derive_state(s: StatusSnapshot) -> LauncherState:
    """The one state to show, worst first.

    Several of these can be true at once - a blocked UDP port and a write
    failure and no packets - so the order is the order a person should act on
    them, matching how docs/wiki/Troubleshooting.md teaches diagnosis. The
    window shows one thing, not a list of warnings, because a list is how you
    get someone to read none of it.
    """
    # 1. Not running: nothing the recorder reports means anything yet.
    if s.phase is ServerPhase.STOPPED:
        return LauncherState("STOPPED", "Stopped", "neutral",
                             "The server is not running. Nothing is being recorded.")
    if s.phase is ServerPhase.STARTING:
        return LauncherState("STARTING", "Starting…", "neutral", "Opening the database.")
    if s.phase is ServerPhase.STOPPING:
        return LauncherState("STOPPING", "Stopping — saving the session in progress…",
                             "neutral", "This finishes the lap you were on. It takes a moment.")
    if s.phase is ServerPhase.FAILED:
        return LauncherState("FAILED", "Startup failed", "error",
                             s.startup_error or "The server stopped unexpectedly. See the log below.")

    # 2. Recording into a database that won't take it. Above the UDP check
    #    because packets arriving is the thing that makes this urgent: the
    #    dashboard looks completely healthy while the drive is being lost.
    if s.write_error:
        lost = f" {s.frames_dropped} frames lost so far." if s.frames_dropped else ""
        return LauncherState(
            "NOT_STORING", "Recording — NOT saving", "error",
            f"The database write failed: {s.write_error}.{lost} "
            "Free up disk space; recording resumes on its own.")

    # 3. No telemetry can arrive at all until this is cleared. Not fatal - past
    #    sessions still open in the browser - so it reads as a problem with the
    #    recording, not with LapScope.
    if s.udp_error:
        return LauncherState("UDP_BLOCKED", "Telemetry port blocked", "error", s.udp_error)

    # 4. Something is on the port, but it isn't FH6 Data Out. Usually another
    #    game left configured, occasionally a game update changing the packet.
    if s.bad_packets > 0 and s.last_packet_size not in (None, GOOD_PACKET_SIZE):
        return LauncherState(
            "WRONG_PACKETS", "Wrong-size packets", "warn",
            f"{s.bad_packets:,} packets on UDP {s.udp_port} were not FH6 Data Out "
            f"({s.last_packet_size} bytes, expected {GOOD_PACKET_SIZE}). "
            "Check which game is sending, and that it is set to the right format.")

    # 5. Serving fine, nothing has ever arrived: the setup isn't finished. This
    #    is the state a first-time user sits in, so the detail is instructions.
    if s.packets_total == 0:
        return LauncherState(
            "WAITING", "Waiting for telemetry", "warn",
            f"In Forza Horizon 6: Settings → HUD and Gameplay → Data Out ON, "
            f"IP 127.0.0.1, Port {s.udp_port}.")

    if s.session_active:
        if _fresh(s):
            best = f", best {fmt_lap(s.session_best)}" if s.session_best else ""
            return LauncherState("RECORDING", f"Recording — session {s.session_id}{best}",
                                 "ok", _stream_detail(s))
        # 7. Mid-session and the stream stopped. The session is still open and
        #    resumes on the next frame, so this is a note, not a failure.
        return LauncherState("PAUSED", "Paused — game not sending", "warn", _stream_detail(s))

    if _fresh(s):
        return LauncherState("DRIVING", "Connected — driving", "ok", _stream_detail(s))

    # 9. Connected, has seen packets, none lately and no session open: sitting
    #    in a menu. Named so it doesn't read as a fault - FH6 genuinely stops
    #    sending, and people ask why it says "no data" when nothing is wrong.
    return LauncherState("IDLE", "Idle — Forza only sends while you drive", "neutral",
                         _stream_detail(s))
