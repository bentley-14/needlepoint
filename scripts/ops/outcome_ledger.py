#!/usr/bin/env python3
"""Case-outcome ledger — the system learns from its OWN verdicts.

Scans every case state dir (production + calibration) and appends any case not
yet recorded into validation/outcomes/case_outcomes.jsonl (append-only; one
row per case_id; re-runs are idempotent). The ledger feeds:

* scripts/target_board.py — never re-queue a decided target
* the calibration battery / precision stats (TP file-rate, TN decline-rate)
* future adjudicated-TN label proposals (our own declines are candidate TNs
  after human review — proposals only, never auto-labels)

Row: case_id, npi, subject, profile (calibration|production), status,
final_verdict, strength, evf, checkpoint (type + resolution when one fired),
recorded_at.

Usage:  python scripts/ops/outcome_ledger.py [--dry-run]
"""
from __future__ import annotations

import argparse
import datetime
import glob
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LEDGER = REPO_ROOT / "validation/outcomes/case_outcomes.jsonl"
STATE_ROOTS = [
    (REPO_ROOT / "fraud-investigator-v2/state", "production"),
    (REPO_ROOT / "fraud-investigator-v2/state/calibration", "calibration"),
]


def _npi_from_subject(subject: str) -> str | None:
    for tok in str(subject).replace("—", " ").split():
        if tok.isdigit() and len(tok) == 10:
            return tok
    return None


def collect() -> list[dict]:
    rows = []
    for root, profile in STATE_ROOTS:
        for sj in sorted(glob.glob(str(root / "*/summary.json"))):
            case_dir = Path(sj).parent
            if profile == "production" and case_dir.parent.name == "calibration":
                continue   # calibration dirs nest under production's root
            try:
                s = json.loads(Path(sj).read_text())
            except Exception:
                continue
            if str(s.get("status")) not in ("complete", "abandoned"):
                continue   # only decided cases enter the ledger
            rr = s.get("review_request") or {}
            rows.append({
                "case_id": s.get("case_id") or case_dir.name,
                "npi": _npi_from_subject(s.get("subject") or ""),
                "subject": str(s.get("subject") or "")[:120],
                "profile": profile,
                "status": s.get("status"),
                "final_verdict": s.get("final_verdict"),
                "strength": s.get("strength_score") or s.get("strength"),
                "evf": s.get("evf"),
                "checkpoint": ({"type": rr.get("checkpoint_type"),
                                "resolved": rr.get("resolved"),
                                "resolution": rr.get("resolution")}
                               if rr else None),
                "recorded_at": datetime.datetime.now().isoformat(timespec="seconds"),
            })
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    have = set()
    if LEDGER.exists():
        for line in LEDGER.open():
            if line.strip():
                try:
                    have.add(json.loads(line)["case_id"])
                except json.JSONDecodeError:
                    continue
    new = [r for r in collect() if r["case_id"] not in have]
    for r in new:
        print(f"{'DRY ' if args.dry_run else ''}+ {r['case_id']} {r['profile']:11s} "
              f"{str(r['final_verdict']):22s} {str(r['subject'])[:48]}")
    if not args.dry_run and new:
        with LEDGER.open("a") as f:
            for r in new:
                f.write(json.dumps(r, default=str) + "\n")
    print(f"{len(new)} new outcome(s); ledger={LEDGER}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
