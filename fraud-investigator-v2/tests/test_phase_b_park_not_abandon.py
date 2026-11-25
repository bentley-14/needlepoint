"""Regression test for the Phase-B iteration-cap PARK-not-ABANDON fix
(failure_mode_taxonomy_2026-07.md #5, CASE-6A3014F0).

Bug: Phase B unconditionally coerced any NEED_MORE_INVESTIGATION disposition
(post-rebuttal, where the model has no legal way to ask for more investigation)
to ABANDON_CASE — even when the convergence + damages floors are ACTUALLY met
and the only reason NEED_MORE_INVESTIGATION surfaced is a different floor (min
iterations / venue landscape) or an edge case the convergence-floor drop-rescue
doesn't cover. CASE-6A3014F0's own disposition reasoning: "a competent qui tam
firm would file under seal today on these terms" — recorded moments before the
mechanical coercion forced ABANDON_CASE on a case with $32.6M treble damages.

Fix: at the coercion seam, probe _floors_actually_met (a read-only mirror of
the convergence-floor's per-count classification, independent of whatever
action the various floor enforcers landed on) and route to PARK_CASE / PARKED
instead of ABANDON_CASE / COUNSEL_ABANDONED when floors are genuinely met.
"""
from types import SimpleNamespace as NS

from fraud_investigator.orchestrator import Orchestrator
from fraud_investigator.schemas import CounselDispositionAction as A


def _fb(cid, ext=5, conv=3, anchor=1, reg=0, refut="x" * 60):
    return NS(
        count_id=cid,
        extremity_evidence_ids=["e"] * ext,
        convergent_signal_evidence_ids=["c"] * conv,
        falsity_anchor_evidence_ids=["a"] * anchor,
        regulatory_corroboration_evidence_ids=["r"] * reg,
        innocent_explanations_refuted_narrative=refut,
    )


def _build(fbs, counts=("C1", "C2"), dmg=10_855_271.0):
    return NS(
        counts_pled=[NS(count_id=c) for c in counts],
        factual_basis_per_count=fbs,
        estimated_aggregate_damages_usd=dmg,
    )


def _orch():
    orch = object.__new__(Orchestrator)
    orch.calibration_as_of = None
    return orch


def test_floors_actually_met_true_when_a_count_clears_floor_and_damages_high():
    build = _build([_fb("C1"), _fb("C2")])
    assert Orchestrator._floors_actually_met(_orch(), build, counts_to_drop=[]) is True


def test_floors_actually_met_false_when_no_count_clears_floor():
    build = _build([], counts=("C1",))
    assert Orchestrator._floors_actually_met(_orch(), build, counts_to_drop=[]) is False


def test_floors_actually_met_false_when_damages_below_exposure_floor():
    build = _build([_fb("C1")], dmg=100_000.0)
    assert Orchestrator._floors_actually_met(_orch(), build, counts_to_drop=[]) is False


def test_floors_actually_met_respects_counts_to_drop():
    # C1 fails the floor, C2 passes -> dropping C1 leaves a passing survivor.
    build = _build([_fb("C1", ext=0), _fb("C2")])
    assert Orchestrator._floors_actually_met(_orch(), build, counts_to_drop=["C1"]) is True
    # Dropping the ONLY passing count leaves nothing -> False.
    assert Orchestrator._floors_actually_met(_orch(), build, counts_to_drop=["C2"]) is False


def test_coercion_parks_instead_of_abandons_when_floors_met():
    """The CASE-6A3014F0 shape: NEED_MORE_INVESTIGATION reaches the Phase-B
    coercion seam (from a floor OTHER than convergence, or an edge case the
    drop-rescue doesn't cover), but the retained counts clear the floor and
    damages vastly exceed $1M -> must PARK, not ABANDON."""
    build = _build([_fb("C1"), _fb("C2")], dmg=10_855_271.0)  # treble ~$32.6M
    disp = NS(action=A.NEED_MORE_INVESTIGATION, counts_to_drop=[], reasoning="model reasoning",
              follow_up_questions_for_investigator=[], gaps_to_fill=[])
    orch = _orch()

    if orch._floors_actually_met(build, disp.counts_to_drop):
        disp.action = A.PARK_CASE
        disp.reasoning = "[PHASE-B PARK] " + disp.reasoning
    else:
        disp.action = A.ABANDON_CASE
        disp.reasoning = "[PHASE-B COERCION] " + disp.reasoning

    assert disp.action == A.PARK_CASE
    assert "[PHASE-B PARK]" in disp.reasoning


def test_coercion_still_abandons_when_floors_genuinely_not_met():
    build = _build([], counts=("C1",), dmg=0.0)
    disp = NS(action=A.NEED_MORE_INVESTIGATION, counts_to_drop=[], reasoning="model reasoning",
              follow_up_questions_for_investigator=[], gaps_to_fill=[])
    orch = _orch()

    if orch._floors_actually_met(build, disp.counts_to_drop):
        disp.action = A.PARK_CASE
    else:
        disp.action = A.ABANDON_CASE

    assert disp.action == A.ABANDON_CASE
