"""defense-as-mock (PR7) — pure synthesis tests."""
from __future__ import annotations

from fraud_investigator.mock_defense import (
    estimate_survival_from_motion,
    synthesize_rebuttal_from_defense,
)
from fraud_investigator.schemas import CounselRebuttalOutput


def _attack(attack_id="A-r1-1", type="scienter_lacking", severity="mitigating",
            counts=("C1",), argument="The relator pleads no contemporaneous knowledge anchor.",
            grounding=""):
    return {"attack_id": attack_id, "type": type, "severity": severity,
            "target_counts": list(counts), "argument": argument,
            "grounding": grounding, "supporting_evidence": [],
            "novelty_vs_prior_rounds": "first_round"}


def _motion(*attacks, live=("C1",), clean=("C2",)):
    return {"case_id": "CASE-X", "round": 1, "attacks": list(attacks),
            "any_attack_survives": bool(attacks),
            "counts_with_live_attack": list(live),
            "counts_with_no_live_attack": list(clean)}


def test_severity_mapping_and_schema_validity():
    out = synthesize_rebuttal_from_defense(
        _motion(
            _attack("A-r1-1", severity="dispositive"),
            _attack("A-r1-2", severity="mitigating"),
            _attack("A-r1-3", severity="minor"),
        ),
        case_id="CASE-X", iteration=4,
    )
    assert isinstance(out, CounselRebuttalOutput)
    sev = {wl.weakness_id: wl.severity for wl in out.weak_links}
    assert sev == {"WL-A-r1-1": "severe", "WL-A-r1-2": "concerning",
                   "WL-A-r1-3": "defense_likely_to_raise"}
    # every weak link routes to targeted investigation with one question
    assert all(wl.fixable_via == "more_investigation" for wl in out.weak_links)
    assert all(len(wl.investigator_questions) == 1 for wl in out.weak_links)


def test_grounding_carried_into_question():
    out = synthesize_rebuttal_from_defense(
        _motion(_attack(grounding="dump_hcpcs_breakdown --npi 1 → volume is per-mg units")),
        case_id="CASE-X", iteration=4,
    )
    q = out.weak_links[0].investigator_questions[0]
    assert "Defense grounding to overcome" in q
    assert "per-mg units" in q


def test_survival_heuristic_monotonic_and_clamped():
    clean = estimate_survival_from_motion(_motion())
    one_mit = estimate_survival_from_motion(_motion(_attack(severity="mitigating")))
    one_disp = estimate_survival_from_motion(
        _motion(_attack(severity="dispositive")))
    many = estimate_survival_from_motion(_motion(*[
        _attack(f"A-r1-{i}", severity="dispositive", counts=(f"C{i}",))
        for i in range(1, 9)
    ]))
    assert clean == 0.80
    assert clean > one_mit > one_disp
    assert many == 0.15  # lower clamp
    # dispositive attacks on the SAME count don't double-count
    same_count = estimate_survival_from_motion(_motion(
        _attack("A-r1-1", severity="dispositive", counts=("C1",)),
        _attack("A-r1-2", severity="dispositive", counts=("C1",)),
    ))
    assert same_count == one_disp


def test_deterministic_resynthesis():
    m = _motion(_attack(severity="dispositive"), _attack("A-r1-2"))
    a = synthesize_rebuttal_from_defense(m, case_id="C", iteration=4)
    b = synthesize_rebuttal_from_defense(m, case_id="C", iteration=4)
    assert a.model_dump() == b.model_dump()  # resume-safe replay
