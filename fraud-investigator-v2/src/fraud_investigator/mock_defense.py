"""Defense-round-1-as-mock (PR7) — synthesize a rebuttal record from a real
defense motion.

Rationale (Cypress negative-control finding + cost): counsel_rebuttal is the
relator red-teaming itself with FEWER tools than the real defense agent (no
data tools, no numeric audit, no tier-lint flags). The defense stage runs
regardless once a case is ready — so when ``defense_as_mock`` is enabled,
Phase B fires the REAL defense round 1 early (into ``adv-r1/``, where the
adversarial loop later replays it from the per-step cache at zero cost) and
this module converts the motion into the ``CounselRebuttalOutput`` shape the
rest of Phase B consumes (targeted investigation → final build revision →
disposition). One fewer Opus stage per case, and the attacks driving the
pre-file fix-up are the better attacks.

Flag-gated, default OFF — A/B on the calibration cases (Aspen TP,
Cypress negative control) before flipping the default.
"""
from __future__ import annotations

from .schemas import CounselRebuttalOutput, WeakLink

# Defense attack severity → rebuttal weak-link severity. Same three-tier
# intent, different vocabularies.
_SEVERITY_MAP = {
    "dispositive": "severe",
    "mitigating": "concerning",
    "minor": "defense_likely_to_raise",
}

# Deterministic survival heuristic feeding scoring.survival_residual.
# Calibrated to the band counsel_rebuttal.md documents for filed data-mining
# qui tams (0.40-0.75): base 0.80, minus 0.15 per DISTINCT count carrying a
# dispositive attack, minus 0.04 per mitigating attack, clamped [0.15, 0.85].
# Monotonic in attack badness; documented + tested rather than model-asserted.
_BASE = 0.80
_PER_DISPOSITIVE_COUNT = 0.15
_PER_MITIGATING_ATTACK = 0.04
_CLAMP = (0.15, 0.85)


def estimate_survival_from_motion(motion: dict) -> float:
    attacks = motion.get("attacks") or []
    dispositive_counts: set[str] = set()
    n_mitigating = 0
    for a in attacks:
        if a.get("severity") == "dispositive":
            dispositive_counts.update(a.get("target_counts") or [])
        elif a.get("severity") == "mitigating":
            n_mitigating += 1
    raw = (_BASE
           - _PER_DISPOSITIVE_COUNT * len(dispositive_counts)
           - _PER_MITIGATING_ATTACK * n_mitigating)
    return max(_CLAMP[0], min(_CLAMP[1], round(raw, 2)))


def synthesize_rebuttal_from_defense(motion: dict, *, case_id: str,
                                     iteration: int) -> CounselRebuttalOutput:
    """Convert a round-1 DefenseMotion dict into the CounselRebuttalOutput
    shape Phase B consumes. Pure + deterministic (resume-safe: re-synthesis
    from the cached motion yields the identical record)."""
    weak_links: list[WeakLink] = []
    for a in motion.get("attacks") or []:
        attack_id = a.get("attack_id") or "A-r1-?"
        a_type = a.get("type") or "other"
        argument = (a.get("argument") or "").strip()
        grounding = (a.get("grounding") or "").strip()
        question = (
            f"Rebut or concede defense attack {attack_id} "
            f"({a_type}, {a.get('severity')}): {argument}"
        )
        if grounding:
            question += f" | Defense grounding to overcome: {grounding}"
        weak_links.append(WeakLink(
            weakness_id=f"WL-{attack_id}",
            affected_counts=list(a.get("target_counts") or []),
            description=argument[:400] or f"defense attack {attack_id} ({a_type})",
            attack_vector=argument or a_type,
            severity=_SEVERITY_MAP.get(a.get("severity"), "concerning"),
            # Phase B exists to close gaps with targeted public-record work;
            # what genuinely can't be closed surfaces honestly when the
            # rebuttal investigator reports "could not rebut".
            fixable_via="more_investigation",
            investigator_questions=[question],
        ))

    survival = estimate_survival_from_motion(motion)
    n_live = len(motion.get("counts_with_live_attack") or [])
    n_clean = len(motion.get("counts_with_no_live_attack") or [])
    assessment = (
        f"[defense-as-mock] Real defense round 1 served as the mock-defense "
        f"exercise: {len(weak_links)} attack(s) converted to weak links; "
        f"{n_live} count(s) under live attack, {n_clean} clean. Survival "
        f"{survival:.2f} is the documented deterministic heuristic "
        f"(base {_BASE} − {_PER_DISPOSITIVE_COUNT}/dispositive-count − "
        f"{_PER_MITIGATING_ATTACK}/mitigating-attack, clamped "
        f"{_CLAMP[0]}–{_CLAMP[1]}), not a model self-report. The adversarial "
        f"loop replays this motion from adv-r1 cache and proceeds with "
        f"legal_adv_response onward against the revised build."
    )
    return CounselRebuttalOutput(
        case_id=case_id,
        iteration=iteration,
        weak_links=weak_links,
        overall_assessment=assessment,
        estimated_survival_likelihood=survival,
    )
