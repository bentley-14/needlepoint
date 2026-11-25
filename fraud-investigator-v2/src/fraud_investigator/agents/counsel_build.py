"""Counsel-build agent — constructs case theory + Rule 9(b) elements (step 1/3).

Session continuation:
    The counsel_build agent fires 3-4 times per case (3 Phase A iters +
    optionally 1 Phase B revision). Across these calls, the prior-iter
    context is identical except for new investigator evidence and the
    first-chair partner's feedback. Rather than re-feeding the full prior
    context in each iter's prompt (which grew from 22KB at iter-1 to
    36KB at iter-3 in the Juniper rerun), the orchestrator passes a
    stable ``session_id`` per (case_id, "counsel_build") and a
    ``prior_call_count`` indicator.

      - call #0 (first):  full template rendered with iter-1 evidence + no prior disposition
      - call #1+ (cont):  short delta message — "iter-N investigator returned X new
                          evidence items; first-chair said: <action> with reasoning <...>.
                          Revise your build." The model retains its prior turns in
                          claude -p's session-on-disk + 1-hour prompt cache, so it
                          doesn't have to re-derive case theory from scratch.

    Token-savings illustration (Opus high on counsel_build, Juniper case shape):
      Without session continuation: 3 iters × 27KB avg prompt = ~22K input tokens × Opus rate
      With session continuation: iter-1 full (22KB) + iter-2 delta (~3KB) + iter-3 delta (~4KB)
                                  Subsequent calls hit prompt cache → ~10% input rate.
      Estimated savings: 60-70% of counsel_build input cost.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..claude_p import invoke_agent, session_id_for
from ..schemas import CounselBuildOutput
from .base import load_prompt, render


def _render_continuation_message(
    *,
    iteration: int,
    new_evidence: list[dict],
    prior_disposition: dict | None,
    verified_precedents_added: list[str] | None,
) -> str:
    """Build the short delta message for a continuation call.

    The model already has all prior context in its session memory; this
    message just summarizes what changed since the last call.
    """
    parts = [
        f"# Phase A — Iteration {iteration} — counsel_build (continuation)",
        "",
        "The investigator has completed this iteration's evidence gathering "
        "and the first-chair partner has reviewed your prior build. Update "
        "your case theory + counts + Rule 9(b) elements based on what's new.",
        "",
    ]
    if prior_disposition:
        parts.append("## First-chair partner's review of your prior iter's build:")
        parts.append("```json")
        parts.append(json.dumps(prior_disposition, indent=2))
        parts.append("```")
        parts.append("")
        action = prior_disposition.get("action") or prior_disposition.get("recommended_action")
        if action:
            parts.append(f"**First-chair decision:** `{action}`. "
                         f"Address the `counts_to_drop`, `follow_up_questions_for_investigator` answers, "
                         f"and `gaps_to_fill` from above as you revise.")
            parts.append("")
    if new_evidence:
        parts.append(f"## New evidence from iter-{iteration} investigator "
                     f"({len(new_evidence)} items):")
        parts.append("```json")
        parts.append(json.dumps(new_evidence, indent=2))
        parts.append("```")
        parts.append("")
    if verified_precedents_added:
        parts.append(f"## Newly-verified precedents ({len(verified_precedents_added)}):")
        for p in verified_precedents_added:
            parts.append(f"  - {p}")
        parts.append("")
    parts.append("Output the revised `CounselBuildOutput` JSON, drawing on "
                 "your full session memory of prior iterations plus the "
                 "above delta. The schema is unchanged from your prior call.")
    return "\n".join(parts)


def run(
    *,
    subject: str,
    case_id: str,
    iteration: int,
    all_evidence: list[dict],
    prior_disposition: dict | None,
    verified_precedents: list[str] | None = None,
    out_dir: Path,
    prior_call_count: int = 0,
    new_evidence_since_last_call: list[dict] | None = None,
    verified_precedents_added_since_last_call: list[str] | None = None,
) -> CounselBuildOutput:
    """Run counsel_build, optionally as a session continuation.

    Parameters
    ----------
    prior_call_count
        Number of prior counsel_build calls in this case. 0 = first call
        (full prompt). >0 = continuation (delta message + session reuse).
    new_evidence_since_last_call
        Evidence items added by the most-recent investigator iter (NOT the
        full accumulated evidence — that's already in session memory).
        Required when ``prior_call_count > 0``.
    verified_precedents_added_since_last_call
        Same: newly-verified precedents since last call, NOT the full set.
    """
    session_id = session_id_for(case_id, "counsel_build")
    is_continuation = prior_call_count > 0

    if is_continuation:
        prompt = _render_continuation_message(
            iteration=iteration,
            new_evidence=new_evidence_since_last_call or [],
            prior_disposition=prior_disposition,
            verified_precedents_added=verified_precedents_added_since_last_call,
        )
    else:
        template = load_prompt("counsel_build")
        prompt = render(
            template,
            subject=subject,
            case_id=case_id,
            iteration=iteration,
            all_evidence=json.dumps(all_evidence, indent=2),
            prior_disposition=json.dumps(prior_disposition or {}, indent=2),
            verified_precedents=json.dumps(verified_precedents or [], indent=2),
        )
    return invoke_agent(
        prompt=prompt,
        schema=CounselBuildOutput,
        out_dir=out_dir,
        step_name="counsel_build",
        session_id=session_id,
        is_continuation=is_continuation,
        # Knob C: tightly restrict counsel_build's tool surface.
        # counsel_build is a STRATEGY agent — its job is case theory,
        # count framing, Rule 9(b) mapping, and follow-up-question
        # generation for the next investigator iter. Diagnostic of a
        # prior sonnet rerun showed counsel_build spent 34 of 42 tool
        # calls (81%) re-verifying CourtListener cites — work that
        # is supposed to be cached in `verified_precedents` and done
        # ONCE upstream by the investigator. counsel_build should
        # treat `verified_precedents` as authoritative and propose new
        # cites for the NEXT investigator iter to validate; it does
        # not validate cites itself.
        #
        # Allowed tools: Read (parsed.json drilldowns), Grep/Glob
        # (search the case state), the scoped precedent_lookup (read-only
        # R2 doctrine corpus, structured by defense_mode x circuit), the
        # scoped doctrine_lookup (read-only local docs/legal_corpus/
        # narrative doctrine cards, free-text BM25; gives pleading-time doctrine context instead of it landing late in
        # adversarial rounds), and precedent_cache's `get` subcommand ONLY
        # (T3b — check whether a citation is already verified before
        # proposing a re-verify; the allowlist prefix stops at "get" so
        # `put` stays unreachable — counsel_build must never assert a
        # citation is verified itself, only the investigator's real
        # courtlistener_search.py verify can do that). General Bash, Edit,
        # Write, Agent, WebSearch, WebFetch stay closed — counsel still
        # can't re-verify cites or shell-spelunk for evidence; it can only
        # pull controlling precedent/doctrine to plead the venue's standard.
        extra_args=[
            "--allowedTools",
            "Read,Grep,Glob,"
            "Bash(python scripts/precedent_lookup.py:*),Bash(python3 scripts/precedent_lookup.py:*),"
            "Bash(python scripts/doctrine_lookup.py:*),Bash(python3 scripts/doctrine_lookup.py:*),"
            "Bash(python scripts/precedent_cache.py get:*),Bash(python3 scripts/precedent_cache.py get:*)",
        ],
    )
