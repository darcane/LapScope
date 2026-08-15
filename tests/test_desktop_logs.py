"""Log plumbing for the launcher (desktop/logs.py).

The handler runs on the server thread, where nothing would catch an exception,
so the interesting property is that it cannot fail loudly no matter what the UI
is doing.
"""

from __future__ import annotations

import logging
from pathlib import Path

from desktop.logs import LOG_FORMAT, LogRing, LogSink

ROOT = Path(__file__).resolve().parent.parent


def record(msg: str) -> logging.LogRecord:
    return logging.LogRecord("lapscope.test", logging.INFO, __file__, 1, msg, None, None)


def sink_with(n: int, maxlen: int = 2000) -> LogSink:
    s = LogSink(maxlen=maxlen)
    s.setFormatter(logging.Formatter("%(message)s"))
    for i in range(n):
        s.emit(record(f"line {i}"))
    return s


def test_lines_come_back_in_order():
    assert sink_with(3).drain() == ["line 0", "line 1", "line 2"]


def test_draining_empties_the_buffer():
    s = sink_with(3)
    s.drain()
    assert s.drain() == []


def test_a_full_buffer_sheds_the_oldest_line_instead_of_raising():
    """The UI drains on a timer; a burst between two ticks must not be able to
    raise inside a log call on the recorder's thread. Shed lines are still in
    the log file, so nothing is actually lost."""
    s = sink_with(10, maxlen=4)
    assert s.drain() == ["line 6", "line 7", "line 8", "line 9"]


def test_drain_is_bounded_so_one_burst_cannot_stall_the_ui():
    s = sink_with(500)
    first = s.drain(limit=200)
    assert len(first) == 200 and first[0] == "line 0"
    assert len(s.drain(limit=200)) == 200


def test_emit_survives_a_formatter_that_raises():
    """handleError swallows it - and no-ops entirely when sys.stderr is None,
    which under a windowed build is always."""
    class Boom(logging.Formatter):
        def format(self, record):
            raise ValueError("bad format")

    s = LogSink()
    s.setFormatter(Boom())
    s.emit(record("anything"))  # must not raise
    assert s.drain() == []


# --- the pane's line budget --------------------------------------------------

def test_ring_asks_for_no_deletion_until_it_is_full():
    r = LogRing(maxlen=10)
    assert r.add(4) == 0
    assert r.add(6) == 0
    assert r.count == 10


def test_ring_trims_exactly_the_overflow():
    r = LogRing(maxlen=10)
    r.add(10)
    assert r.add(3) == 3
    assert r.count == 10


def test_ring_handles_a_single_batch_larger_than_the_whole_budget():
    r = LogRing(maxlen=10)
    assert r.add(25) == 15
    assert r.count == 10


def test_log_format_matches_the_servers_own():
    """app/main.py calls basicConfig with this format string too. It no-ops
    once the launcher has installed handlers, but Docker still runs it - if
    these drift, the same line looks different depending on how it was
    started."""
    main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert LOG_FORMAT in main, "app/main.py's log format changed; update LOG_FORMAT to match"
