#!/usr/bin/env python3
"""Mine hard negatives — adversarially-cleared providers (loop 5).

A top-ranked NPI that went through the FULL loop and was killed ON THE
MERITS (≥1 grounded DISPOSITIVE_NOT_SURVIVED — not an EVF-economics
decline) is a precious training negative: it sits exactly in the score
region we pay to investigate, and it survived more scrutiny than any
random negative ever gets.

Labels are PROVISIONAL by construction: "the loop cleared them" is strong
but not infallible evidence of innocence. Two safeguards:
  1. sample-weight 0.5 in fit_scoring_weights.py (half a vote);
  2. the monthly hindsight replay re-audits this file — an NPI later
     appearing in gold_set is dropped here automatically (the delayed
     oracle outranks us; fit_scoring_weights also enforces gold_set wins).

Output: results/hard_negatives.jsonl (idempotent re-mine; latest case per
NPI). Consumers: fit_scoring_weights.py (y=0, weight 0.5) and — future —
walk-forward ranker retraining as weighted negatives.

Usage:
    python scripts/mine_hard_negatives.py            # mine + write
    python scripts/mine_hard_negatives.py --dry-run  # print only
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

DECLINE_CLASS = {"NOT_ACTIONABLE", "COUNSEL_ABANDONED", "INDEFENSIBLE_AT_CAP",
                 "RECONSIDER"}


def _npi_of(summary: dict) -> str | None:
    m = re.search(r"(?<!\d)(\d{10})(?!\d)", summary.get("subject") or "")
    return m.group(1) if m else None


def _dispositive_kills(summary: dict) -> int:
    n = 0
    for rnd in summary.get("adversarial_rounds") or []:
        lar = rnd.get("legal_adv_response") or {}
        for a in lar.get("attack_dispositions") or []:
            if a.get("disposition") == "DISPOSITIVE_NOT_SURVIVED":
                n += 1
    return n


def gold_npis(root: Path) -> set[str]:
    out: set[str] = set()
    p = root / "data" / "extracted" / "gold_set.jsonl"
    if p.exists():
        for line in p.read_text().splitlines():
            if line.strip():
                npi = str(json.loads(line).get("npi") or "")
                if npi and npi != "None":
                    out.add(npi)
    return out


def mine(root: Path) -> list[dict]:
    """Merits-killed, fully-adjudicated, not-DOJ-confirmed providers."""
    oracle = gold_npis(root)
    by_npi: dict[str, dict] = {}
    for p in sorted(root.glob("fraud-investigator-v2/state/CASE-*/summary.json")):
        try:
            d = json.loads(p.read_text())
        except Exception:
            continue
        npi = _npi_of(d)
        verdict = d.get("final_verdict")
        if not npi or verdict not in DECLINE_CLASS:
            continue
        kills = _dispositive_kills(d)
        if kills < 1:
            # EVF-economics declines (the pre-overhaul Cypress pattern) are
            # NOT merits clearances — too weak for a negative label.
            continue
        if npi in oracle:
            continue  # DOJ already says fraud — the oracle outranks our loop
        by_npi[npi] = {
            "npi": npi,
            "case_id": d.get("case_id"),
            "verdict": verdict,
            "n_dispositive_kills": kills,
            "evf": d.get("expected_value_factor"),
            "mined_at": date.today().isoformat(),
            "provisional": True,
            "recheck": "hindsight replay — drop this row if the NPI ever "
                       "appears in gold_set (delayed oracle outranks the loop)",
        }
    return sorted(by_npi.values(), key=lambda r: r["npi"])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, default=REPO_ROOT)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    rows = mine(args.root)
    out = args.out or (args.root / "results" / "hard_negatives.jsonl")
    print(f"{len(rows)} hard negative(s) (merits-killed, non-oracle)")
    for r in rows:
        print(f"  {r['npi']}  {r['case_id']}  {r['verdict']}  "
              f"kills={r['n_dispositive_kills']}")
    if not args.dry_run:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("".join(json.dumps(r) + "\n" for r in rows))
        print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
