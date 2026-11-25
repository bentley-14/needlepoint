"""Guardrail-4 invariant: NEEDS_HUMAN_CHECKPOINT must always carry a
ReviewRequest (failure_mode_taxonomy_2026-07.md #5 / CASE-6A2B3445).

CASE-6A2B3445 (the Birch family) ended status=case_ready, final_verdict=
NEEDS_HUMAN with review_request absent and an empty verdict_rationale — the
pre-Guardrail-4 gap this whole checkpoint module exists to close. This pins
the mechanical backstop: checkpoint.assert_checkpoint_invariant raises, and
state.write_summary calls it on every persisted state so no future call site
can silently reintroduce the gap.

Run: pytest fraud-investigator-v2/tests/test_checkpoint_invariant.py -v
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fraud_investigator import checkpoint as C  # noqa: E402
from fraud_investigator.schemas import CaseState, FinalVerdict, ReviewRequest  # noqa: E402
from fraud_investigator.state import write_summary  # noqa: E402


def _rr(case_id="c") -> ReviewRequest:
    return ReviewRequest(
        case_id=case_id,
        checkpoint_type="jurisdiction_bar_unverifiable",
        question="q", unverifiable_premise="p", why_unverifiable="w",
        provisional_verdict_if_yes="y", provisional_verdict_if_no="n",
        what_would_resolve_it="r", raised_at=datetime.now(timezone.utc),
    )


# ── assert_checkpoint_invariant (pure function) ──────────────────────────────

def test_checkpoint_with_review_request_is_fine():
    C.assert_checkpoint_invariant(FinalVerdict.NEEDS_HUMAN_CHECKPOINT, _rr())  # no raise


def test_checkpoint_without_review_request_raises():
    """The CASE-6A2B3445 gap, reproduced directly against the pure function."""
    try:
        C.assert_checkpoint_invariant(FinalVerdict.NEEDS_HUMAN_CHECKPOINT, None)
        assert False, "expected CheckpointInvariantError"
    except C.CheckpointInvariantError as e:
        assert "review_request is None" in str(e)


def test_string_value_form_also_checked():
    # CaseState may hand through a plain string, not the enum — must still catch it.
    try:
        C.assert_checkpoint_invariant("NEEDS_HUMAN_CHECKPOINT", None)
        assert False, "expected CheckpointInvariantError"
    except C.CheckpointInvariantError:
        pass


def test_other_verdicts_never_require_a_review_request():
    for v in (FinalVerdict.NOT_ACTIONABLE, FinalVerdict.COUNSEL_ABANDONED,
              FinalVerdict.SETTLEMENT_READY, FinalVerdict.NEEDS_HUMAN, None):
        C.assert_checkpoint_invariant(v, None)  # no raise for any of these


def test_other_verdicts_with_a_review_request_is_also_fine():
    C.assert_checkpoint_invariant(FinalVerdict.NOT_ACTIONABLE, _rr())  # no raise


# ── write_summary wiring (the real chokepoint) ───────────────────────────────

def test_write_summary_raises_on_the_case_6a2b3445_shape(tmp_path):
    """Reproduces the real bug shape directly: status=case_ready,
    final_verdict=NEEDS_HUMAN_CHECKPOINT, review_request=None. write_summary
    (the single persistence chokepoint) must refuse to write it."""
    state = CaseState(
        case_id="CASE-6A2B3445-2026-TEST",
        subject="test subject",
        start_time=datetime.utcnow(), last_updated=datetime.utcnow(),
        status="case_ready",
        final_verdict=FinalVerdict.NEEDS_HUMAN_CHECKPOINT,
        review_request=None,
    )
    try:
        write_summary(state, tmp_path)
        assert False, "expected CheckpointInvariantError"
    except C.CheckpointInvariantError:
        pass
    # And the corrupt state must NOT have been persisted (atomic tmp+rename
    # never reached — the assertion fires before any file I/O).
    assert not (tmp_path / state.case_id / "summary.json").exists()


def test_write_summary_succeeds_with_a_paired_review_request(tmp_path):
    state = CaseState(
        case_id="CASE-OK-TEST",
        subject="test subject",
        start_time=datetime.utcnow(), last_updated=datetime.utcnow(),
        status="needs_human_checkpoint",
        final_verdict=FinalVerdict.NEEDS_HUMAN_CHECKPOINT,
        review_request=_rr("CASE-OK-TEST"),
    )
    path = write_summary(state, tmp_path)
    assert path.exists()
