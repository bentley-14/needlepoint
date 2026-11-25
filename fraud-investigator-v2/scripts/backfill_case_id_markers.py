#!/usr/bin/env python3
"""One-shot: write missing `case_id.txt` markers into historical run dirs.

Background
----------
`_collect_execution_trace()` in the orchestrator uses `case_id.txt` markers
in each run dir to figure out which `events.jsonl` files belong to a given
case (a case can span multiple resume-runs). Before fix/bundle-complete-
execution-trace landed, the marker was written lazily at finalize time;
runs that crashed or were interrupted before reaching the case-writer step
never got a marker. Result: the bundle's `execution_trace[]` was missing
every tool call / reasoning turn from the run that did the actual
investigation work.

The orchestrator now writes the marker eagerly at __init__, but
historical runs are still markerless. This script backfills them by
inspecting each run's first event for a `payload.case_id` field
(STAGE_START events always carry it).

Run from repo root::

    python fraud-investigator-v2/scripts/backfill_case_id_markers.py [--dry-run]

Idempotent: skips runs that already have a marker, and never overwrites.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--runs-root",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "state" / "runs",
        help="Path to state/runs/ (default: <repo>/state/runs).",
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be written; don't touch disk.",
    )
    args = ap.parse_args()

    if not args.runs_root.exists():
        print(f"ERROR: runs root not found: {args.runs_root}", file=sys.stderr)
        return 1

    n_seen = n_already = n_written = n_unknown = n_no_events = 0
    for run_dir in sorted(args.runs_root.iterdir()):
        if not run_dir.is_dir():
            continue
        n_seen += 1
        marker = run_dir / "case_id.txt"
        if marker.exists():
            n_already += 1
            continue
        events_path = run_dir / "events.jsonl"
        if not events_path.exists():
            n_no_events += 1
            continue
        case_id = _extract_case_id(events_path)
        if not case_id:
            n_unknown += 1
            print(f"  ?  {run_dir.name}: could not infer case_id from "
                  f"first event")
            continue
        if args.dry_run:
            print(f"  DRY: would write {marker.relative_to(args.runs_root.parent)} = {case_id}")
        else:
            try:
                marker.write_text(case_id)
                print(f"  +  {run_dir.name}: case_id={case_id}")
            except Exception as e:
                print(f"  !  {run_dir.name}: write failed: {e}", file=sys.stderr)
                continue
        n_written += 1

    print()
    print(f"Summary: {n_seen} runs scanned")
    print(f"  already had marker: {n_already}")
    print(f"  written:            {n_written}{' (DRY RUN)' if args.dry_run else ''}")
    print(f"  no events.jsonl:    {n_no_events}")
    print(f"  case_id unknown:    {n_unknown}")
    return 0


def _extract_case_id(events_path: Path) -> str | None:
    """Read the first STAGE_START event and return its payload.case_id."""
    try:
        with events_path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cid = (e.get("payload") or {}).get("case_id")
                if cid:
                    return str(cid)
                # First event without a case_id payload — give up; this
                # likely isn't an orchestrator-launched run.
                return None
    except Exception:
        return None
    return None


if __name__ == "__main__":
    raise SystemExit(main())
