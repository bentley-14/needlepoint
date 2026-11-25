"""Pydantic models for every agent's input/output JSON contract.

Every model emits a Pydantic model; the orchestrator validates with
``model_validate_json`` and retries (with the validation error appended to
the prompt) up to ``MAX_PARSE_RETRIES`` times before escalating to a
``needs_human`` terminal state.
"""
from __future__ import annotations

import json
from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator


# ─────────────────────────────────────────────────────────────────────────────
# Common types
# ─────────────────────────────────────────────────────────────────────────────


class EvidenceTier(str, Enum):
    CONFIRMED_FACT = "confirmed_fact"
    STATISTICAL_SIGNAL = "statistical_signal"
    LEGAL_HYPOTHESIS = "legal_hypothesis"


class CaseStrength(str, Enum):
    WEAK = "weak"
    MODERATE = "moderate"
    STRONG = "strong"


class EvidenceItem(BaseModel):
    """One piece of evidence gathered by the investigator."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., description="Stable identifier (slug). Used for dedup across iterations.")
    # `tier` is REQUIRED — it's a real scoring input (confirmed_fact items feed
    # n_confirmed_facts). We deliberately do NOT default a missing tier: silently
    # defaulting mis-grades evidence and degrades signal. Instead, when the model
    # omits tier, claude_p does a SURGICAL REPAIR (re-prompts with the model's own
    # output + the exact errors, "fix only these fields") so the agent is forced to
    # actually grade each item. See _repair_prompt in claude_p.py.
    tier: EvidenceTier
    fact: str = Field(..., min_length=10, description="One-sentence factual statement.")
    citation: str = Field(..., description="Source citation (URL, dataset name + query, statute cite, etc.).")
    supports_counts: list[str] = Field(default_factory=list, description="Which legal counts this supports (free-form ids).")
    confidence: float = Field(..., ge=0.0, le=1.0)
    rationale: str = Field("", description="Why this fact supports the counts listed.")
    # Provenance — system metadata, NOT graded by the agent (default "agent"; agents
    # omit it). "human_attested" marks a fact supplied by a human answering a
    # ReviewRequest checkpoint (scripts/answer_checkpoint.py). A human_attested item may
    # legitimately carry tier=confirmed_fact precisely BECAUSE a human verified it — the
    # original sin (the Dogwood/Birch failure) was an *agent* claiming confirmed_fact on a
    # sealed docket it could not read.
    provenance: Literal["agent", "human_attested"] = "agent"
    # ── Surprise-quotient grading (discovery-mode prompts, 2026-05-21) ──
    # Encourages the investigator to differentiate "expected" findings
    # (the NPI bills Medicare for podiatric services — duh) from
    # "surprising" ones (a within-practice peer at the same suite bills
    # nothing of the target's most-anomalous code — strongly informative).
    # counsel_build's strength scoring should weight "surprising" higher;
    # see fraud_investigator/scoring.py for the integration.
    #
    # Default is "expected" for backward compatibility with older parsed.json
    # files that lack this field; new agent invocations are prompted to grade.
    novelty: Literal["expected", "moderate", "surprising"] = Field(
        default="expected",
        description=(
            "Surprise-quotient grading. 'expected' = baseline-consistent finding "
            "(billing exists, NPI is enrolled, etc.); 'moderate' = anomaly that "
            "matches one of the standard fraud signatures (upcoding, code "
            "stacking); 'surprising' = pattern you didn't anticipate from the "
            "subject summary, contradicts an initial hypothesis, or reveals a "
            "structural feature (e.g., co-located peer divergence, NPPES "
            "staleness exceeding regulatory window, cross-program selectivity). "
            "Counsel assesses novelty itself against the Step-0 pack baseline "
            "when scoring strength; this label is a hint, not the input — "
            "grade honestly, do not inflate."
        ),
    )
    novelty_rationale: str = Field(
        default="",
        description=(
            "1-2 sentences explaining why this is or isn't surprising relative "
            "to baseline expectations for an NPI of this specialty + region + "
            "scale. Required for 'moderate' or 'surprising' grades."
        ),
    )

    @field_validator("id")
    @classmethod
    def _id_is_slug(cls, v: str) -> str:
        if not v or " " in v:
            raise ValueError("id must be a non-empty slug (no spaces)")
        return v

    @field_validator("novelty_rationale", mode="before")
    @classmethod
    def _novelty_rationale_none_to_empty(cls, v):
        # Agents emit null when novelty == "expected"; keep the field a plain str
        # instead of failing validation (and forcing a whole-output retry).
        return "" if v is None else v

    @field_validator("confidence", mode="before")
    @classmethod
    def _coerce_confidence(cls, v):
        # Agents sometimes emit a qualitative grade ("high") or a numeric string
        # ("0.85" / "95%") in place of a float. Coerce the value the agent DID
        # provide rather than aborting a 30-min run on a whole-output retry (same
        # philosophy as coerce_numbers_to_str + _novelty_rationale_none_to_empty).
        # A genuinely MISSING confidence is NOT defaulted — the field stays
        # required, so Pydantic raises "field required" → surgical repair (cf. the
        # `tier` policy: don't silently fabricate a grade the agent never gave).
        if not isinstance(v, str):
            return v
        s = v.strip().lower().rstrip("%")
        try:
            n = float(s)
            return n / 100 if n > 1 else n  # "95%"/"95" → 0.95
        except ValueError:
            pass
        return {
            "very high": 0.95, "high": 0.9, "medium-high": 0.75, "med-high": 0.75,
            "medium": 0.6, "moderate": 0.6, "med": 0.6,
            "low-medium": 0.45, "medium-low": 0.45,
            "low": 0.3, "very low": 0.15,
        }.get(s, v)  # unmapped → return original so Pydantic raises a clear error → repair


class AnalysisUnit(str, Enum):
    """The unit of analysis the investigator chose to anchor the case on.

    Forcing the investigator to declare this explicitly prevents the default-
    to-'single_npi' failure mode that misses practice-wide conspiracies
    (Juniper 2026-05-21 cold-eyes test surfaced this gap — the original v1
    case anchored on a 5-NPI synchronized upcoding conspiracy at Foot &
    Ankle Concepts; the cold-eyes opus run defaulted to single_npi and
    missed the within-practice peer trajectory entirely).

    Counsel_build will mirror this choice and reject 'single_npi' anchoring
    unless it's explicitly justified by either (a) the address sweep
    returning no co-billers, or (b) co-billers' patterns documented as
    DIVERGING (used as within-practice control evidence).
    """

    SINGLE_NPI = "single_npi"
    OFFICE_SUITE = "office_suite"            # multi-NPI same address
    MULTI_OFFICE_NETWORK = "multi_office_network"   # NPIs across multiple locations of one entity
    SPECIALTY_COHORT = "specialty_cohort"    # broader peer comparator group


# ─────────────────────────────────────────────────────────────────────────────
# Investigator
# ─────────────────────────────────────────────────────────────────────────────


class OsintFinding(BaseModel):
    """One open-web (real-world) investigation result — the OSINT contract.

    Added 2026-08-18 after a citation census on CASE-6A842598-2026 showed
    phase-A agents NEVER browse: web guidance was prose with no output
    contract, so turn-pressured agents satisfied only the mechanical floors
    (all script tools) while the adversarial agents (whose goals force it)
    browsed freely. This field is the mechanical floor for phase A.
    REPLAY NOTE: runs started before 2026-08-18 record [] here — that means
    "field did not exist," never "nothing was found" (see the
    replay-fixture-schema-confound lesson, 2026-07-31).
    """

    model_config = ConfigDict(extra="forbid")

    source_class: Literal["practice_website", "reviews", "state_board", "news",
                          "facility_data", "social_or_directory", "other"]
    status: Literal["finding", "blocked", "nothing_found"]
    url: str = Field("", description=(
        "The specific URL fetched or attempted. May be '' only for "
        "nothing_found, with the exact query recorded in summary."))
    summary: str = Field(..., description=(
        "What the source showed, or exactly what was attempted. For "
        "'blocked'/'nothing_found' name the query/page — an unreachable "
        "source is UNKNOWN, never a clean record."))


class InvestigatorOutput(BaseModel):
    """What the investigator agent emits."""

    # coerce_numbers_to_str: agents emit `iteration: 2` (int), but the field is a
    # str label ("2" / "rebuttal-r2") — coerce instead of failing validation.
    model_config = ConfigDict(extra="forbid", coerce_numbers_to_str=True)

    case_id: str
    iteration: str = Field(..., description="Iteration label, e.g., '1' or 'rebuttal-r2'")
    evidence: list[EvidenceItem] = Field(default_factory=list)
    notes_for_counsel: str = Field("", description="Free-form notes the investigator wants counsel to read.")

    # ── Discovery-mode fields (2026-05-21) ──
    # These force the investigator out of pure checklist mode and into
    # exploratory hypothesis-building. Defaults are empty/lenient for
    # backward compatibility with older parsed.json files; new prompts
    # mandate non-trivial content via prompt language + post-hoc validation.

    surprises: list[str] = Field(
        default_factory=list,
        description=(
            "1-5 things that surprised the investigator most about this NPI's "
            "profile. Patterns NOT anticipated from the subject summary, "
            "absences that should have been presences, hypotheses contradicted "
            "by the data, structural features (e.g., 'this NPI bills 100% of "
            "Medicare patients for a code while billing zero Medicaid patients "
            "for the same') — the unexpected findings that distinguish this "
            "investigation from a generic checklist run. 'Surprising' means "
            "NOT derivable from the Step-0 evidence pack. Empty is legal — "
            "the prompt requires an attestation in notes_for_counsel of what "
            "was checked and found unremarkable; fabricated entries are worse "
            "than none."
        ),
    )
    osint_findings: list[OsintFinding] = Field(
        default_factory=list,
        description=(
            "Open-web results — the subject's REAL-WORLD footprint. The prompt "
            "requires >=3 source classes attempted per investigation "
            "(practice_website, reviews, state_board at minimum); blocked / "
            "nothing_found entries count as attempts and must name what was "
            "tried. [] is tolerated only for pre-2026-08-18 replay compat and "
            "triggers a Guardrail warning + next-iteration re-injection."
        ),
    )
    unexpected_absences: list[str] = Field(
        default_factory=list,
        description=(
            "Negative-space findings: 1-3 things you would EXPECT to find for "
            "this case theory but did NOT find. Examples: 'I expected to find "
            "the practice's other DPMs sharing the upcoding pattern, but "
            "Hawthorn (same suite) bills 0% high-complexity E&M', or 'I "
            "expected CLIA certification at one of the five locations given "
            "the 87798 volume; none of the addresses have CLIA records'. "
            "Absent evidence is informative; this field forces counterfactual "
            "reasoning."
        ),
    )
    working_hypotheses: list[str] = Field(
        default_factory=list,
        description=(
            "Scratchpad / working memory for the NEXT investigator iter. "
            "1-5 short statements of hypotheses the agent is still pursuing "
            "but couldn't fully verify this iter. Format: 'H1: practice "
            "operates a synchronized E&M upcoding scheme across 5 senior "
            "DPMs — verified Juniper + Hawthorn divergence in iter-1; need "
            "to query Matthews, Tatum, plus the 3 junior podiatrists'. The "
            "next investigator call sees these and is expected to pursue or "
            "explicitly retire each one."
        ),
    )
    analysis_unit_chosen: AnalysisUnit = Field(
        default=AnalysisUnit.SINGLE_NPI,
        description=(
            "The unit of analysis the investigator anchored on. See "
            "AnalysisUnit enum for the four options. Defaulting to "
            "'single_npi' is NOT acceptable unless explicitly justified by "
            "the address sweep finding no co-billers; the prompt forces an "
            "explicit choice."
        ),
    )
    analysis_unit_rationale: str = Field(
        default="",
        description=(
            "Why this unit was chosen. If 'single_npi', state explicitly: "
            "(a) address sweep found no relevant co-billers, OR (b) co-billers "
            "found but their billing diverged from the target (cite their NPIs "
            "+ evidence_ids of the divergence), OR (c) data was insufficient "
            "(and what additional data would be needed). 'Default' is not "
            "an acceptable answer."
        ),
    )

    @field_validator("surprises", "unexpected_absences", "working_hypotheses", mode="before")
    @classmethod
    def _coerce_str_list(cls, v):
        # Agents drift on these freeform list[str] fields two ways (both seen in the
        # 2026-06-10 iter-2 calibration abort): emitting [null, ...] placeholders,
        # or "upgrading" each entry to a structured {id, description, ...} object
        # (the iter-2 investigator restyled surprises/absences like evidence items).
        # Recover a plain list[str] — drop nulls, keep strings, collapse an object to
        # its description/text (else compact JSON). Same "absorb representation drift
        # instead of forcing a whole-output retry" philosophy as _coerce_confidence;
        # [] is legal (the prompt requires an attestation when surprises is empty).
        if not isinstance(v, list):
            return v
        out = []
        for x in v:
            if x is None:
                continue
            if isinstance(x, str):
                out.append(x)
            elif isinstance(x, dict):
                out.append(str(x.get("description") or x.get("text") or x.get("fact")
                               or x.get("label") or json.dumps(x, separators=(",", ":"))))
            else:
                out.append(str(x))
        return out


# ─────────────────────────────────────────────────────────────────────────────
# Counsel — three steps per iteration
# ─────────────────────────────────────────────────────────────────────────────


class Rule9bElements(BaseModel):
    """The five Rule 9(b) particularity elements with their evidence ids."""

    model_config = ConfigDict(extra="forbid")

    who: list[str] = Field(default_factory=list, description="evidence ids supporting WHO")
    what: list[str] = Field(default_factory=list, description="evidence ids supporting WHAT")
    when: list[str] = Field(default_factory=list, description="evidence ids supporting WHEN")
    where: list[str] = Field(default_factory=list, description="evidence ids supporting WHERE")
    how: list[str] = Field(default_factory=list, description="evidence ids supporting HOW")


class LegalCount(BaseModel):
    """One pleadable count (e.g., 'Count I: 31 USC 3729(a)(1)(A) — false claims')."""

    model_config = ConfigDict(extra="forbid")

    count_id: str
    statute: str = Field(..., description="e.g., '31 USC 3729(a)(1)(A)'")
    description: str
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    estimated_single_damages_usd: float | None = None

    # ── Guard A — damages altitude (2026-06-07, sandbox experiment) ──
    damages_floor_usd: float | None = Field(
        None,
        description=(
            "Single damages under the MOST DEFENSIBLE falsity theory for this "
            "count — NOT the smallest dollar figure. For an *increment* falsity "
            "(upcoding/unbundling) this is the delta. For a *whole-claim* falsity "
            "(false certification / incident-to conditions unmet / "
            "services-not-rendered / ineligibility / kickback-tainted) it is the "
            "FULL claim value — the claim was not payable at all, so a rate "
            "differential is the defense's argument, not the floor. This is the "
            "HEADLINE number whenever the aggressive theory depends on facts not "
            "yet in hand."
        ),
    )
    damages_ceiling_usd: float | None = Field(
        None,
        description=(
            "Aggressive-theory single damages (e.g. full phantom). Preserved "
            "and pled in the alternative; never the headline when ceiling_requires "
            "is non-empty."
        ),
    )
    damages_basis: Literal["floor", "ceiling", "point"] = Field(
        "point",
        description=(
            "Which figure is the primary pleaded damages. Use 'floor' when the "
            "ceiling depends on facts not yet in hand (the common case for a "
            "distinct-dates/volume anomaly that fits both upcoding AND phantom)."
        ),
    )
    mundane_alternative: str = Field(
        "",
        description=(
            "The lower-altitude theory that fits the SAME data as the aggressive "
            "one (e.g. 'unbundling/upcoding of simple excisions' vs. 'phantom "
            "services never rendered') and why current data cannot exclude it."
        ),
    )
    ceiling_requires: list[str] = Field(
        default_factory=list,
        description=(
            "Evidence that would justify pleading the ceiling instead of the "
            "floor (operative/pathology records, claim-level modifiers, etc.). "
            "These become discovery dependencies. If non-empty, the headline "
            "damages use damages_floor_usd."
        ),
    )

    # ── Guard B — co-located fusion test (2026-06-07, sandbox experiment) ──
    co_defendant_link_basis: list[str] = Field(
        default_factory=list,
        description=(
            "If this count is built on a CO-LOCATED other provider's billing, "
            "the links to the subject, any of: {shared_tin, shared_beneficiaries, "
            "shared_billing_agent, documented_referral, shared_address, "
            "temporal_coincidence}. A count may be pled AGAINST THE SUBJECT only "
            "if at least one SUBSTANTIVE link is present (shared_tin / "
            "shared_beneficiaries / shared_billing_agent / documented_referral). "
            "shared_address and/or temporal_coincidence ALONE are insufficient — "
            "emit that anomaly as a SEPARATE lead, not a count against the subject. "
            "Leave empty for counts about the subject's own billing."
        ),
    )

    # ── Guardrail 5 — per-count precedent anchor (2026-06-11) ──
    precedent_anchor: str = Field(
        "",
        description=(
            "The prior case where a SUBSTANTIALLY SIMILAR count/theory-of-falsity "
            "was pled and survived (or the controlling doctrine that makes this "
            "theory pleadable) — e.g. for an incident-to/false-certification CCM "
            "count: 'United States v. Mackby, 261 F.3d 821 (9th Cir. 2001)' or "
            "'Universal Health Servs. v. Escobar, 579 U.S. 176 (2016)'. CITE FROM "
            "verified_precedents / the precedent_lookup corpus whenever possible so "
            "the scorer can confirm it. A statistical OUTLIER alone (a billing "
            "ratio/percentile) is NOT a theory of falsity and does not anchor a "
            "count — it is Rule 9(b) corroboration for a falsity theory that has its "
            "own anchor. A count whose anchor cannot be matched to a verified "
            "precedent can still be pled, but it does not count as confirmed and "
            "DOWN-WEIGHTS the case strength (Guardrail 5 precedent-coverage cap)."
        ),
    )
    adverse_precedent_confronted: str = Field(
        "",
        description=(
            "The on-point ADVERSE authority for this count's theory + why this count "
            "is distinguishable — e.g. 'Integra Med Analytics v. Baylor Scott & "
            "White, 816 F. App'x 892 (5th Cir. 2020): dismisses data-mining counts "
            "equally consistent with an innocent explanation; distinguished here "
            "because [the falsity rests on unmet conditions of payment, not a bare "
            "statistical outlier].' Leave empty only if no on-point adverse authority "
            "exists. Required posture for any statistics-heavy count in the 5th Cir."
        ),
    )


class FactualBasis(BaseModel):
    """Per-count factual convergence record — the discrimination floor.

    Forces counsel to articulate, in structured form, why this count is a
    sure-fire fraud signal and not a high-volume-but-legitimate practice.
    Disposition mechanically checks the cardinalities of these fields against
    the readiness thresholds.
    """

    # Tolerate model-invented extra fields (silently dropped — the pipeline reads
    # only the declared fields) instead of failing the build with a repair round.
    model_config = ConfigDict(extra="ignore")

    count_id: str
    extremity_evidence_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Evidence ids of statistics at ≥95th percentile vs. the CORRECT peer "
            "cohort (same specialty, same legal theory). One is enough for this slot."
        ),
    )
    convergent_signal_evidence_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Evidence ids of INDEPENDENT corroborating signals on different "
            "dimensions than the extremity stat. svcs/bene + drug_fraction + "
            "dual_share are independent; svcs/bene + total_services + claims_count "
            "are NOT (all measure volume). Need ≥2 for DECLARE_CASE_READY."
        ),
    )
    falsity_anchor_evidence_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Evidence ids of confirmed-fact FALSITY anchors — facts that make the "
            "claims false rather than merely extreme: impossible per-day/per-bene "
            "volume vs a VERIFIED workforce denominator, billing on closed/travel "
            "dates, claims for deceased/incarcerated/non-existent beneficiaries, "
            "billing under a stripped/invalid NPI, a service physically incompatible "
            "with the specialty/setting, predicate-code absence (Mackby), or a "
            "contemporaneous LCD/NCD violation. Floor slot 3 = (this ≥1) OR "
            "(regulatory_corroboration ≥1) — novel-detection targets have clean "
            "regulatory records by premise, so the anchor is the primary grounding."
        ),
    )
    regulatory_corroboration_evidence_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Evidence ids of at least one regulatory/enforcement corroboration: "
            "LEIE exclusion (defendant or affiliate), state board action, prior "
            "DOJ/OIG enforcement, CMS revocation, abnormal denial-rate trail, "
            "prior settled qui tam against affiliated entity. Alternative slot-3 "
            "grounding alongside falsity_anchor_evidence_ids; also a strength "
            "bonus in scoring."
        ),
    )
    innocent_explanations_considered: list[str] = Field(
        default_factory=list,
        description=(
            "Plain-language list of innocent explanations the data could fit "
            "absent fraud: 'large group practice', 'infusion center', 'oncology "
            "biologics mix', 'dialysis nephrology', 'specialty subset', etc."
        ),
    )
    innocent_explanations_refuted_narrative: str = Field(
        "",
        description=(
            "2-4 sentence narrative explaining HOW the evidence rules out the "
            "innocent explanations listed above. E.g., 'NPPES shows solo "
            "practice, not group; CMS POS code is office not infusion; specialty "
            "code IM rules out oncology biologics.'"
        ),
    )


class Hypothesis(BaseModel):
    """One competing theory of the fraud — counsel must enumerate ≥3 before pleading counts.

    Forces counsel to consider STRUCTURALLY DIFFERENT theories (solo
    upcoding vs. multi-NPI conspiracy vs. kickback-and-Stark vs.
    incident-to violation vs. beneficiary-induction). Choosing one
    theory means explicitly rejecting the others; the rejection often
    surfaces the evidence we'd need to favor each alternative, which
    becomes a follow-up question for the next investigator iter.

    Discovery-mode addition (2026-05-21) — see counsel_build.md prompt
    section 'Competing hypotheses' for the structured workflow.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=5, description=(
        "Short label, e.g., 'individual_upcoding', 'practice_wide_conspiracy', "
        "'kickback_and_stark', 'incident_to_violation', 'beneficiary_induction'."
    ))
    description: str = Field(..., min_length=20, description=(
        "1-2 sentence framing of the theory: who is doing what to whom, "
        "what the fraud mechanism is, and what the financial exposure looks like."
    ))
    fits_evidence: str = Field(..., description=(
        "Which evidence items would support THIS theory if it were true? "
        "Cite by evidence_id."
    ))
    requires_evidence_we_lack: str = Field(..., description=(
        "What additional evidence would be needed to favor THIS theory over "
        "the chosen one? This often surfaces follow-up questions for the "
        "next investigator iter."
    ))
    survived_or_rejected: Literal["chosen", "rejected"] = Field(..., description=(
        "Was this the chosen theory ('chosen') or did counsel reject it "
        "in favor of another ('rejected')? Exactly ONE hypothesis must be "
        "'chosen'; the rest must be 'rejected'."
    ))
    rejection_reason: str = Field(default="", description=(
        "If 'rejected', why? Acceptable answers: 'insufficient evidence', "
        "'contradicted by ev-X-Y', 'subset of chosen theory', 'lower damages'."
    ))


class NumericPredicate(BaseModel):
    """A structured statement of ONE headline quantitative claim, so the
    deterministic Guardrail-2 auditor (scripts/audit_case_numerics.py) can
    re-derive it from canonical constants and flag inflation/error BEFORE the
    claim reaches attorney-facing output (the Birch 99489 60-vs-30-min class of
    error). Kind-specific fields are allowed through (extra='allow'):
      time_impossibility : codes=[{code, units, year}]            -> claimed.total_hours
      fte_equivalent     : total_hours, productive_hours_per_fte  -> claimed.fte
      ratio / market_share : numerator, denominator (or codes=[{units|count,
                            national_services|beneficiaries|bene_day_pairs|
                            peer_services|ring_size, ...}])         -> claimed.ratio
      rank                : rank + of/cohort (top-level, in codes[0], or as
                            *_rank + *_cohort/*_of claimed keys)    -> claimed.*_percentile
                            RECOMPUTED AS A PERCENTILE — a rank claim ("#5 of
                            394") emitted as kind="ratio" with numerator=rank,
                            denominator=cohort is auto-detected and treated as
                            rank/percentile (NOT a bare rank/cohort quotient
                            compared against the raw rank number — that false
                            MISMATCH cost 4 wasted adversarial attacks in
                            CASE-6A305421; see failure_mode_taxonomy_2026-07.md #3).
      multiple           : value, baseline                        -> claimed.multiple
      damages            : units, unit_amount_usd (or line_items) -> claimed.damages_usd
      product/sum        : factors[] / addends[]                  -> claimed.value
    """

    model_config = ConfigDict(extra="allow")

    id: str
    # Free string, not a strict Literal: the Guardrail-2 auditor re-derives the
    # known kinds (time_impossibility, fte_equivalent, ratio, market_share, rank,
    # multiple, damages, product, sum) and reports any OTHER kind as UNVERIFIABLE
    # (never a false MISMATCH — see audit_case_numerics). Keeping it strict only
    # forced a counsel_build repair round whenever the model coined a sensible
    # new kind (e.g. "service_volume"); the auditable high-stakes kinds are
    # unaffected.
    kind: str = Field(
        ...,
        description=("Predicate kind. Auditable: time_impossibility, "
                     "fte_equivalent, ratio, market_share (alias of ratio), "
                     "rank (auto-detected within ratio/market_share and "
                     "recomputed as a percentile), multiple, damages, product, "
                     "sum. Any other kind validates but is reported UNVERIFIABLE."),
    )
    claimed: dict = Field(default_factory=dict,
                          description='The asserted scalar, e.g. {"total_hours": 59400}.')
    statement: str = Field("", description="The prose claim this number backs, verbatim.")


class CounselBuildOutput(BaseModel):
    """Step 1 of counsel: construct the case theory."""

    # Tolerate model-invented extra fields (silently dropped — the pipeline reads
    # only the declared fields) instead of failing the build with a repair round.
    model_config = ConfigDict(extra="ignore")

    case_id: str
    iteration: int
    case_theory: str = Field(..., min_length=20, description="1-2 paragraph case narrative.")
    counts_pled: list[LegalCount] = Field(default_factory=list)
    rule_9b_elements: Rule9bElements
    precedents: list[str] = Field(default_factory=list, description="Case citations used.")
    public_disclosure_concerns: list[str] = Field(default_factory=list)
    first_to_file_concerns: list[str] = Field(default_factory=list)
    estimated_aggregate_damages_usd: float | None = None
    current_strength: CaseStrength
    reasoning: str = Field("", description="Why this strength rating.")
    factual_basis_per_count: list[FactualBasis] = Field(
        default_factory=list,
        description=(
            "Per-count factual-convergence record. Required for any count that "
            "counsel will defend as ready. Disposition mechanically checks these "
            "against the convergence floor (extremity ≥1, convergent ≥2, "
            "regulatory ≥1, innocent_explanations refuted)."
        ),
    )

    # ── Discovery-mode fields (2026-05-21) ──
    # Force counsel to articulate the unit-of-analysis choice and consider
    # ≥3 competing theories BEFORE drafting counts. Default-tolerant so
    # older parsed.json files still validate.

    analysis_unit: AnalysisUnit = Field(
        default=AnalysisUnit.SINGLE_NPI,
        description=(
            "Counsel's chosen unit of analysis. Should mirror the "
            "investigator's analysis_unit_chosen, BUT counsel can override "
            "(e.g., investigator chose 'office_suite' but counsel decides to "
            "plead only against the named NPI for first-to-file reasons). "
            "'single_npi' is the lazy default — must be explicitly justified."
        ),
    )
    analysis_unit_rationale: str = Field(
        default="",
        description=(
            "Why this unit. For 'single_npi': cite the address-sweep "
            "evidence_id showing no relevant co-billers OR the divergence "
            "evidence_ids showing co-billers' patterns differ from target. "
            "For broader units: name the co-defendants or comparator NPIs "
            "and the structural feature (e.g., shared CLIA, joint office, "
            "common PA/NP supervision) that makes the broader unit defensible."
        ),
    )
    within_practice_control_established: bool = Field(
        default=False,
        description=(
            "True ONLY if the case establishes a within-practice control: a "
            "co-located peer provider (same address / suite / building, same "
            "payer) whose billing pattern does NOT match the subject's — "
            "isolating the subject as the anomaly rather than a "
            "practice/specialty/locality artifact. This is the anti-outlier "
            "control and the single strongest rebuttal to the 'everyone here "
            "bills this way' defense (strength-score weight 0.20). Set True "
            "only when the comparator NPI(s) and their divergence are named "
            "in within_practice_comparator_npis + analysis_unit_rationale."
        ),
    )
    within_practice_comparator_npis: list[str] = Field(
        default_factory=list,
        description=(
            "The co-located comparator NPI(s) whose divergent billing "
            "establishes the within-practice control. Empty if none exist "
            "(solo practice, or all co-billers share the suspect pattern)."
        ),
    )
    competing_hypotheses: list[Hypothesis] = Field(
        default_factory=list,
        description=(
            "≥3 STRUCTURALLY DIFFERENT theories of the fraud. Exactly ONE "
            "marked 'chosen'; the rest 'rejected' with reasons. The "
            "rejected hypotheses' 'requires_evidence_we_lack' fields often "
            "become follow-up questions for the next investigator iter. "
            "Counsel disposition will weight a case higher if competing "
            "hypotheses were genuinely considered + rejected than if the "
            "first-found theory was assumed correct."
        ),
    )
    numeric_predicates: list[NumericPredicate] = Field(
        default_factory=list,
        description=(
            "STRUCTURED form of every headline quantitative claim in the case "
            "theory / counts (impossibility hours, FTE, ratios, percentile "
            "multiples, damages). The orchestrator runs the deterministic "
            "Guardrail-2 auditor over these before the defense stage; any "
            "MISMATCH becomes a dispositive integrity defect. Emit one per "
            "number you want a court to rely on — the re-derivation is what "
            "catches a wrong constant (e.g. a 2x CPT-minute error) before it ships."
        ),
    )


class WeakLink(BaseModel):
    """One self-identified weakness counsel needs to fix or accept."""

    model_config = ConfigDict(extra="forbid")

    weakness_id: str
    affected_counts: list[str]
    description: str
    attack_vector: str = Field(..., description="How an opposing counsel would exploit this.")
    severity: Literal["severe", "concerning", "defense_likely_to_raise"]
    fixable_via: Literal["more_investigation", "drop_count", "rephrase", "discovery", "amend_complaint", "no_fix_accept"]
    investigator_questions: list[str] = Field(default_factory=list,
                                              description="Questions for investigator IF fixable_via == more_investigation.")


class CounselRebuttalOutput(BaseModel):
    """Step 2 of counsel: devil's advocate self-attack."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    iteration: int
    weak_links: list[WeakLink] = Field(default_factory=list)
    overall_assessment: str = Field(..., description="Will this case survive opposing counsel's Motion to Dismiss?")
    estimated_survival_likelihood: float = Field(..., ge=0.0, le=1.0)


class CounselFirstChairReviewAction(str, Enum):
    """Pre-rebuttal first-chair review action (Phase A judgement).

    The first-chair partner reads complaint v2 (iter-N counsel_build) and decides
    whether the case is ready for internal mock-defense (red-team rebuttal) or
    needs another build iteration. This is distinct from the final managing-
    partner sign-off (CounselDispositionAction) which happens AFTER rebuttal.
    """
    CONTINUE_BUILD = "CONTINUE_BUILD"                    # loop back to investigator
    DROP_COUNTS_AND_CONTINUE = "DROP_COUNTS_AND_CONTINUE"  # drop weak counts, loop back
    PROCEED_TO_REBUTTAL = "PROCEED_TO_REBUTTAL"          # exit Phase A → enter Phase B
    ABANDON_CASE = "ABANDON_CASE"                        # terminal


class CounselFirstChairReviewOutput(BaseModel):
    """Phase A judgement output. Schema mirrors CounselDispositionOutput so the
    same downstream counsel_build prompt can consume either as ``prior_disposition``.
    """
    model_config = ConfigDict(extra="forbid")

    case_id: str
    iteration: int
    action: CounselFirstChairReviewAction
    reasoning: str
    counts_to_drop: list[str] = Field(default_factory=list,
                                      description="Used when action == DROP_COUNTS_AND_CONTINUE")
    follow_up_questions_for_investigator: list[str] = Field(
        default_factory=list,
        description="Used when action ∈ {CONTINUE_BUILD, DROP_COUNTS_AND_CONTINUE}")
    gaps_to_fill: list[str] = Field(default_factory=list)


class CounselDispositionAction(str, Enum):
    DECLARE_CASE_READY = "DECLARE_CASE_READY"
    DECLARE_CASE_READY_WITH_DEPENDENCIES = "DECLARE_CASE_READY_WITH_DEPENDENCIES"
    NEED_MORE_INVESTIGATION = "NEED_MORE_INVESTIGATION"
    DROP_COUNTS_AND_REASSESS = "DROP_COUNTS_AND_REASSESS"
    ABANDON_CASE = "ABANDON_CASE"
    # Procedural-only: an iteration-cap coercion target for a case whose
    # convergence + damages floors ARE met but whose model-asserted action
    # (NEED_MORE_INVESTIGATION) has no legal path post-rebuttal. Never
    # asserted by an agent directly — set only by phase_b.py's coercion logic
    # (failure_mode_taxonomy_2026-07.md #5, CASE-6A3014F0).
    PARK_CASE = "PARK_CASE"


class CounselDispositionOutput(BaseModel):
    """Step 3 of counsel: meta-decision over build + rebuttal."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    iteration: int
    action: CounselDispositionAction
    reasoning: str
    counts_to_drop: list[str] = Field(default_factory=list,
                                      description="Used when action == DROP_COUNTS_AND_REASSESS")
    follow_up_questions_for_investigator: list[str] = Field(default_factory=list,
                                                            description="Used when action == NEED_MORE_INVESTIGATION")
    gaps_to_fill: list[str] = Field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Defense (Layer 4)
# ─────────────────────────────────────────────────────────────────────────────


class DefenseAttackType(str, Enum):
    RULE_9B_INSUFFICIENT = "rule_9b_insufficient"
    PUBLIC_DISCLOSURE_BAR = "public_disclosure_bar"
    FIRST_TO_FILE_BAR = "first_to_file_bar"
    STATUTE_OF_LIMITATIONS = "statute_of_limitations"
    SAFE_HARBOR = "safe_harbor"
    SCIENTER_LACKING = "scienter_lacking"
    MATERIALITY_LACKING = "materiality_lacking"
    DAMAGES_OVERSTATED = "damages_overstated"
    LEGITIMATE_EXPLANATION = "legitimate_explanation"
    DATA_DREDGING = "data_dredging"
    OTHER = "other"


class DefenseAttack(BaseModel):
    # extra="ignore" (was "forbid"): the model reliably emits a richer motion
    # shape (title, evidence_cited, data_queries, strength, disposition_requested,
    # notes, …). Forbidding them flooded the repair loop with 50+ "extra field"
    # errors that buried the 1-2 real ones and exhausted the retry budget before
    # convergence (observed: defense failed 63→37→12 errors over 3 tries). Ignoring
    # the extras lets the repair loop focus on genuine contract errors.
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    attack_id: str
    type: DefenseAttackType
    # accept the model's frequent rename `counts_targeted` for `target_counts`.
    target_counts: list[str] = Field(
        ..., validation_alias=AliasChoices("target_counts", "counts_targeted"))
    argument: str = Field(..., min_length=20)
    supporting_evidence: list[str] = Field(default_factory=list,
                                           description="External case citations, statutes, prior pleadings, etc.")
    severity: Literal["dispositive", "mitigating", "minor"]
    novelty_vs_prior_rounds: str = Field("first_round",
                                         description="'first_round' | 'new_attack' | 'renewed_via_new_evidence'")
    grounding: str = Field(
        "",
        description=(
            "For a legitimate_explanation attack: the literal tool query you ran "
            "+ the result that establishes (or fails to establish) that the "
            "billing was legitimate, not false. An ungrounded legitimacy attack "
            "is discarded by the adjudicator. severity='dispositive' means the "
            "data shows the claims were legitimate (not false); severity="
            "'mitigating' means the explanation is contested (plausible but the "
            "data cannot resolve it). Empty for procedural attack types."
        ),
    )


class DefenseMotion(BaseModel):
    """Defense's Motion to Dismiss."""

    # extra="ignore" (was "forbid") for the same reason as DefenseAttack — the
    # model wraps the motion in extra metadata (motion_id, filed_by, case_ref,
    # respondent_npi, data_cutoff, cumulative_round_summary, …) that is harmless
    # to drop.
    model_config = ConfigDict(extra="ignore")

    # case_id: the orchestrator owns the canonical id; the model frequently omits
    # it (or emits it as motion_id/case_ref). Optional + injected downstream rather
    # than a hard contract error.
    case_id: str = ""
    round: int
    attacks: list[DefenseAttack] = Field(default_factory=list)
    # any_attack_survives drives loop termination. The model sometimes omits it;
    # derive it from the attack list when missing (live attacks present ⇒ survives)
    # rather than failing the whole motion on a single missing boolean.
    any_attack_survives: bool | None = Field(
        None, description="False = no live attack found, terminal for the loop.")
    counts_with_live_attack: list[str] = Field(default_factory=list)
    counts_with_no_live_attack: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _derive_any_attack_survives(self):
        if self.any_attack_survives is None:
            self.any_attack_survives = len(self.attacks) > 0
        return self


# ─────────────────────────────────────────────────────────────────────────────
# Legal adversarial response (Layer 4)
# ─────────────────────────────────────────────────────────────────────────────


class AttackDisposition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attack_id: str
    disposition: Literal["SURVIVED", "MITIGATED", "DISPOSITIVE_NOT_SURVIVED"]
    discount_pct: int = Field(0, ge=0, le=50, description="Mitigation discount (0-50%).")
    reasoning: str
    classification: dict | None = Field(
        None,
        description=(
            "Per-attack-type classification payload for a MITIGATED attack "
            "(must include attack_type + target_counts; see "
            "prompts/legal_adv_response.md). The pipeline computes the "
            "discount deterministically from it (discounts.py)."
        ),
    )
    discount_pct_model: int | None = Field(
        None, ge=0, le=50,
        description=(
            "The model's original free-pick discount, preserved for audit "
            "when discounts.py overrides discount_pct."
        ),
    )


class SettlementVerdict(str, Enum):
    SETTLEMENT_READY = "SETTLEMENT_READY"
    PENDING_REBUTTAL = "PENDING_REBUTTAL"
    NOT_ACTIONABLE = "NOT_ACTIONABLE"


class LegalAdversarialResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    round: int
    attack_dispositions: list[AttackDisposition] = Field(default_factory=list)
    counts_retained_this_round: list[str] = Field(default_factory=list)
    counts_abandoned_this_round: list[str] = Field(default_factory=list)
    counts_pending_rebuttal: list[str] = Field(default_factory=list)
    follow_up_questions: list[str] = Field(default_factory=list,
                                           description="Questions for rebuttal investigator if PENDING_REBUTTAL.")
    cumulative_case_discount_pct: int = Field(..., ge=0, le=100)
    settlement_target_range_usd: str = Field("", description='Free-form, e.g. "$2.4M – $4.8M"')
    settlement_verdict: SettlementVerdict
    post_defense_reasoning: str


# ─────────────────────────────────────────────────────────────────────────────
# Innocent-Explanation Foreclosure Check (Guardrail 3 — post-case-ready stage)
# ─────────────────────────────────────────────────────────────────────────────


class InnocentExplanationStatus(str, Enum):
    FORECLOSED = "FORECLOSED"   # ruled out by cited, sound evidence
    PARTIAL = "PARTIAL"          # narrowed but not fully eliminated
    LIVE = "LIVE"                # plausible and NOT foreclosed by the record


class InnocentExplanation(BaseModel):
    """One row of the Foreclosure Ledger: an innocent (non-fraud) account of the
    billing pattern, steelmanned, then sought-to-be-foreclosed with CITED, audited
    evidence. Operationalizes the Twombly/Iqbal/Integra survival test — fraud must
    be the MORE plausible inference, and every obvious innocent explanation must be
    foreclosed (with a source) or flagged LIVE."""

    model_config = ConfigDict(extra="forbid")

    id: str
    explanation: str = Field(..., min_length=10,
                             description="The innocent (non-fraud) account of the pattern.")
    taxonomy_tag: str = Field(
        "",
        description=(
            "Reusable category if it fits one, e.g. "
            "'large_clinical_staff_workforce_410_26' (a big incident-to staff "
            "furnished the time — the one LIVE for Birch until a verified FTE count "
            "closes it), 'per_unit_drug_quantity' (services are per-mg units not "
            "visits), 'specialty_case_mix', 'bona_fide_group_practice', "
            "'referral_region_demographics', 'coding_transition_artifact'."
        ),
    )
    steelman: str = Field(..., min_length=10,
                          description="The BEST good-faith version — argue it as the defense's expert would.")
    plausibility_unrebutted: float = Field(
        ..., ge=0.0, le=1.0,
        description=("P(this innocent explanation accounts for the pattern) on its FACE, "
                     "before foreclosing evidence. The deterministic strength cap reads the "
                     "max of this across LIVE rows."),
    )
    status: InnocentExplanationStatus
    foreclosing_evidence: list[str] = Field(
        default_factory=list,
        description=("CITED facts that rule it out: evidence_ids, the literal tool query + its "
                     "result, or a regulatory cite. Empty ⇒ it CANNOT be FORECLOSED (stays LIVE/PARTIAL)."),
    )
    evidence_soundness_check: str = Field(
        "",
        description=("Audit of the CONSTANTS/data behind the foreclosing evidence — re-derive any "
                     "numeric predicate and name the constant + its AUTHORITATIVE source. This is "
                     "where a wrong code constant (e.g. a 2x CPT-minute error) gets caught before it "
                     "props up a bogus foreclosure."),
    )
    residual: str = Field("", description="What remains unexplained if PARTIAL/LIVE.")
    closure_owner: str = Field(
        "",
        description="How it closes: 'closed_on_record' | 'discovery:<item>' | 'cannot_close_pre_filing'.",
    )


class ForeclosureLedgerOutput(BaseModel):
    """Guardrail 3 output — the Innocent-Explanation Foreclosure Ledger."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    explanations: list[InnocentExplanation] = Field(default_factory=list)
    fraud_is_more_plausible_inference: bool = Field(
        ...,
        description=("The plausibility standard: across the ledger, is fraud the MORE plausible "
                     "inference — every obvious innocent explanation foreclosed, or only "
                     "low-plausibility ones left LIVE? NOT a 'zero conceivable alternative' bar."),
    )
    max_live_plausibility: float = Field(
        0.0, ge=0.0, le=1.0,
        description=("Highest plausibility_unrebutted among LIVE explanations (0 if none LIVE). "
                     "The orchestrator RE-derives this from the rows deterministically; this "
                     "self-report is a cross-check."),
    )
    overall_assessment: str


# ─────────────────────────────────────────────────────────────────────────────
# Case writer (Layer 5)
# ─────────────────────────────────────────────────────────────────────────────


class CaseWriterOutput(BaseModel):
    """The case writer's assembled prose deliverables (markdown contents).

    ``final_state`` is intentionally NOT here. ``03_Full_State.json`` is collated
    deterministically by the orchestrator straight from ``CaseState.model_dump()``
    — having the model emit it was wasteful AND lossy (it retyped a ~240 KB state
    down to ~5.6 KB). The full raw reason-trace lives in
    ``04_Full_Investigation_Bundle.json`` (also orchestrator-collated, not model-
    generated).
    """

    model_config = ConfigDict(extra="forbid")

    case_id: str
    case_summary: str = Field(..., description="00_README.md contents")
    full_case_report: str = Field(..., description="01_Case_Report.md contents")
    layperson_summary: str = Field(..., description="02_Layperson_Summary.md contents")


# ─────────────────────────────────────────────────────────────────────────────
# Orchestrator-internal state
# ─────────────────────────────────────────────────────────────────────────────


class FinalVerdict(str, Enum):
    SETTLEMENT_READY = "SETTLEMENT_READY"
    # Survived the adversarial cap with retained counts at a viable but discount-prone EVF
    # (0.20 ≤ EVF < 0.40 → evf_recommendation == "FILE_WITH_DEPS"). Distinct from
    # INDEFENSIBLE_AT_CAP, which now means strictly "survived to cap but EVF below the 0.20
    # abandon floor." Prevents the label from colliding with the genuinely-non-viable path.
    FILE_WITH_DISCOVERY_DEPS = "FILE_WITH_DISCOVERY_DEPS"
    NOT_ACTIONABLE = "NOT_ACTIONABLE"
    INDEFENSIBLE_AT_CAP = "INDEFENSIBLE_AT_CAP"
    PENDING_REBUTTAL_CAP = "PENDING_REBUTTAL_CAP"
    COUNSEL_ABANDONED = "COUNSEL_ABANDONED"
    # Iteration-cap action-space limitation, NOT a merits decline: the
    # convergence floor was actually MET (retained counts clear the
    # discrimination + damages thresholds) but Phase B's disposition action
    # space at the iteration cap only allows NEED_MORE_INVESTIGATION (coerced
    # to ABANDON_CASE, since Phase A is closed) or a READY-class action the
    # model didn't assert. Distinct from COUNSEL_ABANDONED — this case is
    # filable, just capped on iterations, not on merits (CASE-6A3014F0: model's
    # own reasoning said "a competent qui tam firm would file under seal
    # today" while the mechanical coercion forced ABANDON_CASE).
    PARKED = "PARKED"
    # Terminal "the loop got stuck (max-iter / spend-limit / agent error) — a human
    # should look." Consumed by _finalize as status="complete"; NOT resumable.
    NEEDS_HUMAN = "NEEDS_HUMAN"
    # RESUMABLE pause: the loop was about to make a dispositive, case-killing call on a
    # premise it CANNOT verify (e.g. is a sealed prior case a qui tam? is the LIVE innocent
    # explanation real?). Instead of guessing, it pauses with a structured ReviewRequest,
    # exits cleanly (exit 0, no dossier), and waits for a human answer to be injected
    # (scripts/answer_checkpoint.py) before resuming. Distinct from terminal NEEDS_HUMAN so
    # _finalize / _reconcile_status_from_disk don't collapse it to complete/abandoned.
    NEEDS_HUMAN_CHECKPOINT = "NEEDS_HUMAN_CHECKPOINT"


class ReviewRequest(BaseModel):
    """A human-in-the-loop checkpoint: a specific question the loop must NOT answer
    itself because it would resolve a dispositive call on an unverifiable premise.

    Raised by ``checkpoint.needs_human_checkpoint`` at a dispositive seam (Phase B
    disposition for a jurisdiction bar; Phase C foreclosure for a LIVE innocent
    explanation). Persisted on ``CaseState.review_request``; surfaced to a human, who
    answers via ``scripts/answer_checkpoint.py``. One open checkpoint at a time.
    """

    model_config = ConfigDict(extra="forbid")

    case_id: str
    checkpoint_type: Literal[
        "jurisdiction_bar_unverifiable",
        "foreclosure_live_explanation",
    ]
    question: str = Field(..., description="The plain-language question for the human.")
    unverifiable_premise: str = Field(
        ..., description="The dispositive premise the loop cannot verify on its own.")
    cited_evidence_id: str = Field(
        "", description="The evidence id / explanation id the premise rests on.")
    why_unverifiable: str = Field(
        ..., description="Why the loop cannot settle this from available data (e.g. sealed docket).")
    provisional_verdict_if_yes: str = Field(
        ..., description="What the verdict becomes if the human confirms the premise.")
    provisional_verdict_if_no: str = Field(
        ..., description="What the verdict becomes if the human refutes the premise.")
    what_would_resolve_it: str = Field(
        ..., description="The concrete fact/document a human could supply to resolve it.")
    raised_at: datetime
    resolved: bool = False
    human_answer: str | None = None
    resolved_at: datetime | None = None
    resolution: Literal["affirm_decline", "foreclose_explanation"] | None = Field(
        None,
        description=(
            "Set when resolved: the human's DIRECTION on the provisional verdict — "
            "'affirm_decline' (provisional_verdict_if_yes stands, e.g. the innocent "
            "explanation holds) or 'foreclose_explanation' (provisional_verdict_if_no "
            "applies — the premise is refuted and the loop should re-decide on the "
            "merits). Lets scripts/answer_checkpoint.py's resume rewind commit an "
            "affirmed foreclosure decline deterministically instead of clearing the "
            "ledger and re-deriving the same answer from a fresh, non-deterministic "
            "agent call (the confirm-decline infinite-loop bug)."
        ),
    )


class IterationRecord(BaseModel):
    """One iteration of the main loop.

    Two phases share this record type:
    - phase="build" (Phase A): investigator + counsel_build + counsel_first_chair_review.
      Loops up to MAX_PRE_REBUTTAL_ITERS times; the first-chair judgement decides
      whether to keep building, drop counts, proceed to rebuttal, or abandon.
    - phase="rebuttal_closing" (Phase B): counsel_rebuttal + investigator +
      counsel_build + counsel_disposition. Fires exactly once after Phase A
      exits via PROCEED_TO_REBUTTAL. Disposition is the final file/abandon
      decision.
    """
    iteration: int
    timestamp: datetime
    phase: Literal["build", "rebuttal_closing"] = "build"
    investigator: InvestigatorOutput | None = None
    counsel_build: CounselBuildOutput | None = None
    counsel_first_chair_review: CounselFirstChairReviewOutput | None = None
    counsel_rebuttal: CounselRebuttalOutput | None = None
    counsel_disposition: CounselDispositionOutput | None = None


class AdversarialRoundRecord(BaseModel):
    round: int
    timestamp: datetime
    defense: DefenseMotion | None = None
    legal_adv_response: LegalAdversarialResponse | None = None
    rebuttal_investigator: InvestigatorOutput | None = None


class CaseState(BaseModel):
    """Top-level state object persisted to state/{case_id}/summary.json."""

    case_id: str
    subject: str
    start_time: datetime
    last_updated: datetime
    status: str = "in_progress"
    final_verdict: FinalVerdict | None = None
    final_settlement_target_usd: str = ""
    final_cumulative_discount_pct: int = 0
    iterations: list[IterationRecord] = Field(default_factory=list)
    adversarial_rounds: list[AdversarialRoundRecord] = Field(default_factory=list)
    counts_retained: list[str] = Field(default_factory=list)
    counts_abandoned: list[str] = Field(default_factory=list)
    all_evidence_ids: list[str] = Field(default_factory=list)
    report_paths: dict[str, str] = Field(default_factory=dict)
    # ── Discrimination upgrades (Upgrade 1 + 2) ──────────────────────────
    strength_score: float | None = Field(
        default=None,
        description=(
            "Computed case-strength score (0.0-1.0) from fraud_investigator.scoring. "
            "Set at Phase B end (provisional, no discount tax) and re-set after "
            "Phase C adversarial loop (final, with discount tax)."
        ),
    )
    strength_bucket: str | None = Field(
        default=None,
        description="Bucketed strength: 'STRONG' | 'MODERATE' | 'WEAK' | 'ABANDON'.",
    )
    strength_components: dict[str, float] | None = Field(
        default=None,
        description=(
            "Per-component breakdown of the strength score (factual_density, "
            "n_counts, regulatory, venue_precedent, same_district, within_practice, "
            "survival_residual, damages, discount_tax). Persisted for transparency "
            "and downstream auditing."
        ),
    )
    expected_value_factor: float | None = Field(
        default=None,
        description=(
            "Upgrade 2 — EVF = survival × (1 - discount/100). Pre-recovery "
            "expected value as fraction of trebled-damages base. Used to gate "
            "FILE / FILE_WITH_DEPS / ABANDON at final disposition."
        ),
    )
    verified_precedents: list[str] = Field(
        default_factory=list,
        description=(
            "Citations already verified in prior iterations. Passed into "
            "subsequent counsel_build / counsel_rebuttal / defense / "
            "legal_adv_response prompts so agents don't re-verify the same "
            "cites in every iteration (each subprocess has naive context). "
            "Deduplicated by case-insensitive string match on the citation."
        ),
    )
    numeric_audit: list[dict] = Field(
        default_factory=list,
        description=(
            "Guardrail 2 — deterministic re-derivation of the case's headline "
            "quantitative claims (scripts/audit_case_numerics.py) from canonical "
            "constants. One finding per numeric_predicate: "
            "{id, kind, status (MATCH|MISMATCH|UNVERIFIABLE), claimed, recomputed, "
            "rel_error, constants_used, detail}. Computed before the defense stage "
            "and injected into the defense prompt; any MISMATCH is an authoritative "
            "integrity defect independent of whether the LLM defense raises it."
        ),
    )
    foreclosure_ledger: dict | None = Field(
        default=None,
        description=(
            "Guardrail 3 — the Innocent-Explanation Foreclosure Ledger "
            "(ForeclosureLedgerOutput dump). One row per innocent explanation, "
            "steelmanned then sought-to-be-foreclosed with cited/audited evidence. "
            "The orchestrator re-derives max LIVE plausibility from the rows and "
            "caps strength / blocks a clean FILE when a high-plausibility innocent "
            "explanation remains LIVE."
        ),
    )
    verdict_rationale: str | None = Field(
        default=None,
        description=(
            "Plain-language one-line summary of WHY compute_verdict reached the "
            "final_verdict (the legal-predicate filing test). Surfaced in the dossier."
        ),
    )
    verdict_predicates: list[dict] = Field(
        default_factory=list,
        description=(
            "Per-predicate filing scorecard from compute_verdict: "
            "[{name, status (MET/DISCOVERY_DEPENDENT/FAILED), why}]. The legible "
            "replacement for the opaque strength score."
        ),
    )
    review_request: ReviewRequest | None = Field(
        default=None,
        description=(
            "Human-in-the-loop checkpoint (Guardrail-4). Set when the loop hit a "
            "dispositive call on an unverifiable premise and paused (final_verdict="
            "NEEDS_HUMAN_CHECKPOINT) instead of guessing. A human answers via "
            "scripts/answer_checkpoint.py, which injects a human_attested fact, marks "
            "this resolved, and resumes the run. NOTE: this field holds only the "
            "MOST RECENT checkpoint — a second checkpoint of a different type "
            "overwrites it. Use resolved_checkpoint_types for 'has this type already "
            "been answered?'."
        ),
    )
    resolved_checkpoint_types: list[str] = Field(
        default_factory=list,
        description=(
            "Every checkpoint_type a human has already answered on this case. "
            "review_request holds only the latest checkpoint, so a case failing two "
            "predicates (e.g. jurisdiction_clear AND innocent_foreclosed) used to "
            "ping-pong forever: answering the second overwrote the first, and the "
            "first's resolved-type guard stopped recognising its own resolution. "
            "Append-only; the Phase-C guard tests membership here."
        ),
    )
