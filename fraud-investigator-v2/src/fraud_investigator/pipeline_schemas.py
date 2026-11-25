"""Pydantic schemas for the operationalized pipeline agents.

Stage 4 (investigator/counsel/defense/case_writer) uses schemas.py.
This module holds the NEW agent schemas:
  - Stage 1 (rule_derivation), Stage 3 (triage)
  - Supporting agents: data_intake, data_auditor, pipeline_engineer,
    feature_engineer, corpus_curator

All follow the same conventions as schemas.py:
  - `model_config = ConfigDict(extra="forbid")` to catch agent mistakes
  - Field descriptions explain what the agent should produce
  - Free-form `rationale` fields to require the agent show its work
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ─────────────────────────────────────────────────────────────────────────────
# Stage 1 — Rule Derivation
# ─────────────────────────────────────────────────────────────────────────────


class RuleItem(BaseModel):
    """One rule in the derived agent filter."""

    model_config = ConfigDict(extra="forbid")

    feature: str = Field(..., description="Feature column name in features_full_{year}.parquet, e.g., 'partb_total_medicare_payment'")
    direction: Literal["low", "high"] = Field(..., description="'low' = drop rows where feature < threshold; 'high' = drop where feature > threshold")
    threshold: float = Field(..., description="Cutoff value for the rule")
    rationale: str = Field(..., min_length=20, description="Why this rule? What pattern does it filter? Should reference specific positive-vs-negative distribution observations.")


class PerSchemeStats(BaseModel):
    """Per-scheme positive preservation under the derived filter."""

    model_config = ConfigDict(extra="forbid")

    scheme: str
    total: int = Field(..., ge=0, description="Positives in score panel for this scheme")
    preserved: int = Field(..., ge=0, description="Positives that survive the filter")
    preserved_pct: float = Field(..., ge=0, le=100, description="100 * preserved / total")


class EvaluationStats(BaseModel):
    """Filter's evaluation on the 2022/2023 score panel."""

    model_config = ConfigDict(extra="forbid")

    score_year: int
    n_rows_total: int
    n_pos_total: int
    n_dropped: int
    drop_pct: float = Field(..., ge=0, le=100)
    pos_loss_pct: float = Field(..., ge=0, le=100)
    n_pos_preserved: int
    per_scheme: list[PerSchemeStats] = Field(default_factory=list)


class RuleDerivationOutput(BaseModel):
    """Final output of the Stage 1 rule derivation agent."""

    model_config = ConfigDict(extra="forbid")

    rules: list[RuleItem] = Field(..., min_length=1, description="The derived rules (≥1)")
    evaluation: EvaluationStats = Field(..., description="Filter's empirical performance")
    rationale_summary: str = Field(..., min_length=50, description="Overall design philosophy: why these rules together?")

    @field_validator("rules")
    @classmethod
    def _unique_features(cls, v: list[RuleItem]) -> list[RuleItem]:
        if len(v) != len({(r.feature, r.direction) for r in v}):
            raise ValueError("rules must be unique by (feature, direction)")
        return v


# ─────────────────────────────────────────────────────────────────────────────
# Stage 3 — Triage  (canonical; supersedes the reorder-only "rerank" agent)
# ─────────────────────────────────────────────────────────────────────────────

VERDICTS = ("HIGH_RISK", "MODERATE_RISK", "LOW_RISK", "DISMISS")
# Canonical archetypes = the 5 ranker heads (fraudlib/eval/archetypes.py
# ARCHETYPES minus OTHER_FRAUD), so triage verdicts route 1:1 onto the heads.
# Legacy lowercase values (pre-2026-06 runs) are normalized on parse — see
# _LEGACY_ARCHETYPE_MAP below.
ARCHETYPES = ("OPIOID_MILL", "BILLING_FRAUD", "KICKBACK",
              "UPCODING", "MEDICALLY_UNNECESSARY")

_LEGACY_ARCHETYPE_MAP = {
    "phantom_billing":        "BILLING_FRAUD",
    "npi_theft":              "BILLING_FRAUD",   # consolidated per archetypes.py
    "kickback":               "KICKBACK",
    "medically_unnecessary":  "MEDICALLY_UNNECESSARY",
    "opioid_diversion":       "OPIOID_MILL",
    "upcoding":               "UPCODING",
    "other":                  None,  # never observed in HIGH_RISK artifacts
}


class TriageItem(BaseModel):
    """One triaged NPI — a forensic verdict (exact Agent-Triage-v3 behavior)."""

    model_config = ConfigDict(extra="forbid")

    npi: str = Field(..., description="Zero-padded 10-digit NPI string")
    rank: int = Field(..., ge=1, description="ML rank by score_rrf in scored_full_*.parquet (verbatim from input)")
    verdict: Literal["HIGH_RISK", "MODERATE_RISK", "LOW_RISK", "DISMISS"] = Field(
        ..., description="Forensic verdict tier")
    confidence: float = Field(..., ge=0.0, le=1.0, description="Confidence in the verdict, 0-1")
    matched_archetype: Literal[
        "OPIOID_MILL", "BILLING_FRAUD", "KICKBACK",
        "UPCODING", "MEDICALLY_UNNECESSARY"
    ] | None = Field(None, description="Ranker archetype head; REQUIRED when verdict=HIGH_RISK, else null")
    signals_detected: list[str] = Field(
        default_factory=list,
        description="snake_case named signals that drove the verdict; >=1 required if HIGH_RISK/MODERATE_RISK")
    reasoning: str = Field(..., min_length=100,
        description="3-5 sentences; MUST cite >=2 numbers from the record AND name the provider's exact specialty")
    specialty: str = Field(..., description="Provider specialty, verbatim from input (do NOT group)")
    follow_up_questions: list[str] = Field(default_factory=list)

    @field_validator("npi")
    @classmethod
    def _zero_padded(cls, v: str) -> str:
        if not v.isdigit() or len(v) != 10:
            raise ValueError(f"npi must be 10 digits, got {v!r}")
        return v

    @field_validator("matched_archetype", mode="before")
    @classmethod
    def _normalize_legacy_archetype(cls, v):
        """Map legacy lowercase archetype values (pre-canonical runs) onto
        the 5 ranker heads so old parsed.json / queue artifacts re-validate."""
        if isinstance(v, str) and v in _LEGACY_ARCHETYPE_MAP:
            return _LEGACY_ARCHETYPE_MAP[v]
        return v

    @model_validator(mode="after")
    def _high_risk_requirements(self) -> "TriageItem":
        if self.verdict == "HIGH_RISK" and not self.matched_archetype:
            raise ValueError("matched_archetype is required when verdict=HIGH_RISK")
        if self.verdict in ("HIGH_RISK", "MODERATE_RISK") and not self.signals_detected:
            raise ValueError(f"signals_detected must be non-empty when verdict={self.verdict}")
        return self


class TriageOutput(BaseModel):
    """Output of one triage-agent batch (a slice of the top-K)."""

    model_config = ConfigDict(extra="forbid")

    score_year: int
    k_range: tuple[int, int] = Field(..., description="Inclusive (lo, hi) rank range processed in this batch")
    items: list[TriageItem] = Field(..., min_length=1)
    batch_summary: str = Field(..., min_length=50, description="What patterns did the agent observe across this batch?")

    @field_validator("k_range")
    @classmethod
    def _valid_range(cls, v: tuple[int, int]) -> tuple[int, int]:
        if v[0] < 1 or v[1] < v[0]:
            raise ValueError(f"k_range must be (lo>=1, hi>=lo), got {v}")
        return v


# ─────────────────────────────────────────────────────────────────────────────
# Supporting agent — Data Intake
# ─────────────────────────────────────────────────────────────────────────────


class DataSourceStatus(BaseModel):
    """Per-source freshness summary."""

    model_config = ConfigDict(extra="forbid")

    have_years: list[int] = Field(default_factory=list, description="Years already on disk")
    next_year_available: bool = Field(..., description="Is the next year available upstream?")
    next_year: int | None = None
    fetched_now: bool = Field(False, description="True if this run actually downloaded it")
    reason: str = Field("", description="Why we did or didn't fetch")


class DataIntakeOutput(BaseModel):
    """Output of the data_intake agent."""

    model_config = ConfigDict(extra="forbid")

    checked_at: str = Field(..., description="ISO 8601 UTC timestamp")
    cms_status: dict[str, DataSourceStatus] = Field(..., description="Per-source (partb, partd, dmepos) status")
    doj_status: dict = Field(..., description="last_scrape_at, days_since_last_scrape, recommendation")
    nppes_status: dict = Field(..., description="last_refresh_at, days_since_refresh, next_refresh_recommended_at")
    summary: str = Field(..., min_length=30, description="One-paragraph plain-English summary")


# ─────────────────────────────────────────────────────────────────────────────
# Supporting agent — Data Auditor
# ─────────────────────────────────────────────────────────────────────────────


class AuditFlag(BaseModel):
    """One flagged v9 row."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    npi: str = Field(..., description="Zero-padded 10-digit NPI")
    severity: Literal["low", "medium", "high"]
    issue: str = Field(..., min_length=10, description="What's wrong with this row")
    suggested_fix: str = Field(..., min_length=10, description="What the human reviewer should change")

    @field_validator("npi")
    @classmethod
    def _zero_padded(cls, v: str) -> str:
        if not v.isdigit() or len(v) != 10:
            raise ValueError(f"npi must be 10 digits, got {v!r}")
        return v


class DataAuditorOutput(BaseModel):
    """Output of the data_auditor agent."""

    model_config = ConfigDict(extra="forbid")

    audit_date: str = Field(..., description="ISO 8601 UTC timestamp")
    corpus_file: str = Field(..., description="Path to the v9 file audited")
    n_rows_audited: int = Field(..., ge=0)
    flagged: list[AuditFlag] = Field(default_factory=list)
    pass_count: int = Field(..., ge=0)
    fail_count: int = Field(..., ge=0)
    summary: str = Field(..., min_length=30, description="Top-level findings + recommendations")


# ─────────────────────────────────────────────────────────────────────────────
# Supporting agent — Pipeline Engineer
# ─────────────────────────────────────────────────────────────────────────────


class PipelineEngineerOutput(BaseModel):
    """Output of the pipeline_engineer agent (diagnoses a failed run)."""

    model_config = ConfigDict(extra="forbid")

    diagnosed_run_id: str
    failure_stage: str = Field(..., description="Which stage failed: rule_derivation / ml_training / triage / investigator")
    likely_cause: str = Field(..., min_length=20, description="Specific cause: OOM, CLI not in PATH, JSON parse error, etc.")
    evidence: list[str] = Field(..., min_length=1, description="Concrete evidence from logs/system state")
    remediation_steps: list[str] = Field(..., min_length=1, description="Specific actions to fix (the human enacts these)")
    can_resume: bool = Field(..., description="True if resume from the failed step is safe")
    summary: str = Field(..., min_length=30)


# ─────────────────────────────────────────────────────────────────────────────
# Supporting agent — Auto Patcher (Tier-1 self-healing)
# ─────────────────────────────────────────────────────────────────────────────


class AutoPatchProposal(BaseModel):
    """One proposed config-level fix for an allowlisted dict entry.

    The auto-patcher is INTENTIONALLY restricted to dict-literal edits in
    specific files (currently only TURN_BUDGET_BY_STEP in claude_p.py).
    The validator in agents/auto_patcher.py rejects any proposal whose
    (file_path, dict_name, key) tuple isn't on the allowlist or whose
    new_value falls outside the allowed bound.
    """

    model_config = ConfigDict(extra="forbid")

    file_path: str = Field(..., description="Repo-relative path, e.g., 'fraud-investigator-v2/src/fraud_investigator/claude_p.py'")
    dict_name: str = Field(..., description="The Python identifier for the dict being edited, e.g., 'TURN_BUDGET_BY_STEP'")
    key: str = Field(..., description="String key within the dict, e.g., 'rebuttal_investigator'")
    old_value: int | str = Field(..., description="Current value the agent observed")
    new_value: int | str = Field(..., description="Proposed value after patch")
    rationale: str = Field(..., min_length=30, description="Why this specific bump fixes the diagnosed failure")


class AutoPatcherOutput(BaseModel):
    """Output of the auto_patcher agent."""

    model_config = ConfigDict(extra="forbid")

    diagnosed_run_id: str = Field(..., description="The failed run this patcher targeted")
    proposals: list[AutoPatchProposal] = Field(
        default_factory=list,
        description="Proposed patches — empty if the failure is not auto-patchable"
    )
    summary: str = Field(..., min_length=20, description="One-paragraph summary of what was proposed (and why nothing was, if proposals is empty)")


# ─────────────────────────────────────────────────────────────────────────────
# Supporting agent — Data Repair (Tier-1 corpus self-healing)
# ─────────────────────────────────────────────────────────────────────────────


class DataRepairProposal(BaseModel):
    """One proposed correction for a flagged v9 corpus row.

    Mirrors AutoPatchProposal's shape but operates on JSONL rows in the
    v9 ground-truth corpus instead of dict literals in Python code.
    Validator restrictions are stricter because the corpus is the
    foundation of all downstream training + scoring — a bad row poisons
    every subsequent investigation.

    Field paths supported (Tier-1 allowlist):
      - "outcome.type"       (e.g., "indictment" → "settlement")
      - "outcome.amount_usd" (numeric correction, bounded)
      - "fraud_types"        (canonical-vocab subset replacement)
      - "all_active_years"   (TRIM ONLY — may only REMOVE years, never add)
    """

    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(..., min_length=4, description="Canonical v9 case_id (must exist in corpus)")
    field_path: str = Field(..., description="Dot-path to the field, e.g., 'outcome.type' or 'all_active_years'")
    old_value: int | float | str | list | None = Field(..., description="Value currently in the corpus (must match for apply to succeed)")
    new_value: int | float | str | list | None = Field(..., description="Proposed replacement value")
    source_evidence: str = Field(
        ..., min_length=40,
        description="URL or quoted excerpt from the original DOJ press release / court doc that supports the new_value",
    )
    severity: str = Field(..., description="Severity from the audit: low / medium / high")
    rationale: str = Field(..., min_length=30, description="Why this specific change resolves the flagged issue")


class DataRepairOutput(BaseModel):
    """Output of the data_repair agent."""

    model_config = ConfigDict(extra="forbid")

    audit_run_id: str = Field(..., description="The data_auditor run whose audit_report this targeted")
    proposals: list[DataRepairProposal] = Field(
        default_factory=list,
        description="Proposed corpus row corrections — empty if no rows are auto-repairable",
    )
    summary: str = Field(..., min_length=20)


# ─────────────────────────────────────────────────────────────────────────────
# Supporting agent — Feature Engineer
# ─────────────────────────────────────────────────────────────────────────────


class FeatureProposal(BaseModel):
    """One proposed new feature."""

    model_config = ConfigDict(extra="forbid")

    feature_name: str = Field(..., description="Snake_case name, e.g., 'hcpcs_skin_substitute_share'")
    formula: str = Field(..., min_length=10, description="Pseudo-code or pandas expression")
    rationale: str = Field(..., min_length=30, description="Why this catches the target scheme")
    expected_signal_range: str = Field(..., description="e.g., '0.0-1.0 with >0.5 anomalous in this specialty'")


class FeatureEngineerOutput(BaseModel):
    """Output of the feature_engineer agent."""

    model_config = ConfigDict(extra="forbid")

    target_scheme: str = Field(..., description="The fraud scheme the new features target")
    scheme_description: str = Field(..., min_length=30, description="Plain-English description of the scheme pattern")
    proposed_features: list[FeatureProposal] = Field(..., min_length=1, max_length=20)
    skeleton_script_path: str = Field(..., description="Where the agent wrote the skeleton compute script")
    summary: str = Field(..., min_length=30)


# ─────────────────────────────────────────────────────────────────────────────
# Supporting agent — Corpus Curator
# ─────────────────────────────────────────────────────────────────────────────


class ProposedCase(BaseModel):
    """One proposed new v9 row from a fresh DOJ press release."""

    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(..., description="Slug, e.g., 'DOJ-2026-newdefendant-name'")
    defendant_name: str
    defendant_npi_candidates: list[str] = Field(default_factory=list, description="NPPES-match candidates (zero-padded)")
    defendant_specialty: str = Field("", description="From NPPES taxonomy")
    defendant_state: str = Field("", description="2-letter")
    all_active_years: list[int] = Field(..., min_length=1)
    fraud_types: list[str] = Field(default_factory=list)
    outcome_type: Literal["indictment", "plea", "settlement", "judgment", "sentencing", "civil_judgment", "unknown"] = "unknown"
    outcome_date: str = Field(..., description="ISO YYYY-MM-DD")
    outcome_amount_usd: float | None = None
    source_url: str
    confidence: Literal["low", "medium", "high"]
    rationale: str = Field(..., min_length=20)
    active_years_source: Literal[
        "source_stated", "web_corroborated", "inferred", "outcome_fallback", "unknown"
    ] = Field("unknown", description=(
        "How all_active_years was grounded: explicit in the source / corroborated via "
        "web / inferred / outcome-year fallback. The deterministic gate routes "
        "'inferred' + 'outcome_fallback' (and the 'unknown' default) to HOLD — never "
        "auto-accept guessed conduct years."))


class CorpusCuratorOutput(BaseModel):
    """Output of the corpus_curator agent."""

    model_config = ConfigDict(extra="forbid")

    scraped_at: str
    n_press_releases_reviewed: int = Field(..., ge=0)
    proposed_cases: list[ProposedCase] = Field(default_factory=list)
    skipped_already_in_corpus: int = Field(..., ge=0)
    skipped_corporate_only: int = Field(..., ge=0, description="Cases with no individual NPI (an individual NPI is required)")
    skipped_other: int = Field(..., ge=0)
    summary: str = Field(..., min_length=30)
