"""Prompt-budget hard ceiling — F2's root cause (prompt mass outgrowing turn
budgets) must not silently regrow. WARN is report-only; HARD fails CI."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.prompt_budget_report import HARD_TOKENS, measure


def test_no_prompt_exceeds_hard_ceiling():
    over = [r for r in measure() if r["est_tokens"] > HARD_TOKENS]
    assert not over, (
        f"Prompt(s) over the {HARD_TOKENS:,}-token HARD ceiling: "
        f"{[(r['prompt'], r['est_tokens']) for r in over]} — trim the prompt "
        f"or consciously raise the ceiling in scripts/prompt_budget_report.py "
        f"(instructions beyond what the turn budget honors get dropped "
        f"silently — that was F2)."
    )


def test_measure_covers_all_templates():
    rows = measure()
    names = {r["prompt"] for r in rows}
    assert {"investigator", "counsel_build", "defense", "triage"} <= names
    assert all(r["est_tokens"] > 0 for r in rows)
