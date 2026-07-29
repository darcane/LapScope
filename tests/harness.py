"""Fast, game-free test harness for the recorder.

Drives the real telemetry simulator (`tools/simulator.py`) through a fake
socket straight into a real `SessionTracker` + `Store`, with the simulator's
clock stubbed out so a scenario that takes minutes over real UDP runs in
milliseconds. The packets fed to the tracker are byte-identical to what the
simulator sends over the wire, so the recorder decisions asserted in the tests
match the manual simulator test matrix documented in AGENTS.md — but without a
game, a container, or a wall-clock wait.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.recorder.laps import SessionTracker  # noqa: E402
from app.recorder.store import Store  # noqa: E402
from app.telemetry.packet import parse  # noqa: E402
import tools.simulator as simulator  # noqa: E402

RATE = 60.0


class _FastClock:
    """Stand-in for the simulator's `time` module: never actually waits, so
    the 60 Hz pacing loop and the finish-cinematic `sleep()` run instantly."""

    def sleep(self, *_):
        pass

    def monotonic(self):
        return 0.0

    def time(self):
        return 0.0


# Redirect only the simulator's clock; the recorder and everything else keep
# real time. Done once at import — the harness is test-only code.
simulator.time = _FastClock()


class FakeSocket:
    """Parses every packet the simulator "sends" and feeds it to the tracker
    with a synthetic 60 Hz arrival clock. The tracker's only wall-clock need
    is relative age checks (e.g. `t - lap_opened_t > 5`), which a steady
    per-packet counter satisfies exactly."""

    def __init__(self, tracker: SessionTracker, rate: float = RATE) -> None:
        self.tracker = tracker
        self.dt = 1.0 / rate
        self.n = 0
        self.t = 0.0

    def sendto(self, data: bytes, _addr) -> None:
        self.t = self.n * self.dt
        self.n += 1
        self.tracker.on_frame(self.t, data, parse(data))

    def close(self) -> None:
        pass


def run(scenario, tmp_path, *, rate: float = RATE, wet: bool = False,
        jumps: bool = False, seed: int = 1234) -> Store:
    """Play `scenario(sim)` through a fresh tracker + temp-file store and return
    the closed store for assertions. A fixed RNG seed keeps the simulator's
    pace/puddle jitter deterministic so lap counts and flags are stable."""
    random.seed(seed)
    simulator.JUMPS = jumps
    store = Store(str(Path(tmp_path) / "telemetry.db"))
    tracker = SessionTracker(store)
    args = SimpleNamespace(host="127.0.0.1", port=9999, rate=rate, wet=wet)
    sim = simulator.Sim(args)
    sim.sock.close()  # the real UDP socket Sim() opened; we don't use it
    sim.sock = FakeSocket(tracker, rate)
    scenario(sim)
    # the game/simulator just stops sending; the production watchdog closes the
    # session after RACE_OFF_GRACE of silence. shutdown() does the same close.
    tracker.shutdown(sim.sock.t + 1.0)
    store.close()  # checkpoint WAL and release the file for reader() + teardown
    return store


def sessions(store: Store) -> list[dict]:
    """All recorded sessions, newest first (with lap_count / best_lap joins)."""
    return store.list_sessions()


def completed_laps(store: Store, session_id: int) -> list[dict]:
    """Laps that got a real time (an open/aborted lap has lap_time = NULL)."""
    return [lap for lap in store.session_laps(session_id)
            if lap["lap_time"] is not None]


def flags_of(lap: dict) -> str:
    return lap["flags"] or ""


def route_of(store: Store, session_id: int) -> dict | None:
    """The route row a session was fingerprinted onto (incl. `kind`)."""
    route_id = store.get_session(session_id)["route_id"]
    if route_id is None:
        return None
    with store.reader() as conn:
        row = conn.execute("SELECT * FROM routes WHERE id = ?",
                           (route_id,)).fetchone()
    return dict(row) if row else None
