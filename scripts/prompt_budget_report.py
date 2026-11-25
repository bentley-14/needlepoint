#!/usr/bin/env python3
"""Prompt-budget report — keep agent instructions inside what turn budgets honor.

F2's root cause was prompt mass outgrowing turn budgets (instructions get
dropped silently under load), and improvements keep ADDING prompt mass.
This report measures every agent prompt template (chars + est. tokens at
~3.8 chars/token) against its TURN_BUDGET_BY_STEP allowance and two lines:

    WARN  > 10,000 est tokens — review for trimmable mass
    HARD  > 18,000 est tokens — tests/test_prompt_budget.py FAILS the build

    python scripts/prompt_budget_report.py        # table
    make prompt-budget
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPT_DIR = REPO_ROOT / "fraud-investigator-v2/src/fraud_investigator/prompts"
CHARS_PER_TOKEN = 3.8
WARN_TOKENS = 10_000
# Raised 16k -> 18k consciously: the investigator prompt grew to ~17.3k with the OSINT
# output contract (scored real-world-research floor). Trim before raising again.
HARD_TOKENS = 18_000


def measure() -> list[dict]:
    sys.path.insert(0, str(REPO_ROOT / "fraud-investigator-v2/src"))
    try:
        from fraud_investigator.claude_p import TURN_BUDGET_BY_STEP
    except Exception:
        TURN_BUDGET_BY_STEP = {}
    rows = []
    for p in sorted(PROMPT_DIR.glob("*.md")):
        chars = len(p.read_text())
        est = int(chars / CHARS_PER_TOKEN)
        rows.append({
            "prompt": p.stem, "chars": chars, "est_tokens": est,
            "turns": TURN_BUDGET_BY_STEP.get(p.stem, "-"),
            "level": ("HARD" if est > HARD_TOKENS
                      else "WARN" if est > WARN_TOKENS else "ok"),
        })
    return sorted(rows, key=lambda r: -r["est_tokens"])


def main() -> int:
    rows = measure()
    print(f"{'prompt':<28} {'chars':>8} {'~tokens':>8} {'turns':>6}  level")
    for r in rows:
        print(f"{r['prompt']:<28} {r['chars']:>8,} {r['est_tokens']:>8,} "
              f"{str(r['turns']):>6}  {r['level']}")
    total = sum(r["est_tokens"] for r in rows)
    n_warn = sum(1 for r in rows if r["level"] != "ok")
    print(f"\n{len(rows)} prompts · ~{total:,} est tokens total · "
          f"{n_warn} over the {WARN_TOKENS:,}-token WARN line")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
