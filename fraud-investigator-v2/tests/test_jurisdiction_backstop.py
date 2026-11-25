"""Regression for the jurisdiction-bar fix (the Spruce/Warren case).

Bug: legal_adv ruled the Warren first-to-file attack DISPOSITIVE_NOT_SURVIVED but
left attack_type=None, so build_scorecard's `surviving & _JURISDICTION` missed it →
jurisdiction_barred=False → P6 jurisdiction_clear=MET → verdict FILE_WITH_DISCOVERY_DEPS
that contradicted the case's own "$0 / barred" economics.

Layer 1 (verdict): a text backstop classifies a dispositive attack from its reasoning
when attack_type is null → jurisdiction_barred=True → P6 FAILS.
Layer 2 (checkpoint): the jurisdiction detector then fires on a sealed prior qui tam of
unknowable SCOPE (Warren), not only an unverified-NATURE sealed case (Dogwood).

Hermetic; fixtures mirror the real CASE-6A378AB5 shapes.

Run: pytest fraud-investigator-v2/tests/test_jurisdiction_backstop.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fraud_investigator import verdict as V  # noqa: E402
from fraud_investigator import checkpoint as C  # noqa: E402
from fraud_investigator.schemas import FinalVerdict, ReviewRequest  # noqa: E402

FILE_CLASS = {FinalVerdict.SETTLEMENT_READY, FinalVerdict.FILE_WITH_DISCOVERY_DEPS}

# The real Spruce/Warren dispositive disposition reasoning (attack_type was None).
WARREN_FTF_REASONING = (
    "The R1-requested rebuttal resolved AGAINST the relator: no Warren termination event "
    "(presumed PENDING under Carter) and sealed/unknowable scope (the relator cannot show "
    "the material elements differ). Warren v. Wound Co Nutmeg LLC names the exact single-purpose "
    "entity; first-to-file under 31 U.S.C. 3730(b)(5)."
)
# A dispositive attack that is NOT jurisdictional (control — backstop must NOT fire).
MERITS_REASONING = (
    "The statistical outlier is equally consistent with a lawful explanation; the relator's "
    "Rule 9(b) particularity is insufficient as pleaded."
)
WARREN_EV = {
    "id": "ev-2-warren-qui-tam-first-to-file", "tier": "confirmed_fact",
    "fact": ("Warren v. Wound Co Nutmeg LLC, SDTX 9:99-cv-00002, was filed 2025-12-12 as a Qui Tam "
             "action under 31 U.S.C. 3729(a) (nature code 376), naming Wound Co Nutmeg LLC."),
    "citation": "courtlistener docket 9:99-cv-00002",
}


def _summary(*, attack_type, reasoning, all_merits_met=True):
    """A serialized CaseState where every predicate but P6 is MET, plus one
    DISPOSITIVE_NOT_SURVIVED adversarial attack with the given type/reasoning."""
    build = {
        "counts_pled": [{"count_id": "C1", "damages_floor_usd": 206_000_000, "ceiling_requires": True}],
        "factual_basis_per_count": [{"count_id": "C1", "falsity_anchor_evidence_ids": ["ev-x"]}],
    } if all_merits_met else {}
    return {
        "iterations": [{"phase": "rebuttal_closing", "counsel_build": build}],
        "counts_retained": ["C1"] if all_merits_met else [],
        "foreclosure_ledger": {"fraud_is_more_plausible_inference": True},
        "expected_value_factor": 0.35,
        "adversarial_rounds": [{
            "legal_adv_response": {"attack_dispositions": [
                {"disposition": "DISPOSITIVE_NOT_SURVIVED",
                 "classification": {"attack_type": attack_type},
                 "reasoning": reasoning},
            ]},
        }],
    }


# ── Layer 1: the verdict text backstop ───────────────────────────────────────

def test_backstop_catches_ftf_when_attack_type_null():
    """The bug: attack_type=None + first-to-file reasoning → was missed. Now caught."""
    sc = V.build_scorecard(_summary(attack_type=None, reasoning=WARREN_FTF_REASONING))
    assert sc.jurisdiction_barred is True
    dec = V.compute_verdict(sc)
    p6 = next(p for p in dec.predicates if p.name == "jurisdiction_clear")
    assert p6.status == "FAILED"
    assert dec.verdict not in FILE_CLASS  # no more false FILE


def test_backstop_specific_not_blanket():
    """A dispositive NON-jurisdiction attack (null type) must NOT trip the bar —
    otherwise the whole case (all other predicates MET) would still FILE."""
    sc = V.build_scorecard(_summary(attack_type=None, reasoning=MERITS_REASONING))
    assert sc.jurisdiction_barred is False
    dec = V.compute_verdict(sc)
    assert dec.verdict in FILE_CLASS


def test_explicit_attack_type_still_works():
    sc = V.build_scorecard(_summary(attack_type="first_to_file_bar", reasoning="(unset)"))
    assert sc.jurisdiction_barred is True


def test_public_disclosure_text_backstop():
    sc = V.build_scorecard(_summary(
        attack_type=None,
        reasoning="The scheme was publicly disclosed in the criminal indictment — 31 U.S.C. 3730(e)(4) public-disclosure bar."))
    assert sc.jurisdiction_barred is True


# ── Layer 2: the checkpoint fires on a sealed-SCOPE prior qui tam (Warren) ────

def test_checkpoint_fires_on_sealed_scope_qui_tam():
    """Warren's nature is CONFIRMED (NOS 376); its SCOPE is sealed — so whether it bars
    these claims is unverifiable → pause, don't auto-bar."""
    rr = C.detect_jurisdiction_checkpoint(
        case_id="CASE-SPRUCE",
        evidence=[WARREN_EV],
        disposition_reasoning=WARREN_FTF_REASONING,  # carries "sealed/unknowable scope"
        jurisdiction_barred=True,
        provisional_verdict="NOT_ACTIONABLE",
    )
    assert isinstance(rr, ReviewRequest)
    assert rr.checkpoint_type == "jurisdiction_bar_unverifiable"
    assert rr.cited_evidence_id == "ev-2-warren-qui-tam-first-to-file"
    assert "scope" in rr.why_unverifiable.lower()


def test_checkpoint_silent_when_scope_not_sealed():
    """A confirmed prior qui tam with a known/public scope is not unverifiable → no pause."""
    rr = C.detect_jurisdiction_checkpoint(
        case_id="c",
        evidence=[WARREN_EV],
        disposition_reasoning="first-to-file bar; the Warren complaint is unsealed and its counts are public and identical.",
        jurisdiction_barred=True,
        provisional_verdict="NOT_ACTIONABLE",
    )
    assert rr is None
