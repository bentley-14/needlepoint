"""Deterministic tier-inflation linter (R3).

The documented tier-inflation failure mode: an agent emits ``confirmed_fact``
whose `fact` is actually the agent's own inference — detectable mechanically
because the reasoning leaks as modal/inferential language ("would have",
"must have", "no later than") or as a government-knowledge EVENT claim no
source literally states. Four prompts used to carry prose reminders about
this; prose reminders are exactly the kind of instruction that gets dropped
under load. This module flags the pattern on every evidence item, every
time, and the flags ride into the gating prompts (first_chair audit Q4 keeps
the semantic judgment; this catches the mechanical class with 100% recall).

Why it matters: an over-tiered inference becomes the relator's affirmative
ADMISSION — defense weaponizes it under Escobar's continued-payment prong,
the public-disclosure bar, and scienter-defeats arguments (see the Juniper
adv-r2 episode referenced in counsel_first_chair_review.md).
"""
from __future__ import annotations

import re

# Modal/inferential language that means the EVENT is the agent's reasoning,
# not a literal statement in the cited source (investigator.md § "Literal vs
# inferential").
_MODAL_RE = re.compile(
    r"\b(would have|must have|had to have|structurally would|"
    r"by no later than|no later than)\b",
    re.IGNORECASE,
)

# Government-knowledge/detection EVENT claims — confirmed_fact tier requires
# the cited source to literally document the event, which is rarely true.
_GOV_EVENT_RE = re.compile(
    r"\b(CMS|DOJ|OIG|HHS|FPS|the government|government)\b"
    r"[^.;]{0,80}?"
    r"\b(knew|was aware|detected|identified|flagged|audited|investigated)\b",
    re.IGNORECASE,
)


def lint_evidence_tiers(evidence: list[dict]) -> list[dict]:
    """Flag ``confirmed_fact`` items whose fact text carries inferential or
    government-event language. Returns one flag dict per hit."""
    flags: list[dict] = []
    for ev in evidence or []:
        if (ev.get("tier") or "").strip() != "confirmed_fact":
            continue
        fact = ev.get("fact") or ""
        m = _MODAL_RE.search(fact)
        if m:
            flags.append({
                "evidence_id": ev.get("id"),
                "rule": "modal_inferential_language",
                "matched": m.group(0),
                "fact_excerpt": fact[:200],
                "required_action": (
                    "Re-tier to legal_hypothesis/statistical_signal unless the "
                    "cited source LITERALLY states the event."
                ),
            })
            continue
        m = _GOV_EVENT_RE.search(fact)
        if m:
            flags.append({
                "evidence_id": ev.get("id"),
                "rule": "government_event_claim",
                "matched": m.group(0),
                "fact_excerpt": fact[:200],
                "required_action": (
                    "confirmed_fact for a government knowledge/detection EVENT "
                    "requires a source documenting the event itself (not just a "
                    "system with the capacity to detect). Re-tier unless literal."
                ),
            })
    return flags


# ── Code-identity lint (failure_mode_taxonomy_2026-07.md #4) ────────────────
#
# 4 counts were killed LATE by the relator's own code-identity errors, all
# knowable at pleading time: CALIBRATION-C4E2135E benchmarked 64612
# (blepharospasm/facial-paralysis) when the target's dominant code was 64615
# (chronic migraine) — a different, in-label code; CALIBRATION-E8A553FE
# claimed J9299=nivolumab identity in error ("the second instance of the same
# recall error in the build"); CALIBRATION-01108299 mixed up J3032 and J7336.
# This mechanically extracts "<CODE> is/=/identified as <label>"-shaped
# identity claims from evidence fact text and verifies them against
# scripts/hcpcs_lookup.py's ground truth — the same reference the investigator
# is already told to resolve codes against, but never mechanically checked.

# "64615 is chronic migraine", "J9299 = nivolumab", "code 64612 (facial
# paralysis)" — captures a HCPCS/CPT code immediately followed by an
# identity assertion and the claimed label text up to the next clause break.
_CODE_IDENTITY_RE = re.compile(
    r"\b(?P<code>[A-Z]?\d{4,5}|[A-Z]\d{4})\b"
    r"\s*(?:is|=|:|—|—|,?\s+coded as|,?\s+identified as)\s+"
    r"(?:the\s+code\s+for\s+)?"
    r"(?P<label>[a-zA-Z][a-zA-Z0-9 /\-]{2,60})",
    re.IGNORECASE,
)

# Stopwords that don't carry identity-verifiable content (skip flags where the
# "label" text is generic, not a drug/procedure name).
_LABEL_STOPWORDS = {"correctly coded", "the same", "not covered", "billed"}


def _label_overlaps_descriptor(label: str, descriptor: str) -> bool:
    """True if any distinctive (>=4-char) word in `label` appears in the
    resolved descriptor — a cheap, conservative overlap check. Absence of
    overlap does not by itself PROVE a mismatch (descriptors are terse CMS
    prose), so this only flags for downstream (human/agent) review, exactly
    like lint_evidence_tiers — it never asserts a verdict on its own."""
    label_words = {w.lower() for w in re.findall(r"[a-zA-Z]{4,}", label)}
    if not label_words:
        return True  # nothing distinctive to check -> don't flag
    desc_words = {w.lower() for w in re.findall(r"[a-zA-Z]{4,}", descriptor)}
    return bool(label_words & desc_words)


def lint_code_identity_claims(evidence: list[dict], resolve_code=None) -> list[dict]:
    """Flag evidence whose fact text asserts a HCPCS/CPT code's identity
    ("64615 is chronic migraine", "J9299 = nivolumab") when scripts/
    hcpcs_lookup.py's ground-truth descriptor for that code shares no
    distinctive vocabulary with the claim — the mechanical class behind 4
    late-killed counts (C4E2135E 64612/64615, E8A553FE J9299, 01108299
    J3032/J7336; failure_mode_taxonomy_2026-07.md #4).

    A flag is a DOWNGRADE SIGNAL, not an automatic re-tier — same contract as
    lint_evidence_tiers: it rides into the gating prompt so the agent/human
    confirms or corrects the identity claim before it ships.

    ``resolve_code`` is injectable (tests pass a stub); defaults to
    ``scripts.hcpcs_lookup.resolve_code``."""
    if resolve_code is None:
        try:
            import sys as _sys
            from pathlib import Path as _Path
            _scripts = str(_Path(__file__).resolve().parents[3] / "scripts")
            if _scripts not in _sys.path:
                _sys.path.insert(0, _scripts)
            from hcpcs_lookup import resolve_code  # noqa: PLC0415
        except Exception:  # pragma: no cover — defensive; lint degrades to a no-op
            return []

    flags: list[dict] = []
    for ev in evidence or []:
        fact = ev.get("fact") or ""
        for m in _CODE_IDENTITY_RE.finditer(fact):
            code, label = m.group("code").upper(), m.group("label").strip()
            if label.lower() in _LABEL_STOPWORDS:
                continue
            try:
                resolved = resolve_code(code)
            except Exception:
                continue  # resolver failure (network/parquet) — skip, don't crash the lint
            if resolved is None:
                continue  # unrecognized code — not this lint's job (no ground truth to check against)
            descriptor = str(resolved.get("descriptor") or "")
            # Check the CMS descriptor AND the canonical MPFS-RVU short descriptor —
            # e.g. 64615's PUF descriptor never says "migraine" but its canonical
            # short descriptor ("Chemodenerv musc migraine") does.
            full_ref_text = f"{descriptor} {resolved.get('canonical_short_descriptor') or ''}"
            if _label_overlaps_descriptor(label, full_ref_text):
                continue
            flags.append({
                "evidence_id": ev.get("id"),
                "rule": "code_identity_mismatch",
                "code": code,
                "claimed_label": label,
                "resolved_descriptor": descriptor,
                "fact_excerpt": fact[:200],
                "required_action": (
                    f"Verify {code} against hcpcs_lookup.py before relying on this claim: "
                    f"ground truth is \"{descriptor}\", which shares no distinctive wording "
                    f"with the claimed identity \"{label}\". Downgrade this evidence item's "
                    f"tier (confirmed_fact -> legal_hypothesis/statistical_signal) unless the "
                    f"identity is independently confirmed."
                ),
            })
    return flags


# ── Unit-vs-claim semantics lint (the Alder 10,724 episode) ──────────────────
#
# CALIBRATION-6DA0BBC3 (Alder) pled "10,724 claims averaging $0.80–$1.23 per
# claim". A sub-$5 average payment per "claim" is the signature of per-UNIT
# J-code billing (CMS PUF line items count billed UNITS — a J-code dose unit
# pays cents, an actual CLAIM does not): the N is almost certainly units, not
# claims. The conflation both muddies falsity (what exactly was falsely
# presented?) and inflates per-claim civil-penalty exposure ~100x (31 USC 3729
# penalties attach per CLAIM). Same family as the 99489-minutes and svcs/bene
# episodes: a unit-semantics error nothing mechanically linted. This lint is
# ADDITIVE — a flag is a review signal riding the tier_lint_flags channel into
# the gating prompts; it never re-tiers, discounts, or kills anything.

# "10,724 claims" / "10,724 drug-classified suppressed claims" — a number
# (with or without thousands commas) followed by up to 3 modifier words and
# the word "claims".
_CLAIM_COUNT_RE = re.compile(
    r"\b(?P<n>\d{1,3}(?:,\d{3})+|\d{2,})\s+(?:[\w-]+\s+){0,3}?claims?\b",
    re.IGNORECASE,
)

# Explicit average-payment-per-claim forms (never inferred from totals — a
# wrong implied average would make the lint itself the numeric error):
#   "averaging $0.87 per claim" / "averaging $0.80–$1.23 per claim"
#   "$0.87 per claim" / "average payment of $0.87 per claim"
_AVG_PER_CLAIM_RE = re.compile(
    r"(?:averag\w+[^.;$]{0,40}?)?"
    r"\$(?P<a>[\d.,]+)"
    r"(?:\s*[-–—]\s*\$?(?P<b>[\d.,]+))?"
    r"\s*(?:per\s+claim|/\s*claim)",
    re.IGNORECASE,
)

# Conservative per-unit-dosed HCPCS family: J-codes only (J0000-J9999 drug
# codes are dosed per unit — the canonical units≠claims surface).
_J_CODE_RE = re.compile(r"\bJ\d{4}\b")

# Below this, an "average payment per claim" is not credible for a real
# CLAIM and indicates per-unit line items. $5 (not $1) so multi-unit
# sub-dollar dose codes billed a few units per line still trip it.
_UNIT_AVG_THRESHOLD_USD = 5.0


def _parse_usd(s: str) -> float | None:
    try:
        return float(s.replace(",", ""))
    except (TypeError, ValueError):
        return None


def unit_claim_flag_text(n: str) -> str:
    """The exact required_action wording (single-sourced with the numeric
    audit's UNVERIFIABLE_UNIT_SEMANTICS detail — see audit_case_numerics)."""
    return (
        f"possible units≠claims (per-unit J-code billing): {n} may be units, "
        f"not claims — resolve unit semantics before damages/penalty math "
        f"(cf. 99489-minutes + svcs/bene episodes)"
    )


def lint_unit_claim_semantics(evidence: list[dict]) -> list[dict]:
    """Flag evidence whose fact text asserts a count of "claims" where either
    (a) an EXPLICIT average payment per claim is under $5 (per-unit J-code
    line items masquerading as claims), or (b) a per-unit-dosed J-code is
    named alongside the claims count. One flag per evidence item.

    A flag is a REVIEW SIGNAL, not an automatic re-tier/discount — same
    contract as lint_evidence_tiers / lint_code_identity_claims: it rides
    into the gating prompts so the agent/human resolves the unit semantics
    before damages or per-claim penalty math ships."""
    flags: list[dict] = []
    for ev in evidence or []:
        fact = ev.get("fact") or ""
        m = _CLAIM_COUNT_RE.search(fact)
        if not m:
            continue
        n_str = m.group("n")

        avg = None
        avg_m = _AVG_PER_CLAIM_RE.search(fact)
        if avg_m:
            lo = _parse_usd(avg_m.group("a"))
            hi = _parse_usd(avg_m.group("b")) if avg_m.group("b") else None
            candidates = [v for v in (lo, hi) if v is not None]
            avg = max(candidates) if candidates else None
        sub_threshold = avg is not None and avg < _UNIT_AVG_THRESHOLD_USD

        j_m = _J_CODE_RE.search(fact)

        if not (sub_threshold or j_m):
            continue
        flags.append({
            "evidence_id": ev.get("id"),
            "rule": "unit_claim_semantics",
            "claim_count": n_str,
            "avg_per_claim_usd": avg,
            "jcode": j_m.group(0) if j_m else None,
            "fact_excerpt": fact[:200],
            "required_action": unit_claim_flag_text(n_str),
        })
    return flags


def format_unit_claim_flags(flags: list[dict]) -> str:
    """Markdown block for prompt injection. Empty string when clean."""
    if not flags:
        return ""
    lines = [
        "",
        "## Unit-vs-claim semantics lint (mechanical — resolve whether each N "
        "counts CLAIMS or billed UNITS before relying on it)",
        "",
        "| evidence id | asserted claims | avg $/claim | J-code | excerpt |",
        "|---|---|---|---|---|",
    ]
    for f in flags:
        excerpt = (f.get("fact_excerpt") or "").replace("|", "\\|")[:100]
        avg = f.get("avg_per_claim_usd")
        lines.append(
            f"| {f.get('evidence_id')} | {f.get('claim_count')} | "
            f"{'—' if avg is None else f'${avg:,.2f}'} | "
            f"{f.get('jcode') or '—'} | {excerpt} |")
    lines.append("")
    lines.append(
        "A sub-$5 average payment per \"claim\" is the per-UNIT J-code billing "
        "signature: N is likely units, not claims. The conflation muddies "
        "falsity and inflates per-claim civil-penalty exposure ~100x. Resolve "
        "unit semantics before any damages/penalty math ships (cf. the "
        "99489-minutes and svcs/bene unit-semantics episodes)."
    )
    return "\n".join(lines)


def format_code_identity_flags(flags: list[dict]) -> str:
    """Markdown block for prompt injection. Empty string when clean."""
    if not flags:
        return ""
    lines = [
        "",
        "## Code-identity lint (mechanical — verify against hcpcs_lookup.py before relying "
        "on any flagged claim)",
        "",
        "| evidence id | code | claimed | hcpcs_lookup ground truth |",
        "|---|---|---|---|",
    ]
    for f in flags:
        desc = (f.get("resolved_descriptor") or "").replace("|", "\\|")[:100]
        claimed = (f.get("claimed_label") or "").replace("|", "\\|")[:60]
        lines.append(f"| {f.get('evidence_id')} | {f.get('code')} | {claimed} | {desc} |")
    lines.append("")
    lines.append(
        "A code-identity error becomes the relator's own recall mistake — the class that "
        "killed 4 counts late in production (benchmarking the wrong code, misidentifying a "
        "drug code). Confirm or correct every flag before the claim ships."
    )
    return "\n".join(lines)


def format_tier_flags(flags: list[dict]) -> str:
    """Markdown block for prompt injection. Empty string when clean."""
    if not flags:
        return ""
    lines = [
        "",
        "## Tier-inflation lint (mechanical — every flag below needs a re-tier "
        "or an explicit literal-source justification)",
        "",
        "| evidence id | rule | matched | excerpt |",
        "|---|---|---|---|",
    ]
    for f in flags:
        excerpt = (f.get("fact_excerpt") or "").replace("|", "\\|")[:120]
        lines.append(f"| {f.get('evidence_id')} | {f.get('rule')} | "
                     f"{f.get('matched')} | {excerpt} |")
    lines.append("")
    lines.append(
        "An over-tiered inference is the relator's affirmative ADMISSION — "
        "defense will weaponize it (Escobar continued-payment, public-disclosure "
        "bar, scienter-defeats). Treat every flag as a defect until resolved."
    )
    return "\n".join(lines)
