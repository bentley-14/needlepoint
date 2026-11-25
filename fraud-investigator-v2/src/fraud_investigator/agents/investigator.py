"""Investigator agent — gathers evidence per iteration.

Two-pass discovery-mode architecture (2026-05-21):

  Pass A (DISCOVER) — investigator_discover.md prompt, fires ONCE at the
    start of Phase A iter-1. No checklist, no fraud-signature priors,
    no schema-driven evidence tiers. Pure exploration: "what's weird
    here?" Outputs surprises + working_hypotheses + analysis_unit
    declaration. ~2K-token prompt, ~10-15 turns.

  Pass B (VERIFY) — investigator.md prompt, the existing rigorous
    investigation. Receives Pass A's surprises + working_hypotheses
    as input so it knows where to focus the disciplined work. Outputs
    full structured evidence + Rule 9(b) mapping + co-biller
    characterization. ~10K-token prompt, ~25 turns.

Background: monolithic prompts past ~10K tokens hit instruction-following
degradation (Lost-in-the-Middle, Liu et al. 2023). Provider Juniper 2026-05-21
cold-eyes run executed Step 2's address-sweep IDENTIFICATION but missed
Step 2's billing CHARACTERIZATION (10 of 10 mandatory steps competing for
attention → checklist-mode triage). Separating discovery from verification
gives each pass concentrated attention on its own task.

Session continuation: fires 3-4 times per case (Phase A iter-1..3 + Phase B
rebuttal-closing). Stable session_id per (case_id, "investigator"). First
verify-pass call gets the full investigator.md prompt (~36KB); continuation
calls send only the iter-N task + the new counsel disposition. The
investigator retains memory of all prior tool calls + evidence within
session state, which both saves tokens AND gives the agent natural
awareness of "what I've already verified" without re-deriving from a
re-injected JSON dump.

The DISCOVER pass uses its own session_id_for(case_id, "investigator_discover")
so it's separable from the VERIFY session — discover fires once and is
self-contained; the verify session is the one that continues across iters.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..claude_p import invoke_agent, session_id_for
from ..schemas import InvestigatorOutput
from .base import load_prompt, render


def _render_continuation_message(
    *,
    iteration: str,
    prior_counsel_disposition: dict | None,
    prior_evidence_ids: list[str] | None,
    prior_working_hypotheses: list[str] | None = None,
    prior_unexpected_absences: list[str] | None = None,
) -> str:
    parts = [
        f"# Iteration {iteration} — investigator (continuation)",
        "",
        "You are resuming this case investigation. Counsel has reviewed "
        "your prior work and the rest of the team has advanced the case. "
        "Below is what's new since your last turn. Do NOT re-verify what "
        "you've already gathered — the prior evidence is in your session "
        "memory.",
        "",
    ]
    if prior_counsel_disposition:
        parts += [
            "## Counsel's latest disposition / direction:",
            "```json",
            json.dumps(prior_counsel_disposition, indent=2),
            "```",
            "",
            "Focus this iteration on:",
            "  - `follow_up_questions_for_investigator` items above (highest priority)",
            "  - `gaps_to_fill` items above",
            "  - Any `counts_to_drop` that affect your evidence prioritization",
            "",
        ]
    if prior_working_hypotheses:
        # Scratchpad threading (discovery-mode prompts): your own prior
        # working_hypotheses are your continuity. Each one must be either
        # PURSUED (advanced this iter) or RETIRED (explicitly killed with
        # a reason). Leaving them un-touched is silent abandonment.
        parts += [
            f"## Your own working_hypotheses from the prior iter "
            f"({len(prior_working_hypotheses)} threads — each must be "
            f"PURSUED or RETIRED this iter):",
        ]
        for h in prior_working_hypotheses[:5]:
            parts.append(f"  - {h}")
        parts += [
            "",
            "For each hypothesis above: emit at least one piece of evidence "
            "advancing it (PURSUED) OR a single sentence in this iter's "
            "`notes_for_counsel` killing it with a reason (RETIRED). "
            "Silently dropping a working hypothesis is a failure mode — "
            "it leaves a known-unknown unresolved across the case lifecycle.",
            "",
        ]
    if prior_unexpected_absences:
        parts += [
            f"## Your own unexpected_absences from prior iter "
            f"({len(prior_unexpected_absences)} items — each is a thread "
            f"the prior iter recognized as 'should have been here but wasn't'):",
        ]
        for a in prior_unexpected_absences[:3]:
            parts.append(f"  - {a}")
        parts += [
            "",
            "Each absence is either explanatory (the missing thing isn't "
            "available, document why) or actionable (try harder to find it "
            "this iter). Don't let absences drift across iterations.",
            "",
        ]
    if prior_evidence_ids:
        parts += [
            "## Evidence IDs YOU already produced (don't duplicate):",
            "```",
            "\n".join(f"  - {eid}" for eid in prior_evidence_ids[:50]),
            "```",
            "",
        ]
    parts += [
        "Execute the same workflow as before but focus on the gaps + follow-ups "
        "+ working_hypotheses above. Output the InvestigatorOutput JSON with "
        "NEW evidence only (the items you added this iteration), not the "
        "cumulative pool. Remember: `surprises`, `unexpected_absences`, "
        "`working_hypotheses`, and `analysis_unit_chosen` are MANDATORY "
        "fields. An empty `surprises` list is acceptable only with a brief "
        "attestation in `notes_for_counsel` of what you checked and found "
        "unremarkable — never pad it with pack-derivable or fabricated "
        "entries.",
    ]
    return "\n".join(parts)


_NO_PACK_FALLBACK = (
    "(no Step-0 evidence pack was built for this case — run the identity / "
    "PECOS / exclusion / billing-profile / co-biller lookups yourself per the "
    "workflow steps)"
)


def run_discover(
    *,
    subject: str,
    case_id: str,
    out_dir: Path,
    evidence_pack: str = "",
) -> InvestigatorOutput:
    """Pass A — open-ended discovery investigation.

    Fires ONCE at the start of Phase A iter-1. No checklist, no fraud-
    signature priors. Outputs surprises + working_hypotheses + analysis_unit
    declaration. Verify-pass (run() below) receives these as priors.

    Separate session_id from the verify pass so the two can be debugged
    independently. The discover output is self-contained (no continuation
    calls).
    """
    template = load_prompt("investigator_discover")
    prompt = render(
        template,
        subject=subject,
        case_id=case_id,
        iteration="1-discover",
        evidence_pack=evidence_pack or _NO_PACK_FALLBACK,
    )
    session_id = session_id_for(case_id, "investigator_discover")
    return invoke_agent(
        prompt=prompt,
        schema=InvestigatorOutput,
        out_dir=out_dir,
        step_name="investigator_discover",
        session_id=session_id,
        is_continuation=False,
        # Discovery pass has full tool access — no restrictions. The
        # verify pass has session-state memory of Pass A's tool calls
        # via prior_working_hypotheses threading; the actual tool history
        # is separate (different session_id) so the model doesn't get
        # confused between exploration and verification.
    )


def run(
    *,
    subject: str,
    case_id: str,
    iteration: str,
    prior_counsel_disposition: dict | None,
    existing_evidence: list[dict],
    out_dir: Path,
    prior_call_count: int = 0,
    prior_evidence_ids: list[str] | None = None,
    prior_working_hypotheses: list[str] | None = None,
    prior_unexpected_absences: list[str] | None = None,
    discover_surprises: list[str] | None = None,
    discover_analysis_unit: str | None = None,
    evidence_pack: str = "",
    osint_mandate: bool = False,
) -> InvestigatorOutput:
    session_id = session_id_for(case_id, "investigator")
    is_continuation = prior_call_count > 0
    if is_continuation:
        prompt = _render_continuation_message(
            iteration=iteration,
            prior_counsel_disposition=prior_counsel_disposition,
            prior_evidence_ids=prior_evidence_ids,
            prior_working_hypotheses=prior_working_hypotheses,
            prior_unexpected_absences=prior_unexpected_absences,
        )
    else:
        template = load_prompt("investigator")
        # If Pass A (discover) produced surprises + analysis_unit, splice them
        # into the verify prompt as priors so the verify pass knows where the
        # discovery pass identified the interesting threads. This is what
        # makes the two-pass architecture better than two independent passes —
        # the discoveries directly shape the verify pass's focus.
        discover_priors_block = ""
        if discover_surprises or discover_analysis_unit:
            parts = ["", "## Priors from the discovery pass (read FIRST)", ""]
            if discover_surprises:
                parts.append(
                    f"The discovery pass surfaced these {len(discover_surprises)} surprises. "
                    "Each is a thread the verify pass must either confirm with structured "
                    "evidence OR explicitly retire with a reason:"
                )
                for s in discover_surprises:
                    parts.append(f"  - {s}")
                parts.append("")
            if discover_analysis_unit:
                parts.append(
                    f"The discovery pass chose analysis_unit = `{discover_analysis_unit}`. "
                    "Verify pass should anchor evidence-gathering on that unit. "
                    "If you disagree with the choice after running the disciplined "
                    "steps, override in your output's analysis_unit_chosen but justify "
                    "with evidence_ids in analysis_unit_rationale."
                )
                parts.append("")
            discover_priors_block = "\n".join(parts)
        prompt = render(
            template,
            subject=subject,
            case_id=case_id,
            iteration=iteration,
            prior_counsel_disposition=json.dumps(prior_counsel_disposition or {}, indent=2),
            existing_evidence=json.dumps(existing_evidence, indent=2),
            evidence_pack=evidence_pack or _NO_PACK_FALLBACK,
        )
        # Append discover priors block AFTER rendering (it's not a template var)
        if discover_priors_block:
            prompt = prompt + discover_priors_block
    if osint_mandate:
        # The previous iteration returned zero osint_findings. Prose guidance
        # loses to turn pressure; this block is the mechanical re-injection.
        prompt = prompt + (
            "\n\n## ⚠ MANDATORY THIS ITERATION — the open-web scan you skipped\n\n"
            "Your previous iteration returned ZERO `osint_findings`. The web scan "
            "is not optional color; it is part of the evidence contract. THIS "
            "iteration you MUST attempt at least THREE source classes — "
            "practice_website, reviews, state_board (news/facility_data as "
            "warranted) — via WebSearch → WebFetch (→ browser_fetch.py on 403), "
            "and record EVERY attempt in `osint_findings` with status "
            "finding | blocked | nothing_found. A blocked or empty source is "
            "recorded as such, never treated as a clean record. Identity-verify "
            "every profile hit (NPI / practice name / address, not name alone) "
            "per the directory-profile rule above.\n"
        )
    return invoke_agent(
        prompt=prompt,
        schema=InvestigatorOutput,
        out_dir=out_dir,
        step_name="investigator",
        session_id=session_id,
        is_continuation=is_continuation,
        # Note: knob B (disabling the Agent / sub-agent spawn tool) was
        # tried and rolled back. iter-4 weak-link investigations
        # legitimately benefit from sub-agent parallelism — five 5-10 min
        # nested spawns can cover more ground than 25 min of serial
        # reasoning. Keep the door open; opus quality at the parent level
        # already compensates for some of the per-spawn overhead.
    )
