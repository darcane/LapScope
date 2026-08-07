"""Build app/track_catalog.json from a database where the official routes have
been driven and named.

The catalogue is what lets a fresh install name a route on its first completed
lap (see app/tracks.py). It is generated, not hand-written: every entry is one
row of `routes` reduced to its fingerprint - start point, lap length, bounding
box - plus the name and shape the owner typed in.

Two things this does that a plain SELECT can't:

  * Fills in missing bounding boxes. Routes recorded before the span term
    existed (issue #53) have NULL span_x/span_z, and a fingerprint without a
    span is useless. Rather than requiring a reprocess of the source database,
    the box is recomputed here from the fastest completed lap's stored frames -
    the same min/max over pos_x/pos_z the recorder does in laps.py
    `_lap_extents`. The fastest lap is used because it is the cleanest line on
    the route, matching store.route_outline_lap's reasoning.

  * Refuses to emit an ambiguous catalogue. Two entries with different names
    that would both match one fingerprint would make naming a coin flip, so
    they are reported and the export fails instead.

The source database is opened read-only; this never writes to it.

Usage (from the repo root, plain stdlib):
    python tools/export_track_catalog.py --db data/telemetry.db
    python tools/export_track_catalog.py --db data/telemetry.db --dry-run
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.recorder.store import (ROUTE_LENGTH_TOLERANCE,  # noqa: E402
                                ROUTE_START_RADIUS_M, _spans_match)
from app.telemetry.packet import parse  # noqa: E402
from app.tracks import CATALOG_VERSION, MAX_NAME_LEN  # noqa: E402

DEFAULT_DB = "data/telemetry.db"
DEFAULT_OUT = "app/track_catalog.json"
GAME = "fh6"


def slug(name: str) -> str:
    """Stable key for a track name. Kept ASCII and punctuation-free so a later
    name correction (a typo, a re-spelling) keeps the same key and updates the
    existing entry instead of orphaning it."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", ascii_name.lower())).strip("-")


def lap_bbox(db: sqlite3.Connection, session_id: int,
             started_t: float, ended_t: float) -> tuple[float, float] | None:
    """The bounding-box dimensions of one lap, from its stored raw frames."""
    xs: list[float] = []
    zs: list[float] = []
    for (raw,) in db.execute(
            "SELECT raw FROM frames WHERE session_id = ? AND t BETWEEN ? AND ?"
            " ORDER BY t", (session_id, started_t, ended_t)):
        frame = parse(raw)
        if frame:
            xs.append(frame["pos_x"])
            zs.append(frame["pos_z"])
    if not xs:
        return None
    return max(xs) - min(xs), max(zs) - min(zs)


def fastest_lap(db: sqlite3.Connection, route_id: int) -> sqlite3.Row | None:
    return db.execute(
        "SELECT l.* FROM laps l JOIN sessions s ON s.id = l.session_id"
        " WHERE s.route_id = ? AND l.lap_time IS NOT NULL"
        " ORDER BY l.lap_time LIMIT 1", (route_id,)).fetchone()


def collect(db: sqlite3.Connection, verbose: bool) -> list[dict]:
    """One entry per named route, spans filled in where they are missing.
    Routes that can't be fingerprinted are skipped with a reason."""
    rows = db.execute(
        "SELECT r.*, COUNT(s.id) AS n_sessions FROM routes r"
        " LEFT JOIN sessions s ON s.route_id = r.id"
        " WHERE r.name IS NOT NULL AND TRIM(r.name) <> ''"
        " GROUP BY r.id ORDER BY r.id").fetchall()
    entries = []
    for row in rows:
        name = row["name"].strip()
        if len(name) > MAX_NAME_LEN:
            print(f"  skip [{row['id']}] {name!r}: name longer than {MAX_NAME_LEN}")
            continue
        span_x, span_z = row["span_x"], row["span_z"]
        note = ""
        if span_x is None or span_z is None:
            # pre-#53 row: recompute the box the recorder would have measured
            lap = fastest_lap(db, row["id"])
            if lap is None:
                print(f"  skip [{row['id']}] {name!r}: no completed lap to fingerprint")
                continue
            box = lap_bbox(db, lap["session_id"], lap["started_t"], lap["ended_t"])
            if box is None:
                print(f"  skip [{row['id']}] {name!r}: no parseable frames")
                continue
            span_x, span_z = box
            note = f" (span computed from session {lap['session_id']})"
        entries.append({
            "key": slug(name), "name": name,
            "kind": row["kind_user"] or row["kind"],
            "start_x": row["start_x"], "start_z": row["start_z"],
            "length": row["lap_length"], "span_x": span_x, "span_z": span_z,
            "_route_id": row["id"], "_n_sessions": row["n_sessions"],
        })
        if verbose:
            print(f"  [{row['id']:>4}] {name:<32} span=({span_x:7.1f},{span_z:7.1f}){note}")
    return entries


def dedupe(entries: list[dict]) -> list[dict]:
    """Collapse rows that carry the same name. A course split across two rows
    (one course, two start-line captures) is the normal cause; keep the row
    with the most sessions, and warn if the two disagree on shape, because then
    the shared name is a mistake rather than a split."""
    by_key: dict[str, dict] = {}
    for entry in sorted(entries, key=lambda e: (-e["_n_sessions"], e["_route_id"])):
        kept = by_key.get(entry["key"])
        if kept is None:
            by_key[entry["key"]] = entry
            continue
        agree = _spans_match(entry["span_x"], entry["span_z"],
                             kept["span_x"], kept["span_z"])
        detail = "same course on two rows" if agree else "SPANS DISAGREE - check this"
        print(f"  merged {entry['name']!r}: route {entry['_route_id']} into "
              f"{kept['_route_id']} ({detail})")
    return list(by_key.values())


def ambiguous(entries: list[dict]) -> list[tuple[dict, dict]]:
    """Pairs of differently-named entries that would both match one lap."""
    bad = []
    for i, a in enumerate(entries):
        for b in entries[i + 1:]:
            if math.hypot(a["start_x"] - b["start_x"],
                          a["start_z"] - b["start_z"]) > ROUTE_START_RADIUS_M:
                continue
            if abs(a["length"] - b["length"]) > ROUTE_LENGTH_TOLERANCE * b["length"]:
                continue
            if _spans_match(a["span_x"], a["span_z"], b["span_x"], b["span_z"]):
                bad.append((a, b))
    return bad


def render(entries: list[dict]) -> str:
    """One entry per line, sorted by key: a generated file that has to diff
    cleanly in review when a single track is added or corrected."""
    lines = []
    for entry in sorted(entries, key=lambda e: e["key"]):
        lines.append("    " + json.dumps({
            "key": entry["key"], "name": entry["name"], "kind": entry["kind"],
            "start": [round(entry["start_x"], 1), round(entry["start_z"], 1)],
            "length": round(entry["length"], 1),
            "span": [round(entry["span_x"], 1), round(entry["span_z"], 1)],
        }, ensure_ascii=False))
    return ('{\n'
            f'  "version": {CATALOG_VERSION},\n'
            f'  "game": "{GAME}",\n'
            '  "routes": [\n'
            + ',\n'.join(lines)
            + '\n  ]\n}\n')


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=DEFAULT_DB, help=f"source database (default {DEFAULT_DB})")
    ap.add_argument("--out", default=DEFAULT_OUT, help=f"output file (default {DEFAULT_OUT})")
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    ap.add_argument("-q", "--quiet", action="store_true", help="don't list every route")
    args = ap.parse_args()

    db = sqlite3.connect(f"file:{Path(args.db).as_posix()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        print(f"reading {args.db} (read-only)")
        entries = collect(db, verbose=not args.quiet)
    finally:
        db.close()

    entries = dedupe(entries)
    bad = ambiguous(entries)
    if bad:
        print("\nambiguous catalogue - these pairs would both match one lap:")
        for a, b in bad:
            gap = math.hypot(a["start_x"] - b["start_x"], a["start_z"] - b["start_z"])
            print(f"  {a['name']!r} (route {a['_route_id']}) vs "
                  f"{b['name']!r} (route {b['_route_id']}) - {gap:.1f} m apart")
        print("resolve them in the source database (rename or reprocess), then re-run.")
        return 1

    text = render(entries)
    print(f"\n{len(entries)} routes, {len(text)} bytes")
    if args.dry_run:
        return 0
    Path(args.out).write_text(text, encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
