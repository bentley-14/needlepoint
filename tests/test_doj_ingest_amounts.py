"""Regression: DOJ ingest must not capture Strike-Force cumulative boilerplate as the
per-case amount. The old `max(amounts)` grabbed the program's cumulative billings
("...collectively billed approximately $14 billion") over the case figure, contaminating
141 gold_set rows (decontaminated PR #166). See scripts/ingest/doj_api_ingest.py.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "ingest"))

import doj_api_ingest as doj  # noqa: E402


def test_excludes_strike_force_cumulative_boilerplate():
    text = ("Dr. Smith was charged in a $2.5 million Medicare fraud scheme. Since its "
            "inception in 2007, the Medicare Fraud Strike Force has charged more than "
            "5,000 defendants who collectively billed approximately $14 billion.")
    amts = doj._extract_dollar_amounts(text)
    assert 2_500_000 in amts, amts
    assert 14_000_000_000 not in amts, amts
    assert max(amts) == 2_500_000  # per-case amount, NOT the $14B boilerplate


def test_billion_plus_dropped_as_cumulative():
    # No single Medicare-fraud case is >= $1B; the ceiling drops it regardless of context.
    assert doj._extract_dollar_amounts("losses totaling $3 billion") == []


def test_sub_ceiling_cumulative_context_dropped():
    # $100M-$1B figure in explicit cumulative context is still excluded.
    assert doj._extract_dollar_amounts("defendants collectively billed $500 million") == []


def test_keeps_plausible_case_amounts():
    amts = doj._extract_dollar_amounts("a $450 million fraud plus $1.2 million in kickbacks")
    assert set(amts) == {450_000_000, 1_200_000}
