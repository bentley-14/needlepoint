"""Hermetic regression tests for triage._build_record — locks in the two merged
triage fixes that otherwise have NO automated coverage (validated only by a live run).

These guard the DETERMINISTIC record-builder seam, not LLM behavior:

1. Intra-specialty opioid fix (PR #335 / commit 5b42326): the per-row record MUST
   surface the within-specialty long-acting-opioid percentile
   (`within_specialty_percentiles.partd_opioid_la_clm_share_pct`) and the opioid
   within-spec percentile (`partd_opioid_clm_share_pct`). A future refactor that
   drops either field from the record silently disables angle 2's intra-specialty
   diversion catch for `999`-bar specialties (pain / PM&R / anesthesia / CRNA),
   because the agent never sees the signal.

2. svcs/bene unit-semantics guard: the prompt's phantom-billing guard keys on
   `derived.drug_frac_of_payment` (and the unit-count ratios svcs_per_day /
   svcs_per_bene / patients_per_day). The LLM decision is not unit-testable, but
   the deterministic computation + surfacing of `drug_frac_of_payment` IS — if it
   were dropped or miscomputed, the guard could never fire. We assert the value is
   present and equals drug_pymt / max(payment, 1).

Limitation: `_build_record` is the cleanly-callable deterministic seam (it consumes
a single scored-panel row). The angle-2 / phantom *verdicts* live in prompts/triage.md
and are LLM behavior; they are intentionally NOT exercised here. These tests assert
the input contract the prompt depends on — i.e. that the load-bearing fields reach
the agent at all — which is exactly the regression PR #335 closed.

Run:
  PYTHONPATH=fraud-investigator-v2/src python3 -m pytest \
      fraud-investigator-v2/tests/test_triage_record.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fraud_investigator.agents.triage import _build_record  # noqa: E402


def _row(**overrides) -> pd.Series:
    """A synthetic scored-panel row with the columns _build_record reads.

    Defaults model a pain-management provider whose ABSOLUTE opioid bar would be
    999 (specialty-typical), but who is an extreme within-specialty outlier — the
    exact shape PR #335 targets. Override any column per-test.
    """
    base = {
        "npi": "1234567890",
        "partb_rndrng_prvdr_first_name": "Jane",
        "partb_rndrng_prvdr_last_org_name": "Doe",
        "partb_provider_type": "Pain Management",
        "partb_state": "TX",
        "partb_n_beneficiaries": 400,
        "partb_n_services": 50000.0,
        "partb_total_medicare_payment": 1_000_000.0,
        "partb_tot_sbmtd_chrg": 2_000_000.0,
        "partb_total_medicare_allowed": 1_200_000.0,
        "partb_drug_mdcr_pymt_amt": 600_000.0,
        "partb_med_mdcr_pymt_amt": 400_000.0,
        "partb_bene_dual_cnt": 200,
        "partb_bene_age_lt_65_cnt": 50,
        "partb_bene_age_gt_84_cnt": 30,
        "score_rrf": 0.9,
        # within-specialty percentiles — the load-bearing opioid fields
        "partd_opioid_clm_share_pct": 0.97,
        "partd_opioid_la_clm_share_pct": 0.99,
    }
    base.update(overrides)
    return pd.Series(base)


# ── Fix 1: intra-specialty opioid percentiles must reach the agent ────────────

def test_la_opioid_within_spec_percentile_is_surfaced():
    """PR #335: the within-specialty long-acting-opioid percentile must appear in
    the record so angle 2 can fire even when the absolute OPIOID_NORMS bar is 999."""
    rec = _build_record(_row(), rank=1, cross_year_ranks={}, prior_year=2022)
    wsp = rec["within_specialty_percentiles"]
    assert "partd_opioid_la_clm_share_pct" in wsp
    assert wsp["partd_opioid_la_clm_share_pct"] == 0.99


def test_opioid_within_spec_percentile_is_surfaced():
    """The (non-LA) opioid within-specialty percentile must also be present — angle 2
    fires on either `partd_opioid_clm_share_pct` OR `partd_opioid_la_clm_share_pct`."""
    rec = _build_record(_row(), rank=1, cross_year_ranks={}, prior_year=2022)
    wsp = rec["within_specialty_percentiles"]
    assert "partd_opioid_clm_share_pct" in wsp
    assert wsp["partd_opioid_clm_share_pct"] == 0.97


def test_opioid_percentiles_present_even_when_absent_in_row():
    """If the panel lacks the column the keys must still exist (value None), so a
    refactor can't quietly remove them — the agent always sees the field name."""
    row = _row()
    row = row.drop(["partd_opioid_la_clm_share_pct", "partd_opioid_clm_share_pct"])
    rec = _build_record(row, rank=1, cross_year_ranks={}, prior_year=2022)
    wsp = rec["within_specialty_percentiles"]
    assert "partd_opioid_la_clm_share_pct" in wsp
    assert "partd_opioid_clm_share_pct" in wsp
    assert wsp["partd_opioid_la_clm_share_pct"] is None
    assert wsp["partd_opioid_clm_share_pct"] is None


def test_la_opioid_percentile_rounded_to_three_digits():
    """Percentiles go through _pct (3-digit rounding); a precision change would
    shift where the ≥0.95 / ≥0.90 bars trip."""
    rec = _build_record(
        _row(partd_opioid_la_clm_share_pct=0.987654), rank=1,
        cross_year_ranks={}, prior_year=2022,
    )
    assert rec["within_specialty_percentiles"]["partd_opioid_la_clm_share_pct"] == 0.988


# ── Fix 2: unit-semantics guard's deterministic input (drug_frac) ─────────────

def test_drug_frac_of_payment_is_surfaced_and_correct():
    """The phantom-billing unit-semantics guard requires drug_frac_of_payment to
    distinguish drug-unit volume from true encounter volume. Assert it is present
    and equals drug_pymt / max(payment, 1) (here 600k / 1.0M = 0.6, rounded to 3)."""
    rec = _build_record(_row(), rank=1, cross_year_ranks={}, prior_year=2022)
    assert "drug_frac_of_payment" in rec["derived"]
    assert rec["derived"]["drug_frac_of_payment"] == 0.6


def test_drug_frac_zero_payment_does_not_divide_by_zero():
    """max(payment, 1) guard: a zero-payment row must yield 0.0, not raise."""
    rec = _build_record(
        _row(partb_total_medicare_payment=0.0, partb_drug_mdcr_pymt_amt=0.0),
        rank=1, cross_year_ranks={}, prior_year=2022,
    )
    assert rec["derived"]["drug_frac_of_payment"] == 0.0


def test_unit_count_ratios_present_for_phantom_guard():
    """svcs_per_day / patients_per_day / svcs_per_bene feed the phantom signal that
    the unit-semantics guard gates; they must be surfaced for the guard to apply."""
    rec = _build_record(_row(), rank=1, cross_year_ranks={}, prior_year=2022)
    derived = rec["derived"]
    for k in ("svcs_per_day", "patients_per_day", "svcs_per_bene"):
        assert k in derived
    # services / 250.0 = 50000 / 250 = 200.0
    assert derived["svcs_per_day"] == 200.0
    # benes / 250.0 = 400 / 250 = 1.6
    assert derived["patients_per_day"] == 1.6
    # services / max(benes, 1) = 50000 / 400 = 125.0
    assert derived["svcs_per_bene"] == 125.0


# ── Sanity: builder is callable in isolation, no leakage ──────────────────────

def test_record_omits_outcome_label_columns():
    """Defense-in-depth: even if y_eval/archetype/fraud_npi sneak into a row, the
    record builder does not echo them into any nested block."""
    row = _row(y_eval=1, archetype="opioid_diversion", fraud_npi=1)
    rec = _build_record(row, rank=1, cross_year_ranks={}, prior_year=2022)
    flat = repr(rec)
    assert "y_eval" not in flat
    assert "fraud_npi" not in flat
    assert "archetype" not in flat
