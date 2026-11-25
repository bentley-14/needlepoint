"""Rebuttal investigator (Layer 4) — surfaces evidence to close defense attacks.

Same schema as the primary investigator; just a different prompt that focuses
on defense-targeted follow-up questions instead of general gap-filling.

Session continuation: fires 1-2 times per case (one per adversarial round
that requires further evidence). Stable session_id per (case_id,
"rebuttal_investigator"). First round gets the full prompt; subsequent
rounds receive only the new defense attacks' follow-up questions.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..claude_p import invoke_agent, session_id_for
from ..schemas import InvestigatorOutput
from .base import load_prompt, render


def _render_continuation_message(
    *,
    round_num: int,
    follow_up_questions: list[str],
    prior_evidence_ids: list[str] | None,
) -> str:
    parts = [
        f"# Adversarial Round {round_num} — rebuttal_investigator (continuation)",
        "",
        "You are continuing as plaintiff's targeted-evidence investigator. "
        "Defense's new round has surfaced new attacks that need rebuttal "
        "evidence. Do NOT re-verify what you've already gathered.",
        "",
        f"## Follow-up questions from this round's defense attacks "
        f"({len(follow_up_questions)} items):",
    ]
    for q in follow_up_questions:
        parts.append(f"  - {q}")
    parts.append("")
    if prior_evidence_ids:
        parts += [
            "## Evidence IDs YOU already produced (don't duplicate):",
            "```",
            "\n".join(f"  - {eid}" for eid in prior_evidence_ids[:50]),
            "```",
            "",
        ]
    parts += [
        "Output InvestigatorOutput JSON with NEW evidence only — items you "
        "added this round, not the cumulative pool.",
    ]
    return "\n".join(parts)


def run(
    *,
    subject: str,
    case_id: str,
    round_num: int,
    follow_up_questions: list[str],
    all_evidence: list[dict],
    out_dir: Path,
    prior_call_count: int = 0,
    prior_evidence_ids: list[str] | None = None,
) -> InvestigatorOutput:
    session_id = session_id_for(case_id, "rebuttal_investigator")
    is_continuation = prior_call_count > 0
    if is_continuation:
        prompt = _render_continuation_message(
            round_num=round_num,
            follow_up_questions=follow_up_questions,
            prior_evidence_ids=prior_evidence_ids,
        )
    else:
        template = load_prompt("rebuttal_investigator")
        prompt = render(
            template,
            subject=subject,
            case_id=case_id,
            round_num=round_num,
            follow_up_questions=json.dumps(follow_up_questions, indent=2),
            all_evidence=json.dumps(all_evidence, indent=2),
        )
    return invoke_agent(
        prompt=prompt,
        schema=InvestigatorOutput,
        out_dir=out_dir,
        step_name="rebuttal_investigator",
        session_id=session_id,
        is_continuation=is_continuation,
    )
