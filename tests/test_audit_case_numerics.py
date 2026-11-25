"""Guardrail 2 — deterministic numeric-predicate audit.

Pins the behavior that would have caught the Birch 99489 error: a unit->time claim
built on the wrong (60-min PUF) descriptor is flagged MISMATCH, while the
canonical (30-min AMA-CPT) figure passes. Hermetic — 99489 resolves through the
in-repo authoritative-override table (no parquet / network needed); the generic
arithmetic kinds need no data either.

Run: pytest tests/test_audit_case_numerics.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import audit_case_numerics as A  # noqa: E402
from hcpcs_lookup import canonical_minutes_per_unit  # noqa: E402


# ── single source of truth ──────────────────────────────────────────────────

def test_99489_resolves_to_30_via_override():
    res = canonical_minutes_per_unit("99489", 2023)
    assert res is not None
    assert res["minutes_per_unit"] == 30          # AMA-CPT, NOT the PUF's 60
    assert res["source"] == "authoritative_override"


# ── the Birch catch ────────────────────────────────────────────────────────────
# 99489 = each-additional 30 min. 118,800 units -> 59,400 h at 30 min; the buggy
# 60-min PUF descriptor would have yielded 118,800 h (the figure that reached
# attorney-facing output). The audit must reject the inflated claim.

_CODES = [{"code": "99489", "units": 118800, "year": 2023}]

def test_inflated_60min_figure_is_mismatch():
    f = A.audit_predicate({"id": "wrong", "kind": "time_impossibility",
                           "codes": _CODES, "claimed": {"total_hours": 118800}})
    assert f["status"] == A.MISMATCH
    assert round(f["recomputed"]) == 59400
    assert f["constants_used"][0]["minutes_per_unit"] == 30
    assert f["constants_used"][0]["source"] == "authoritative_override"

def test_canonical_30min_figure_matches():
    f = A.audit_predicate({"id": "right", "kind": "time_impossibility",
                           "codes": _CODES, "claimed": {"total_hours": 59400}})
    assert f["status"] == A.MATCH

def test_audit_predicates_gates_on_any_mismatch():
    findings = A.audit_predicates([
        {"id": "wrong", "kind": "time_impossibility", "codes": _CODES,
         "claimed": {"total_hours": 118800}},
        {"id": "right", "kind": "time_impossibility", "codes": _CODES,
         "claimed": {"total_hours": 59400}},
    ])
    assert any(f["status"] == A.MISMATCH for f in findings)


# ── generic arithmetic kinds (no data needed) ───────────────────────────────

def test_fte_equivalent():
    ok = A.audit_predicate({"id": "f", "kind": "fte_equivalent", "total_hours": 59400,
                            "productive_hours_per_fte": 2080, "claimed": {"fte": 28.56}})
    assert ok["status"] == A.MATCH
    bad = A.audit_predicate({"id": "f", "kind": "fte_equivalent", "total_hours": 59400,
                             "productive_hours_per_fte": 2080, "claimed": {"fte": 5}})
    assert bad["status"] == A.MISMATCH

def test_ratio_multiple_damages():
    assert A.audit_predicate({"id": "r", "kind": "ratio", "numerator": 4200,
                              "denominator": 100, "claimed": {"ratio": 42}})["status"] == A.MATCH
    assert A.audit_predicate({"id": "m", "kind": "multiple", "value": 858273,
                              "baseline": 1000, "claimed": {"multiple": 858}})["status"] == A.MATCH
    assert A.audit_predicate({"id": "d", "kind": "damages", "units": 100,
                              "unit_amount_usd": 42.5, "claimed": {"damages_usd": 4250}})["status"] == A.MATCH

def test_malformed_predicate_is_unverifiable():
    # fte_equivalent without productive_hours_per_fte -> can't re-derive, not a silent pass
    f = A.audit_predicate({"id": "x", "kind": "fte_equivalent", "total_hours": 100,
                           "claimed": {"fte": 1}})
    assert f["status"] == A.UNVERIFIABLE


# ── Guardrail-2 predicate-schema repair (failure_mode_taxonomy_2026-07.md #3) ──
# 44/48 predicates UNVERIFIABLE corpus-wide; the two real MISMATCHes were BOTH
# false positives from a rank predicate emitted as kind="ratio". This is the
# real, verbatim c1_national_rank_2022 predicate from
# fraud-investigator-v2/state/CASE-6A305421-2026/iter-01/counsel_build.parsed.json
# that spawned 4 wasted adversarial attacks across 2 rounds (§1, §4d).

_REAL_RANK_PREDICATE = {
    "id": "c1_national_rank_2022",
    "kind": "ratio",
    "claimed": {
        "q4xx_rank": 5, "q4xx_cohort": 394, "q4xx_percentile": 98.7,
        "q4210_rank": 1, "q4210_cohort": 13,
    },
    "statement": "Q4xx 2022: #5 of 394 podiatrists billing Q4xx to >=11 benes "
                 "(98.7th pct); #1 of 13 national Q4210 billers.",
    "numerator": 5,
    "denominator": 394,
}

_REAL_RANK_PREDICATE_2 = {
    "id": "c2_naa_rank_2022",
    "kind": "ratio",
    "claimed": {"rank": 6, "cohort": 118, "percentile": 94.9, "multiple_over_avg": 5.0},
    "statement": "87798 2022: #6 of 118 podiatrists (94.9th pct, borderline-extremity); "
                 "8,331 srvcs vs national podiatric average 1,657 (5.0x).",
    "numerator": 6,
    "denominator": 118,
}


def test_real_rank_predicate_no_longer_false_mismatches():
    """Regression for the CASE-6A305421 false MISMATCH: a rank claim emitted as
    kind='ratio' (numerator=rank, denominator=cohort) must be recomputed as a
    PERCENTILE, not compared as a bare rank/cohort quotient against the raw rank
    number. Before the fix: claimed=5.0, recomputed=5/394=0.0127, 99.7% "error",
    reported MISMATCH — a false positive that cost 4 wasted adversarial attacks."""
    f = A.audit_predicate(_REAL_RANK_PREDICATE)
    assert f["status"] == A.MATCH, f
    assert f["claimed"] == 98.7
    assert round(f["recomputed"], 1) == 99.0

    f2 = A.audit_predicate(_REAL_RANK_PREDICATE_2)
    assert f2["status"] == A.MATCH, f2
    assert f2["claimed"] == 94.9
    assert round(f2["recomputed"], 1) == 95.8


def test_market_share_kind_is_a_ratio_alias():
    """market_share (the exact taxonomy example) must not fall through to
    'unknown predicate kind' -> UNVERIFIABLE; it is semantically a ratio."""
    f = A.audit_predicate({"id": "np-market-share", "kind": "market_share",
                           "claimed": {"q4247_pct": 30.2},
                           "numerator": 4704, "denominator": 15565})
    assert f["status"] == A.MATCH
    assert "unknown predicate kind" not in (f["detail"] or "")


def test_ring_enrichment_denominator_shape_is_verifiable():
    """The np-ring-pecos-mismatch shape (codes[0] carries count/ring_size, not
    the hardcoded national_services/beneficiaries/... names) must resolve
    instead of UNVERIFIABLE 'no explicit numerator/denominator'."""
    f = A.audit_predicate({
        "id": "np-ring-pecos-mismatch", "kind": "ratio",
        "claimed": {"mismatch_rate_pct": 37.5,
                    "method": "members_with_mismatch / ring_size * 100"},
        "codes": [{"item": "members_with_mismatch", "count": 3, "ring_size": 8}],
    })
    assert f["status"] == A.MATCH
    assert f["recomputed"] == 37.5


def test_bare_rank_without_cohort_is_unverifiable_not_a_crash():
    f = A.audit_predicate({"id": "x", "kind": "ratio", "claimed": {"some_rank": 5}})
    assert f["status"] == A.UNVERIFIABLE


def test_sum_addends_with_labeled_dict_shape_is_verifiable():
    """The real counsel_build 'sum' shape is [{label, amount}, ...], not bare
    numbers — the original addends=[float(a) for a in addends] raised
    TypeError on a dict, reported as 'malformed predicate' (UNVERIFIABLE)."""
    f = A.audit_predicate({
        "id": "agg_single_damages", "kind": "sum",
        "claimed": {"value": 10649879},
        "addends": [{"label": "C1_q4xx", "amount": 10238144},
                    {"label": "C2_naa", "amount": 411735}],
    })
    assert f["status"] == A.MATCH
    assert f["recomputed"] == 10649879


def test_real_case_6a305421_predicate_set_all_verifiable():
    """Full regression: every numeric_predicate emitted in the real
    CASE-6A305421 counsel_build (the case whose false rank-MISMATCH triggered
    this repair) must now resolve to MATCH or MISMATCH — never UNVERIFIABLE —
    given the shapes actually seen in production."""
    preds = [
        {"id": "agg_single_damages", "kind": "sum", "claimed": {"value": 10649879},
         "addends": [{"label": "C1_q4xx", "amount": 10238144},
                     {"label": "C2_naa", "amount": 411735}]},
        {"id": "c1_q4xx_cumulative_floor", "kind": "sum", "claimed": {"value": 10238144},
         "addends": [{"label": "2021_Q4188", "amount": 1875376},
                     {"label": "2022_Q4xx", "amount": 5691905},
                     {"label": "2023_Q4210_est", "amount": 1340851},
                     {"label": "2024_drug_paid", "amount": 1330012}]},
        {"id": "c2_naa_floor", "kind": "sum", "claimed": {"value": 411735},
         "addends": [{"label": "2021_87798", "amount": 123447},
                     {"label": "2022_87798", "amount": 288288}]},
        _REAL_RANK_PREDICATE,
        _REAL_RANK_PREDICATE_2,
    ]
    findings = A.audit_predicates(preds)
    assert all(f["status"] in (A.MATCH, A.MISMATCH) for f in findings), findings


# ── Penalty-exposure unit-semantics (units≠claims; additive status) ─────────
# Provider Alder CALIBRATION-6DA0BBC3 episode: C1 asserted "10,724 claims" whose
# average payment per "claim" was sub-dollar — the per-UNIT J-code billing
# signature. Per-claim penalty math (× $13,946–$27,894) on a units count
# inflates exposure ~100x. The audit flags these UNVERIFIABLE_UNIT_SEMANTICS —
# an additive review status, deliberately NEVER MISMATCH (nothing is provably
# wrong; a false dispositive defect would tank a real case).

_ALDER_PENALTY_PRED = {
    # Verbatim shape from CALIBRATION-6DA0BBC3 iter-02 counsel_build.
    "id": "c1_civil_penalty_exposure",
    "kind": "civil_penalty_exposure",
    "claimed": {
        "total_drug_suppressed_claims_2022_2023": 10724,
        "min_penalty_per_claim_2026": 13946,
        "max_penalty_per_claim_2026": 27894,
        "total_min_civil_penalty": 149599304,
        "total_max_civil_penalty": 299248056,
    },
    "statement": ("If each of 10,724 drug-classified suppressed claims (3,866 in "
                  "2022 + 6,858 in 2023) is a separate false presentment: minimum "
                  "civil penalty at 2026 rates: 10,724 × $13,946 = $149,599,304; "
                  "maximum: 10,724 × $27,894 = $299,248,056."),
    "codes": [],
}


def test_real_alder_penalty_predicate_is_unit_semantics_not_mismatch():
    f = A.audit_predicate(_ALDER_PENALTY_PRED)
    assert f["status"] == A.UNVERIFIABLE_UNIT_SEMANTICS, f
    assert f["claimed"] == 10724
    assert f["recomputed"] is None
    assert "10,724 may be units, not claims" in f["detail"]
    assert "drug-classified" in f["detail"]


def test_alder_shaped_sub_dollar_average_fires():
    f = A.audit_predicate({
        "id": "x", "kind": "civil_penalty_exposure",
        "claimed": {"total_claims": 10724, "min_penalty_per_claim": 13946},
        "statement": "10,724 claims averaging $0.87 per claim × $13,946 each.",
    })
    assert f["status"] == A.UNVERIFIABLE_UNIT_SEMANTICS
    assert "avg payment per claim $0.87" in f["detail"]


def test_jcode_in_codes_fires():
    f = A.audit_predicate({
        "id": "x", "kind": "civil_penalty_exposure",
        "claimed": {"total_claims": 6858, "min_penalty_per_claim": 13946},
        "statement": "Each of 6,858 claims is a separate presentment.",
        "codes": [{"code": "J1100", "units": 6858}],
    })
    assert f["status"] == A.UNVERIFIABLE_UNIT_SEMANTICS
    assert "J1100" in f["detail"]


def test_em_penalty_predicate_at_normal_average_does_not_fire():
    """A 99214 E&M penalty predicate at $92/claim carries no units≠claims
    signal — it must fall through to the normal path (here: unknown kind →
    plain UNVERIFIABLE), never the unit-semantics status."""
    f = A.audit_predicate({
        "id": "c2_penalty", "kind": "civil_penalty_exposure",
        "claimed": {"total_em_claims": 8547, "min_penalty_per_claim": 13946},
        "statement": "8,547 claims for 99214 averaging $92 per claim × $13,946.",
    })
    assert f["status"] == A.UNVERIFIABLE
    assert "unknown predicate kind" in f["detail"]


def test_non_penalty_predicates_never_get_unit_semantics_status():
    """The check is scoped to penalty-exposure predicates: a plain ratio with
    claims wording keeps its existing verdict (PR #348 schema untouched)."""
    f = A.audit_predicate({"id": "r", "kind": "ratio", "numerator": 4200,
                           "denominator": 100, "claimed": {"ratio": 42},
                           "statement": "10,724 claims averaging $0.87 per claim"})
    assert f["status"] == A.MATCH


def test_flag_text_pinned_to_tier_lint_wording():
    """The audit's detail text and the tier_lint required_action are the SAME
    sentence (duplicated by design across the standalone-script boundary;
    this pin is what prevents drift)."""
    sys.path.insert(0, str(REPO / "fraud-investigator-v2" / "src"))
    from fraud_investigator.tier_lint import unit_claim_flag_text
    f = A.audit_predicate(_ALDER_PENALTY_PRED)
    assert unit_claim_flag_text("10,724") in f["detail"]
