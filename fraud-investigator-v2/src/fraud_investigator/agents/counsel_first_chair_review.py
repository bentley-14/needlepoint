"""Counsel first-chair review (Phase A judgement, between build and rebuttal).

Decides whether the iter-N draft complaint is mature enough to send to the
internal mock-defense red-team (counsel_rebuttal) or needs another build
iteration. Distinct from counsel_disposition, which is the post-rebuttal
managing-partner sign-off.

Session continuation: fires 3 times per Phase A. Stable session_id per
(case_id, "counsel_first_chair_review") — first call sees the full prompt
with iter-1 build; continuation calls receive only the new build output and
prior review. Partner retains memory of prior reviews across calls.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..claude_p import invoke_agent, session_id_for
from ..schemas import CounselFirstChairReviewOutput
from .base import load_prompt, render


def _floor_audit_block(floor_audit: dict | None) -> str:
    """Render the mechanical floor audit as an appended prompt block.

    Appended (rather than templated) so it reaches continuation calls
    identically to fresh ones. The slot arithmetic in the audit is
    authoritative — the agent adjudicates the semantic residue.
    """
    if not floor_audit:
        return ""
    return (
        "\n\n## Mechanical floor audit (computed by the pipeline — "
        "authoritative slot arithmetic; do NOT re-count evidence ids)\n"
        "```json\n" + json.dumps(floor_audit, indent=2) + "\n```\n"
        "Your floor job is the part arithmetic can't do: are the convergent "
        "signals genuinely INDEPENDENT, does the refutation narrative actually "
        "refute, and is each claimed falsity anchor a real confirmed-fact "
        "anchor (literal in its cited source) rather than an inference?\n"
    )


def _render_continuation_message(
    *,
    iteration: int,
    counsel_build_output: dict,
    prior_first_chair_review: dict | None,
    max_pre_rebuttal_iters: int,
) -> str:
    parts = [
        f"# Phase A — Iteration {iteration}/{max_pre_rebuttal_iters} — "
        f"first-chair review (continuation)",
        "",
        "Counsel-build has produced an updated draft based on your prior "
        "review feedback. Apply the same decision framework as before "
        "(CONTINUE_BUILD / DROP_COUNTS_AND_CONTINUE / PROCEED_TO_REBUTTAL / "
        "ABANDON_CASE) and emit a fresh CounselFirstChairReviewOutput.",
        "",
    ]
    if prior_first_chair_review:
        parts += [
            "## Your prior review (for reference — already in session memory):",
            "```json",
            json.dumps(prior_first_chair_review, indent=2),
            "```",
            "",
        ]
    parts += [
        f"## New counsel_build output (iter-{iteration}):",
        "```json",
        json.dumps(counsel_build_output, indent=2),
        "```",
        "",
        "Output your CounselFirstChairReviewOutput JSON.",
    ]
    return "\n".join(parts)


def run(
    *,
    subject: str,
    case_id: str,
    iteration: int,
    counsel_build_output: dict,
    prior_first_chair_review: dict | None,
    max_pre_rebuttal_iters: int,
    out_dir: Path,
    prior_call_count: int = 0,
    floor_audit: dict | None = None,
) -> CounselFirstChairReviewOutput:
    session_id = session_id_for(case_id, "counsel_first_chair_review")
    is_continuation = prior_call_count > 0
    if is_continuation:
        prompt = _render_continuation_message(
            iteration=iteration,
            counsel_build_output=counsel_build_output,
            prior_first_chair_review=prior_first_chair_review,
            max_pre_rebuttal_iters=max_pre_rebuttal_iters,
        )
    else:
        template = load_prompt("counsel_first_chair_review")
        prompt = render(
            template,
            subject=subject,
            case_id=case_id,
            iteration=iteration,
            max_pre_rebuttal_iters=max_pre_rebuttal_iters,
            counsel_build_output=json.dumps(counsel_build_output, indent=2),
            prior_first_chair_review=json.dumps(prior_first_chair_review or {}, indent=2),
        )
    prompt += _floor_audit_block(floor_audit)
    return invoke_agent(
        prompt=prompt,
        schema=CounselFirstChairReviewOutput,
        out_dir=out_dir,
        step_name="counsel_first_chair_review",
        session_id=session_id,
        is_continuation=is_continuation,
    )
