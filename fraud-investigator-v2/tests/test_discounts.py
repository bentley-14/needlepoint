"""Deterministic damage-discount schedule (discounts.py) — PR6 of the overhaul.

The LLM classifies, the code computes: every MITIGATED attack's discount_pct
is recomputed from a per-type classification payload; the model's free-pick
is preserved as discount_pct_model. Covers each attack type's mapping, the
clamps, missing-classification fallbacks, the damages-weighted cumulative
formula, back-compat with classification-less dispositions, and that
SURVIVED / DISPOSITIVE_NOT_SURVIVED dispositions are untouched.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fraud_investigator.discounts import (  # noqa: E402
    apply_deterministic_discounts,
    compute_discount_pct,
)
from fraud_investigator.schemas import AttackDisposition  # noqa: E402


# ── compute_discount_pct: per-type mappings ──────────────────────────────────


def test_sol_proportional():
    cls = {"out_of_window_damages_usd": 200_000, "total_damages_usd": 1_000_000}
    assert compute_discount_pct("statute_of_limitations", cls) == 20


def test_sol_capped_at_50():
    cls = {"out_of_window_damages_usd": 900_000, "total_damages_usd": 1_000_000}
    assert compute_discount_pct("statute_of_limitations", cls) == 50


def test_sol_missing_fields_falls_back_to_30():
    assert compute_discount_pct("statute_of_limitations", {}) == 30
    assert compute_discount_pct(
        "statute_of_limitations",
        {"out_of_window_damages_usd": "lots", "total_damages_usd": 1}) == 30
    assert compute_discount_pct(
        "statute_of_limitations",
        {"out_of_window_damages_usd": 5, "total_damages_usd": 0}) == 30


def test_damages_overstated_proportional():
    # pled 1M, re-derived increment 800k → overstated by 20%
    cls = {"recomputed_increment_usd": 800_000, "pled_amount_usd": 1_000_000}
    assert compute_discount_pct("damages_overstated", cls) == 20


@pytest.mark.parametrize("inc,pled,expected", [
    (990_000, 1_000_000, 10),   # barely overstated → floor 10
    (100_000, 1_000_000, 50),   # wildly overstated → cap 50
    (1_500_000, 1_000_000, 10),  # UNDER-stated (negative raw) → floor 10
])
def test_damages_overstated_clamped(inc, pled, expected):
    cls = {"recomputed_increment_usd": inc, "pled_amount_usd": pled}
    assert compute_discount_pct("damages_overstated", cls) == expected


def test_damages_overstated_missing_falls_back_to_30():
    assert compute_discount_pct("damages_overstated", {}) == 30


@pytest.mark.parametrize("atype", ["public_disclosure_bar", "first_to_file_bar"])
def test_binary_doctrines_never_discount(atype):
    assert compute_discount_pct(atype, {}) == 0
    assert compute_discount_pct(atype, {"anything": 1}) == 0


@pytest.mark.parametrize("cls,expected", [
    ({"grounded": False, "goes_to": "falsity_core", "resolvable_by": "never"}, 0),
    ({"grounded": True, "goes_to": "periphery", "resolvable_by": "discovery"}, 20),
    ({"grounded": True, "goes_to": "falsity_core", "resolvable_by": "discovery"}, 35),
    ({"grounded": True, "goes_to": "falsity_core", "resolvable_by": "on_record"}, 35),
    ({"grounded": True, "goes_to": "falsity_core", "resolvable_by": "never"}, 50),
])
def test_legitimate_explanation_matrix(cls, expected):
    assert compute_discount_pct("legitimate_explanation", cls) == expected


def test_legitimate_explanation_missing_falls_back_to_30():
    assert compute_discount_pct("legitimate_explanation", {}) == 30
    assert compute_discount_pct(
        "legitimate_explanation", {"grounded": True, "goes_to": "???"}) == 30


@pytest.mark.parametrize("atype", ["scienter_lacking", "materiality_lacking"])
@pytest.mark.parametrize("n,expected", [(0, 10), (1, 15), (2, 30), (3, 45), (7, 45)])
def test_contested_factors_mapping(atype, n, expected):
    assert compute_discount_pct(atype, {"contested_factors_count": n}) == expected


def test_contested_factors_missing_falls_back_to_30():
    assert compute_discount_pct("scienter_lacking", {}) == 30
    assert compute_discount_pct(
        "materiality_lacking", {"contested_factors_count": "two"}) == 30


@pytest.mark.parametrize("atype", ["rule_9b_insufficient", "safe_harbor", "other"])
@pytest.mark.parametrize("sev,expected",
                         [("minor", 10), ("mitigating", 25), ("dispositive", 40)])
def test_severity_mapping(atype, sev, expected):
    assert compute_discount_pct(atype, {"severity": sev}) == expected


def test_severity_missing_falls_back_to_30():
    assert compute_discount_pct("other", {}) == 30
    assert compute_discount_pct("other", {"severity": "catastrophic"}) == 30


# ── apply_deterministic_discounts ────────────────────────────────────────────


def _build(counts):
    """counsel_build with counts_pled = [(count_id, single_damages), ...]."""
    return {"counts_pled": [
        {"count_id": cid, "estimated_single_damages_usd": usd}
        for cid, usd in counts
    ]}


def _adv(dispositions, retained, cumulative=0, settlement=""):
    return {
        "case_id": "CASE-X", "round": 1,
        "attack_dispositions": dispositions,
        "counts_retained_this_round": retained,
        "counts_abandoned_this_round": [],
        "counts_pending_rebuttal": [],
        "follow_up_questions": [],
        "cumulative_case_discount_pct": cumulative,
        "settlement_target_range_usd": settlement,
        "settlement_verdict": "SETTLEMENT_READY",
        "post_defense_reasoning": "x",
    }


def test_weighted_cumulative_two_counts():
    # C1: $1M with a 20% SOL trim; C2: $3M with a 50% legitimacy concession.
    # Weighted: (0.20*1M + 0.50*3M) / 4M = 42.5% → 43 (banker's rounding aside,
    # round(42.5) == 42 in Python; use clean numbers instead).
    disps = [
        {"attack_id": "A1", "disposition": "MITIGATED", "discount_pct": 40,
         "reasoning": "r",
         "classification": {"attack_type": "statute_of_limitations",
                            "target_counts": ["C1"],
                            "out_of_window_damages_usd": 200_000,
                            "total_damages_usd": 1_000_000}},
        {"attack_id": "A2", "disposition": "MITIGATED", "discount_pct": 10,
         "reasoning": "r",
         "classification": {"attack_type": "legitimate_explanation",
                            "target_counts": ["C2"],
                            "grounded": True, "goes_to": "falsity_core",
                            "resolvable_by": "never"}},
    ]
    out = apply_deterministic_discounts(
        _adv(disps, ["C1", "C2"], cumulative=15, settlement="$9.9M – $9.9M"),
        _build([("C1", 1_000_000), ("C2", 3_000_000)]))
    d1, d2 = out["attack_dispositions"]
    assert d1["discount_pct"] == 20 and d1["discount_pct_model"] == 40
    assert d2["discount_pct"] == 50 and d2["discount_pct_model"] == 10
    assert d2["classification"]["warning"]  # never-resolvable → should-be-kill
    # (0.20*1M + 0.50*3M) / 4M = 42.5 → round → 42
    assert out["cumulative_case_discount_pct"] == 42
    # settlement high = 3 * (1 - 0.42) * 4M = 6.96M; low = high/2
    assert out["settlement_target_range_usd"] == "$3.5M – $7.0M"


def test_survived_and_dispositive_untouched():
    disps = [
        {"attack_id": "A1", "disposition": "SURVIVED", "discount_pct": 0,
         "reasoning": "r"},
        {"attack_id": "A2", "disposition": "DISPOSITIVE_NOT_SURVIVED",
         "discount_pct": 0, "reasoning": "r",
         "classification": {"attack_type": "other", "target_counts": ["C2"],
                            "severity": "dispositive"}},
    ]
    out = apply_deterministic_discounts(
        _adv(disps, ["C1"], cumulative=10, settlement="$1.0M – $2.0M"),
        _build([("C1", 1_000_000)]))
    assert out["attack_dispositions"][0] == disps[0]
    assert out["attack_dispositions"][1] == disps[1]
    # No MITIGATED recompute happened → model's cumulative/settlement stand.
    assert out["cumulative_case_discount_pct"] == 10
    assert out["settlement_target_range_usd"] == "$1.0M – $2.0M"


def test_backcompat_no_classification_falls_back_to_model_pick():
    disps = [{"attack_id": "A1", "disposition": "MITIGATED",
              "discount_pct": 40, "reasoning": "r"}]  # legacy: no classification
    out = apply_deterministic_discounts(
        _adv(disps, ["C1"], cumulative=40, settlement="$1.8M – $1.8M"),
        _build([("C1", 1_000_000)]))
    d = out["attack_dispositions"][0]
    assert d["discount_pct"] == 40
    assert "discount_pct_model" not in d
    # Cumulative can't be re-derived (unmapped attack) → untouched.
    assert out["cumulative_case_discount_pct"] == 40
    assert out["settlement_target_range_usd"] == "$1.8M – $1.8M"


def test_missing_per_type_fields_fall_back_to_30_and_flag():
    disps = [{"attack_id": "A1", "disposition": "MITIGATED",
              "discount_pct": 50, "reasoning": "r",
              "classification": {"attack_type": "statute_of_limitations",
                                 "target_counts": ["C1"]}}]
    out = apply_deterministic_discounts(
        _adv(disps, ["C1"]), _build([("C1", 1_000_000)]))
    d = out["attack_dispositions"][0]
    assert d["discount_pct"] == 30 and d["discount_pct_model"] == 50
    assert d["classification"]["computed"] is False
    assert out["cumulative_case_discount_pct"] == 30


def test_idempotent_reapply_keeps_original_model_pick():
    disps = [{"attack_id": "A1", "disposition": "MITIGATED",
              "discount_pct": 40, "reasoning": "r",
              "classification": {"attack_type": "other",
                                 "target_counts": ["C1"],
                                 "severity": "minor"}}]
    adv = _adv(disps, ["C1"])
    build = _build([("C1", 1_000_000)])
    once = apply_deterministic_discounts(adv, build)
    twice = apply_deterministic_discounts(once, build)
    assert twice["attack_dispositions"][0]["discount_pct"] == 10
    assert twice["attack_dispositions"][0]["discount_pct_model"] == 40


def test_input_not_mutated():
    disps = [{"attack_id": "A1", "disposition": "MITIGATED",
              "discount_pct": 40, "reasoning": "r",
              "classification": {"attack_type": "other",
                                 "target_counts": ["C1"],
                                 "severity": "minor"}}]
    adv = _adv(disps, ["C1"])
    apply_deterministic_discounts(adv, _build([("C1", 1_000_000)]))
    assert adv["attack_dispositions"][0]["discount_pct"] == 40
    assert "discount_pct_model" not in adv["attack_dispositions"][0]


# ── schema back-compat ───────────────────────────────────────────────────────


def test_schema_validates_legacy_disposition_without_new_fields():
    d = AttackDisposition.model_validate(
        {"attack_id": "A1", "disposition": "MITIGATED",
         "discount_pct": 20, "reasoning": "r"})
    assert d.classification is None and d.discount_pct_model is None


def test_schema_accepts_new_fields():
    d = AttackDisposition.model_validate(
        {"attack_id": "A1", "disposition": "MITIGATED", "discount_pct": 20,
         "discount_pct_model": 40, "reasoning": "r",
         "classification": {"attack_type": "other", "target_counts": ["C1"],
                            "severity": "mitigating", "computed": True}})
    assert d.classification["attack_type"] == "other"
