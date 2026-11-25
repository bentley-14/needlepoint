"""Legal adversarial response (Layer 4) — disposes each defense attack.

Session continuation: fires 2-3 times per case (one per adversarial round).
Stable session_id per (case_id, "legal_adv_response"). First round gets the
full prompt; subsequent rounds receive only the new defense motion and
optionally the cumulative discount state. Plaintiff's response agent
retains its prior dispositions in session memory.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..claude_p import invoke_agent, session_id_for
from ..discounts import apply_deterministic_discounts
from ..schemas import LegalAdversarialResponse
from .base import load_prompt, render


def _render_continuation_message(
    *,
    round_num: int,
    defense_motion: dict,
    prior_round_reasoning: str,
    updated_counsel_build: dict | None,
) -> str:
    parts = [
        f"# Adversarial Round {round_num} — legal_adv_response (continuation)",
        "",
        "You are continuing as plaintiff's adversarial-response counsel. "
        "Defense has produced a new motion with attacks not already "
        "addressed in prior rounds. Apply the same three-tier disposition "
        "discipline (SURVIVED / MITIGATED with discount / "
        "DISPOSITIVE_NOT_SURVIVED) as before. Reference your prior round's "
        "reasoning where attacks build on earlier ones.",
        "",
    ]
    if prior_round_reasoning:
        parts += [
            "## Your prior round reasoning (already in session memory):",
            prior_round_reasoning[:2000] + (
                "..." if len(prior_round_reasoning) > 2000 else ""
            ),
            "",
        ]
    parts += [
        f"## Defense's Round {round_num} motion:",
        "```json",
        json.dumps(defense_motion, indent=2),
        "```",
        "",
    ]
    if updated_counsel_build:
        parts += [
            "## Plaintiff's current case build (post any rebuttal revision):",
            "```json",
            json.dumps(updated_counsel_build, indent=2),
            "```",
            "",
        ]
    parts += [
        "Output the LegalAdversarialResponse JSON. Pay attention to "
        "cumulative discount carry-forward and counts_pending_rebuttal vs "
        "counts_abandoned_this_round distinctions.",
    ]
    return "\n".join(parts)


def run(
    *,
    subject: str,
    case_id: str,
    round_num: int,
    counsel_final_build: dict,
    defense_motion: dict,
    all_evidence: list[dict],
    prior_round_reasoning: str,
    verified_precedents: list[str] | None = None,
    out_dir: Path,
    prior_call_count: int = 0,
    numeric_audit: list | None = None,
) -> LegalAdversarialResponse:
    session_id = session_id_for(case_id, "legal_adv_response")
    is_continuation = prior_call_count > 0
    if is_continuation:
        prompt = _render_continuation_message(
            round_num=round_num,
            defense_motion=defense_motion,
            prior_round_reasoning=prior_round_reasoning,
            updated_counsel_build=counsel_final_build,
        )
    else:
        template = load_prompt("legal_adv_response")
        prompt = render(
            template,
            subject=subject,
            case_id=case_id,
            round_num=round_num,
            counsel_final_build=json.dumps(counsel_final_build, indent=2),
            defense_motion=json.dumps(defense_motion, indent=2),
            all_evidence=json.dumps(all_evidence, indent=2),
            prior_round_reasoning=prior_round_reasoning,
            verified_precedents=json.dumps(verified_precedents or [], indent=2),
        )
    parsed = invoke_agent(
        prompt=prompt,
        schema=LegalAdversarialResponse,
        out_dir=out_dir,
        step_name="legal_adv_response",
        session_id=session_id,
        is_continuation=is_continuation,
    )
    # Deterministic discount schedule: the LLM classifies, the code computes.
    # Recomputes each MITIGATED attack's discount_pct from its classification
    # (the model's own pick is preserved as discount_pct_model) and the
    # damages-weighted cumulative + settlement target. SURVIVED/DISPOSITIVE
    # dispositions are untouched; old outputs without classification fall back
    # to the model's pick.
    adjusted = apply_deterministic_discounts(
        parsed.model_dump(mode="json"), counsel_final_build, numeric_audit)
    return LegalAdversarialResponse.model_validate(adjusted)
