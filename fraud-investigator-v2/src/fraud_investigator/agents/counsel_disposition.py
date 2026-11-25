"""Counsel-disposition agent — meta-decision over build+rebuttal (step 3/3)."""
from __future__ import annotations

import json
from pathlib import Path

from ..claude_p import invoke_agent
from ..schemas import CounselDispositionOutput
from .base import load_prompt, render


def run(
    *,
    subject: str,
    case_id: str,
    iteration: int,
    counsel_build_output: dict,
    counsel_rebuttal_output: dict,
    out_dir: Path,
    floor_audit: dict | None = None,
    human_review_resolutions: list[dict] | None = None,
) -> CounselDispositionOutput:
    template = load_prompt("counsel_disposition")
    prompt = render(
        template,
        subject=subject,
        case_id=case_id,
        iteration=iteration,
        counsel_build_output=json.dumps(counsel_build_output, indent=2),
        counsel_rebuttal_output=json.dumps(counsel_rebuttal_output, indent=2),
    )
    if human_review_resolutions:
        # Guardrail-4 — a human reviewer resolved a question the loop could not
        # verify (e.g. "is this sealed prior case a qui tam?"). These answers are
        # AUTHORITATIVE and override any contrary inference in the record.
        lines = "\n".join(
            f"- Q: {r.get('question', '')}\n  A (authoritative): {r.get('answer', '')}"
            for r in human_review_resolutions
        )
        prompt += (
            "\n\n## Human-review resolutions (AUTHORITATIVE — overrides the record)\n"
            "A human reviewer has resolved a question the loop could NOT verify on its "
            "own. Treat each answer below as an established confirmed fact. You MUST NOT "
            "abandon or decline on any premise these answers refute (e.g. a first-to-file "
            "bar the reviewer says does not apply). If an answer removes the only "
            "dispositive defect, proceed accordingly.\n"
            f"{lines}\n"
        )
    if floor_audit:
        prompt += (
            "\n\n## Mechanical floor audit (computed by the pipeline — "
            "authoritative slot arithmetic; do NOT re-count evidence ids)\n"
            "```json\n" + json.dumps(floor_audit, indent=2) + "\n```\n"
            "Your floor job is the part arithmetic can't do: signal "
            "INDEPENDENCE, refutation QUALITY, and whether each claimed "
            "falsity anchor is a genuine confirmed-fact anchor.\n"
        )
    return invoke_agent(
        prompt=prompt,
        schema=CounselDispositionOutput,
        out_dir=out_dir,
        step_name="counsel_disposition",
    )
