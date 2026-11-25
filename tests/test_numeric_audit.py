"""Guardrail-2 numeric audit (scripts/audit_case_numerics.py).

Pins the 2026-06-14 fix: the audit's ratio/damages handlers + claimed-value
extractor had drifted from the counsel_build `numeric_predicates` schema, so
EVERY ratio predicate came back UNVERIFIABLE ("malformed predicate: 'numerator'")
and the guardrail silently did nothing. These tests pin that (a) the real agent
schema (codes[] + free-form claimed{}) re-derives, (b) a genuinely inflated number
is caught as MISMATCH, and — most important — (c) a predicate whose denominator
isn't carried in it returns UNVERIFIABLE, NEVER a false MISMATCH (a false
dispositive defect would wrongly tank a real case).
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import audit_case_numerics as a  # noqa: E402


def _status(pred):
    return a.audit_predicate(pred)["status"]


def test_market_share_ratio_from_codes_matches():
    # Real Bradley shape: percent ratio carried as units / national_services in
    # codes[], computed value in a free-form `*_pct` claimed key.
    pred = {"id": "np-market-share", "kind": "ratio",
            "claimed": {"q4247_pct": 30.2, "method": "units / national_services * 100"},
            "codes": [{"code": "Q4247", "units": 4704, "national_services": 15565}]}
    f = a.audit_predicate(pred)
    assert f["status"] == "MATCH", f
    assert abs(f["recomputed"] - 30.22) < 0.1


def test_per_bene_ratio_matches_without_pct_scale():
    pred = {"id": "np-naa-intensity", "kind": "ratio",
            "claimed": {"tests_per_bene": 27.4, "method": "units / beneficiaries"},
            "codes": [{"code": "87798", "units": 8331, "beneficiaries": 304}]}
    assert _status(pred) == "MATCH"


def test_inflated_ratio_is_mismatch():
    # Same inputs (→ 30.2%) but a claim of 80% — the re-derivation must catch it.
    pred = {"id": "x", "kind": "ratio", "claimed": {"share_pct": 80.0},
            "codes": [{"units": 4704, "national_services": 15565}]}
    assert _status(pred) == "MISMATCH"


def test_unrederivable_ratio_is_unverifiable_not_false_mismatch():
    # No denominator in the predicate (the national-biller-mean lives elsewhere).
    # Must be UNVERIFIABLE — never a guessed denominator → false MISMATCH.
    pred = {"id": "c2_autonomic", "kind": "ratio",
            "claimed": {"ratio_vs_national_biller_mean": 3.47, "percentile": 0.961},
            "codes": [{"code": "95924", "units": 243, "year": 2024}]}
    assert _status(pred) == "UNVERIFIABLE"


def test_percentile_is_not_treated_as_percent():
    # "percentile" must NOT trigger the ×100 percent scale.
    pred = {"id": "p", "kind": "ratio", "claimed": {"percentile": 0.5},
            "codes": [{"units": 5, "beneficiaries": 10}]}
    f = a.audit_predicate(pred)
    assert f["recomputed"] == 0.5  # raw, not 50


def test_damages_from_components_matches():
    pred = {"id": "np-c1-damages", "kind": "damages",
            "claimed": {"point_usd": 3_000_000},
            "components": [{"code": "A", "est_usd": 1_000_000},
                           {"code": "B", "est_usd": 2_000_000}]}
    assert _status(pred) == "MATCH"


def test_explicit_numerator_denominator_still_works():
    pred = {"id": "legacy", "kind": "ratio", "claimed": {"ratio": 2.0},
            "numerator": 10, "denominator": 5}
    assert _status(pred) == "MATCH"


def test_time_impossibility_unchanged():
    # Smoke: the time-impossibility path (the 99489-episode class) is untouched.
    pred = {"id": "t", "kind": "time_impossibility", "claimed": {"total_hours": 1.0},
            "codes": []}
    # empty codes → 0 hours; just assert it doesn't crash and yields a finding
    assert a.audit_predicate(pred)["kind"] == "time_impossibility"
