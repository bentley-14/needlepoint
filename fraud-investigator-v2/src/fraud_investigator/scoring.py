"""Case-strength signals + the filing decision's tuning surface.

WHAT DECIDES A CASE (post-refactor): the file/decline verdict is set by
`verdict.compute_verdict` (the legal-predicate filing test) — NOT by the additive
strength score in this module. That score/bucket is now REPORTING-ONLY telemetry
(a feature-derived quality proxy, validated on the 10 dossiers at Spearman 0.903 /
within-1-bucket 100% — see scripts/calibration_audit.py). What this module still
feeds the live decision: the EVF, confirmed-damages, and foreclosure SIGNALS that
compute_verdict reads, plus the production-only early-exit floor
(_enforce_strength_floor, skipped in calibration).

TUNING SURFACE — every knob that can move a filing outcome (the one question:
"what number changes whether we FILE?"):

  LIVE · flips FILE <-> DECLINE  (production AND calibration)
    MIN_CONFIRMED_DAMAGES_USD   (in verdict.py — predicate P5; the only $ floor)
    · P2 foreclosure gate is the BOOLEAN fraud_is_more_plausible_inference (no constant)

  LIVE · sets the FILE sub-tier (SETTLEMENT_READY vs FILE_WITH_DISCOVERY_DEPS),
         and drives the production-only abandon/downgrade floor (calibration no-op)
    EVF_FILE_WITH_DEPS_BELOW = 0.40 · EVF_ABANDON_BELOW = 0.20

  LIVE · production-only early-exit/disposition floor (calibration no-op);
         the STRONG/MODERATE/WEAK bucket label is itself reporting-only
    THRESH_STRONG = 0.55 · THRESH_MODERATE = 0.35 · THRESH_WEAK = 0.18

  REPORTING-ONLY · telemetry/logs; NO effect on file/decline
    FORECLOSURE_LIVE_CAP = 0.5   (flags a strong unrebutted innocent explanation for
                                  the human; the verdict gate is the boolean above)
    PRECEDENT_CAP_FLOOR  = 0.70  (multiplies the reporting-only strength score)
    the additive WEIGHTS / compute_strength_score machinery
"""
from __future__ import annotations

import math
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .schemas import CounselBuildOutput, CounselRebuttalOutput, EvidenceItem


# ─── Detector regexes (shared with calibration_audit.py) ────────────────────

_DISTRICT_RE = re.compile(
    r"\b(N\.D\.|S\.D\.|E\.D\.|W\.D\.|M\.D\.|C\.D\.|D\.N\.J\.|D\.D\.C\.|D\.Mass\.|D\.Md\.)\s?"
    r"(Ga\.|Fla\.|Tex\.|Cal\.|N\.Y\.|Ill\.|La\.|Va\.|Pa\.|Mo\.|N\.J\.)?",
    re.IGNORECASE,
)
_USAO_RE = re.compile(r"usao-[a-z]{2,4}", re.IGNORECASE)
_SAME_DISTRICT_RE = re.compile(
    r"(same district|same federal district|same court district|VENUE_ENFORCEMENT_LANDSCAPE|"
    r"same\s+(USAO|US Attorney['s]*\s+Office)|direct (?:precedent|N\.D\.|S\.D\.|E\.D\.))",
    re.IGNORECASE,
)
# Fallback regex for cases scored before `within_practice_control_established`
# existed. Vocabulary is intentionally broad — the agent legitimately says
# "within-suite/within-building control" for single-office practices, and
# "co-located peer control" for shared-address comparisons.
_WITHIN_PRACTICE_RE = re.compile(
    r"(within[-\s](practice|suite|building|office|address)\s+controls?|"
    r"mid[-\s]level (control|differential)|"
    r"co[-\s]?located (peer|provider|colleague|comparator)|"
    r"peer[-\s]at[-\s](same[-\s])?address|"
    r"same address (.{0,80})(0|zero|absent)|practice roster|"
    r"colleagues all properly designated|"
    r"WITHIN-(PRACTICE|SUITE|BUILDING|OFFICE)\s+CONTROLS?)",
    re.IGNORECASE,
)
_REGULATORY_RE = re.compile(
    r"(LEIE|state[-\s]board|board of healing|TMB|OIG audit|DOJ press|prior enforcement|"
    r"CMS revocation|denial-rate|prior qui tam|Texas Medical Board|Composite Medical Board|"
    r"IDFPR|MO Board|"
    r"S&I List|S&I[- ](?:list|exclusion)|Suspended and Ineligible|"
    r"Welfare and Institutions Code 14043|Medi-Cal exclusion|CA exclusion)",
    re.IGNORECASE,
)
# Cross-program audit detector — investigator must populate MEDICAID_CROSS_PROGRAM:
# section if Step 6.5 audit produced a positive cross-program finding (BOTH or
# selective-payer MEDICARE-ONLY pattern). The literal token is checked verbatim.
_MEDICAID_CROSS_PROGRAM_RE = re.compile(
    r"MEDICAID_CROSS_PROGRAM[^a-zA-Z]",
    re.IGNORECASE,
)
# Cross-program POSITIVE patterns — only credit the score if the section
# describes an actual systemic-or-selective finding (not "no Medicaid records").
_CROSS_PROGRAM_POSITIVE_RE = re.compile(
    r"(systemic|both[- ]?payer|cross[- ]?program|MEDICAID-ONLY|MEDICARE-ONLY|"
    r"selective[- ]?payer|knowing[ ]?(?:falsity|payer|reimbursement))",
    re.IGNORECASE,
)


# ─── Score formula ─────────────────────────────────────────────────────────


# Bucket boundaries (validated by calibration audit)
THRESH_STRONG: float = 0.55
THRESH_MODERATE: float = 0.35
THRESH_WEAK: float = 0.18

# Component weights — each component is 0.0-1.0; weights sum to ~1.0 for
# positive components; discount_tax is subtractive (weight 1.0 × negative value)
WEIGHTS: dict[str, float] = {
    "factual_density":            0.10,   # n_confirmed_facts / 10 capped
    "n_counts":                   0.05,   # n_counts / 3 capped
    "regulatory":                 0.10,   # n_regulatory_predicates / 3 capped
    "venue_precedent":            0.05,   # any-district mention (weak signal)
    "same_district":              0.20,   # same-district precedent (scienter anchor)
    "within_practice":            0.20,   # within-practice controls (anti-outlier)
    "survival_residual":          0.15,   # max(0, (survival - 0.4) / 0.5)
    "damages":                    0.10,   # log10($X / $500K) / log10(20)
    "medicaid_cross_program":     0.05,   # systemic-vs-selective audit (Step 6.5)
    "discount_tax":               1.00,   # -min(0.30, discount_pct/100)
}


def compute_components(
    *,
    n_confirmed_facts: int,
    n_counts: int,
    n_regulatory_predicates: int,
    has_venue_precedent: bool,
    has_same_district_precedent: bool,
    has_within_practice_controls: bool,
    survival_likelihood: float,
    damages_usd: float,
    cumulative_discount_pct: float,
    has_medicaid_cross_program_match: bool = False,
) -> dict[str, float]:
    """Compute the per-component values (each 0.0-1.0; discount_tax is negative).

    Inputs are extracted from CounselBuildOutput + CounselRebuttalOutput +
    LegalAdversarialResponse + accumulated investigator notes via the helper
    `extract_features_from_state()`.
    """
    components: dict[str, float] = {}

    components["factual_density"] = min(1.0, n_confirmed_facts / 10.0)
    components["n_counts"] = min(1.0, n_counts / 3.0)
    components["regulatory"] = min(1.0, n_regulatory_predicates / 3.0)
    components["venue_precedent"] = 1.0 if has_venue_precedent else 0.0
    components["same_district"] = 1.0 if has_same_district_precedent else 0.0
    components["within_practice"] = 1.0 if has_within_practice_controls else 0.0

    components["survival_residual"] = max(0.0, min(1.0, (survival_likelihood - 0.4) / 0.5))

    if damages_usd > 500_000:
        components["damages"] = min(1.0, math.log10(damages_usd / 500_000) / math.log10(20))
    else:
        components["damages"] = 0.0

    components["medicaid_cross_program"] = 1.0 if has_medicaid_cross_program_match else 0.0
    components["discount_tax"] = -min(0.30, cumulative_discount_pct / 100.0)
    return components


def score_from_components(components: dict[str, float]) -> float:
    """Apply weights to the per-component dict; clamp to [0.0, 1.0]."""
    score = sum(WEIGHTS[k] * components[k] for k in components)
    return max(0.0, min(1.0, score))


def compute_strength_score(
    *,
    n_confirmed_facts: int,
    n_counts: int,
    n_regulatory_predicates: int,
    has_venue_precedent: bool,
    has_same_district_precedent: bool,
    has_within_practice_controls: bool,
    survival_likelihood: float,
    damages_usd: float,
    cumulative_discount_pct: float = 0.0,
    has_medicaid_cross_program_match: bool = False,
) -> tuple[float, dict[str, float]]:
    """Compute strength score (0.0-1.0) + component breakdown.

    Single source of truth for the case-strength signal — used by the
    orchestrator's `_enforce_strength_floor()` and by the calibration audit.
    """
    components = compute_components(
        n_confirmed_facts=n_confirmed_facts,
        n_counts=n_counts,
        n_regulatory_predicates=n_regulatory_predicates,
        has_venue_precedent=has_venue_precedent,
        has_same_district_precedent=has_same_district_precedent,
        has_within_practice_controls=has_within_practice_controls,
        survival_likelihood=survival_likelihood,
        damages_usd=damages_usd,
        cumulative_discount_pct=cumulative_discount_pct,
        has_medicaid_cross_program_match=has_medicaid_cross_program_match,
    )
    return score_from_components(components), components


def bucket(score: float) -> str:
    """Bucket a strength score into STRONG / MODERATE / WEAK / ABANDON."""
    if score >= THRESH_STRONG:
        return "STRONG"
    if score >= THRESH_MODERATE:
        return "MODERATE"
    if score >= THRESH_WEAK:
        return "WEAK"
    return "ABANDON"


def expected_value_factor(survival_likelihood: float, cumulative_discount_pct: float) -> float:
    """Upgrade 2 — pre-recovery EV factor.

    EVF = survival × (1 - discount/100)

    Interpretation: probability of survival × fraction of damages retained
    after defense discount. < 0.20 = not worth seal-period investment;
    0.20-0.40 = file with discovery deps only; ≥ 0.40 = clean filing.
    """
    return max(0.0, survival_likelihood * (1.0 - cumulative_discount_pct / 100.0))


# EVF gates (Upgrade 2 thresholds)
EVF_ABANDON_BELOW: float = 0.20
EVF_FILE_WITH_DEPS_BELOW: float = 0.40


def evf_recommendation(evf: float) -> str:
    """Map an EVF to a recommended disposition.

    Returns one of:
      "FILE"             — EVF ≥ 0.40
      "FILE_WITH_DEPS"   — 0.20 ≤ EVF < 0.40
      "ABANDON"          — EVF < 0.20
    """
    if evf >= EVF_FILE_WITH_DEPS_BELOW:
        return "FILE"
    if evf >= EVF_ABANDON_BELOW:
        return "FILE_WITH_DEPS"
    return "ABANDON"


# ── Guardrail 3: Innocent-Explanation Foreclosure cap ───────────────────────
# A LIVE innocent explanation at/above this plausibility blocks a clean FILE and
# caps the strength bucket. 0.5 = "as likely as not the pattern has a non-fraud
# account that the record does not foreclose" — you cannot cleanly file on a
# coin-flip innocent explanation; it becomes a discovery dependency.
FORECLOSURE_LIVE_CAP: float = 0.5

# Hard-DECLINE tier (distinct from the soft cap above). When the foreclosure
# check concludes fraud is NOT the more-plausible inference, the complaint fails
# the Twombly/Iqbal plausibility standard outright: you cannot file when the
# innocent account is at least as plausible as fraud. This is a MERITS gate, not
# an economics floor, so (unlike the EVF/worthwhile gates) it is NOT skipped in
# calibration.
#
# We deliberately gate on the agent's holistic BOOLEAN conclusion, NOT on a raw
# residual threshold: validation against real ledgers showed the residual does
# NOT separate true positives from over-filed controls — a genuine fraud case
# (Willow, TP) had a LIVE innocent explanation at residual 0.85, essentially the
# same as the over-filed control Baobab (0.95). Only the boolean separates them
# (both TPs True; Baobab False). A residual backstop would kill recall.


def foreclosure_residual(ledger: dict | None) -> float:
    """Deterministically re-derive the max plausibility among LIVE innocent
    explanations (0.0 if the ledger is empty / has no LIVE rows).

    Reads the explanation ROWS, not the LLM's self-reported max_live_plausibility
    summary field — so a model that under-reports its own residual cannot dodge
    the cap. Tolerant of partial dicts (missing fields → treated as 0 / non-LIVE)."""
    if not ledger:
        return 0.0
    vals = [float(e.get("plausibility_unrebutted") or 0.0)
            for e in (ledger.get("explanations") or [])
            if e.get("status") == "LIVE"]
    return max(vals, default=0.0)


def foreclosure_declines(ledger: dict | None) -> bool:
    """True if the foreclosure ledger FORECLOSES a filing (hard DECLINE tier).

    Triggers strictly on the foreclosure agent's holistic conclusion that fraud
    is NOT the more-plausible inference (``fraud_is_more_plausible_inference is
    False``) — the Twombly/Iqbal plausibility test. We do NOT also gate on a raw
    residual threshold: it does not separate true positives from over-filed
    controls (see the note on FORECLOSURE_DECLINE removal above). Empty/absent
    ledger, or a missing/True boolean → False (no basis to foreclose)."""
    if not ledger:
        return False
    return ledger.get("fraud_is_more_plausible_inference") is False


# ── Guardrail 5: per-count precedent-anchor coverage (soft strength cap) ─────
# Every pled count should cite a precedent where a substantially-similar count
# /theory was pled and survived (or controlling doctrine). A count whose anchor
# can't be matched to a VERIFIED precedent is still pleadable but doesn't count
# as confirmed — it down-weights the case strength. This is a soft multiplicative
# cap (max ~30% haircut at zero coverage), NOT a verdict flip; the existing
# strength→verdict gate handles any downstream effect.
PRECEDENT_CAP_FLOOR: float = 0.70

_CITE_STOP = {"united", "states", "the", "and", "rel", "ex", "inc", "llc", "corp",
              "company", "systems", "system", "health", "servs", "services", "v",
              "vs", "of", "co", "et", "al"}


def _cite_signature(citation: str) -> set[str]:
    """Distinctive party/case tokens of a citation (len≥4, not a generic cite
    word, not a pure number) — used to match a count's precedent_anchor against
    the verified precedent set without trusting an LLM 'verified' flag."""
    import re as _re
    toks = _re.findall(r"[a-z0-9]+", (citation or "").lower())
    return {t for t in toks if len(t) >= 4 and t not in _CITE_STOP and not t.isdigit()}


def precedent_coverage(counts, verified_names) -> dict:
    """Fraction of pled counts whose precedent_anchor matches a VERIFIED precedent
    (verified_precedents ∪ doctrine-corpus controlling cases). Accepts counts as
    dicts or LegalCount objects. No counts ⇒ coverage 1.0 (nothing to penalize)."""
    counts = counts or []
    n = len(counts)
    if n == 0:
        return {"n_counts": 0, "n_covered": 0, "coverage": 1.0, "n_adverse_unconfronted": 0}
    sigs = [s for s in (_cite_signature(v) for v in (verified_names or [])) if s]

    def _get(c, k):
        return (c.get(k) if isinstance(c, dict) else getattr(c, k, "")) or ""

    n_cov = n_adv = 0
    for c in counts:
        a_sig = _cite_signature(_get(c, "precedent_anchor"))
        if a_sig and any(a_sig & s for s in sigs):
            n_cov += 1
        if not _get(c, "adverse_precedent_confronted").strip():
            n_adv += 1
    return {"n_counts": n, "n_covered": n_cov, "coverage": n_cov / n,
            "n_adverse_unconfronted": n_adv}


def precedent_strength_factor(coverage: float) -> float:
    """Map precedent coverage (0-1) to a strength multiplier in [FLOOR, 1.0]."""
    coverage = max(0.0, min(1.0, coverage))
    return PRECEDENT_CAP_FLOOR + (1.0 - PRECEDENT_CAP_FLOOR) * coverage


# ─── Feature extraction from runtime state ─────────────────────────────────


def extract_features_from_state(
    *,
    counsel_build,                                  # CounselBuildOutput
    counsel_rebuttal,                               # CounselRebuttalOutput | None
    all_evidence: list,                             # list[EvidenceItem]
    investigator_notes_concat: str,                 # ALL iters' notes_for_counsel
    cumulative_discount_pct: float = 0.0,
) -> dict:
    """Extract the canonical feature dict from runtime state objects.

    Returns a dict suitable for `compute_strength_score(**features)`.
    """
    # n_confirmed_facts — count CONFIRMED_FACT tier evidence
    n_confirmed = 0
    for ev in (all_evidence or []):
        tier = getattr(ev, "tier", None)
        if tier is None:
            continue
        tier_str = tier.value if hasattr(tier, "value") else str(tier)
        if tier_str == "confirmed_fact":
            n_confirmed += 1

    # n_counts
    counts = getattr(counsel_build, "counts_pled", []) or []
    n_counts = len(counts)

    # damages — Guard A: headline uses the per-count FLOOR whenever the
    # aggressive theory depends on facts not yet in hand. Guard B: a count
    # built on a co-located other-provider's billing without a SUBSTANTIVE
    # link to the subject does not count toward the subject's damages.
    _SUBSTANTIVE_LINKS = {
        "shared_tin", "shared_beneficiaries", "shared_billing_agent",
        "documented_referral",
    }
    per_count_headline: list[float] = []
    for c in counts:
        link_basis = set(getattr(c, "co_defendant_link_basis", None) or [])
        if link_basis and not (link_basis & _SUBSTANTIVE_LINKS):
            continue  # Guard B: co-located, address/temporal only → separate lead
        ceiling_requires = getattr(c, "ceiling_requires", None) or []
        floor = getattr(c, "damages_floor_usd", None)
        point = getattr(c, "estimated_single_damages_usd", None)
        ceiling = getattr(c, "damages_ceiling_usd", None)
        if ceiling_requires and floor is not None:
            per_count_headline.append(floor)          # ceiling needs discovery → floor
        elif point is not None:
            per_count_headline.append(point)
        elif floor is not None:
            per_count_headline.append(floor)
        elif ceiling is not None:
            per_count_headline.append(ceiling)
    if per_count_headline:
        damages = float(sum(per_count_headline))
    else:
        # Backward-compat: pre-Guard-A cases without per-count fields.
        damages = getattr(counsel_build, "estimated_aggregate_damages_usd", None) or 0.0

    # survival
    if counsel_rebuttal is not None:
        survival = float(getattr(counsel_rebuttal, "estimated_survival_likelihood", 0.5))
    else:
        survival = 0.5

    # Text-based features — concatenate notes + theory + reasoning + precedents
    text_parts: list[str] = [investigator_notes_concat or ""]
    theory = getattr(counsel_build, "case_theory", "") or ""
    text_parts.append(theory)
    reasoning = getattr(counsel_build, "reasoning", "") or ""
    text_parts.append(reasoning)
    precedents = getattr(counsel_build, "precedents", []) or []
    text_parts.append("\n".join(precedents))
    # factual_basis_per_count narratives are rich signal for within-practice + scienter
    fb_list = getattr(counsel_build, "factual_basis_per_count", []) or []
    for fb in fb_list:
        text_parts.append(getattr(fb, "innocent_explanations_refuted_narrative", "") or "")
    # analysis_unit_rationale is where the unit-of-analysis / within-practice
    # reasoning naturally lives — and evidence rationales carry the co-located
    # peer divergence facts. Both were previously excluded, silently dropping
    # legitimate within-practice controls from the score.
    text_parts.append(getattr(counsel_build, "analysis_unit_rationale", "") or "")
    for ev in (all_evidence or []):
        text_parts.append(getattr(ev, "rationale", "") or "")
    full_text = "\n".join(text_parts)

    has_venue = bool(_DISTRICT_RE.search(full_text)) or bool(_USAO_RE.search(full_text))
    has_same_district = bool(_SAME_DISTRICT_RE.search(full_text))
    # Structured field is authoritative; the broadened regex over the broadened
    # text slice is the backward-compat fallback for pre-structured-field cases.
    has_within_practice = (
        bool(getattr(counsel_build, "within_practice_control_established", False))
        or bool(_WITHIN_PRACTICE_RE.search(full_text))
    )
    n_regulatory = len(set(m.group(0).lower() for m in _REGULATORY_RE.finditer(full_text)))

    # Medicaid cross-program — only credit when the MEDICAID_CROSS_PROGRAM:
    # section is present AND describes an actual systemic-or-selective finding
    # (not "no Medicaid records / data unavailable" placeholders).
    has_medicaid_cross = (
        bool(_MEDICAID_CROSS_PROGRAM_RE.search(full_text))
        and bool(_CROSS_PROGRAM_POSITIVE_RE.search(full_text))
    )

    return {
        "n_confirmed_facts": n_confirmed,
        "n_counts": n_counts,
        "n_regulatory_predicates": n_regulatory,
        "has_venue_precedent": has_venue,
        "has_same_district_precedent": has_same_district,
        "has_within_practice_controls": has_within_practice,
        "survival_likelihood": survival,
        "damages_usd": damages,
        "cumulative_discount_pct": cumulative_discount_pct,
        "has_medicaid_cross_program_match": has_medicaid_cross,
    }
