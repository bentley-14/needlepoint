"""Resume must enter Phase B when Phase A already decided PROCEED_TO_REBUTTAL.

The 2026-06-12 Maple bug: a case crashed in Phase B (counsel_rebuttal).
On resume, _run_phase_a counted 3 completed build iters → start_iter=4 →
the build loop `range(4, 4)` was empty → it never re-saw iter-3's
PROCEED_TO_REBUTTAL → fell through to "cap reached → max_iterations → return
False" → Phase B + the entire adversarial loop were skipped and case_writer
wrote an incomplete, verdict-less dossier. These tests pin the two fixes:
the Phase-A resume early-return, and the case_writer terminal-outcome guard.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from fraud_investigator.orchestrator import Orchestrator
from fraud_investigator.schemas import (
    CaseStrength,
    CounselBuildOutput,
    Rule9bElements,
    CounselFirstChairReviewAction,
    CounselFirstChairReviewOutput,
    InvestigatorOutput,
    IterationRecord,
)


def _orch(tmp_path):
    return Orchestrator(
        subject="Provider Maple — PMR — NPI 1900000016",
        state_dir=tmp_path, dry_run=True,
    )


def _build_iter(n, fcr_action, complete=True):
    """A COMPLETE Phase A build iter (investigator + counsel_build +
    first_chair all present) so the resume-trim keeps it. The trim pops any
    trailing build iter missing one of the three."""
    return IterationRecord(
        iteration=n, timestamp=datetime.utcnow(), phase="build",
        investigator=(InvestigatorOutput(case_id="CASE-X", iteration=str(n))
                      if complete else None),
        counsel_build=(CounselBuildOutput(
            case_id="CASE-X", iteration=n, case_theory="placeholder case theory for the resume fixture",
            rule_9b_elements=Rule9bElements(),
            current_strength=CaseStrength.MODERATE) if complete else None),
        counsel_first_chair_review=CounselFirstChairReviewOutput(
            case_id="CASE-X", iteration=n, action=fcr_action,
            reasoning="t", counts_to_drop=[],
            follow_up_questions_for_investigator=[], gaps_to_fill=[]),
    )


def test_phase_a_resume_proceeds_when_already_decided(tmp_path):
    """3 completed build iters, last = PROCEED_TO_REBUTTAL → _run_phase_a
    returns True (go to Phase B), NOT False (cap-abandon)."""
    orch = _orch(tmp_path)
    orch.max_pre_rebuttal_iters = 3
    orch.state.iterations = [
        _build_iter(1, CounselFirstChairReviewAction.CONTINUE_BUILD),
        _build_iter(2, CounselFirstChairReviewAction.CONTINUE_BUILD),
        _build_iter(3, CounselFirstChairReviewAction.PROCEED_TO_REBUTTAL),
    ]
    # dry_run short-circuits the per-iter body, but the resume guard runs
    # before the loop — so the empty-range trap is what we're testing.
    assert orch._run_phase_a() is True
    assert orch.state.status != "max_iterations"


def test_phase_a_resume_abandon_stays_abandoned(tmp_path):
    """A genuinely-exhausted Phase A (last iter CONTINUE at the cap, never
    proceeded) is unaffected — still returns False."""
    orch = _orch(tmp_path)
    orch.max_pre_rebuttal_iters = 3
    orch.state.iterations = [
        _build_iter(1, CounselFirstChairReviewAction.CONTINUE_BUILD),
        _build_iter(2, CounselFirstChairReviewAction.CONTINUE_BUILD),
        _build_iter(3, CounselFirstChairReviewAction.CONTINUE_BUILD),
    ]
    assert orch._run_phase_a() is False


def test_case_writer_guard_blocks_incomplete(tmp_path):
    """A case with no disposition + non-terminal status must NOT be deemed
    dossier-ready (the incomplete-dossier guard)."""
    orch = _orch(tmp_path)
    orch.state.status = "max_iterations"
    orch.state.iterations = [
        _build_iter(1, CounselFirstChairReviewAction.PROCEED_TO_REBUTTAL)]
    assert orch._case_has_terminal_outcome() is False


def test_case_writer_guard_allows_terminal(tmp_path):
    orch = _orch(tmp_path)
    for status in ("case_ready", "abandoned", "complete"):
        orch.state.status = status
        assert orch._case_has_terminal_outcome() is True
    # abandoned-via-first-chair also counts
    orch.state.status = "running"
    orch.state.iterations = [
        _build_iter(1, CounselFirstChairReviewAction.ABANDON_CASE)]
    assert orch._case_has_terminal_outcome() is True
