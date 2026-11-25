"""Regression: the foreclosure-checkpoint confirm-decline infinite-loop bug.

Guardrail-3/4 seam: a foreclosure_live_explanation checkpoint pauses the loop
when a DECLINE rests on a LIVE innocent explanation closable only by an
off-record fact (checkpoint.detect_foreclosure_checkpoint). A human answers via
scripts/answer_checkpoint.py, which used to rewind the SAME way regardless of
which direction the answer took: always clear foreclosure_ledger + purge the
foreclosure_check cache, forcing the resume to re-derive the ledger from a
fresh, non-deterministic agent call.

That's correct when the human REFUTES the explanation (foreclose_explanation —
the premise changed; a fresh derivation is exactly what's needed) but wrong
when the human CONFIRMS it (affirm_decline — the ledger the loop already
produced IS what the human just endorsed; re-deriving it risks re-pausing on
the identical checkpoint or, worse, silently flipping the verdict if the fresh
agent call disagrees with itself).

The fix: ReviewRequest carries the human's ``resolution`` direction
(schemas.py), and answer_checkpoint.resolve_checkpoint's Phase-C rewind
branches on it — 'affirm_decline' PRESERVES the ledger (+ its cache); anything
else clears it (existing behavior, unchanged). The two paths below pin that
divergence, both at the resolve_checkpoint (rewind) level and end-to-end
through the Orchestrator resume seam (_run_foreclosure_check +
_finalize_phase_c_strength_score). test_checkpoint_answer.py covers the
Phase-B / jurisdiction (Birch/Dogwood) ABANDON path, which this change does not
touch; test_jurisdiction_checkpoint_resolution_optional below pins that a
jurisdiction checkpoint still resolves fine with no --resolution at all.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import answer_checkpoint as A  # noqa: E402
from fraud_investigator.agents import foreclosure_check as _foreclosure_check  # noqa: E402
from fraud_investigator.orchestrator import Orchestrator  # noqa: E402
from fraud_investigator.schemas import (  # noqa: E402
    CaseState,
    CaseStrength,
    CounselBuildOutput,
    CounselDispositionAction,
    CounselDispositionOutput,
    EvidenceItem,
    EvidenceTier,
    FinalVerdict,
    ForeclosureLedgerOutput,
    InnocentExplanation,
    InvestigatorOutput,
    IterationRecord,
    ReviewRequest,
    Rule9bElements,
)
from fraud_investigator.state import case_dir, iter_dir, load_summary, write_summary  # noqa: E402

CID = "CASE-FORECLOSURE-TEST-2026"
STEELMAN = "Community oncology infusion-center referral pattern (steelman)."


# ── shared fixture builders ──────────────────────────────────────────────────

def _live_ledger() -> dict:
    return {
        "case_id": CID,
        "explanations": [{
            "id": "IE1",
            "explanation": "Community oncology infusion-center referral pattern.",
            "taxonomy_tag": "referral_region_demographics",
            "steelman": STEELMAN,
            "plausibility_unrebutted": 0.8,
            "status": "LIVE",
            "closure_owner": "discovery:claim/chart-level beneficiary diagnoses",
            "residual": "Whether the actual beneficiaries lacked a covered indication.",
        }],
        "fraud_is_more_plausible_inference": False,
        "max_live_plausibility": 0.8,
        "overall_assessment": "Innocent explanation remains LIVE and unrebutted.",
    }


def _checkpointed_foreclosure_case(state_root: Path) -> Path:
    """A Phase-C foreclosure checkpoint: rebuttal_closing iter with
    DECLARE_CASE_READY_WITH_DEPENDENCIES + the LIVE ledger that triggered the
    pause, plus its on-disk foreclosure_check cache (as Guardrail 3 would
    have left it)."""
    inv = InvestigatorOutput(case_id=CID, iteration=1, evidence=[
        EvidenceItem(id="ev-1", tier=EvidenceTier.CONFIRMED_FACT,
                    fact="Billing pattern evidence.", citation="cms puf", confidence=0.9),
    ])
    build = CounselBuildOutput(
        case_id=CID, iteration=1, case_theory="x" * 25,
        rule_9b_elements=Rule9bElements(), current_strength=CaseStrength.MODERATE,
    )
    disp = CounselDispositionOutput(
        case_id=CID, iteration=1,
        action=CounselDispositionAction.DECLARE_CASE_READY_WITH_DEPENDENCIES,
        reasoning="case reached case_ready")
    it = IterationRecord(iteration=1, timestamp=datetime.utcnow(), phase="rebuttal_closing",
                         investigator=inv, counsel_build=build, counsel_disposition=disp)
    rr = ReviewRequest(
        case_id=CID, checkpoint_type="foreclosure_live_explanation",
        question="Confirm or refute?", unverifiable_premise=STEELMAN,
        cited_evidence_id="IE1", why_unverifiable="LIVE, closure_owner=discovery:...",
        provisional_verdict_if_yes="NOT_ACTIONABLE (innocent explanation holds).",
        provisional_verdict_if_no="Explanation foreclosed — resume.",
        what_would_resolve_it="claim-level beneficiary diagnoses",
        raised_at=datetime.now(timezone.utc))
    st = CaseState(
        case_id=CID, subject="Dr X (NPI 1900000027)",
        start_time=datetime.utcnow(), last_updated=datetime.utcnow(),
        status="needs_human_checkpoint", final_verdict=FinalVerdict.NEEDS_HUMAN_CHECKPOINT,
        iterations=[it], all_evidence_ids=["ev-1"], review_request=rr,
        foreclosure_ledger=_live_ledger(), counts_retained=["C1"])
    (state_root / CID).mkdir(parents=True)
    write_summary(st, state_root)
    d = case_dir(CID, state_root)
    (d / "foreclosure_check.done").write_text("x")
    (d / "foreclosure_check.parsed.json").write_text("{}")
    return d


# ── resolve_checkpoint (rewind) level ────────────────────────────────────────

def test_resolve_checkpoint_requires_resolution_for_foreclosure_checkpoint(tmp_path):
    _checkpointed_foreclosure_case(tmp_path)
    with pytest.raises(SystemExit):
        A.resolve_checkpoint(CID, "Confirmed, the pattern is benign.", state_root=tmp_path)


def test_resolve_checkpoint_affirm_decline_preserves_ledger_and_cache(tmp_path):
    cache_dir = _checkpointed_foreclosure_case(tmp_path)

    A.resolve_checkpoint(
        CID, "Confirmed — the referral pattern is the ordinary community-oncology structure.",
        resolution="affirm_decline", state_root=tmp_path)

    st = load_summary(CID, tmp_path)
    assert st.status == "case_ready"
    assert st.final_verdict is None  # cleared, awaiting the resume's commit
    assert st.review_request.resolved is True
    assert st.review_request.resolution == "affirm_decline"
    # The ledger the checkpoint fired on is PRESERVED, not discarded.
    assert st.foreclosure_ledger is not None
    assert st.foreclosure_ledger["fraud_is_more_plausible_inference"] is False
    assert (cache_dir / "foreclosure_check.parsed.json").exists()
    assert (cache_dir / "foreclosure_check.done").exists()


def test_resolve_checkpoint_foreclose_explanation_clears_ledger_and_cache(tmp_path):
    """Existing behavior, unchanged: a genuine refutation clears the ledger +
    purges the cache so the resume re-derives for real."""
    cache_dir = _checkpointed_foreclosure_case(tmp_path)

    A.resolve_checkpoint(
        CID, "Refuted — no co-located referring specialists in any year of the panel.",
        resolution="foreclose_explanation", state_root=tmp_path)

    st = load_summary(CID, tmp_path)
    assert st.status == "case_ready"
    assert st.review_request.resolution == "foreclose_explanation"
    assert st.foreclosure_ledger is None
    assert not (cache_dir / "foreclosure_check.parsed.json").exists()
    assert not (cache_dir / "foreclosure_check.done").exists()


# ── end-to-end through the Orchestrator resume seam ──────────────────────────

def test_affirm_decline_resume_commits_not_actionable_with_no_repause(tmp_path, monkeypatch):
    """(a) THE regression test for the reported bug: answered-AFFIRM-decline →
    resume finalizes NOT_ACTIONABLE, with NO re-pause and NO re-run of the
    (non-deterministic) foreclosure_check agent."""
    _checkpointed_foreclosure_case(tmp_path)
    A.resolve_checkpoint(
        CID, "Confirmed — the referral pattern is the ordinary community-oncology structure.",
        resolution="affirm_decline", state_root=tmp_path)
    original_raised_at = load_summary(CID, tmp_path).review_request.raised_at

    def _must_not_run(**kw):
        raise AssertionError(
            "foreclosure_check agent re-ran despite an affirm_decline resolution "
            "on the resolved checkpoint — the resume should trust the human's "
            "answer, not re-derive it.")

    monkeypatch.setattr(_foreclosure_check, "run", _must_not_run)

    orch = Orchestrator(subject="Dr X (NPI 1900000027)", case_id=CID, state_dir=tmp_path)
    assert orch.state.status == "case_ready"  # not pinned back to needs_human_checkpoint

    orch._run_foreclosure_check()   # must hit the resume-guard, not call the stub above
    orch._finalize_phase_c_strength_score()

    assert orch.state.final_verdict == FinalVerdict.NOT_ACTIONABLE
    assert orch.state.status != "needs_human_checkpoint"
    # No NEW checkpoint was raised — same resolved ReviewRequest, untouched.
    assert orch.state.review_request.resolved is True
    assert orch.state.review_request.raised_at == original_raised_at


def test_foreclose_explanation_resume_rederives_on_the_merits(tmp_path, monkeypatch):
    """(b) The existing, correct behavior for the other direction: a genuine
    refutation still triggers a real re-run, and the loop re-decides."""
    _checkpointed_foreclosure_case(tmp_path)
    A.resolve_checkpoint(
        CID, "Refuted — no co-located referring specialists in any year of the panel.",
        resolution="foreclose_explanation", state_root=tmp_path)

    calls = {"n": 0}

    def _stub_run(*, subject, case_id, counsel_final_build, all_evidence,
                  numeric_audit, verified_precedents, out_dir):
        calls["n"] += 1
        exp = InnocentExplanation(
            id="IE1", explanation="re-derived", taxonomy_tag="x",
            steelman="re-derived after the human's refutation", plausibility_unrebutted=0.1,
            status="FORECLOSED", closure_owner="closed_on_record", residual="")
        return ForeclosureLedgerOutput(
            case_id=case_id, explanations=[exp], fraud_is_more_plausible_inference=True,
            max_live_plausibility=0.1, overall_assessment="Explanation foreclosed.")

    monkeypatch.setattr(_foreclosure_check, "run", _stub_run)

    orch = Orchestrator(subject="Dr X (NPI 1900000027)", case_id=CID, state_dir=tmp_path)
    orch._run_foreclosure_check()
    assert calls["n"] == 1, "the refutation direction must still re-run Guardrail 3"
    assert orch.state.foreclosure_ledger["fraud_is_more_plausible_inference"] is True

    orch._finalize_phase_c_strength_score()
    predicate_status = {p["name"]: p["status"] for p in orch.state.verdict_predicates}
    assert predicate_status.get("innocent_foreclosed") == "MET"


# ── jurisdiction (Birch/Dogwood) path unaffected ───────────────────────────────────

def test_jurisdiction_checkpoint_resolution_optional(tmp_path):
    """(c) Provider Birch/Dogwood jurisdiction path (Phase-B ABANDON rewind) is untouched
    by this fix: --resolution is neither required nor consulted for a
    jurisdiction_bar_unverifiable checkpoint. (test_checkpoint_answer.py's
    test_resolve_checkpoint_full_rewind already exercises this end-to-end;
    this pins the specific "resolution is optional here" contract.)"""
    inv = InvestigatorOutput(case_id=CID, iteration=3, evidence=[
        EvidenceItem(id="ev-1-qui-tam-filed", tier=EvidenceTier.CONFIRMED_FACT,
                    fact="Dogwood v. Agency Myrtle is a qui tam; docket under seal.",
                    citation="courtlistener", confidence=0.9),
    ])
    disp = CounselDispositionOutput(
        case_id=CID, iteration=3, action=CounselDispositionAction.ABANDON_CASE,
        reasoning="first-to-file bar (31 USC 3730(b)(5)); Dogwood remains under seal.")
    it = IterationRecord(iteration=3, timestamp=datetime.utcnow(), phase="rebuttal_closing",
                         investigator=inv, counsel_disposition=disp)
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
    (tmp_path / CID).mkdir(parents=True)
    write_summary(st, tmp_path)
    d = iter_dir(CID, 3, tmp_path)
    (d / "counsel_disposition.done").write_text("x")
    (d / "counsel_disposition.parsed.json").write_text("{}")

    # No --resolution passed at all — must not raise, must rewind exactly as before.
    info = A.resolve_checkpoint(
        CID, "Dogwood v. Agency Myrtle (9:99-cv-00001) is NOS 442 Employment, not a qui tam.",
        state_root=tmp_path)
    assert info["checkpoint_type"] == "jurisdiction_bar_unverifiable"

    resolved = load_summary(CID, tmp_path)
    assert resolved.status == "in_progress"
    assert resolved.review_request.resolution is None
    assert resolved.iterations[0].counsel_disposition is None  # stale disposition dropped


# ── cross-type ping-pong (two checkpoints on one case) ───────────────────────

def _always_checkpoint(kind: str):
    """A detector stub that ALWAYS returns a ReviewRequest of ``kind``.

    Both real detectors return None unless the state carries their specific
    unverifiable premise, which a synthetic fixture does not — so without this
    stub these tests pass for the wrong reason (they passed against the pre-fix
    guard). Stubbing makes the resolved-type guard the ONLY thing that can
    suppress a pause, which is exactly the logic under test.
    """
    def _stub(**kw):
        return ReviewRequest(
            case_id=CID, checkpoint_type=kind, question="Confirm or refute?",
            unverifiable_premise=STEELMAN, cited_evidence_id="IE1",
            why_unverifiable="synthetic", provisional_verdict_if_yes="NOT_ACTIONABLE",
            provisional_verdict_if_no="resume", what_would_resolve_it="a human answer",
            raised_at=datetime.now(timezone.utc))
    return _stub


class _FailedPredicate:
    def __init__(self, name):
        self.name, self.status = name, "FAILED"


class _DeclineDecision:
    verdict = FinalVerdict.NOT_ACTIONABLE

    def __init__(self, *names):
        self.predicates = [_FailedPredicate(n) for n in names]


def _stub_both_detectors(monkeypatch):
    import fraud_investigator.phase_c as PC
    monkeypatch.setattr(PC, "detect_jurisdiction_checkpoint",
                        _always_checkpoint("jurisdiction_bar_unverifiable"))
    monkeypatch.setattr(PC, "detect_foreclosure_checkpoint",
                        _always_checkpoint("foreclosure_live_explanation"))


def test_two_checkpoint_types_do_not_ping_pong(tmp_path, monkeypatch):
    """Regression: a case failing BOTH jurisdiction_clear and innocent_foreclosed
    raises two checkpoints. ``review_request`` holds only the latest, so the
    Phase-C guard used to compare against that single type and re-raise the
    other one forever:

        jurisdiction → answered → foreclosure → answered (overwrites) →
        jurisdiction's guard no longer sees its own resolution → jurisdiction → …

    An unattended calibration battery stalls permanently on any such case.
    ``CaseState.resolved_checkpoint_types`` records every answered type, and the
    guard tests membership in that set.
    """
    _stub_both_detectors(monkeypatch)
    _checkpointed_foreclosure_case(tmp_path)

    # 1st checkpoint on this case is a JURISDICTION bar, answered with no
    # --resolution (allowed for non-foreclosure types).
    st = load_summary(CID, tmp_path)
    st.review_request.checkpoint_type = "jurisdiction_bar_unverifiable"
    write_summary(st, tmp_path)
    A.resolve_checkpoint(CID, "Dogwood v. Agency Myrtle is NOS 442 Employment, not a qui tam.",
                         state_root=tmp_path)
    assert load_summary(CID, tmp_path).resolved_checkpoint_types == [
        "jurisdiction_bar_unverifiable"]

    # 2nd checkpoint — FORECLOSURE — overwrites review_request, as it does live.
    st = load_summary(CID, tmp_path)
    st.review_request = ReviewRequest(
        case_id=CID, checkpoint_type="foreclosure_live_explanation",
        question="Confirm or refute?", unverifiable_premise=STEELMAN,
        cited_evidence_id="IE1", why_unverifiable="LIVE",
        provisional_verdict_if_yes="NOT_ACTIONABLE", provisional_verdict_if_no="resume",
        what_would_resolve_it="claim-level diagnoses",
        raised_at=datetime.now(timezone.utc))
    st.foreclosure_ledger = _live_ledger()
    write_summary(st, tmp_path)
    A.resolve_checkpoint(CID, "Confirmed — the community-oncology pattern holds.",
                         resolution="affirm_decline", state_root=tmp_path)

    # BOTH types are remembered even though review_request only holds the latest.
    final = load_summary(CID, tmp_path)
    assert final.review_request.checkpoint_type == "foreclosure_live_explanation"
    assert set(final.resolved_checkpoint_types) == {
        "jurisdiction_bar_unverifiable", "foreclosure_live_explanation"}

    # So the Phase-C guard suppresses BOTH — neither can re-raise. With both
    # detectors stubbed to always fire, a None here can ONLY come from the guard.
    orch = Orchestrator(subject="Dr X (NPI 1900000027)", case_id=CID, state_dir=tmp_path)
    assert orch._human_checkpoint_for_decline(
        _DeclineDecision("jurisdiction_clear", "innocent_foreclosed")) is None


def test_unanswered_second_type_still_raises(tmp_path, monkeypatch):
    """The guard must not over-suppress: answering ONE type leaves a genuinely
    unanswered second type free to pause (that pause is the feature)."""
    _stub_both_detectors(monkeypatch)
    _checkpointed_foreclosure_case(tmp_path)
    st = load_summary(CID, tmp_path)
    st.review_request.checkpoint_type = "jurisdiction_bar_unverifiable"
    write_summary(st, tmp_path)
    A.resolve_checkpoint(CID, "Prior case is NOS 442 Employment, not a qui tam.",
                         state_root=tmp_path)

    orch = Orchestrator(subject="Dr X (NPI 1900000027)", case_id=CID, state_dir=tmp_path)
    orch.state.foreclosure_ledger = _live_ledger()

    rr = orch._human_checkpoint_for_decline(_DeclineDecision("innocent_foreclosed"))
    assert rr is not None and rr.checkpoint_type == "foreclosure_live_explanation"
