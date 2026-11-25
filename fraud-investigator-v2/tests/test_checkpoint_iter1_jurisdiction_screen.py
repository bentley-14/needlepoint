"""Unit tests for the iter-1 blocking jurisdiction screen
(checkpoint.detect_iteration1_jurisdiction_screen).

failure_mode_taxonomy_2026-07.md #1: counsel_build's first_to_file_concerns /
public_disclosure_concerns fields were advisory-only and never checked against a
live docket. Two real runs (Spruce/Warren, Birch/Dogwood) burned 3+ iterations before a
PACER-discoverable prior qui tam surfaced. This screen fires at iter-1, right
after counsel_build, using an injectable docket_search callable so the test is
hermetic (no live CourtListener call).

Run: pytest fraud-investigator-v2/tests/test_checkpoint_iter1_jurisdiction_screen.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fraud_investigator import checkpoint as C  # noqa: E402
from fraud_investigator.schemas import ReviewRequest  # noqa: E402


def test_no_concerns_is_a_noop():
    calls = []
    rr = C.detect_iteration1_jurisdiction_screen(
        case_id="c1",
        first_to_file_concerns=[],
        public_disclosure_concerns=[],
        subject="Jane Doe, MD (NPI 1234567890)",
        docket_search=lambda q: calls.append(q) or [],
    )
    assert rr is None
    assert calls == [], "no concerns raised -> must not spend a live docket query"


def test_concerns_but_no_docket_hits_is_a_noop():
    rr = C.detect_iteration1_jurisdiction_screen(
        case_id="c2",
        first_to_file_concerns=["possible prior relator action"],
        public_disclosure_concerns=[],
        subject="Acme Clinic LLC",
        docket_search=lambda q: [],
    )
    assert rr is None


def test_terminated_docket_does_not_bar():
    """A CLOSED prior case cannot bar under first-to-file -> no checkpoint."""
    rr = C.detect_iteration1_jurisdiction_screen(
        case_id="c3",
        first_to_file_concerns=["possible prior qui tam vs Acme"],
        public_disclosure_concerns=[],
        subject="Acme Clinic LLC",
        docket_search=lambda q: [{"case": "US ex rel. X v. Acme", "docket": "1:20-cv-1",
                                  "court": "dcd", "filed": "2020-01-01",
                                  "terminated": "2021-06-01", "nature": "False Claims Act"}],
    )
    assert rr is None


def test_open_docket_raises_checkpoint_not_more_iterations():
    """A confirmed PENDING (open, no termination date) prior case routes to the
    Guardrail-4 checkpoint instead of letting the loop burn further iterations —
    the Spruce/Warren and Birch/Dogwood pattern, caught at iter-1 instead of iter-3+."""
    rr = C.detect_iteration1_jurisdiction_screen(
        case_id="CASE-TEST",
        first_to_file_concerns=["Warren v. Wound Co Nutmeg may cover the same conduct"],
        public_disclosure_concerns=[],
        subject="Provider Spruce (NPI 1900000068)",
        docket_search=lambda q: [{"case": "US ex rel. Warren v. Wound Co Nutmeg",
                                  "docket": "9:99-cv-00003", "court": "txnd",
                                  "filed": "2025-02-01", "terminated": None,
                                  "nature": "False Claims Act"}],
    )
    assert isinstance(rr, ReviewRequest)
    assert rr.checkpoint_type == "jurisdiction_bar_unverifiable"
    assert rr.cited_evidence_id == "9:99-cv-00003"
    assert not rr.resolved
    assert "first-to-file" in rr.question.lower() or "public-disclosure" in rr.question.lower()


def test_prefers_open_docket_over_terminated_when_both_present():
    rr = C.detect_iteration1_jurisdiction_screen(
        case_id="c4",
        first_to_file_concerns=["prior relator action"],
        public_disclosure_concerns=[],
        subject="Acme Clinic LLC",
        docket_search=lambda q: [
            {"case": "closed one", "docket": "1:19-cv-1", "court": "dcd",
             "filed": "2019-01-01", "terminated": "2020-01-01"},
            {"case": "open one", "docket": "1:24-cv-2", "court": "dcd",
             "filed": "2024-01-01", "terminated": None},
        ],
    )
    assert isinstance(rr, ReviewRequest)
    assert rr.cited_evidence_id == "1:24-cv-2"
