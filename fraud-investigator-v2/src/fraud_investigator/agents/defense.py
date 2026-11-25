"""Defense agent (Layer 4) — adversarial Motion to Dismiss.

Session continuation: fires 2-3 times per case (one per adversarial round).
Stable session_id per (case_id, "defense"). First round gets the full
defense.md prompt + complete case build; subsequent rounds get a delta
message — "Plaintiff mitigated N of your prior attacks at these discount %.
Find NEW defects that survive plaintiff's rebuttal. Don't repeat dismissed
arguments." The defense agent retains its prior attacks in session memory.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..claude_p import invoke_agent, session_id_for
from ..schemas import DefenseMotion
from .base import load_prompt, render


def _format_numeric_audit(numeric_audit: list[dict] | None) -> str:
    """Render the Guardrail-2 findings into a prompt block. MISMATCH findings are
    dispositive integrity defects the defense MUST raise; UNVERIFIABLE findings are
    numbers the case asserts but the auditor could not re-derive (also attackable)."""
    if not numeric_audit:
        return ("(No structured numeric predicates were declared, so no automated "
                "re-derivation ran. You MUST still independently re-derive every "
                "headline quantitative claim — impossibility hours, FTE, percentile "
                "multiples, ratios, damages — from the underlying units and CANONICAL "
                "code definitions, and attack any number you cannot reproduce.)")
    mism = [f for f in numeric_audit if f.get("status") == "MISMATCH"]
    unv = [f for f in numeric_audit if f.get("status") == "UNVERIFIABLE"]
    lines = [f"{len(numeric_audit)} numeric claim(s) were re-derived deterministically "
             f"from canonical constants ({len(mism)} MISMATCH, {len(unv)} UNVERIFIABLE):", ""]
    for f in numeric_audit:
        lines.append(
            f"- [{f['status']}] {f.get('id')} ({f.get('kind')}): "
            f"claimed={f.get('claimed')}, recomputed={f.get('recomputed')} — {f.get('detail')}")
    if mism:
        lines += ["", "🛑 The MISMATCH finding(s) above are DISPOSITIVE INTEGRITY DEFECTS: "
                  "the case asserts a number that does not match the canonical re-derivation. "
                  "You MUST raise each as a `damages_overstated` (or `other`) attack with "
                  "severity='dispositive' — a real defense expert reproduces these numbers and "
                  "an unreproducible/inflated figure impeaches the whole pleading."]
    return "\n".join(lines)


def _render_continuation_message(
    *,
    round_num: int,
    prior_rounds_summary: list[dict],
    new_evidence_since_last_round: list[dict] | None,
    updated_counsel_build: dict | None,
) -> str:
    parts = [
        f"# Adversarial Round {round_num} — defense (continuation)",
        "",
        "You are continuing the mock-defense engagement. Plaintiff's "
        "rebuttal-investigator surfaced new evidence + plaintiff disposed "
        "of your prior round's attacks. Now find NEW defects that survive "
        "plaintiff's rebuttal. Do NOT repeat arguments already mitigated or "
        "dispositively defeated — the model retains your full prior round "
        "in session memory.",
        "",
    ]
    if prior_rounds_summary:
        parts += [
            f"## Disposition summary of your prior round(s) "
            f"({len(prior_rounds_summary)} prior):",
            "```json",
            json.dumps(prior_rounds_summary, indent=2),
            "```",
            "",
        ]
    if new_evidence_since_last_round:
        parts += [
            f"## New evidence plaintiff surfaced since your last round "
            f"({len(new_evidence_since_last_round)} items):",
            "```json",
            json.dumps(new_evidence_since_last_round, indent=2),
            "```",
            "",
        ]
    if updated_counsel_build:
        parts += [
            "## Plaintiff's CURRENT case build (post-rebuttal-revision):",
            "```json",
            json.dumps(updated_counsel_build, indent=2),
            "```",
            "",
        ]
    parts += [
        "Now produce a fresh DefenseMotion targeting weaknesses NOT already "
        "addressed. Emit standard DefenseMotion JSON.",
    ]
    return "\n".join(parts)


def run(
    *,
    subject: str,
    case_id: str,
    round_num: int,
    counsel_final_build: dict,
    all_evidence: list[dict],
    prior_rounds_summary: list[dict],
    verified_precedents: list[str] | None = None,
    out_dir: Path,
    prior_call_count: int = 0,
    new_evidence_since_last_round: list[dict] | None = None,
    numeric_audit: list[dict] | None = None,
    tier_lint_block: str = "",
) -> DefenseMotion:
    session_id = session_id_for(case_id, "defense")
    is_continuation = prior_call_count > 0
    numeric_audit_block = _format_numeric_audit(numeric_audit)
    if is_continuation:
        prompt = _render_continuation_message(
            round_num=round_num,
            prior_rounds_summary=prior_rounds_summary,
            new_evidence_since_last_round=new_evidence_since_last_round,
            updated_counsel_build=counsel_final_build,
        )
        prompt += ("\n\n## Guardrail-2 numeric audit (carried across rounds):\n"
                   + numeric_audit_block)
        if tier_lint_block:
            prompt += "\n" + tier_lint_block
    else:
        template = load_prompt("defense")
        prompt = render(
            template,
            subject=subject,
            case_id=case_id,
            round_num=round_num,
            counsel_final_build=json.dumps(counsel_final_build, indent=2),
            all_evidence=json.dumps(all_evidence, indent=2),
            prior_rounds_summary=json.dumps(prior_rounds_summary, indent=2),
            verified_precedents=json.dumps(verified_precedents or [], indent=2),
            numeric_audit=numeric_audit_block,
        )
        if tier_lint_block:
            prompt += "\n" + tier_lint_block
    return invoke_agent(
        prompt=prompt,
        schema=DefenseMotion,
        out_dir=out_dir,
        step_name="defense",
        session_id=session_id,
        is_continuation=is_continuation,
    )
