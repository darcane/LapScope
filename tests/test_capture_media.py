"""tools/capture_media.py - the pure-logic half (no Playwright, no Pillow, no browser).

The capture itself is proven end to end by hand against simulator data
(docs/MEDIA.md); what is tested here is everything that decides *whether and
what* to capture: the manifest, the refusals, the replay, the budget, the atomic
write and the read-only snapshot.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import capture_media as cm  # noqa: E402

DELETED = ["accent-analysis-violet.png", "hero.png", "live-map-jumps.png",
           "live-map-pause-kept.png", "map-zoom-highlight.png",
           "track-map-3d.png", "track-map-jumps.png"]
SHOT_LIST = ["hero.gif", "analysis-compare.png", "track-map.png", "track-map-3d-jumps.png",
             "track-map-3d-animated.gif", "session-list.png", "session-sidebar.png",
             "settings.png", "accent-live-sunset.png", "paused-chip.png",
             "browse-bar-menu.png", "csv-export-choice.png", "launcher-window.png"]


def shot(output="a.png", kind="still", session=1, laps=(), **kw):
    return {"output": output, "kind": kind, "session": session, "laps": list(laps),
            "viewport": {"width": 800, "height": 600}, **kw}


def write_manifest(tmp_path, shots):
    p = tmp_path / "m.json"
    p.write_text(json.dumps({"shots": shots}), encoding="utf-8")
    return p


def make_db(path: Path, wal: bool = False) -> Path:
    conn = sqlite3.connect(path)
    if wal:
        conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE sessions (id INTEGER PRIMARY KEY, started_at REAL);
        CREATE TABLE laps (id INTEGER PRIMARY KEY, session_id INTEGER);
        CREATE TABLE frames (id INTEGER PRIMARY KEY, session_id INTEGER, t REAL, raw BLOB);
        INSERT INTO sessions VALUES (1, 0), (2, 0);
        INSERT INTO laps VALUES (10, 1), (11, 1), (20, 2);
    """)
    conn.commit()
    conn.close()
    return path


def add_frames(path: Path, session, rows):
    conn = sqlite3.connect(path)
    conn.executemany("INSERT INTO frames (session_id, t, raw) VALUES (?, ?, ?)",
                     [(session, t, raw) for t, raw in rows])
    conn.commit()
    conn.close()


def ids(path: Path):
    conn = sqlite3.connect(path)
    try:
        return cm.collect_db_ids(conn)
    finally:
        conn.close()


# --- manifest ----------------------------------------------------------------
def test_shipped_manifest_parses():
    shots = cm.load_manifest(ROOT / "tools" / "media_shots.json")
    outputs = [s["output"] for s in shots]
    assert sorted(outputs) == sorted(SHOT_LIST)
    assert len(set(outputs)) == len(outputs)
    assert "fh6-settings.png" not in outputs
    for s in shots:
        for i in cm.shot_ids(s):
            assert i == cm.SENTINEL, s["output"]
        # every windowless shot has a capture recipe
        assert s["kind"] == "window" or Path(s["output"]).stem in cm.RECIPES, s["output"]
    assert "_comment" in json.loads((ROOT / "tools" / "media_shots.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("bad,needle", [
    (shot("x.png", kind="video"), "x.png"),
    ({"kind": "still", "session": 1, "laps": [], "viewport": {"width": 1, "height": 1}}, "missing 'output'"),
    (shot("sub/x.png"), "sub/x.png"),
    (shot("sub\\x.png"), "x.png"),
    ({**shot("x.png"), "laps": "nope"}, "x.png"),
    ({"output": "x.png", "kind": "still", "laps": [], "viewport": {"width": 1, "height": 1}}, "session"),
])
def test_manifest_rejects_bad_shape(tmp_path, bad, needle):
    with pytest.raises(cm.ManifestError, match="shot") as e:
        cm.load_manifest(write_manifest(tmp_path, [bad]))
    assert needle in str(e.value)


def test_manifest_rejects_duplicate_outputs(tmp_path):
    with pytest.raises(cm.ManifestError, match="dup.png.*duplicate"):
        cm.load_manifest(write_manifest(tmp_path, [shot("dup.png"), shot("dup.png")]))


def test_manifest_rejects_unreadable_file(tmp_path):
    with pytest.raises(cm.ManifestError):
        cm.load_manifest(tmp_path / "missing.json")


# --- validation --------------------------------------------------------------
def test_placeholder_shot_refused_others_proceed(tmp_path):
    db = make_db(tmp_path / "t.db")
    shots = [shot("bad.png", session=cm.SENTINEL), shot("badlap.png", laps=[cm.SENTINEL]),
             shot("good.png", session=1, laps=[10, 11])]
    v = cm.validate_shots(shots, ids(db))
    assert not v["bad.png"].ok and "bad.png" in v["bad.png"].reason
    assert not v["badlap.png"].ok and "badlap.png" in v["badlap.png"].reason
    assert v["good.png"].ok


def test_missing_session_and_lap_id_refused(tmp_path):
    db = make_db(tmp_path / "t.db")
    v = cm.validate_shots([shot("s.png", session=99), shot("l.png", session=1, laps=[10, 4242])], ids(db))
    assert not v["s.png"].ok and "s.png" in v["s.png"].reason and "99" in v["s.png"].reason
    assert not v["l.png"].ok and "l.png" in v["l.png"].reason and "4242" in v["l.png"].reason


def test_window_shot_needs_no_ids(tmp_path):
    db = make_db(tmp_path / "t.db")
    s = {"output": "launcher-window.png", "kind": "window"}
    assert cm.load_manifest(write_manifest(tmp_path, [s]))
    assert cm.validate_shots([s], ids(db))["launcher-window.png"].ok


# --- db path / abort ---------------------------------------------------------
def test_no_db_aborts_naming_path(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    expected = Path(tmp_path) / "LapScope" / "telemetry.db"
    with pytest.raises(cm.AbortRun) as e:
        cm.resolve_db_path(None)
    assert str(expected) in str(e.value)

    def boom(*a, **k):
        raise AssertionError("server/browser started before the DB check")
    monkeypatch.setattr(cm, "CaptureServer", boom)
    monkeypatch.setattr(cm, "snapshot_db", boom)
    monkeypatch.setattr(cm, "_require_playwright", boom)
    assert cm.main(["--manifest", str(ROOT / "tools" / "media_shots.json"),
                    "--out", str(tmp_path / "out")]) == 2
    assert str(expected) in capsys.readouterr().err


def test_explicit_db_wins(tmp_path):
    db = make_db(tmp_path / "x.db")
    assert cm.resolve_db_path(str(db)) == db
    with pytest.raises(cm.AbortRun, match="nope.db"):
        cm.resolve_db_path(str(tmp_path / "nope.db"))


def test_only_unknown_name_aborts(tmp_path):
    db = make_db(tmp_path / "x.db")
    with pytest.raises(cm.AbortRun, match="nothing.png"):
        cm.run(cm.build_parser().parse_args(["--db", str(db), "--only", "nothing.png"]))


def test_all_refused_run_exits_nonzero_without_server(tmp_path, monkeypatch, capsys):
    db = make_db(tmp_path / "x.db")

    def boom(*a, **k):
        raise AssertionError("server started for a run with nothing runnable")
    monkeypatch.setattr(cm, "CaptureServer", boom)
    rc = cm.main(["--db", str(db), "--out", str(tmp_path / "out"),
                  "--only", "hero.gif", "paused-chip.png"])
    out = capsys.readouterr().out
    assert rc == 1 and "hero.gif" in out and "refused" in out
    assert not (tmp_path / "out").exists() or not list((tmp_path / "out").iterdir())


# --- replay ------------------------------------------------------------------
def test_replay_reads_in_t_order_and_window(tmp_path):
    db = make_db(tmp_path / "t.db")

    def blob(n):
        return bytes([n]) * 324
    add_frames(db, 1, [(103.0, blob(3)), (100.0, blob(0)), (101.0, blob(1)),
                       (101.5, b"short"), (102.0, blob(2)), (110.0, blob(9))])
    add_frames(db, 2, [(5.0, blob(7))])
    conn = sqlite3.connect(db)
    allf = list(cm.read_replay(conn, 1, 0, None))
    assert [t for t, _ in allf] == [100.0, 101.0, 102.0, 103.0, 110.0]   # sorted, short skipped
    assert all(len(r) == 324 for _, r in allf)
    win = list(cm.read_replay(conn, 1, 1.0, 3.0))
    assert [t for t, _ in win] == [101.0, 102.0, 103.0]
    assert list(cm.read_replay(conn, 99, 0, None)) == []
    conn.close()


def test_pace_uses_stored_deltas():
    frames = [(10.0, b""), (10.02, b""), (10.01, b""), (14.0, b""), (14.01, b"")]
    d = list(cm.pace(frames))
    assert d[0] == 0.0
    assert d[1] == pytest.approx(0.02)
    assert d[2] == 0.0                       # negative clamped
    assert d[3] == cm.GAP_CAP_S              # gap capped
    assert d[4] == pytest.approx(0.01)
    assert list(cm.pace([])) == []


# --- budget ------------------------------------------------------------------
def test_gif_ladder_order_and_budget():
    ladder = cm.pick_gif_ladder(fps=12, scale=1.0, colors=256)
    assert ladder[0] == {"fps": 12, "scale": 1.0, "colors": 256}
    kinds = []
    for prev, cur in zip(ladder, ladder[1:]):
        changed = [k for k in ("fps", "scale", "colors") if cur[k] != prev[k]]
        assert len(changed) == 1 and cur[changed[0]] < prev[changed[0]]
        kinds.append(changed[0])
    assert kinds == sorted(kinds, key=["fps", "scale", "colors"].index)   # fps, then scale, then colors
    assert set(kinds) == {"fps", "scale", "colors"}
    assert cm.fits_budget(10 * 1024 * 1024)
    assert not cm.fits_budget(10 * 1024 * 1024 + 1)


def test_ladder_with_nothing_to_lower_is_one_rung():
    assert cm.pick_gif_ladder(fps=6, scale=0.5, colors=64) == [{"fps": 6, "scale": 0.5, "colors": 64}]


# --- ports -------------------------------------------------------------------
def test_free_port_excludes_reserved():
    for _ in range(50):
        a, b = cm.pick_free_ports(2)
        assert a != b
        for p in (a, b):
            assert p not in (8000, 9999) and not 5200 <= p <= 5300
    assert {8000, 9999, 5200, 5250, 5300} <= cm.RESERVED_PORTS


# --- atomic write ------------------------------------------------------------
def test_atomic_write_keeps_existing_on_failure(tmp_path):
    target = tmp_path / "out" / "a.png"
    target.parent.mkdir()
    target.write_bytes(b"OLD")

    def failing(tmp):
        tmp.write_bytes(b"partial")
        raise RuntimeError("capture died")
    with pytest.raises(RuntimeError):
        cm.atomic_write(target, failing)
    assert target.read_bytes() == b"OLD"
    assert [p.name for p in target.parent.iterdir()] == ["a.png"]       # temp removed

    assert cm.atomic_write(target, lambda tmp: tmp.write_bytes(b"NEW")) == 3
    assert target.read_bytes() == b"NEW"
    assert [p.name for p in target.parent.iterdir()] == ["a.png"]


def test_atomic_write_rejects_empty_output(tmp_path):
    target = tmp_path / "a.png"
    target.write_bytes(b"OLD")
    with pytest.raises(RuntimeError):
        cm.atomic_write(target, lambda tmp: tmp.write_bytes(b""))
    assert target.read_bytes() == b"OLD"


def test_failed_shot_reported_and_run_continues(tmp_path):
    out = tmp_path / "out"
    ctx = {"scratch": tmp_path, "get_browser": lambda: None,
           "server": lambda: None, "conn": lambda: None}
    r = cm.run_shot(shot("unknown-recipe.png"), out, ctx)
    assert r.status == "failed" and "unknown-recipe" in r.detail
    assert not (out / "unknown-recipe.png").exists()


# --- snapshot ----------------------------------------------------------------
def test_source_db_untouched_by_snapshot(tmp_path):
    src = make_db(tmp_path / "src.db", wal=True)
    add_frames(src, 1, [(1.0, b"x" * 324)])

    def digest():
        return hashlib.sha256(src.read_bytes()).hexdigest()
    before = digest()
    dest_dir = tmp_path / "scratch"
    dest_dir.mkdir()
    copy = cm.snapshot_db(src, dest_dir)
    assert copy == dest_dir / "telemetry.db" and copy != src
    assert digest() == before
    for db in (src, copy):
        c = sqlite3.connect(db)
        assert c.execute("SELECT COUNT(*) FROM frames").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM laps").fetchone()[0] == 3
        c.close()


# --- repo hygiene ------------------------------------------------------------
def test_no_app_imports_tool():
    for folder in ("app", "desktop"):
        for py in (ROOT / folder).rglob("*.py"):
            assert "capture_media" not in py.read_text(encoding="utf-8"), py
    for req in ("requirements.txt", "requirements-dev.txt"):
        text = (ROOT / req).read_text(encoding="utf-8").lower()
        assert "playwright" not in text and "pillow" not in text
    media = (ROOT / "requirements-media.txt").read_text(encoding="utf-8").lower()
    assert "playwright>=" in media and "pillow>=" in media


def test_media_deps_are_imported_lazily():
    src = (ROOT / "tools" / "capture_media.py").read_text(encoding="utf-8")
    for line in src.splitlines():
        if line.startswith(("import ", "from ")):       # module level only
            assert "playwright" not in line and "PIL" not in line, line


def test_docs_references_resolve():
    docs = [ROOT / "README.md", *(ROOT / "docs" / "wiki").glob("*.md"), ROOT / "docs" / "MEDIA.md",
            ROOT / "AGENTS.md", ROOT / "ARCHITECTURE.md"]
    for doc in docs:
        text = doc.read_text(encoding="utf-8")
        for name in DELETED:
            assert f"media/{name}" not in text, f"{doc.name} references removed {name}"
    for name in DELETED:
        assert not (ROOT / "docs" / "media" / name).exists()
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for name in ("launcher-window.png", "paused-chip.png", "browse-bar-menu.png", "csv-export-choice.png"):
        assert f"docs/media/{name}" in readme
