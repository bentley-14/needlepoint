"""Counsel-rebuttal agent — devil's-advocate self-attack on the build (step 2/3)."""
from __future__ import annotations

import json
from pathlib import Path

from ..claude_p import invoke_agent
from ..schemas import CounselRebuttalOutput
from .base import load_prompt, render


def run(
    *,
    subject: str,
    case_id: str,
    iteration: int,
    counsel_build_output: dict,
    verified_precedents: list[str] | None = None,
    out_dir: Path,
) -> CounselRebuttalOutput:
    """Counsel rebuttal — adversarial-prep critique of the build.

    Does NOT receive a separate all_evidence payload. The build output already
    references evidence ids via its Rule 9(b) grid + factual_basis_per_count;
    if rebuttal needs full evidence text, it can Bash-read
    iter-{N}/investigator.parsed.json.
    """
    template = load_prompt("counsel_rebuttal")
    prompt = render(
        template,
        subject=subject,
        case_id=case_id,
        iteration=iteration,
        counsel_build_output=json.dumps(counsel_build_output, indent=2),
        verified_precedents=json.dumps(verified_precedents or [], indent=2),
    )
    return invoke_agent(
        prompt=prompt,
        schema=CounselRebuttalOutput,
        out_dir=out_dir,
        step_name="counsel_rebuttal",
    )
