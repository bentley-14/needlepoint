"""Tests for scripts/cohort_screen_count.py — the selection-effect screen counter.

Pure-local: builds a small synthetic parquet in tmp_path (pandas → parquet) and
exercises parse_predicate + run_screen directly. No network, no repo data.

Run: pytest tests/test_cohort_screen_count.py -v
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location(
    "cohort_screen_count", REPO / "scripts" / "cohort_screen_count.py")
csc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(csc)


@pytest.fixture(scope="module")
def parquet(tmp_path_factory) -> Path:
    """50 providers: NPIs 1000000000..1000000049, two specialties, two numeric
    features with known distributions so screen counts are hand-checkable."""
    n = 50
    df = pd.DataFrame({
        "npi": [str(1000000000 + i) for i in range(n)],
        "provider_type": ["Internal Medicine" if i % 2 == 0 else "Cardiology"
                          for i in range(n)],
        "total_payment": [float(i * 1000) for i in range(n)],      # 0..49000
        "services_per_bene": [float(i % 10) for i in range(n)],    # 0..9 cycling
    })
    path = tmp_path_factory.mktemp("data") / "synthetic_features.parquet"
    df.to_parquet(path)
    return path


# ── parse_predicate: the injection gate ──────────────────────────────────────

@pytest.mark.parametrize("bad", [
    "col; DROP TABLE x >= 1",                       # injection in column
    "total_payment >= 1 OR 1=1",                    # trailing SQL in value
    "total_payment >= 1; DROP TABLE x",             # stacked statement
    "total_payment >= (SELECT 1)",                  # subquery value
    "total-payment >= 1",                           # bad column char
    "total_payment => 1",                           # bad operator
    "total_payment",                                # no operator
    "total_payment > 'abc",                         # unbalanced quote → not a float
    'provider_type > "Cardiology"',                 # string with ordering op
    "",                                             # empty
])
def test_parse_predicate_rejects_malformed(bad):
    with pytest.raises(ValueError):
        csc.parse_predicate(bad)


def test_parse_predicate_numeric_and_string():
    p = csc.parse_predicate("total_payment >= 25000")
    assert (p.column, p.op, p.value) == ("total_payment", ">=", 25000.0)
    p = csc.parse_predicate('provider_type == "Cardiology"')
    assert (p.column, p.op, p.value) == ("provider_type", "==", "Cardiology")
    p = csc.parse_predicate("services_per_bene != 0")
    assert (p.column, p.op, p.value) == ("services_per_bene", "!=", 0.0)


def test_parse_predicate_quoted_injection_is_inert(parquet):
    """A quoted-string value with SQL inside must bind as a literal, not execute."""
    p = csc.parse_predicate("provider_type == \"x' OR '1'='1\"")
    res = csc.run_screen(parquet, [p])
    assert res["screen_count"] == 0  # no provider literally has that specialty


# ── run_screen: counts, AND-combination, specialty, NPIs ─────────────────────

def test_counts_and_combined(parquet):
    preds = [csc.parse_predicate("total_payment >= 40000"),     # i in 40..49 → 10 rows
             csc.parse_predicate("services_per_bene >= 5")]     # i%10 in 5..9 → 25 rows
    res = csc.run_screen(parquet, preds)
    assert res["total_rows"] == 50
    assert res["cohort_rows"] == 50
    assert res["marginals"][0]["count"] == 10
    assert res["marginals"][1]["count"] == 25
    assert res["screen_count"] == 5                  # i in {45,46,47,48,49}
    assert res["match_rate"] == pytest.approx(5 / 50)
    assert sorted(res["npis"]) == [str(1000000000 + i) for i in range(45, 50)]


def test_specialty_filter(parquet):
    preds = [csc.parse_predicate("total_payment >= 40000")]
    res = csc.run_screen(parquet, preds, specialty="Internal Medicine")
    assert res["cohort_rows"] == 25                  # even i only
    assert res["specialty_column"] == "provider_type"
    assert res["screen_count"] == 5                  # i in {40,42,44,46,48}
    assert res["marginals"][0]["count"] == 5         # marginal is within-cohort
    assert res["npis"] == [str(1000000000 + i) for i in (40, 42, 44, 46, 48)]


def test_unknown_specialty_yields_empty_cohort(parquet):
    res = csc.run_screen(parquet, [csc.parse_predicate("total_payment >= 0")],
                         specialty="Astrology")
    assert res["cohort_rows"] == 0
    assert res["screen_count"] == 0
    assert res["match_rate"] == 0.0


def test_unknown_column_errors_with_near_misses(parquet):
    with pytest.raises(ValueError) as ei:
        csc.run_screen(parquet, [csc.parse_predicate("total_paymnt >= 1")])
    assert "total_paymnt" in str(ei.value)
    assert "total_payment" in str(ei.value)          # near-miss suggestion


def test_npis_suppressed_above_threshold_unless_wanted(parquet):
    preds = [csc.parse_predicate("total_payment >= 0")]          # all 50 match
    res = csc.run_screen(parquet, preds)
    assert res["screen_count"] == 50
    assert res["npis"] is None                       # > LIST_THRESHOLD, not requested
    res = csc.run_screen(parquet, preds, want_npis=True)
    assert len(res["npis"]) == 50
