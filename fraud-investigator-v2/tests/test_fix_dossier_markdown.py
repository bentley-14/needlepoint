"""Tests for scripts/fix_dossier_markdown.py — deterministic dossier autofix.

Run with repo root on sys.path (PYTHONPATH=.) so ``scripts`` imports as a
package, matching the CI invocation.
"""
from __future__ import annotations

import pytest

from scripts.fix_dossier_markdown import fix_text
from scripts.lint_dossier_markdown import lint_file


# ── Table-driven rule cases ─────────────────────────────────────────────────

CASES = [
    # (name, input, expected)
    (
        "dollar_in_prose_escaped",
        "G0483 at $241 per service, totaling $2.87M Medicare paid.",
        "G0483 at \\$241 per service, totaling \\$2.87M Medicare paid.",
    ),
    (
        "escaped_dollar_not_doubled",
        "Already escaped: \\$1,530,724 in damages.",
        "Already escaped: \\$1,530,724 in damages.",
    ),
    (
        "dollar_in_inline_code_untouched",
        "The value `$241` stays verbatim but $500 in prose escapes.",
        "The value `$241` stays verbatim but \\$500 in prose escapes.",
    ),
    (
        "dollar_in_fenced_block_untouched",
        "Prose $100 here.\n```\ncode $100 here\n```\nProse $200 again.\n",
        "Prose \\$100 here.\n```\ncode $100 here\n```\nProse \\$200 again.\n",
    ),
    (
        "dollar_not_before_digit_untouched",
        "Variable $foo and a trailing $ are left alone.",
        "Variable $foo and a trailing $ are left alone.",
    ),
    (
        "triple_asterisk_to_bold",
        "***U.S. v. Kabakibou — directly on point***",
        "**U.S. v. Kabakibou — directly on point**",
    ),
    (
        "snake_case_in_prose_backticked",
        "The confirmed_fact tier supports this count.",
        "The `confirmed_fact` tier supports this count.",
    ),
    (
        "snake_case_with_digits_backticked",
        "Derived from rules_v3 features.",
        "Derived from `rules_v3` features.",
    ),
    (
        "snake_case_already_backticked_untouched",
        "The `confirmed_fact` tier supports this count.",
        "The `confirmed_fact` tier supports this count.",
    ),
    (
        "snake_case_in_link_target_untouched",
        "See [the panel](data/processed/features_full_2023.parquet) for detail.",
        "See [the panel](data/processed/features_full_2023.parquet) for detail.",
    ),
    (
        "snake_case_in_link_text_backticked",
        "See [features_full panel](docs/PIPELINE.md) for detail.",
        "See [`features_full` panel](docs/PIPELINE.md) for detail.",
    ),
    (
        "mixed_case_token_untouched",
        "The Final_Verdict and SOME_CONST tokens stay bare.",
        "The Final_Verdict and SOME_CONST tokens stay bare.",
    ),
    (
        "snake_case_in_fenced_block_untouched",
        "```json\n{\"strength_score\": 0.84}\n```\n",
        "```json\n{\"strength_score\": 0.84}\n```\n",
    ),
    (
        "table_pipes_untouched",
        "| evidence | tier |\n| --- | --- |\n| claim | fact |\n",
        "| evidence | tier |\n| --- | --- |\n| claim | fact |\n",
    ),
    (
        "combined_line",
        "***Key:*** statistical_signal shows $2.87M billed.",
        "**Key:** `statistical_signal` shows \\$2.87M billed.",
    ),
]


@pytest.mark.parametrize("name,raw,expected", CASES, ids=[c[0] for c in CASES])
def test_fix_rules(name, raw, expected):
    assert fix_text(raw) == expected


@pytest.mark.parametrize("name,raw,expected", CASES, ids=[c[0] for c in CASES])
def test_idempotent(name, raw, expected):
    once = fix_text(raw)
    assert fix_text(once) == once


# ── Round-trip: fixer output passes the lint ────────────────────────────────

DIRTY_DOSSIER = """# Case report — CALIBRATION-TEST

**Computed strength:** 0.847 — STRONG · EVF 0.625

***Smoking gun:*** the confirmed_fact tier shows partb_total_medicare_payment
of $1,530,724 against a peer median of $13,946–$27,894. The statistical_signal
and within_practice components both fire on the same_district precedent.

| code | paid |
| --- | --- |
| G0483 | $241 per service |

```json
{"strength_score": 0.847, "paid": "$2.87M"}
```

See `discount_tax` (already backticked) and the
[panel](data/processed/features_full_2023.parquet) for sources.
"""


def test_round_trip_passes_lint(tmp_path):
    fixed = fix_text(DIRTY_DOSSIER)
    out = tmp_path / "01_Case_Report.md"
    out.write_text(fixed)
    findings = lint_file(out)
    assert findings == [], f"fixer output still flagged by lint: {findings}"


def test_round_trip_idempotent():
    fixed = fix_text(DIRTY_DOSSIER)
    assert fix_text(fixed) == fixed
