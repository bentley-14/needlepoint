"""Regression test for the convergence-floor drop-not-abandon fix.

Bug (Bradley CASE-6A3014F0-2026, 2026-06-15): two grounded counts (C1, C2 —
both PASS_FULL) plus one deliberately discovery-held count (C3, an
info-and-belief AKS count with no factual_basis entry) → the convergence floor
saw "1 count fails" and routed the WHOLE disposition to NEED_MORE_INVESTIGATION,
which Phase B coerces to ABANDON_CASE → final_verdict COUNSEL_ABANDONED despite
strength 0.91 / EVF 0.60 / FILE. A firm drops the speculative count and files
the grounded ones; it does not abandon C1+C2 because C3 is unground-able on
public data.

Fix: when SOME counts clear the floor, drop the failing ones (into
counts_to_drop) and proceed on the survivors — preserving the model's action
(never upgrading). Only abandon when NO count clears the floor or damages are
below the exposure floor.
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


def _run(action, fbs, *, drop=None, dmg=10_855_271.0):
    build = NS(
        counts_pled=[NS(count_id=c) for c in ("C1", "C2", "C3")],
        factual_basis_per_count=fbs,
        estimated_aggregate_damages_usd=dmg,
    )
    disp = NS(action=action, counts_to_drop=list(drop or []), reasoning="m",
              follow_up_questions_for_investigator=[], gaps_to_fill=[])
    orch = object.__new__(Orchestrator)
    orch.calibration_as_of = None
    return Orchestrator._enforce_convergence_floor(orch, disp, build)


def test_drops_floor_failing_count_and_proceeds():
    # C1, C2 PASS_FULL; C3 has no factual_basis entry → must drop C3, proceed.
    out = _run(A.DECLARE_CASE_READY_WITH_DEPENDENCIES, [_fb("C1"), _fb("C2", ext=1, anchor=2)])
    assert out.action == A.DECLARE_CASE_READY_WITH_DEPENDENCIES  # model's choice preserved
    assert "C3" in out.counts_to_drop


def test_does_not_upgrade_model_action():
    # Both survivors PASS_FULL, but the model chose WITH_DEPENDENCIES — the floor
    # must NOT upgrade that to full READY.
    out = _run(A.DECLARE_CASE_READY_WITH_DEPENDENCIES, [_fb("C1"), _fb("C2")])
    assert out.action == A.DECLARE_CASE_READY_WITH_DEPENDENCIES


def test_zero_grounded_counts_still_abandons():
    out = _run(A.DECLARE_CASE_READY_WITH_DEPENDENCIES, [])  # no count clears
    assert out.action == A.NEED_MORE_INVESTIGATION


def test_clean_full_pass_unaffected():
    out = _run(A.DECLARE_CASE_READY, [_fb("C1"), _fb("C2"), _fb("C3")])
    assert out.action == A.DECLARE_CASE_READY
    assert not out.counts_to_drop


def test_below_damages_floor_still_abandons():
    # A failing count + below-$1M-treble exposure → don't rescue a tiny case.
    out = _run(A.DECLARE_CASE_READY_WITH_DEPENDENCIES, [_fb("C1"), _fb("C2")], dmg=100_000.0)
    assert out.action == A.NEED_MORE_INVESTIGATION
