"""Tier-inflation linter (R3) — pure-function tests."""
from __future__ import annotations

from fraud_investigator.tier_lint import (
    format_code_identity_flags,
    format_tier_flags,
    format_unit_claim_flags,
    lint_code_identity_claims,
    lint_evidence_tiers,
    lint_unit_claim_semantics,
    unit_claim_flag_text,
)


def _ev(tier, fact, id="ev-1-x"):
    return {"id": id, "tier": tier, "fact": fact}


def test_modal_language_on_confirmed_fact_flags():
    flags = lint_evidence_tiers([
        _ev("confirmed_fact",
            "FPS would have detected the 87798 outlier by mid-2021."),
    ])
    assert len(flags) == 1
    assert flags[0]["rule"] == "modal_inferential_language"
    assert flags[0]["matched"].lower() == "would have"


def test_all_documented_modal_phrases_flag():
    phrases = ["would have", "must have", "had to have", "structurally would",
               "no later than", "by no later than"]
    for ph in phrases:
        flags = lint_evidence_tiers(
            [_ev("confirmed_fact", f"The defendant {ph} submitted claims.")])
        assert flags, ph


def test_government_event_claim_flags():
    flags = lint_evidence_tiers([
        _ev("confirmed_fact",
            "CMS knew of the 87798 outlier signal by mid-2021."),
    ])
    assert len(flags) == 1
    assert flags[0]["rule"] == "government_event_claim"


def test_lower_tiers_never_flag():
    for tier in ("statistical_signal", "legal_hypothesis"):
        assert lint_evidence_tiers(
            [_ev(tier, "CMS would have detected this; DOJ knew about it.")]) == []


def test_clean_confirmed_facts_pass():
    flags = lint_evidence_tiers([
        _ev("confirmed_fact",
            "NPPES lists the defendant at 100 Main St as of 2026-05-20."),
        _ev("confirmed_fact",
            "GAO-13-104 states FPS prevented $210.7M in FY2012-2013."),
    ])
    assert flags == []


def test_format_block():
    assert format_tier_flags([]) == ""
    flags = lint_evidence_tiers(
        [_ev("confirmed_fact", "She must have known | the rule.")])
    block = format_tier_flags(flags)
    assert "Tier-inflation lint" in block
    assert "ev-1-x" in block
    assert "\\|" in block  # pipe escaped for the markdown table


# ── Code-identity lint (failure_mode_taxonomy_2026-07.md #4) ────────────────
# Hermetic — resolve_code is injected as a stub (no real hcpcs_lookup.py /
# parquet access). Fixtures mirror the real production failures: C4E2135E
# (64612 blepharospasm-benchmark vs the target's actual 64615 chronic-migraine
# code) and E8A553FE (J9299 misidentified — "the second instance of the same
# recall error in the build").

_REF = {
    "64612": {"code": "64612",
              "descriptor": "Injection of chemical for paralysis of nerve muscles on side of face",
              "canonical_short_descriptor": "Destroy nerve face muscle"},
    "64615": {"code": "64615",
              "descriptor": "Injection of chemical for paralysis of facial and neck nerve muscles on both sides of face",
              "canonical_short_descriptor": "Chemodenerv musc migraine"},
    "J9299": {"code": "J9299", "descriptor": "Injection, nivolumab, 1 mg",
              "canonical_short_descriptor": "Injection, nivolumab"},
}


def _stub_resolve(code):
    return _REF.get(code.upper())


def test_correct_code_identity_does_not_flag():
    flags = lint_code_identity_claims(
        [_ev("confirmed_fact", "J9299 is nivolumab, an immunotherapy drug.")],
        resolve_code=_stub_resolve,
    )
    assert flags == []


def test_correct_migraine_code_does_not_flag_via_canonical_short_descriptor():
    """64615's CMS descriptor never says 'migraine' but its canonical MPFS-RVU
    short descriptor does — the lint must check both fields."""
    flags = lint_code_identity_claims(
        [_ev("confirmed_fact", "64615 is chronic migraine chemodenervation treatment.")],
        resolve_code=_stub_resolve,
    )
    assert flags == []


def test_wrong_drug_identity_flags():
    """The E8A553FE class: J9299 misidentified as a different drug."""
    flags = lint_code_identity_claims(
        [_ev("confirmed_fact", "J9299 is pembrolizumab, an immunotherapy drug.")],
        resolve_code=_stub_resolve,
    )
    assert len(flags) == 1
    assert flags[0]["rule"] == "code_identity_mismatch"
    assert flags[0]["code"] == "J9299"
    assert "nivolumab" in flags[0]["resolved_descriptor"]


def test_unrecognized_code_is_not_this_lints_job():
    flags = lint_code_identity_claims(
        [_ev("confirmed_fact", "99999 is a made-up procedure code.")],
        resolve_code=_stub_resolve,
    )
    assert flags == []


def test_resolve_code_import_failure_degrades_to_noop():
    """If hcpcs_lookup can't be imported (e.g. no data/ dir), the lint must
    degrade to an empty flag list, never raise."""
    def _boom(code):
        raise RuntimeError("no parquet source")
    flags = lint_code_identity_claims(
        [_ev("confirmed_fact", "J9299 is nivolumab.")], resolve_code=_boom)
    assert flags == [], "a resolver exception must not propagate out of the lint"


def test_format_code_identity_block():
    assert format_code_identity_flags([]) == ""
    flags = lint_code_identity_claims(
        [_ev("confirmed_fact", "J9299 is pembrolizumab.")],
        resolve_code=_stub_resolve,
    )
    block = format_code_identity_flags(flags)
    assert "Code-identity lint" in block
    assert "J9299" in block


# ── Unit-vs-claim semantics lint (the Alder 10,724 episode) ──────────────────
# CALIBRATION-6DA0BBC3 pled "10,724 claims averaging $0.80–$1.23 per claim" —
# sub-dollar averages are the per-UNIT J-code billing signature (N is units,
# not claims), which muddies falsity and inflates per-claim penalty exposure
# ~100x. Pure-regex, hermetic.

_ALDER_FACT = (
    "Defendant submitted 10,724 drug-classified suppressed claims in 2022-2023 "
    "averaging $0.87 per claim under the J1100 family."
)


def test_alder_shaped_sub_dollar_jcode_claims_flag():
    flags = lint_unit_claim_semantics([_ev("statistical_signal", _ALDER_FACT)])
    assert len(flags) == 1
    f = flags[0]
    assert f["rule"] == "unit_claim_semantics"
    assert f["claim_count"] == "10,724"
    assert f["avg_per_claim_usd"] == 0.87
    assert f["jcode"] == "J1100"
    assert f["required_action"] == unit_claim_flag_text("10,724")


def test_flag_text_is_exactly_the_specified_wording():
    assert unit_claim_flag_text("10,724") == (
        "possible units≠claims (per-unit J-code billing): 10,724 may be units, "
        "not claims — resolve unit semantics before damages/penalty math "
        "(cf. 99489-minutes + svcs/bene episodes)"
    )


def test_sub_dollar_average_range_flags_without_jcode():
    """The verbatim Alder phrasing: a $0.80–$1.23 range (max still < $5)."""
    flags = lint_unit_claim_semantics([_ev(
        "statistical_signal",
        "C1 rests on 10,724 claims averaging $0.80–$1.23 per claim.")])
    assert len(flags) == 1
    assert flags[0]["avg_per_claim_usd"] == 1.23
    assert flags[0]["jcode"] is None


def test_jcode_claims_count_flags_without_average():
    flags = lint_unit_claim_semantics([_ev(
        "statistical_signal",
        "Billing shows 6,858 claims for J3490 in 2023.")])
    assert len(flags) == 1
    assert flags[0]["jcode"] == "J3490"
    assert flags[0]["avg_per_claim_usd"] is None


def test_em_count_at_normal_average_does_not_flag():
    """A 99214 count at $92/claim is a genuine claims count — no flag."""
    assert lint_unit_claim_semantics([_ev(
        "statistical_signal",
        "Defendant billed 8,547 claims for 99214 averaging $92 per claim.")]) == []


def test_units_wording_without_claims_does_not_flag():
    assert lint_unit_claim_semantics([_ev(
        "statistical_signal",
        "Defendant billed 10,724 services of J1100 averaging $0.87 per service.")]) == []


def test_format_unit_claim_block():
    assert format_unit_claim_flags([]) == ""
    flags = lint_unit_claim_semantics([_ev("statistical_signal", _ALDER_FACT)])
    block = format_unit_claim_flags(flags)
    assert "Unit-vs-claim semantics lint" in block
    assert "10,724" in block
    assert "J1100" in block
    assert "ev-1-x" in block
