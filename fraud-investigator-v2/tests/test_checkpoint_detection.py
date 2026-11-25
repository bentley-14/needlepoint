"""Unit tests for the Guardrail-4 human-in-the-loop checkpoint detectors
(checkpoint.detect_jurisdiction_checkpoint / detect_foreclosure_checkpoint).

Hermetic — fixtures mirror the real Birch (CASE-6A368C27) failure shape (a sealed
prior case asserted as a qui tam at confirmed_fact tier, driving a first-to-file
ABANDON) without reading production state.

Run: pytest fraud-investigator-v2/tests/test_checkpoint_detection.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fraud_investigator import checkpoint as C  # noqa: E402
from fraud_investigator.schemas import ReviewRequest  # noqa: E402


# ── Fixtures faithful to the Birch failure ──────────────────────────────────────

# The tier-inflated, unverifiable premise that killed Birch.
BIRCH_SEALED_QUI_TAM_EV = {
    "id": "ev-1-qui-tam-filed",
    "tier": "confirmed_fact",
    "fact": (
        "A qui tam lawsuit — Dogwood v. Agency Myrtle LLC — was docketed in N.D. Tex. "
        "on 2025-09-17 (docket 9:99-cv-00001); the complaint remains under seal with "
        "no relator counsel publicly identified."
    ),
    "citation": "python scripts/courtlistener_search.py dockets-party 'Agency Myrtle'",
}
BIRCH_DISP_REASONING = (
    "the dispositive defect is not discovery-curable: Dogwood v. Agency Myrtle "
    "(WL-1, first-to-file, 31 USC 3730(b)(5)) remains pending under seal against "
    "the same defendant on the same CCM conduct."
)


def test_jurisdiction_fires_on_sealed_quitam_confirmed_fact():
    rr = C.detect_jurisdiction_checkpoint(
        case_id="CASE-BIRCH",
        evidence=[BIRCH_SEALED_QUI_TAM_EV],
        disposition_reasoning=BIRCH_DISP_REASONING,
        provisional_verdict="COUNSEL_ABANDONED",
    )
    assert isinstance(rr, ReviewRequest)
    assert rr.checkpoint_type == "jurisdiction_bar_unverifiable"
    assert rr.cited_evidence_id == "ev-1-qui-tam-filed"
    assert "first-to-file" in rr.question.lower() or "qui tam" in rr.question.lower()
    assert not rr.resolved


def test_jurisdiction_seal_signal_from_reasoning_only():
    """The seal cue may live in the disposition reasoning, not the evidence text."""
    ev = dict(BIRCH_SEALED_QUI_TAM_EV, fact="Dogwood v. Agency Myrtle is a qui tam (relator action) on the same facts.")
    rr = C.detect_jurisdiction_checkpoint(
        case_id="c", evidence=[ev],
        disposition_reasoning="first-to-file bar; Dogwood remains under seal.",
    )
    assert rr is not None


def test_jurisdiction_silent_on_verified_unsealed_quitam():
    """A verified, unsealed qui tam with a public complaint is NOT unverifiable."""
    verified = [{
        "id": "ev-x", "tier": "confirmed_fact",
        "fact": "Doe v. Acme is an FCA qui tam; the unsealed complaint (NOS 376) is public on CourtListener.",
        "citation": "courtlistener",
    }]
    rr = C.detect_jurisdiction_checkpoint(
        case_id="c", evidence=verified,
        disposition_reasoning="first-to-file bar: Doe v. Acme covers the same facts",
    )
    assert rr is None


def test_jurisdiction_silent_when_no_bar_in_play():
    rr = C.detect_jurisdiction_checkpoint(
        case_id="c", evidence=[BIRCH_SEALED_QUI_TAM_EV],
        disposition_reasoning="abandoned on weak scienter; no jurisdictional issue",
    )
    assert rr is None


def test_jurisdiction_silent_when_premise_is_not_confirmed_fact():
    """If the qui-tam claim is only inferred (not confirmed_fact), the tier-lint
    owns it — no checkpoint needed (it never should have been dispositive)."""
    ev = dict(BIRCH_SEALED_QUI_TAM_EV, tier="legal_hypothesis")
    rr = C.detect_jurisdiction_checkpoint(
        case_id="c", evidence=[ev], disposition_reasoning=BIRCH_DISP_REASONING,
    )
    assert rr is None


# ── Foreclosure checkpoint ───────────────────────────────────────────────────

def _ledger(*, dispositive: bool, status: str, closure_owner: str):
    return {
        "fraud_is_more_plausible_inference": (not dispositive),
        "explanations": [{
            "id": "exp-1", "status": status, "closure_owner": closure_owner,
            "steelman": "A large incident-to clinical staff furnished the billed time.",
            "residual": "a verified FTE roster would close it",
        }],
    }


def test_foreclosure_fires_on_dispositive_discovery_closable_live():
    rr = C.detect_foreclosure_checkpoint(
        case_id="c",
        foreclosure_ledger=_ledger(dispositive=True, status="LIVE", closure_owner="discovery:fte_count"),
    )
    assert isinstance(rr, ReviewRequest)
    assert rr.checkpoint_type == "foreclosure_live_explanation"
    assert rr.cited_evidence_id == "exp-1"


def test_foreclosure_fires_on_cannot_close_pre_filing():
    rr = C.detect_foreclosure_checkpoint(
        case_id="c",
        foreclosure_ledger=_ledger(dispositive=True, status="LIVE", closure_owner="cannot_close_pre_filing"),
    )
    assert rr is not None


def test_foreclosure_silent_when_not_dispositive():
    """fraud_is_more_plausible_inference is True ⇒ not a decline ⇒ no pause."""
    rr = C.detect_foreclosure_checkpoint(
        case_id="c",
        foreclosure_ledger=_ledger(dispositive=False, status="LIVE", closure_owner="discovery:fte_count"),
    )
    assert rr is None


def test_foreclosure_silent_on_closed_on_record():
    """A row closable on the record is a merits matter, not a human checkpoint."""
    rr = C.detect_foreclosure_checkpoint(
        case_id="c",
        foreclosure_ledger=_ledger(dispositive=True, status="LIVE", closure_owner="closed_on_record"),
    )
    assert rr is None


def test_foreclosure_silent_on_no_ledger():
    assert C.detect_foreclosure_checkpoint(case_id="c", foreclosure_ledger=None) is None
    assert C.detect_foreclosure_checkpoint(case_id="c", foreclosure_ledger={}) is None
