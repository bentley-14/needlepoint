"""Unit tests for the legal-predicate filing test (verdict.compute_verdict).

Hermetic — constructed Scorecards, no data. One test per predicate failing the
case, the conservative default, and the EVF sub-tier (which only distinguishes
SETTLEMENT_READY vs FILE_WITH_DISCOVERY_DEPS *after* the legal test passes).

Run: pytest fraud-investigator-v2/tests/test_verdict.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fraud_investigator import verdict as V  # noqa: E402
from fraud_investigator.schemas import FinalVerdict  # noqa: E402


def _all_met(**over) -> V.Scorecard:
    """A scorecard that clears every predicate; override one field to break it."""
    base = dict(
        n_counts_with_confirmed_falsity=2,
        fraud_more_plausible=True,
        materiality_defeated=False,
        scienter_defeated=False,
        jurisdiction_barred=False,
        confirmed_damages_usd=500_000.0,
        evf=0.45,
        damages_discovery_dependent=False,
        calibration=True,
    )
    base.update(over)
    return V.Scorecard(**base)


def test_all_met_high_evf_is_settlement_ready():
    d = V.compute_verdict(_all_met(evf=0.45))
    assert d.verdict == FinalVerdict.SETTLEMENT_READY
    assert d.is_file()


def test_all_met_mid_evf_is_file_with_deps():
    d = V.compute_verdict(_all_met(evf=0.30))
    assert d.verdict == FinalVerdict.FILE_WITH_DISCOVERY_DEPS
    assert d.is_file()


def test_low_evf_still_files_when_legal_test_passes():
    # economics never substitutes for merit — a met legal test still files (deps tier)
    d = V.compute_verdict(_all_met(evf=0.05))
    assert d.is_file()
    assert d.verdict == FinalVerdict.FILE_WITH_DISCOVERY_DEPS


def test_no_confirmed_false_claim_declines():
    d = V.compute_verdict(_all_met(n_counts_with_confirmed_falsity=0))
    assert d.verdict == FinalVerdict.NOT_ACTIONABLE
    assert "confirmed_false_claim" in d.rationale


def test_live_innocent_explanation_declines():
    d = V.compute_verdict(_all_met(fraud_more_plausible=False))
    assert d.verdict == FinalVerdict.NOT_ACTIONABLE
    assert "innocent_foreclosed" in d.rationale


def test_unassessed_foreclosure_does_not_decline():
    # None (no ledger) is not evidence of a live explanation → must not auto-fail
    d = V.compute_verdict(_all_met(fraud_more_plausible=None))
    assert d.is_file()


def test_materiality_defeated_declines():
    d = V.compute_verdict(_all_met(materiality_defeated=True))
    assert d.verdict == FinalVerdict.NOT_ACTIONABLE
    assert "materiality" in d.rationale


def test_scienter_defeated_declines():
    d = V.compute_verdict(_all_met(scienter_defeated=True))
    assert d.verdict == FinalVerdict.NOT_ACTIONABLE
    assert "scienter" in d.rationale


def test_no_confirmed_damages_declines():
    d = V.compute_verdict(_all_met(confirmed_damages_usd=0.0))
    assert d.verdict == FinalVerdict.NOT_ACTIONABLE
    assert "confirmed_damages" in d.rationale


def test_small_but_real_damages_still_files():
    # magnitude is NOT the gate — a $1 confirmed loss clears P5 (the real TP guard)
    d = V.compute_verdict(_all_met(confirmed_damages_usd=80_000.0))
    assert d.is_file()


def test_jurisdiction_bar_declines():
    d = V.compute_verdict(_all_met(jurisdiction_barred=True))
    assert d.verdict == FinalVerdict.NOT_ACTIONABLE
    assert "jurisdiction" in d.rationale


def test_default_scorecard_declines():
    # the conservative default: an empty scorecard files nothing
    d = V.compute_verdict(V.Scorecard())
    assert d.verdict == FinalVerdict.NOT_ACTIONABLE


# ── P0 bookkeeping coherence (failure_mode_taxonomy_2026-07.md #5 / CASE-6A378AB5) ──
# Reproduces the real Spruce artifact: FILE_WITH_DISCOVERY_DEPS recorded with
# every OTHER predicate MET, on top of an adversarial layer that recorded
# counts_retained=[] and settlement_verdict=NOT_ACTIONABLE. P0 is a deterministic
# backstop independent of how P1-P6 derive — it must refuse FILE on its own.

def test_empty_counts_retained_declines_even_with_all_other_predicates_met():
    d = V.compute_verdict(_all_met(counts_retained=[]))
    assert d.verdict == FinalVerdict.NOT_ACTIONABLE
    assert "bookkeeping_coherent" in d.rationale


def test_not_actionable_settlement_verdict_declines_even_with_all_other_predicates_met():
    d = V.compute_verdict(_all_met(last_settlement_verdict="NOT_ACTIONABLE"))
    assert d.verdict == FinalVerdict.NOT_ACTIONABLE
    assert "bookkeeping_coherent" in d.rationale


def test_untracked_counts_retained_does_not_decline():
    # None = "not tracked" (pre-refactor back-compat) — must not force-decline
    # a case that never populated this field.
    d = V.compute_verdict(_all_met(counts_retained=None))
    assert d.is_file()


def test_nonempty_counts_retained_does_not_decline():
    d = V.compute_verdict(_all_met(counts_retained=["C1", "C2"]))
    assert d.is_file()


def test_spruce_case_reproduction_declines():
    """The real CASE-6A378AB5 shape: falsity/materiality/scienter/damages all
    MET, but counts_retained=[] and the last round's settlement_verdict is
    NOT_ACTIONABLE. Must DECLINE regardless of jurisdiction_barred (defense in
    depth — P0 does not rely on P6 having caught this)."""
    d = V.compute_verdict(_all_met(
        jurisdiction_barred=False,  # even if P6 is (wrongly) MET
        counts_retained=[],
        last_settlement_verdict="NOT_ACTIONABLE",
    ))
    assert d.verdict == FinalVerdict.NOT_ACTIONABLE
    assert not d.is_file()
    assert "bookkeeping_coherent" in d.rationale


def test_build_scorecard_reads_counts_retained_and_last_settlement_verdict():
    """build_scorecard must populate the new P0 inputs from a serialized
    CaseState dict, distinguishing an explicit empty list from an absent key."""
    summary_zero_retained = {
        "counts_retained": [],
        "adversarial_rounds": [
            {"legal_adv_response": {"settlement_verdict": "PENDING_REBUTTAL"}},
            {"legal_adv_response": {"settlement_verdict": "NOT_ACTIONABLE"}},
        ],
        "iterations": [],
    }
    sc = V.build_scorecard(summary_zero_retained)
    assert sc.counts_retained == []
    assert sc.last_settlement_verdict == "NOT_ACTIONABLE"

    summary_untracked = {"iterations": []}
    sc2 = V.build_scorecard(summary_untracked)
    assert sc2.counts_retained is None
    assert sc2.last_settlement_verdict is None


def test_every_predicate_has_a_rationale():
    d = V.compute_verdict(_all_met())
    assert len(d.predicates) == 7  # P0 bookkeeping_coherent + P1-P6
    assert all(p.rationale for p in d.predicates)


def test_filing_decision_md_renders_file_and_decline():
    # FILE case → "FILE" label + every predicate row present
    d = V.compute_verdict(_all_met(evf=0.45))
    preds = [{"name": p.name, "status": p.status, "why": p.rationale} for p in d.predicates]
    md = V.render_filing_decision_md(d.verdict.value, d.rationale, preds,
                                     evf=0.45, strength=0.6, bucket="STRONG")
    assert "# Filing Decision" in md and "— FILE" in md
    assert "confirmed_false_claim" in md and "innocent_foreclosed" in md
    assert "EVF = 0.45" in md  # economics shown as secondary

    # DECLINE case → "DECLINE" label + the failing predicate named
    dd = V.compute_verdict(_all_met(fraud_more_plausible=False))
    dpreds = [{"name": p.name, "status": p.status, "why": p.rationale} for p in dd.predicates]
    md2 = V.render_filing_decision_md(dd.verdict.value, dd.rationale, dpreds)
    assert "— DECLINE" in md2 and "innocent_foreclosed" in md2


def test_filing_decision_md_handles_no_predicates():
    md = V.render_filing_decision_md("NEEDS_HUMAN", None, [])
    assert "DECLINE" in md and "pre-refactor" in md
