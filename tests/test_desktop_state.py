"""The launcher's state machine (desktop/state.py).

Pure input -> output, so this runs anywhere - which is the point of keeping it
out of desktop/ui.py. What is actually worth testing here is not each branch in
isolation but the *order* between them: several of these conditions are true at
once in exactly the situations where showing the wrong one sends someone to fix
the wrong thing.
"""

from __future__ import annotations

from pathlib import Path

from desktop.state import (
    GOOD_PACKET_SIZE,
    STALE_S,
    LauncherState,
    ServerPhase,
    StatusSnapshot,
    derive_state,
    fmt_lap,
)

ROOT = Path(__file__).resolve().parent.parent


def snap(**kw) -> StatusSnapshot:
    """A running server with a healthy stream, overridden per test."""
    base = dict(phase=ServerPhase.RUNNING, udp_port=9999, packets_total=1000,
                last_packet_age=0.1, last_packet_size=GOOD_PACKET_SIZE)
    base.update(kw)
    return StatusSnapshot(**base)


def key(**kw) -> str:
    return derive_state(snap(**kw)).key


# --- phases: nothing the recorder reports matters until the server is up -----

def test_each_phase_has_its_own_state():
    assert derive_state(StatusSnapshot(phase=ServerPhase.STOPPED)).key == "STOPPED"
    assert derive_state(StatusSnapshot(phase=ServerPhase.STARTING)).key == "STARTING"
    assert derive_state(StatusSnapshot(phase=ServerPhase.STOPPING)).key == "STOPPING"


def test_failed_startup_shows_the_reason_it_was_given():
    s = StatusSnapshot(phase=ServerPhase.FAILED, startup_error="database is locked")
    st = derive_state(s)
    assert st.key == "FAILED" and st.tone == "error"
    assert "database is locked" in st.detail


def test_a_stopped_server_never_reports_recorder_state():
    """A stale write_error left over from the last run must not make a stopped
    server look like it is recording and failing."""
    s = StatusSnapshot(phase=ServerPhase.STOPPED, write_error="disk full",
                       session_active=True)
    assert derive_state(s).key == "STOPPED"


# --- ordering: the part with the actual decisions in it ----------------------

def test_write_error_outranks_a_healthy_looking_recording():
    """The dashboard animates happily while nothing is being saved, so this has
    to win over every state that would look fine."""
    assert key(write_error="disk full", session_active=True, session_id=7) == "NOT_STORING"


def test_write_error_outranks_a_blocked_udp_port():
    assert key(write_error="disk full", udp_error="UDP port 9999 is already in use") \
        == "NOT_STORING"


def test_udp_error_outranks_having_seen_no_packets():
    """Both are true when the port is blocked from the start. 'Waiting for
    telemetry' would send someone to check their game settings instead of the
    program holding the port."""
    assert key(udp_error="UDP port 9999 is already in use", packets_total=0) == "UDP_BLOCKED"


def test_write_error_detail_carries_the_dropped_frame_count():
    st = derive_state(snap(write_error="disk full", frames_dropped=1200))
    assert "1200 frames lost" in st.detail
    assert "resumes on its own" in st.detail


# --- the stream states -------------------------------------------------------

def test_wrong_size_packets_are_called_out():
    st = derive_state(snap(bad_packets=42, last_packet_size=311))
    assert st.key == "WRONG_PACKETS" and st.tone == "warn"
    assert "311" in st.detail and str(GOOD_PACKET_SIZE) in st.detail


def test_bad_packets_with_a_good_current_size_is_not_a_wrong_packet_state():
    """A few rejects during startup, now receiving correctly: the counter never
    resets, so keying off it alone would pin a stale warning up forever."""
    assert key(bad_packets=3, last_packet_size=GOOD_PACKET_SIZE, session_active=True) \
        == "RECORDING"


def test_no_packets_yet_explains_the_game_setup():
    st = derive_state(snap(packets_total=0, last_packet_age=None))
    assert st.key == "WAITING"
    assert "Data Out" in st.detail and "9999" in st.detail


def test_recording_names_the_session_and_best_lap():
    st = derive_state(snap(session_active=True, session_id=12, session_best=84.331))
    assert st.key == "RECORDING" and st.tone == "ok"
    assert "session 12" in st.label and "1:24.331" in st.label


def test_a_stalled_stream_mid_session_is_paused_not_idle():
    """The session is still open and resumes on the next frame. Calling this
    idle would suggest the recording had ended."""
    assert key(session_active=True, session_id=3, last_packet_age=STALE_S + 0.5) == "PAUSED"


def test_a_fresh_stream_without_a_session_is_driving():
    assert key(last_packet_age=0.05) == "DRIVING"


def test_a_stale_stream_without_a_session_is_idle():
    assert key(last_packet_age=60.0) == "IDLE"


def test_the_staleness_boundary_is_inclusive():
    assert key(session_active=True, last_packet_age=STALE_S) == "RECORDING"
    assert key(session_active=True, last_packet_age=STALE_S + 0.001) == "PAUSED"


def test_a_never_seen_packet_age_does_not_raise():
    """last_packet_age is None until the first frame ever, and every comparison
    against it has to survive that."""
    for extra in ({}, {"session_active": True}, {"bad_packets": 5}):
        assert derive_state(snap(last_packet_age=None, packets_total=5, **extra))


# --- invariants shared with the frontend -------------------------------------

def test_stale_threshold_matches_the_dashboard():
    """dashboard.js flips its chip to "paused" after 2500 ms. If these drift,
    the window and the page disagree about the same moment."""
    assert STALE_S == 2.5
    js = (ROOT / "app" / "static" / "js" / "dashboard.js").read_text(encoding="utf-8")
    assert "2500" in js, "dashboard.js no longer uses 2500 ms; STALE_S needs the same change"


def test_lap_times_format_the_way_the_pages_do():
    """fmtLap in analysis.js: m:ss.mmm, zero-padded to six characters."""
    assert fmt_lap(84.331) == "1:24.331"
    assert fmt_lap(66.5) == "1:06.500"
    assert fmt_lap(9.25) == "0:09.250"
    assert fmt_lap(None) == "—" and fmt_lap(0) == "—"


def test_every_state_says_what_it_means_in_words():
    """Tone is a hint, never the message: the label has to stand on its own for
    anyone who cannot tell the dot colours apart."""
    for s in (snap(), snap(write_error="x"), snap(udp_error="x"), snap(packets_total=0),
              snap(session_active=True), snap(last_packet_age=99.0),
              StatusSnapshot(phase=ServerPhase.FAILED)):
        st = derive_state(s)
        assert isinstance(st, LauncherState)
        assert st.label.strip() and st.tone in ("ok", "warn", "error", "neutral")
