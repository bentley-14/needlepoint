#!/usr/bin/env python3
"""Unified target board — every head, one prioritized queue.

Joins the org shortlist (entity_target_screen output: MO + anchor columns),
the individual ranker's top-K (when a scored panel is locally present), and
case history (outcome ledger + open cases) into ONE board ranked by the
doctrine order:

    1. anchor_fileable (public coverage-rule anchor exists — the fileable band)
    2. mo_correlates   (conviction-MO configuration match)
    3. model score / $ (magnitude LAST — the camouflaged-middle doctrine)

Rows already investigated (any state dir or the outcomes ledger) are tagged so
the queue never re-runs a decided target. `--to-queue N` feeds the top N
uninvestigated rows into scripts/ops/np_queue.py (the babysat daemon queue).

Usage:
    python scripts/target_board.py                       # print the board
    python scripts/target_board.py --to-queue 1          # queue the top target
    python scripts/target_board.py --out results/entity_head/board.jsonl
"""
from __future__ import annotations

import argparse
import glob
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SHORTLIST_GLOB = str(REPO_ROOT / "results/entity_head/target_shortlist_*.jsonl")
OUTCOMES = REPO_ROOT / "validation/outcomes/case_outcomes.jsonl"
STATE_DIRS = [REPO_ROOT / "fraud-investigator-v2/state",
              REPO_ROOT / "fraud-investigator-v2/state/calibration"]
INDIV_SCORED_GLOB = str(REPO_ROOT / "results/walk_forward_v4_top5_y2024_*/scored_full_2024.parquet")


def investigated_npis() -> dict[str, str]:
    """npi -> disposition, from case state dirs + the outcomes ledger."""
    seen: dict[str, str] = {}
    for root in STATE_DIRS:
        for sj in glob.glob(str(root / "*/summary.json")):
            try:
                s = json.loads(Path(sj).read_text())
            except Exception:
                continue
            subj = str(s.get("subject") or "")
            npi = None
            for tok in subj.replace("—", " ").split():
                if tok.isdigit() and len(tok) == 10:
                    npi = tok
                    break
            if npi:
                seen[npi] = str(s.get("final_verdict") or s.get("status") or "open")
    if OUTCOMES.exists():
        for line in OUTCOMES.open():
            if line.strip():
                try:
                    r = json.loads(line)
                    if r.get("npi"):
                        seen[str(r["npi"])] = str(r.get("final_verdict") or "recorded")
                except json.JSONDecodeError:
                    continue
    return seen


def latest_shortlist() -> list[dict]:
    files = sorted(glob.glob(SHORTLIST_GLOB))
    if not files:
        return []
    return [json.loads(l) for l in open(files[-1]) if l.strip()]


def individual_top(k: int = 15) -> list[dict]:
    """Top-K from the individual ranker when a local scored panel exists —
    graceful empty when panels live only in R2 (the common laptop state)."""
    files = sorted(glob.glob(INDIV_SCORED_GLOB))
    if not files:
        return []
    try:
        import duckdb
        df = duckdb.connect().execute(
            f"SELECT CAST(npi AS VARCHAR) npi, score_rrf FROM read_parquet('{files[-1]}') "
            f"ORDER BY score_rrf DESC LIMIT {k}").fetchdf()
        return [{"npi": r["npi"], "head": "individual", "score": float(r["score_rrf"])}
                for _, r in df.iterrows()]
    except Exception:
        return []


def build_board() -> list[dict]:
    inv = investigated_npis()
    rows: list[dict] = []
    for r in latest_shortlist():
        npi = str(r.get("npi"))
        rows.append({
            "head": "org",
            "npi": npi,
            "name": r.get("org_name"),
            "state": r.get("provider_state") or r.get("nppes_state"),
            "anchor_fileable": bool(r.get("anchor_fileable")),
            "anchor_ids": r.get("anchor_ids") or "",
            "best_mo": r.get("best_mo"),
            "mo_correlates": int(r.get("mo_correlates") or 0),
            "top_hcpcs": r.get("top_hcpcs"),
            "medicaid_pay": r.get("medicaid_total_pay"),
            "score": r.get("score"),
            "investigated": inv.get(npi),
        })
    for r in individual_top():
        npi = str(r["npi"])
        rows.append({**r, "anchor_fileable": None, "mo_correlates": 0,
                     "investigated": inv.get(npi)})
    rows.sort(key=lambda x: (
        not bool(x.get("anchor_fileable")),          # fileable band first
        -(x.get("mo_correlates") or 0),
        -float(x.get("score") or 0)))
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--to-queue", type=int, default=0,
                    help="feed the top-N uninvestigated rows into np_queue")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--top", type=int, default=25)
    args = ap.parse_args(argv)
    board = build_board()
    print(f"{'●':2s}{'ANCHOR':7s}{'MO':22s}{'corr':4s} {'NPI':11s} {'name':32s} {'st':3s} {'invest?':16s}")
    for r in board[:args.top]:
        anc = "FILE✓" if r.get("anchor_fileable") else ("—" if r.get("anchor_fileable") is False else "?")
        print(f"{'○' if r.get('investigated') else '●':2s}{anc:7s}{str(r.get('best_mo'))[:21]:22s}"
              f"{r.get('mo_correlates', 0):<4d} {r['npi']:11s} {str(r.get('name'))[:31]:32s} "
              f"{str(r.get('state'))[:2]:3s} {str(r.get('investigated') or ''):16s}")
    if args.out:
        with args.out.open("w") as f:
            for r in board:
                f.write(json.dumps(r, default=str) + "\n")
        print(f"\nboard -> {args.out}")
    if args.to_queue:
        fresh = [r for r in board if not r.get("investigated")][:args.to_queue]
        for r in fresh:
            subprocess.run([sys.executable, str(REPO_ROOT / "scripts/ops/np_queue.py"),
                            "add", "--npi", r["npi"]], check=False)
            print(f"queued {r['npi']} ({r.get('name')})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
