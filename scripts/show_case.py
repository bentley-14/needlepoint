#!/usr/bin/env python3
"""One-screen summary of an investigation case folder.

    python3 scripts/show_case.py CASE-XXXXXXXX-2026          # looks in fraud-investigator-v2/state/
    python3 scripts/show_case.py path/to/state/CASE-...      # or give a folder
    python3 scripts/show_case.py --latest                    # most recently updated case
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
STATE = REPO / "fraud-investigator-v2" / "state"
MARK = {"MET": "pass", "FAILED": "FAIL"}


def _case_dir(arg: str | None, latest: bool) -> Path:
    if latest:
        cases = sorted(STATE.glob("CASE-*/summary.json"), key=lambda p: p.stat().st_mtime)
        if not cases:
            raise SystemExit(f"no cases under {STATE}")
        return cases[-1].parent
    p = Path(arg)
    return p if p.is_dir() else STATE / arg


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("case", nargs="?")
    ap.add_argument("--latest", action="store_true")
    a = ap.parse_args()
    if not a.case and not a.latest:
        ap.error("give a CASE-ID / folder, or --latest")
    d_dir = _case_dir(a.case, a.latest)
    s = json.loads((d_dir / "summary.json").read_text())

    print(f"{s.get('case_id')}   {s.get('subject')}")
    print(f"status: {s.get('status')}   verdict: {s.get('final_verdict')}")
    if s.get("verdict_rationale"):
        print(f"why: {s['verdict_rationale']}")
    print()
    print("Filing test (all must pass to FILE):")
    for p in s.get("verdict_predicates") or []:
        print(f"  [{MARK.get(p.get('status'), p.get('status'))}] {p.get('name')}: {p.get('why')}")
    print()
    sb = s.get("strength_bucket")
    st = s.get("strength_score")
    evf = s.get("expected_value_factor")
    print(f"strength: {sb} ({st:.2f})" if isinstance(st, float) else f"strength: {sb}", end="")
    print(f"   expected-value factor: {evf:.2f}" if isinstance(evf, float) else "")
    print(f"settlement target: {s.get('final_settlement_target_usd')}   "
          f"cumulative discount: {s.get('final_cumulative_discount_pct')}%")
    print(f"counts retained: {len(s.get('counts_retained') or [])}   "
          f"abandoned: {len(s.get('counts_abandoned') or [])}   "
          f"adversarial rounds: {len(s.get('adversarial_rounds') or [])}")

    fl = s.get("foreclosure_ledger") or {}
    if fl:
        print(f"\nInnocent explanations (max live plausibility {fl.get('max_live_plausibility')}; "
              f"fraud more plausible: {fl.get('fraud_is_more_plausible_inference')}):")
        for e in (fl.get("explanations") or [])[:6]:
            if isinstance(e, dict):
                label = e.get("explanation") or e.get("name") or e.get("id")
                pl = e.get("plausibility_unrebutted", e.get("plausibility", ""))
                print(f"  - [{e.get('status') or e.get('disposition') or ''} {pl}] {str(label)[:120]}")

    rr = s.get("review_request") or {}
    if rr and not rr.get("resolved"):
        print(f"\nOPEN CHECKPOINT ({rr.get('checkpoint_type')}): {str(rr.get('question'))[:300]}")
        print(f"  answer with: python3 scripts/answer_checkpoint.py answer {s.get('case_id')} --resolution ... --answer \"...\"")

    tiers = Counter()
    for it in s.get("iterations") or []:
        for ev in ((it.get("investigator") or {}).get("evidence") or []):
            tiers[ev.get("tier")] += 1
    if tiers:
        print("\nEvidence by tier: " + ", ".join(f"{k}={v}" for k, v in tiers.most_common()))

    reports = sorted(p.name for p in d_dir.glob("0*_*") if p.is_file())
    print("\nReport files: " + (", ".join(reports) if reports else
                                "(none yet: the case writer runs once the case reaches a terminal verdict)"))
    print(f"folder: {d_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
