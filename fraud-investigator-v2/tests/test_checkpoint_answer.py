"""Tests for the answer + resume engine (scripts/answer_checkpoint.py).

Builds a synthetic checkpointed case on disk (a Phase-B jurisdiction pause with an
ABANDON disposition + its cache), runs resolve_checkpoint, and asserts the full
rewind contract: human_attested fact injected, ReviewRequest resolved, stale
disposition nulled, its on-disk step cache purged, status reset to in_progress,
and the case no longer listed as open.

Run: pytest fraud-investigator-v2/tests/test_checkpoint_answer.py -v
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import answer_checkpoint as A  # noqa: E402
from fraud_investigator.schemas import (  # noqa: E402
    CaseState,
    CounselDispositionAction,
    CounselDispositionOutput,
    EvidenceItem,
    EvidenceTier,
    FinalVerdict,
    InvestigatorOutput,
    IterationRecord,
    ReviewRequest,
)
from fraud_investigator.state import iter_dir, load_summary, write_summary  # noqa: E402

CID = "CASE-TEST-2026"


def _checkpointed_case(state_root: Path) -> Path:
    """A Phase-B jurisdiction checkpoint: rebuttal_closing iter with an ABANDON
    disposition + a sealed-qui-tam confirmed_fact, plus its on-disk cache."""
    inv = InvestigatorOutput(case_id=CID, iteration=3, evidence=[
        EvidenceItem(
            id="ev-1-qui-tam-filed", tier=EvidenceTier.CONFIRMED_FACT,
            fact="Dogwood v. Agency Myrtle is a qui tam; docket under seal, relator unknown.",
            citation="courtlistener", confidence=0.9),
    ])
    disp = CounselDispositionOutput(
        case_id=CID, iteration=3, action=CounselDispositionAction.ABANDON_CASE,
        reasoning="first-to-file bar (31 USC 3730(b)(5)); Dogwood remains under seal.")
    it = IterationRecord(iteration=3, timestamp=datetime.utcnow(),
                         phase="rebuttal_closing", investigator=inv, counsel_disposition=disp)
    rr = ReviewRequest(
        case_id=CID, checkpoint_type="jurisdiction_bar_unverifiable",
        question="Provider Dogwood a qui tam?", unverifiable_premise="Dogwood is a qui tam",
        cited_evidence_id="ev-1-qui-tam-filed", why_unverifiable="sealed",
        provisional_verdict_if_yes="abandon", provisional_verdict_if_no="proceed",
        what_would_resolve_it="the NOS code", raised_at=datetime.now(timezone.utc))
    st = CaseState(
        case_id=CID, subject="Dr X (NPI 1900000057)",
        start_time=datetime.utcnow(), last_updated=datetime.utcnow(),
        status="needs_human_checkpoint", final_verdict=FinalVerdict.NEEDS_HUMAN_CHECKPOINT,
        iterations=[it], all_evidence_ids=["ev-1-qui-tam-filed"], review_request=rr)
    (state_root / CID).mkdir(parents=True)
    write_summary(st, state_root)
    d = iter_dir(CID, 3, state_root)
    d.mkdir(parents=True, exist_ok=True)
    (d / "counsel_disposition.done").write_text("x")
    (d / "counsel_disposition.parsed.json").write_text("{}")
    return d


def test_resolve_checkpoint_full_rewind(tmp_path):
    cache_dir = _checkpointed_case(tmp_path)
    assert len(A.list_open(tmp_path)) == 1

    info = A.resolve_checkpoint(
        CID, "Dogwood v. Agency Myrtle (9:99-cv-00001) is NOS 442 Employment, not a qui tam.",
        citation="DOGWOOD_SUMMARY.md", state_root=tmp_path)

    assert info["npi"] == "1900000057"
    assert info["resume_make"] == f"make investigate NPI=1900000057 RESUME={CID} SAFE_PERMS=1"

    st = load_summary(CID, tmp_path)
    # status + verdict reset for a clean resume
    assert st.status == "in_progress"
    assert st.final_verdict is None
    assert st.verdict_predicates == []
    # ReviewRequest resolved + carries the answer
    assert st.review_request.resolved is True
    assert "NOS 442" in st.review_request.human_answer
    # human_attested fact injected (so re-run scoring/legibility sees it)
    inv_ev = st.iterations[0].investigator.evidence
    human = [e for e in inv_ev if e.provenance == "human_attested"]
    assert len(human) == 1 and human[0].tier == EvidenceTier.CONFIRMED_FACT
    assert human[0].id in st.all_evidence_ids
    # stale disposition nulled + its cache purged (the resume re-runs it fresh)
    assert st.iterations[0].counsel_disposition is None
    assert not (cache_dir / "counsel_disposition.done").exists()
    assert not (cache_dir / "counsel_disposition.parsed.json").exists()
    # no longer an open checkpoint
    assert len(A.list_open(tmp_path)) == 0


def test_resolve_rejects_short_answer(tmp_path):
    _checkpointed_case(tmp_path)
    import pytest
    with pytest.raises(SystemExit):
        A.resolve_checkpoint(CID, "no", state_root=tmp_path)


def test_resolve_rejects_no_open_checkpoint(tmp_path):
    _checkpointed_case(tmp_path)
    A.resolve_checkpoint(CID, "Dogwood is NOS 442 Employment, not a qui tam.", state_root=tmp_path)
    import pytest
    with pytest.raises(SystemExit):  # already resolved → no open checkpoint
        A.resolve_checkpoint(CID, "answering again, should fail.", state_root=tmp_path)
