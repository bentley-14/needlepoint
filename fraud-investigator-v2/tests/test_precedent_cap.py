"""Guardrail 5 — per-count precedent-anchor coverage (soft strength cap).

Pins that scoring.precedent_coverage matches a count's precedent_anchor against
the VERIFIED precedent set by case-name signature (not a trusted LLM flag), and
that the strength multiplier down-weights — but never zeroes — an uncovered case.
Hermetic — pure functions, no data/network.

Run: pytest fraud-investigator-v2/tests/test_precedent_cap.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fraud_investigator import scoring  # noqa: E402

VERIFIED = [
    "United States v. Mackby, 261 F.3d 821 (9th Cir. 2001)",
    "Universal Health Servs., Inc. v. United States ex rel. Escobar, 579 U.S. 176 (2016)",
    "United States ex rel. Integra Med Analytics, L.L.C. v. Baylor Scott & White Health",
]


def test_covered_count_matches_verified_anchor():
    counts = [{"precedent_anchor": "United States v. Mackby, 261 F.3d 821 (9th Cir. 2001)",
               "adverse_precedent_confronted": "Integra distinguished"}]
    cov = scoring.precedent_coverage(counts, VERIFIED)
    assert cov["coverage"] == 1.0
    assert cov["n_covered"] == 1
    assert cov["n_adverse_unconfronted"] == 0


def test_uncovered_anchor_not_in_verified_set():
    # cites a real-sounding but unverified case -> not covered
    counts = [{"precedent_anchor": "United States v. Nobody, 999 F.3d 1 (5th Cir. 2099)",
               "adverse_precedent_confronted": ""}]
    cov = scoring.precedent_coverage(counts, VERIFIED)
    assert cov["coverage"] == 0.0
    assert cov["n_adverse_unconfronted"] == 1


def test_empty_anchor_is_uncovered():
    cov = scoring.precedent_coverage([{"precedent_anchor": "", "adverse_precedent_confronted": ""}], VERIFIED)
    assert cov["coverage"] == 0.0


def test_partial_coverage_fraction():
    counts = [
        {"precedent_anchor": "Escobar, 579 U.S. 176", "adverse_precedent_confronted": "x"},  # covered
        {"precedent_anchor": "", "adverse_precedent_confronted": ""},                          # not
    ]
    assert scoring.precedent_coverage(counts, VERIFIED)["coverage"] == 0.5


def test_no_counts_is_full_coverage():
    assert scoring.precedent_coverage([], VERIFIED)["coverage"] == 1.0


def test_strength_factor_bounds_and_monotonic():
    assert scoring.precedent_strength_factor(1.0) == 1.0
    assert scoring.precedent_strength_factor(0.0) == scoring.PRECEDENT_CAP_FLOOR == 0.70
    assert scoring.precedent_strength_factor(0.5) == 0.85
    # never zeroes the case; floor is a haircut, not a kill
    assert scoring.precedent_strength_factor(0.0) > 0.0


def test_accepts_object_counts_not_just_dicts():
    class _C:
        precedent_anchor = "United States v. Mackby, 261 F.3d 821 (9th Cir. 2001)"
        adverse_precedent_confronted = "Integra distinguished"
    cov = scoring.precedent_coverage([_C()], VERIFIED)
    assert cov["coverage"] == 1.0
