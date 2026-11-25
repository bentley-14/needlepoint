#!/usr/bin/env python3
"""Land confirmed org/Medicaid/facility labels into data/extracted/entity_labels.jsonl.

Reads a labelgen_confirm.py output file (entity_labels_CONFIRMED_<date>.jsonl),
keeps only confirmed_label == "keep" rows, and appends them to the landing
file in its existing schema:

    {org_npi, defendant_name, label_class, confidence, basis, state,
     disposition, scheme_summary, source_url, provenance, label: "fraud"}

`provenance` is stamped "haiku_harvest+sonnet_escalation+opus_confirm_<date>"
(date parsed from the --confirmed filename, --date override, or today).

Safety:
- A timestamped `.bak_<ts>` copy of the target is written FIRST, whenever the
  target already exists (skipped on a first run where it doesn't).
- Dedupe by org_npi, against BOTH the existing target rows and within the
  incoming batch — this is a read-merge-write, not a raw file append, because
  a blind append cannot dedupe against content already on disk.
- The target path is validated to be named exactly `entity_labels.jsonl`
  (refuses anything else) — a --target override exists purely for tests
  (tmp_path / "entity_labels.jsonl"), not for redirecting real landings
  elsewhere.
- This script NEVER reads or writes gold_set.jsonl.

Usage:
    python scripts/labelgen_land.py validation/curator/entity_labels_CONFIRMED_2026-07-16.jsonl
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TARGET = REPO_ROOT / "data" / "extracted" / "entity_labels.jsonl"
TARGET_BASENAME = "entity_labels.jsonl"


def build_landing_row(confirmed_row: dict, *, provenance: str) -> dict:
    return {
        "org_npi": confirmed_row.get("org_npi"),
        "defendant_name": confirmed_row.get("defendant_name"),
        "label_class": confirmed_row.get("label_class"),
        "confidence": confirmed_row.get("confidence"),
        "basis": confirmed_row.get("basis"),
        "state": confirmed_row.get("state"),
        "disposition": confirmed_row.get("disposition"),
        "scheme_summary": confirmed_row.get("scheme_summary"),
        "source_url": confirmed_row.get("source_url"),
        "provenance": provenance,
        "label": "fraud",
    }


def derive_date(confirmed_path: Path, override: str | None = None) -> str:
    if override:
        return override
    m = re.search(r"(\d{4}-\d{2}-\d{2})", confirmed_path.name)
    if m:
        return m.group(1)
    return datetime.date.today().isoformat()


def assert_valid_target(target: Path) -> None:
    """Refuse to run against anything not literally named entity_labels.jsonl —
    a --target override exists for tests (tmp_path), not for redirecting a
    real landing run to the wrong file (e.g. gold_set.jsonl) by mistake."""
    if target.name != TARGET_BASENAME:
        raise SystemExit(
            f"refusing to land into {target} — the landing target must be named "
            f"exactly '{TARGET_BASENAME}' (got '{target.name}')")


def load_existing_rows(target: Path) -> list[dict]:
    if not target.exists():
        return []
    rows = []
    for line in target.open():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def backup_target(target: Path) -> Path | None:
    """Timestamped .bak of the target, written BEFORE any modification.
    Returns None (no-op) when the target doesn't exist yet (first run)."""
    if not target.exists():
        return None
    ts = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
    bak = target.with_name(f"{target.name}.bak_{ts}")
    bak.write_bytes(target.read_bytes())
    return bak


def dedupe_and_merge(existing_rows: list[dict], new_rows: list[dict]) -> tuple[list[dict], int, int]:
    """Dedupe by org_npi against existing rows AND within the incoming batch.
    Returns (final_rows, n_written, n_skipped_dup)."""
    seen = {str(r["org_npi"]) for r in existing_rows if r.get("org_npi")}
    final = list(existing_rows)
    n_written = 0
    n_skipped = 0
    for row in new_rows:
        npi = str(row.get("org_npi") or "")
        if not npi or npi in seen:
            n_skipped += 1
            continue
        seen.add(npi)
        final.append(row)
        n_written += 1
    return final, n_written, n_skipped


def land(confirmed_rows: list[dict], *, target: Path, date: str | None = None) -> dict:
    """Core land operation (no CLI/file-arg parsing) — used directly by tests.
    Returns a summary dict; writes `target` (+ its .bak) as a side effect."""
    assert_valid_target(target)
    keeps = [r for r in confirmed_rows if r.get("confirmed_label") == "keep"]
    provenance = f"haiku_harvest+sonnet_escalation+opus_confirm_{date or datetime.date.today().isoformat()}"
    new_rows = [build_landing_row(r, provenance=provenance) for r in keeps]

    existing_rows = load_existing_rows(target)
    bak_path = backup_target(target)
    final_rows, n_written, n_skipped = dedupe_and_merge(existing_rows, new_rows)

    target.parent.mkdir(parents=True, exist_ok=True)
    # Write to a .tmp then atomically rename: a mid-write death can never
    # leave the live landing file truncated (repo discipline: preserve
    # validated artifacts; the .bak above is the belt, this is the braces).
    tmp = target.with_name(target.name + ".tmp")
    with tmp.open("w") as f:
        for row in final_rows:
            f.write(json.dumps(row) + "\n")
    tmp.replace(target)

    return {
        "n_confirmed_rows": len(confirmed_rows),
        "n_keeps": len(keeps),
        "n_existing": len(existing_rows),
        "n_written": n_written,
        "n_skipped_dup": n_skipped,
        "n_final": len(final_rows),
        "backup": str(bak_path) if bak_path else None,
        "target": str(target),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("confirmed", type=Path, help="labelgen_confirm.py output (entity_labels_CONFIRMED_<date>.jsonl)")
    ap.add_argument("--target", type=Path, default=DEFAULT_TARGET,
                    help=f"must be named '{TARGET_BASENAME}' (default: {DEFAULT_TARGET})")
    ap.add_argument("--date", default=None,
                    help="provenance date override (default: parsed from --confirmed filename, else today)")
    args = ap.parse_args(argv)

    confirmed_rows = [json.loads(l) for l in args.confirmed.open() if l.strip()]
    date = derive_date(args.confirmed, args.date)
    summary = land(confirmed_rows, target=args.target, date=date)

    print(f"confirmed rows read: {summary['n_confirmed_rows']} | keeps: {summary['n_keeps']}")
    print(f"backup: {summary['backup'] or '(none — target did not exist yet)'}")
    print(f"written: {summary['n_written']}  skipped (dup org_npi): {summary['n_skipped_dup']}")
    print(f"{summary['target']}: {summary['n_existing']} -> {summary['n_final']} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
