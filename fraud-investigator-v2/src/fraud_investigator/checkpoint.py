"""Guardrail-4 — human-in-the-loop checkpoint detection.

The failure class this guards against: the loop reaches a *dispositive,
case-killing* conclusion that rests on a premise it **cannot verify**, and
silently resolves it AGAINST the case. The motivating incident (Birch,
CASE-6A368C27): the rebuttal investigator found a *sealed* docket
(Dogwood v. Agency Myrtle LLC) and asserted "this is a qui tam lawsuit" at
``confirmed_fact`` tier, triggering a first-to-file bar (31 USC 3730(b)(5)) →
ABANDON. But a sealed complaint's nature-of-suit cannot be established from
public dockets, and the case was in fact NOS 442 Employment — not a qui tam.

Instead of guessing, the loop should PAUSE, ask a human a specific question,
take the answer as ``human_attested`` evidence, and resume. The two detectors
below are *pure* — they return a ``ReviewRequest`` when a pause is warranted, or
``None``. The orchestrator (phase_b / phase_c) calls them at the dispositive
seam and, on a non-None result, parks the case at
``FinalVerdict.NEEDS_HUMAN_CHECKPOINT`` instead of finalizing the decline.

Selective by construction — a checkpoint fires ONLY when the dispositive premise
is (a) the actual driver of the decline AND (b) resolvable by a discrete fact a
human could supply. Genuine merits weaknesses do not pause the loop.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from .schemas import ReviewRequest

# A jurisdictional bar is in play when the disposition reasoning invokes one of
# these doctrines (or the orchestrator has already set jurisdiction_barred).
_FTF_PATTERNS = re.compile(
    r"first[\s-]?to[\s-]?file|3730\s*\(?\s*b\s*\)?\s*\(?\s*5\s*\)?|public[\s-]?disclosure",
    re.IGNORECASE,
)
# The prior case's *nature* being asserted (this is what's unverifiable for a
# sealed complaint — you cannot read a sealed filing to confirm it's an FCA action).
_NATURE_PATTERNS = re.compile(
    r"qui\s*tam|false\s*claims|\bfca\b|31\s*u\.?s\.?c\.?\s*3729|\brelator\b|first[\s-]?to[\s-]?file",
    re.IGNORECASE,
)
# The "unverifiable" signal: the prior case is under seal. Qui tam complaints are
# filed under seal by statute, so a recently-docketed one cannot have its NATURE read
# (the Dogwood pattern) — and even a CONFIRMED prior qui tam has a sealed SCOPE, so
# whether it covers these claims (first-to-file "same material elements") is equally
# unverifiable (the Warren pattern). Both are dispositive-on-an-unverifiable-premise.
_SEAL_PATTERNS = re.compile(
    r"under\s*seal|pending\s*under\s*seal|\bsealed\b|\bseal\b|unknowable\s+scope|"
    r"sealed\s+scope|material\s+elements",
    re.IGNORECASE,
)


def detect_jurisdiction_checkpoint(
    *,
    case_id: str,
    evidence: list[dict],
    disposition_reasoning: str | None,
    jurisdiction_barred: bool = False,
    provisional_verdict: str = "COUNSEL_ABANDONED",
) -> ReviewRequest | None:
    """Pause when a first-to-file / public-disclosure bar is the dispositive driver
    AND it rests on a ``confirmed_fact`` claim about a *sealed* prior case's nature.

    Provider Dogwood/Birch failure: a sealed docket asserted as a "qui tam" at confirmed_fact
    tier cannot be verified from public data, yet it killed the case. A human can
    supply the nature-of-suit / cover sheet to confirm or refute the bar.
    """
    reasoning = disposition_reasoning or ""
    if not (jurisdiction_barred or _FTF_PATTERNS.search(reasoning)):
        return None

    sealed_in_reasoning = bool(_SEAL_PATTERNS.search(reasoning))
    for ev in evidence or []:
        if str(ev.get("tier", "")) != "confirmed_fact":
            continue
        text = f"{ev.get('fact', '')} {ev.get('citation', '')}"
        if not _NATURE_PATTERNS.search(text):
            continue
        # Unverifiable iff the prior case is sealed (signalled in the evidence
        # itself or in the disposition reasoning that relies on it).
        if not (_SEAL_PATTERNS.search(text) or sealed_in_reasoning):
            continue
        fact = (ev.get("fact") or "").strip()
        return ReviewRequest(
            case_id=case_id,
            checkpoint_type="jurisdiction_bar_unverifiable",
            question=(
                "Does the prior sealed case cited here actually bar this qui tam under "
                "first-to-file / public-disclosure (31 USC 3730(b)(5)/(e)(4))? Two things "
                "cannot be verified from public data: (a) whether it is itself an FCA qui "
                "tam on the SAME facts (vs. a non-FCA matter, e.g. employment), and (b) if "
                "it IS a sealed qui tam, whether its sealed SCOPE actually covers these "
                "claims (the first-to-file 'same material elements' test) or the relator is "
                f'an original source. Cited premise: "{fact}"'
            ),
            unverifiable_premise=fact or "A sealed prior case bars this qui tam under first-to-file.",
            cited_evidence_id=str(ev.get("id", "")),
            why_unverifiable=(
                "The prior case is under seal: neither its nature-of-suit (FCA qui tam vs. "
                "another cause of action) nor — if it is a qui tam — its claim SCOPE "
                "(whether it covers these counts under the first-to-file 'same material "
                "elements' test) can be established from public dockets."
            ),
            provisional_verdict_if_yes=f"{provisional_verdict} (bar applies — first-to-file / public-disclosure)",
            provisional_verdict_if_no=(
                "Bar does not apply (different scope / not a qui tam / relator is an original "
                "source) — resume the disposition on the merits."
            ),
            what_would_resolve_it=(
                "The nature-of-suit (NOS) + the complaint/scope of the cited docket (e.g. via "
                "PACER), or counsel's first-to-file / original-source assessment, confirming "
                "whether it bars these claims."
            ),
            raised_at=datetime.now(timezone.utc),
        )
    return None


def detect_iteration1_jurisdiction_screen(
    *,
    case_id: str,
    first_to_file_concerns: list[str] | None,
    public_disclosure_concerns: list[str] | None,
    subject: str,
    docket_search=None,
) -> ReviewRequest | None:
    """Iter-1 blocking jurisdiction screen (top fix #1, failure_mode_taxonomy_2026-07.md).

    ``counsel_build``'s ``first_to_file_concerns`` / ``public_disclosure_concerns``
    fields are advisory and were never checked against a live docket — two full
    case runs (Spruce/Warren, Birch/Dogwood) burned 3+ iterations and multiple
    adversarial rounds before a PACER-discoverable prior qui tam was found. This
    runs a live CourtListener docket search (the same helper
    ``scripts/ring_enrichment.py`` uses) as soon as counsel_build raises a concern,
    BEFORE iteration 2 starts, and pauses for a human on a confirmed PENDING
    docket instead of burning further iterations on a bar that was discoverable
    at intake.

    ``docket_search`` is injectable (tests pass a stub); defaults to
    ``scripts.ring_enrichment._courtlistener``.

    Returns ``None`` when there is nothing to screen (no concerns raised) or no
    pending docket is found — the loop proceeds normally either way.
    """
    concerns = list(first_to_file_concerns or []) + list(public_disclosure_concerns or [])
    if not concerns:
        return None

    if docket_search is None:
        import sys as _sys
        from pathlib import Path as _Path
        _scripts = str(_Path(__file__).resolve().parents[3] / "scripts")
        if _scripts not in _sys.path:
            _sys.path.insert(0, _scripts)
        from ring_enrichment import _courtlistener as docket_search  # noqa: PLC0415

    query = f'"{subject}"' if subject else " ".join(concerns[:1])
    hits = docket_search(query) or []
    # A hit is a CONFIRMED PENDING prior qui tam only if it has no termination
    # date (still open) — a terminated docket cannot bar under first-to-file.
    pending = [h for h in hits if not h.get("terminated")]
    if not pending:
        return None

    hit = pending[0]
    fact = (f"CourtListener docket {hit.get('docket') or '?'} ({hit.get('case') or '?'}, "
            f"{hit.get('court') or '?'}, filed {hit.get('filed') or '?'}) is open "
            f"(no termination date) and facially concerns the same subject.")
    return ReviewRequest(
        case_id=case_id,
        checkpoint_type="jurisdiction_bar_unverifiable",
        question=(
            "A live CourtListener search at iteration 1 found an OPEN docket that may be a "
            f"prior qui tam on the same facts: {fact} Counsel raised this concern in "
            f"first_to_file_concerns/public_disclosure_concerns: {'; '.join(concerns[:3])}. "
            "Does this docket bar the case under first-to-file / public-disclosure "
            "(31 USC 3730(b)(5)/(e)(4)), or is it a different matter / non-FCA cause of action?"
        ),
        unverifiable_premise=fact,
        cited_evidence_id=str(hit.get("docket") or ""),
        why_unverifiable=(
            "The docket's nature-of-suit and scope cannot be confirmed from a search snippet "
            "alone (and if it is itself a sealed qui tam, its content is unreadable)."
        ),
        provisional_verdict_if_yes="COUNSEL_ABANDONED (barred — first-to-file / public-disclosure)",
        provisional_verdict_if_no="Bar does not apply — resume the build loop on the merits.",
        what_would_resolve_it=(
            "The docket's nature-of-suit (NOS) code and complaint/scope (e.g. via PACER), "
            "confirming whether it covers these claims."
        ),
        raised_at=datetime.now(timezone.utc),
    )


def detect_foreclosure_checkpoint(
    *,
    case_id: str,
    foreclosure_ledger: dict | None,
    provisional_verdict: str = "NOT_ACTIONABLE",
) -> ReviewRequest | None:
    """Pause when a foreclosure DECLINE (``fraud_is_more_plausible_inference is
    False``) rests on a LIVE innocent explanation that is closable only by a
    discrete fact a human could supply.

    Selectivity gate: only LIVE rows whose ``closure_owner`` is ``discovery:<item>``
    or ``cannot_close_pre_filing`` qualify — those hinge on an off-record fact
    (an FTE count, "is this a legitimate place of service?"). A ``closed_on_record``
    row would already be FORECLOSED, and a genuine merits gap does not pause.
    """
    if not foreclosure_ledger:
        return None
    if foreclosure_ledger.get("fraud_is_more_plausible_inference") is not False:
        return None

    for exp in foreclosure_ledger.get("explanations", []) or []:
        if str(exp.get("status", "")).upper() != "LIVE":
            continue
        owner = str(exp.get("closure_owner", "")).strip().lower()
        if not (owner.startswith("discovery:") or owner == "cannot_close_pre_filing"):
            continue
        steelman = (exp.get("steelman") or exp.get("explanation") or "").strip()
        residual = (exp.get("residual") or "").strip()
        return ReviewRequest(
            case_id=case_id,
            checkpoint_type="foreclosure_live_explanation",
            question=(
                "The loop is about to DECLINE because a LIVE innocent explanation may "
                f'account for the pattern: "{steelman}". It cannot be closed from the '
                f"record (closure_owner={exp.get('closure_owner', '')}). "
                "Can you confirm it (the pattern is benign → decline stands) or refute "
                "it (the explanation is foreclosed → the case proceeds)? "
                + (f"What remains unexplained: {residual}" if residual else "")
            ).strip(),
            unverifiable_premise=steelman or "A LIVE innocent explanation accounts for the pattern.",
            cited_evidence_id=str(exp.get("id", "")),
            why_unverifiable=(
                f"The explanation is LIVE with closure_owner={exp.get('closure_owner', '')} — "
                "it hinges on a fact not on the record"
                + (f" (e.g. {residual})" if residual else "")
                + "."
            ),
            provisional_verdict_if_yes=f"{provisional_verdict} (innocent explanation holds — decline stands).",
            provisional_verdict_if_no="Explanation foreclosed — resume; fraud may be the more-plausible inference.",
            what_would_resolve_it=(
                residual
                or "The discrete fact named by closure_owner that would foreclose or confirm the explanation."
            ),
            raised_at=datetime.now(timezone.utc),
        )
    return None


class CheckpointInvariantError(RuntimeError):
    """Raised when FinalVerdict.NEEDS_HUMAN_CHECKPOINT is set without a
    ReviewRequest to answer — the CASE-6A2B3445 gap (a case ended
    NEEDS_HUMAN/case_ready with `review_request` absent and an empty
    verdict_rationale — the pre-Guardrail-4 failure this whole module exists
    to close). A checkpoint verdict with no question for the human to answer
    is not resumable and hides the pause reason."""


def assert_checkpoint_invariant(final_verdict, review_request) -> None:
    """Enforce: FinalVerdict.NEEDS_HUMAN_CHECKPOINT => review_request is not None.

    Every current call site (phase_a.py's iter-1 jurisdiction screen, phase_b.py's
    disposition-time jurisdiction checkpoint, phase_c.py's decline-time
    checkpoint) already sets review_request immediately before final_verdict —
    this is a mechanical backstop against a FUTURE call site (or an edge case)
    silently violating that pairing, not evidence any current site is broken.
    Called from state.write_summary so every persisted state is checked, not
    just the ones this module itself raises.

    ``final_verdict`` may be the enum, its ``.value`` string, or plain
    ``"NEEDS_HUMAN_CHECKPOINT"`` (CaseState may hand either through)."""
    value = getattr(final_verdict, "value", final_verdict)
    if value == "NEEDS_HUMAN_CHECKPOINT" and review_request is None:
        raise CheckpointInvariantError(
            "final_verdict=NEEDS_HUMAN_CHECKPOINT but review_request is None — "
            "every checkpoint verdict must carry a ReviewRequest for a human to "
            "answer (see docs/CANONICAL_LOOP_GUARDRAILS.md Guardrail-4)."
        )
