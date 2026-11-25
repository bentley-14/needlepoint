"""Two-pass LLM extractor for case records.

Design (per :doc:`docs/AGENT_PLATFORM_SCOPE`):

  1. Cheap model (Sonnet 4.6) drafts a ``case`` dict from a raw
     DOJ/OIG/docket page, citing the source span for every field.
  2. Stronger model (Opus 4.7) re-reads the source and verifies each
     cited span actually supports the claim. Mismatches are rejected.
  3. :func:`lint` enforces internal consistency (dates, NPI format,
     tier/source coherence) before the record is persisted.

Status: skeleton with a working linter. The LLM calls are stubbed
and should be wired to the Anthropic SDK when the ingest loop is
activated. The research-spike cases in ``gold_set.jsonl`` were built
by hand; this module is what replaces that hand-building for the next
100+ cases.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Linter
# ---------------------------------------------------------------------------

# Case-ID slug pattern — accepts:
#   * standard hand-curated slugs:  "DOJ-2017-ginkgo-oncology"
#   * CL-prefixed RECAP/opinion IDs: "CL-cacd-2_20_cv_02538",
#                                    "CL-2018-trotter-anti-kickbac"
# Format: PREFIX-(any number of - or _ separated alphanumeric segments)
CASE_ID_RE = re.compile(r"^[A-Z]+(?:[-_][A-Za-z0-9_]+)+$")
NPI_RE = re.compile(r"^[0-9]{10}$")

# Active-years sanity bounds. The point is to catch transcription typos
# (e.g. active_years: [1923, 2014]), NOT to enforce CMS panel-window
# membership. Cases with conduct genuinely starting in the early 2000s
# are real — Stark / kickback rings often run 5-15 years before the
# enforcement action lands. Widen to a generous range.
PANEL_START_YEAR = 2000  # was 2013; observed corpus minimum is 2003
PANEL_END_YEAR = 2030    # was 2023; forward buffer for future cases

# Acceptable ISO-date prefixes for outcome.date — full date, year+month, or
# bare year. Hand-curated cases sometimes have only year-or-month precision.
# Validates month (01-12) and day (01-31) to catch transcription typos like
# "2017-13-01" without doing full date validation.
_OUTCOME_DATE_RE = re.compile(
    r"^\d{4}(?:-(0[1-9]|1[0-2])(?:-(0[1-9]|[12]\d|3[01]))?)?$"
)

# Source-kind allow-list for tier=A cases. The schema's `sources[].kind`
# enum has grown from 5 values to 32 across this project's lifetime
# (state_mfcu / state_medical_board / cia / gao_report / fca_annual_stats /
# court_opinion / mfcu_indictment / state_medicaid_excl + observed
# variants like doj_pr / doj_press / law360 / news / academic /
# hhs_alj_decision / oversight_gov / irs_ci / secondary / industry_news /
# legal_summary / fbi_press / state_ag). Tier A requires ANY public-record
# enforcement/news source — every kind below qualifies. The list is
# permissive on purpose: if a source kind is in the schema enum, it
# counts. The audit catches genuine quality issues elsewhere (NPPES
# specialty, NPI validity), not source-kind bookkeeping.
TIER_A_KINDS: set[str] = {
    # original 5
    "doj_press_release", "oig_enforcement", "fbi_press_release",
    "court_docket", "legal_news",
    # observed variants of doj_press_release
    "doj_pr", "doj_press",
    # observed variant of fbi_press_release
    "fbi_press",
    # USAO press releases
    "usao",
    # state-level enforcement
    "state_mfcu", "state_ag", "mfcu_indictment", "state_medical_board",
    "state_medicaid_excl",
    # court records (additional grain)
    "court_opinion",
    # other public-record sources
    "cia", "gao_report", "taf_db", "fca_annual_stats",
    # news / secondary
    "news", "law360", "industry_news", "legal_summary", "academic",
    "secondary", "oversight_gov",
    # agency / ALJ
    "hhs_alj_decision", "irs_ci",
}

# Tier C accepts every Tier-A kind.
TIER_C_KINDS: set[str] = set(TIER_A_KINDS)


def lint(case: dict[str, Any]) -> list[str]:
    """Return a list of internal-consistency errors. Empty list → OK.

    Catches the typical mistakes an LLM extractor makes: date out of
    range, ``active_years`` that don't bracket the outcome date, NPI
    string in the wrong shape, tier/source mismatch, etc. These are
    cheaper to check here than to learn from a downstream model-fit
    failure.
    """
    errs: list[str] = []

    if not CASE_ID_RE.match(case.get("case_id", "")):
        errs.append(f"case_id {case.get('case_id')!r} doesn't match slug pattern")

    active = case.get("active_years") or []
    if active and (min(active) < PANEL_START_YEAR or max(active) > PANEL_END_YEAR):
        errs.append(
            f"active_years {active} extends outside "
            f"{PANEL_START_YEAR}-{PANEL_END_YEAR} panel"
        )

    outcome = case.get("outcome") or {}
    odate = outcome.get("date", "") or ""
    out_year: int | None = None
    if odate:
        if not _OUTCOME_DATE_RE.match(odate):
            errs.append(f"outcome.date {odate!r} not ISO (YYYY, YYYY-MM, or YYYY-MM-DD)")
        else:
            try:
                out_year = int(odate[:4])
            except ValueError:
                errs.append(f"outcome.date {odate!r} year not parseable")
    # outcome.date precedes active_years is NOT a lint error — the corpus
    # has many cases where the conduct began years before formal indictment
    # date. Removed in 2026-04-27 audit (was generating false positives).

    for i, d in enumerate(case.get("defendants") or []):
        npi = d.get("npi")
        conf = d.get("npi_confidence")
        if npi and not NPI_RE.match(str(npi)):
            errs.append(f"defendants[{i}].npi {npi!r} not 10-digit")
        if conf == "high" and not npi:
            errs.append(f"defendants[{i}] confidence=high but no NPI")
        if conf == "corporate_na" and npi:
            errs.append(f"defendants[{i}] confidence=corporate_na but has NPI {npi}")

    tier = case.get("tier")
    sources = case.get("sources") or []
    kinds = {s.get("kind") for s in sources}
    if tier == "A" and not (kinds & TIER_A_KINDS):
        errs.append(f"tier=A requires a public-record source kind; got {kinds}")
    if tier == "C" and not (kinds & TIER_C_KINDS):
        errs.append(f"tier=C requires at least one source kind; got {kinds}")

    if not sources:
        errs.append("sources[] must be non-empty")

    return errs


# ---------------------------------------------------------------------------
# LLM extraction (stubs)
# ---------------------------------------------------------------------------

# Shared prompt fragment describing the defendant.org_name contract. Both
# the drafting pass and the verification pass reference this so the two
# models agree on when the field should be populated. Keep additive and
# minimal; expand only if downstream matchers need more structure.
DEFENDANT_ORG_NAME_PROMPT = """\
For each defendant, extract `defendant.org_name` (optional, string or null)
alongside `defendant.name`.

  * `name` is the natural person named as defendant (e.g. "Provider Hazel").
  * `org_name` is the specific legal entity the defendant owns, operates,
    founded, or works for, when the source explicitly names one. Examples:
      - "Provider Hazel (Group Buckeye)"
          -> name: "Provider Hazel", org_name: "Group Buckeye"
      - "medical director of Hospice Zelkova"
          -> name: <person>, org_name: "Hospice Zelkova"
      - "Dr. Jane Doe, a solo internist in Queens"
          -> name: "Jane Doe", org_name: null
  * Populate `org_name` ONLY when a specific legal entity is identified in
    the source text. If the press release only describes the defendant's
    role (e.g. "a physician") without naming an org, leave `org_name` null.
  * Do not infer or guess an org name from context — it must appear in the
    source span you cite.
  * For corporate-defendant rows (role=corporate), `name` and `org_name`
    may be identical (the entity IS the defendant).
"""


def draft_case_from_source(raw_text: str, url: str, model: str = "sonnet-4-6") -> dict[str, Any]:
    """First pass: cheap model drafts a case dict. STUB.

    Real implementation calls Anthropic Messages API with the v0 schema
    as a tool-use contract and the raw press-release / docket text as
    input. Returns the draft case plus per-field ``_source_spans``.

    The drafting prompt must include :data:`DEFENDANT_ORG_NAME_PROMPT` so
    the model also extracts ``defendant.org_name`` when the source names a
    legal entity (needed by the facility/corporate NPI matcher).
    """
    raise NotImplementedError("Wire to Anthropic SDK when ingest loop activates")


def verify_case(case: dict[str, Any], raw_text: str, model: str = "opus-4-7") -> tuple[dict[str, Any], list[str]]:
    """Second pass: stronger model verifies each cited span. STUB.

    Returns (possibly-corrected case, list of rejections). Caller drops
    the record if rejections is non-empty and the primary model can't
    re-ground the field.

    The verification prompt must also carry :data:`DEFENDANT_ORG_NAME_PROMPT`
    and reject any ``defendant.org_name`` that isn't literally supported by
    the cited span (no inferred or guessed org names).
    """
    raise NotImplementedError("Wire to Anthropic SDK when ingest loop activates")


# ---------------------------------------------------------------------------
# End-to-end
# ---------------------------------------------------------------------------

def extract_case(raw_text: str, url: str) -> dict[str, Any] | None:
    """Run draft → verify → lint. Returns the case dict, or None if rejected."""
    draft = draft_case_from_source(raw_text, url)
    verified, rejections = verify_case(draft, raw_text)
    if rejections:
        return None
    errs = lint(verified)
    if errs:
        return None
    return verified


def load_cases(path: Path | str) -> list[dict[str, Any]]:
    return [json.loads(l) for l in Path(path).read_text().splitlines() if l.strip()]
