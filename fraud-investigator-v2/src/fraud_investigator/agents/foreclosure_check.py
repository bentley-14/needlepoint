"""Innocent-Explanation Foreclosure Check (Guardrail 3) — post-case-ready stage.

Operationalizes the Twombly/Iqbal/Integra survival test: fraud must be the MORE
plausible inference, and every obvious innocent explanation must be foreclosed
(with a cited, audited source) or flagged LIVE. Runs once, after the adversarial
loop, before the final strength score — a LIVE high-plausibility explanation caps
strength and blocks a clean FILE (enforced deterministically in the orchestrator).
"""
from __future__ import annotations

import json
from pathlib import Path

from ..claude_p import invoke_agent
from ..schemas import ForeclosureLedgerOutput
from .base import load_prompt, render


def run(
    *,
    subject: str,
    case_id: str,
    counsel_final_build: dict,
    all_evidence: list[dict],
    numeric_audit: list[dict] | None,
    verified_precedents: list[str] | None,
    out_dir: Path,
) -> ForeclosureLedgerOutput:
    template = load_prompt("foreclosure_check")
    prompt = render(
        template,
        subject=subject,
        case_id=case_id,
        counsel_final_build=json.dumps(counsel_final_build, indent=2),
        all_evidence=json.dumps(all_evidence, indent=2),
        numeric_audit=json.dumps(numeric_audit or [], indent=2),
        verified_precedents=json.dumps(verified_precedents or [], indent=2),
    )
    return invoke_agent(
        prompt=prompt,
        schema=ForeclosureLedgerOutput,
        out_dir=out_dir,
        step_name="foreclosure_check",
    )
