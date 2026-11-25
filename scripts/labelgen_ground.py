#!/usr/bin/env python3
"""DETERMINISTIC (no LLM) grounding stage of the labelgen pipeline.

Turns labelgen_extract.py (Haiku) / labelgen_escalate.py (Sonnet) LLM label
extractions into NPPES-identity-resolved, surface-confirmed grounded
candidates for labelgen_confirm.py's Opus 3-gate review.

## ORIGIN of the identity/surface-confirm functions below

The functions in the "REUSED FROM mine_entity_labels.py" section (_retry,
OrgMatch, resolve_org_npi, extract_venue_state + its state-lookup helpers,
classify_venue_match, compute_identity_confidence, resolve_twin_candidates,
_norm_org_name, FacilityIndex, MedicaidSurface, MedicareSurface,
pick_label_class, load_gold_npis, load_existing_proposal_npis) are copied
VERBATIM from `scripts/mine_entity_labels.py` as it exists on branch
`feat/entity-labels` (the file lives at commit 1e8fb85; that PR is NOT yet
merged to `main` as of 2026-07-16, and this branch was cut from `main`, so
the module isn't importable here). Per the labelgen-pipeline brief: "Import
them if importable without heavy side effects; otherwise copy the exact
functions verbatim with an origin comment." mine_entity_labels.py's own
regex-based ENTITY-NAME EXTRACTION (extract_entity_candidates and the
disposition/amount regex classifiers) is REJECTED and is NOT reused here —
labelgen_extract.py / labelgen_escalate.py's LLM read replaces that step
entirely; only the identity-resolution + surface-confirm logic that runs
AFTER a name is already in hand is reused. When feat/entity-labels merges to
main, the copied block below should be deleted in favor of
`import mine_entity_labels`.

## Pipeline

1. Load the extractions file; overlay --escalated rows on top (an escalated
   row REPLACES the Haiku row for that uuid — see apply_escalation_overlay).
2. Filter to candidates: in_scope && is_resolved && label_class in
   {org, medicaid, facility} && primary_entity_name truthy
   (passes_ground_filter). Separately, in_scope && is_resolved &&
   label_class == "individual" candidates NOT already in gold_set are
   written to a side file for the existing curator flow — this pipeline
   NEVER lands individuals itself.
3. NPPES-resolve each candidate name to a Type-2 org (resolve_org_npi).
4. Group resolved candidates by _norm_org_name and collapse twins
   (resolve_twin_candidates) — the same real-world entity mentioned across
   multiple press releases (e.g. an indictment PR and a later plea PR)
   collapses to ONE row instead of one per release.
5. Dedupe by org_npi vs already-landed entity_labels.jsonl, vs gold_set, and
   vs candidates already emitted earlier in this same run.
6. Surface-confirm (facility parquet / Medicaid T-MSIS / Medicare Part B
   local panels) and pick_label_class (facility > medicaid > org precedence).

## Funnel counters (printed + included in the review .md)

n_in            rows considered (post escalation-overlay, post --limit)
n_filtered      rows passing passes_ground_filter (the candidate universe)
n_nppes_none    filtered candidates with no confident NPPES org match
n_dup           candidates dropped as a DUPLICATE of an entity already
                accounted for: a same-NPI twin re-mention, an
                ambiguous-multi-candidate group's non-winning members, an
                org_npi already in entity_labels.jsonl/gold_set, or an
                org_npi already emitted earlier in this run
n_venue_dropped candidates dropped specifically because a MULTI-candidate
                twin group had exactly one venue-match winner — the losing
                sibling's own geography affirmatively contradicts it being
                the right entity (distinct from a generic "already known" dup)
n_no_surface    resolved+deduped candidates with no facility/Medicaid/
                Medicare billing surface at all (pick_label_class -> None)
n_written       grounded candidates actually written to --out

Invariant: n_filtered == n_nppes_none + n_dup + n_venue_dropped + n_no_surface + n_written.

Usage:
    python scripts/labelgen_ground.py validation/curator/doj_haiku_extractions_2026-07-14.jsonl
    python scripts/labelgen_ground.py <extractions.jsonl> --escalated <extractions.jsonl>.escalated.jsonl
    python scripts/labelgen_ground.py <extractions.jsonl> --limit 40   # smoke test
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))        # sibling-script imports below
sys.path.insert(0, str(REPO_ROOT))         # fraudlib (needed transitively by check_billing_surface)

import doj_pr_lookup                       # noqa: E402  reused: LOCAL_CACHE
import nppes_lookup                        # noqa: E402  reused (via resolve_org_npi): _query
import check_billing_surface as billing_surface   # noqa: E402  reused (via MedicareSurface)
import medicaid_lookup                     # noqa: E402  reused (via MedicaidSurface): _connect, NPI_FILTER

GOLD_SET_PATH = REPO_ROOT / "data" / "extracted" / "gold_set.jsonl"
ENTITY_LABELS_PATH = REPO_ROOT / "data" / "extracted" / "entity_labels.jsonl"
FACILITY_FILES = {
    "hospice": REPO_ROOT / "data" / "processed" / "facility_hospice.parquet",
    "hha": REPO_ROOT / "data" / "processed" / "facility_hha.parquet",
    "snf": REPO_ROOT / "data" / "processed" / "facility_snf.parquet",
    "hospital": REPO_ROOT / "data" / "processed" / "facility_hospital.parquet",
}
# Medicare surface years: local partb parquet covers 2018-2024 (confirmed on disk);
# NP_R2_DISABLE=1 (set inside MedicareSurface) keeps this script from ever
# triggering a first-fetch multi-GB R2 pull for partd/dmepos.
MEDICARE_SURFACE_YEARS = [2018, 2019, 2020, 2021, 2022, 2023, 2024]


# =============================================================================
# REUSED FROM mine_entity_labels.py (feat/entity-labels @ 1e8fb85) — see the
# ORIGIN note in the module docstring above. Copied verbatim.
# =============================================================================

def _retry(fn: Callable[[], dict | requests.Response | None], *, tries: int = 3,
           backoff: float = 1.5, log=print, what: str = "request") -> Any:
    last_err = None
    for attempt in range(1, tries + 1):
        try:
            result = fn()
        except requests.RequestException as e:
            last_err = e
            result = None
        if result is not None and not (isinstance(result, dict) and result.get("_error")):
            return result
        if isinstance(result, dict):
            last_err = result.get("_error")
        if attempt < tries:
            time.sleep(backoff ** attempt)
    log(f"  ! {what} failed after {tries} tries: {last_err}")
    return None


@dataclass
class OrgMatch:
    npi: str
    nppes_org_name: str
    state: str | None
    match_ratio: float
    n_candidates: int
    confidence: str


def resolve_org_npi(candidate_name: str, *, log=print) -> OrgMatch | None:
    clean = re.sub(r"\s+", " ", candidate_name).strip(" ,.")
    res = _retry(lambda: nppes_lookup._query({"organization_name": clean, "limit": 20}),
                 log=log, what=f"NPPES org search {clean!r}")
    if not res:
        return None
    results = res.get("results") or []
    orgs = [p for p in results if p.get("enumeration_type") == "NPI-2"]
    if not orgs:
        return None
    scored = sorted(
        ((SequenceMatcher(None, clean.upper(),
                           ((p.get("basic") or {}).get("organization_name") or "").upper()).ratio(), p)
         for p in orgs),
        key=lambda t: t[0], reverse=True,
    )
    best_ratio, best = scored[0]
    if best_ratio < 0.55:
        return None
    second_ratio = scored[1][0] if len(scored) > 1 else 0.0
    addrs = best.get("addresses") or []
    practice = next((a for a in addrs if a.get("address_purpose") == "LOCATION"), addrs[0] if addrs else {})
    if best_ratio >= 0.85 and (best_ratio - second_ratio) >= 0.10:
        confidence = "high"
    elif best_ratio >= 0.65:
        confidence = "medium"
    else:
        confidence = "low"
    return OrgMatch(
        npi=str(best.get("number")),
        nppes_org_name=(best.get("basic") or {}).get("organization_name") or "",
        state=practice.get("state"),
        match_ratio=round(best_ratio, 3),
        n_candidates=len(orgs),
        confidence=confidence,
    )


# ---- Venue / geography (mine_entity_labels.py labeling-audit bug #2) ------
# resolve_org_npi above is a nationwide, geography-BLIND name-similarity
# search. The fix cross-checks the case's own VENUE (from the release URL's
# usao-<slug> or body "District of X" prose) against whatever state NPPES
# returned, rather than trusting the name match alone.

_USAO_DISTRICT_STATE = {
    # multi-district states: usao-<direction><state> slug -> state
    "ndal": "AL", "mdal": "AL", "sdal": "AL",
    "edar": "AR", "wdar": "AR",
    "ndca": "CA", "edca": "CA", "cdca": "CA", "sdca": "CA",
    "ndfl": "FL", "mdfl": "FL", "sdfl": "FL",
    "ndga": "GA", "mdga": "GA", "sdga": "GA",
    "ndil": "IL", "cdil": "IL", "sdil": "IL",
    "ndin": "IN", "sdin": "IN",
    "ndia": "IA", "sdia": "IA",
    "edky": "KY", "wdky": "KY",
    "edla": "LA", "mdla": "LA", "wdla": "LA",
    "edmi": "MI", "wdmi": "MI",
    "ndms": "MS", "sdms": "MS",
    "edmo": "MO", "wdmo": "MO",
    "ndny": "NY", "sdny": "NY", "edny": "NY", "wdny": "NY",
    "ednc": "NC", "mdnc": "NC", "wdnc": "NC",
    "ndoh": "OH", "sdoh": "OH",
    "ndok": "OK", "edok": "OK", "wdok": "OK",
    "edpa": "PA", "mdpa": "PA", "wdpa": "PA",
    "edtn": "TN", "mdtn": "TN", "wdtn": "TN",
    "ndtx": "TX", "sdtx": "TX", "edtx": "TX", "wdtx": "TX",
    "edva": "VA", "wdva": "VA",
    "edwa": "WA", "wdwa": "WA",
    "ndwv": "WV", "sdwv": "WV",
    "edwi": "WI", "wdwi": "WI",
    # single-district states/territories: usao-<state> slug -> state
    "ak": "AK", "az": "AZ", "co": "CO", "ct": "CT", "de": "DE", "dc": "DC",
    "gu": "GU", "hi": "HI", "id": "ID", "ks": "KS", "me": "ME", "md": "MD",
    "ma": "MA", "mn": "MN", "mt": "MT", "ne": "NE", "nv": "NV", "nh": "NH",
    "nj": "NJ", "nm": "NM", "nd": "ND", "or": "OR", "pr": "PR", "ri": "RI",
    "sc": "SC", "sd": "SD", "ut": "UT", "vt": "VT", "vi": "VI", "wy": "WY",
    "nmi": "NMI",
}
_USAO_SLUG_RE = re.compile(r"justice\.gov/usao-([a-z]+)/", re.I)

_STATE_NAME_TO_ABBR = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI",
    "wyoming": "WY", "columbia": "DC", "puerto rico": "PR", "guam": "GU",
    "virgin islands": "VI",
}
_DISTRICT_OF_RE = re.compile(r"District of ([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\b")


def _map_district_name_to_state(name: str) -> str | None:
    name = re.sub(r"\s+", " ", name).strip().lower()
    if name in _STATE_NAME_TO_ABBR:
        return _STATE_NAME_TO_ABBR[name]
    return _STATE_NAME_TO_ABBR.get(name.split(" ", 1)[0])


def _prose_venue_state(body_text: str) -> str | None:
    """'District of X' mentions in the body -- only trusted when every
    district named agrees on ONE state (a nationwide/multi-district release
    naming several different districts must fall back to unknown, not a
    guess)."""
    found = {st for m in _DISTRICT_OF_RE.finditer(body_text or "")
             if (st := _map_district_name_to_state(m.group(1)))}
    return next(iter(found)) if len(found) == 1 else None


def extract_venue_state(release: dict) -> str | None:
    """Best-effort case VENUE state, independent of the NPPES org's own
    address. DOJ releases are pinned by their usao-<slug> URL (a federal
    district never crosses state lines); body prose ("District of X") is a
    last-resort fallback for releases without a usao- slug. Returns None
    when nothing reliable is available -- never a guess."""
    source = release.get("source") or ""
    if source == "ny_omig":
        return "NY"
    if source == "ca_oag":
        return "CA"
    m = _USAO_SLUG_RE.search(release.get("url") or "")
    if m:
        state = _USAO_DISTRICT_STATE.get(m.group(1).lower())
        if state:
            return state
    return _prose_venue_state(release.get("body") or "")


def classify_venue_match(venue_state: str | None, org_state: str | None) -> str:
    """match/mismatch/unknown -- 'unknown' whenever either side is missing,
    never a forced guess."""
    if not venue_state or not org_state:
        return "unknown"
    return "match" if venue_state.upper() == org_state.upper() else "mismatch"


_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}
_RANK_TO_CONFIDENCE = {v: k for k, v in _CONFIDENCE_RANK.items()}


def compute_identity_confidence(name_confidence: str, venue_match: str, *, ambiguous: bool = False) -> str:
    """Overall confidence that org_npi is the RIGHT entity: name-similarity
    confidence (from resolve_org_npi), demoted one notch when geography
    contradicts it, forced to "low" when the candidate only survived a
    twin-NPI tie-break with no clear venue winner."""
    if ambiguous:
        return "low"
    rank = _CONFIDENCE_RANK.get(name_confidence, 0)
    if venue_match == "mismatch":
        rank = max(0, rank - 1)
    return _RANK_TO_CONFIDENCE[rank]


# ---- Twin-NPI resolution (mine_entity_labels.py labeling-audit bug #4) ----
# The SAME underlying defendant can independently resolve via NPPES to its
# OWN best-scoring org in each of several release mentions -- sometimes a
# genuinely different real NPI in a different state. resolve_twin_candidates
# is the pure decision function: a single distinct NPI in the group is a
# non-issue (just spelling variants, or the same entity mentioned in an
# indictment PR and a later plea/sentencing PR); >=2 distinct NPIs is a
# genuine twin, resolved by preferring whichever candidate's own venue
# corroborates its NPPES state -- and if venue can't disambiguate either,
# exactly ONE row is still emitted, flagged ambiguous_multi_candidate with
# every candidate listed, rather than silently proposing more than one guess
# for the same real-world entity.

def resolve_twin_candidates(
    resolved: list[tuple[str, OrgMatch, dict]],
) -> tuple[str, OrgMatch, dict, str | None, list[dict] | None]:
    """resolved: [(candidate_name, OrgMatch, release), ...] that all
    normalized to the SAME entity name. Returns (name, match, release,
    identity_flag, identity_candidates) for the ONE row to emit."""
    distinct_npis = {m.npi for _, m, _ in resolved}
    if len(distinct_npis) == 1:
        name, match, rel = max(resolved, key=lambda t: t[1].match_ratio)
        return name, match, rel, None, None

    venue_hits = [
        (name, match, rel, classify_venue_match(extract_venue_state(rel), match.state))
        for name, match, rel in resolved
    ]
    winners = [t for t in venue_hits if t[3] == "match"]
    if len(winners) == 1:
        name, match, rel, _ = winners[0]
        return name, match, rel, None, None

    name, match, rel = max(resolved, key=lambda t: t[1].match_ratio)
    identity_candidates = [
        {"npi": m.npi, "state": m.state, "match_ratio": m.match_ratio} for _, m, _ in resolved
    ]
    return name, match, rel, "ambiguous_multi_candidate", identity_candidates


def load_gold_npis(path: Path = GOLD_SET_PATH) -> set[str]:
    npis: set[str] = set()
    if not path.exists():
        return npis
    with path.open() as f:
        for line in f:
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("npi"):
                npis.add(str(d["npi"]))
    return npis


def load_existing_proposal_npis(path: Path) -> set[str]:
    """NPIs already written to an org_npi-keyed output file (entity_labels.jsonl
    or a prior grounded-candidates run) -- the dedupe/skip set."""
    npis: set[str] = set()
    if not path.exists():
        return npis
    with path.open() as f:
        for line in f:
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("org_npi"):
                npis.add(str(d["org_npi"]))
    return npis


def _norm_org_name(name: str) -> str:
    name = re.sub(r"[.,]", "", name).upper()
    name = re.sub(r"\b(INC|LLC|CORP|CORPORATION|CO|COMPANY|LLP|PLLC|PC|LTD|INCORPORATED)\b", "", name)
    return re.sub(r"\s+", " ", name).strip()


class FacilityIndex:
    """All four facility rosters loaded once; org_name+state fuzzy lookup."""

    def __init__(self):
        import pandas as pd
        self.by_type: dict[str, "pd.DataFrame"] = {}
        for kind, path in FACILITY_FILES.items():
            if path.exists():
                df = pd.read_parquet(path, columns=["ccn", "org_name", "state"])
                df["_norm"] = df["org_name"].fillna("").map(_norm_org_name)
                self.by_type[kind] = df

    def match(self, candidate_name: str, state: str | None, *, threshold: float = 0.82):
        norm = _norm_org_name(candidate_name)
        best = None  # (ratio, kind, ccn, org_name)
        for kind, df in self.by_type.items():
            subset = df[df["state"] == state] if state else df
            for _, row in subset.iterrows():
                ratio = SequenceMatcher(None, norm, row["_norm"]).ratio()
                if best is None or ratio > best[0]:
                    best = (ratio, kind, row["ccn"], row["org_name"])
        if best and best[0] >= threshold:
            return {"facility_type": best[1], "ccn": best[2], "org_name": best[3], "ratio": round(best[0], 3)}
        return None


class MedicaidSurface:
    """One duckdb connection, reused for every candidate's point query."""

    def __init__(self):
        self.con = None
        self.table = None
        try:
            self.con, self.table = medicaid_lookup._connect()
        except FileNotFoundError:
            self.con = None

    def has_surface(self, npi: str) -> bool:
        if self.con is None:
            return False
        row = self.con.execute(
            f"SELECT 1 FROM {self.table} WHERE {medicaid_lookup.NPI_FILTER} "
            f"AND BILLING_PROVIDER_NPI_NUM = ? LIMIT 1", [npi]).fetchone()
        return row is not None


class MedicareSurface:
    """Loaded once (local panels only — NP_R2_DISABLE=1 avoids a surprise R2 pull)."""

    def __init__(self, years: list[int] = MEDICARE_SURFACE_YEARS):
        import os
        prior = os.environ.get("NP_R2_DISABLE")
        os.environ["NP_R2_DISABLE"] = "1"
        try:
            self.surf, self.no_panel = billing_surface.load_surfaces("data/panels", years)
        finally:
            if prior is None:
                os.environ.pop("NP_R2_DISABLE", None)
            else:
                os.environ["NP_R2_DISABLE"] = prior
        self.years = years

    def has_surface(self, npi: str) -> tuple[bool, dict]:
        v, hits, _ = billing_surface.verdict_for(npi, self.years, self.surf, self.no_panel)
        return v == "SURFACE_FOUND", hits


def pick_label_class(facility_hit, medicaid_hit: bool, medicare_hit: tuple[bool, dict]) -> tuple[str, str] | None:
    """Precedence facility > medicaid > org (Medicare-only). Returns
    (label_class, surface_confirmed description) or None if no surface."""
    if facility_hit:
        return "facility", f"facility ({facility_hit['facility_type']}, CCN {facility_hit['ccn']})"
    if medicaid_hit:
        return "medicaid", "medicaid (T-MSIS billing NPI match)"
    found, hits = medicare_hit
    if found:
        detail = ", ".join(f"{prog}={yrs}" for prog, yrs in hits.items())
        return "org", f"medicare ({detail})"
    return None


# =============================================================================
# labelgen_ground.py's OWN logic (not from mine_entity_labels.py)
# =============================================================================

def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with path.open() as f:
        for line in f:
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def apply_escalation_overlay(rows: list[dict], escalated_rows: list[dict]) -> list[dict]:
    """An escalated row REPLACES the Haiku row for the same uuid, in place —
    original file order is preserved (escalated rows are always a subset of
    the base rows by uuid)."""
    by_uuid: dict[str, dict] = {}
    order: list[str] = []
    for r in rows:
        u = r.get("uuid")
        if u is None:
            continue
        by_uuid[u] = r
        order.append(u)
    for er in escalated_rows:
        u = er.get("uuid")
        if u and "extract" in er and u in by_uuid:
            by_uuid[u] = er
    return [by_uuid[u] for u in order]


def passes_ground_filter(extract: dict | None) -> bool:
    """Candidate universe for org/Medicaid/facility grounding: in_scope,
    resolved, a labelable org-ish class, and an actual name to resolve."""
    if not isinstance(extract, dict):
        return False
    if not extract.get("in_scope"):
        return False
    if not extract.get("is_resolved"):
        return False
    if extract.get("label_class") not in ("org", "medicaid", "facility"):
        return False
    return bool((extract.get("primary_entity_name") or "").strip())


def is_individual_candidate(extract: dict | None) -> bool:
    """Side-channel selection for individuals_for_curator_<date>.jsonl — this
    pipeline never lands individuals itself; these are proposals for the
    existing curator flow."""
    if not isinstance(extract, dict):
        return False
    if not extract.get("in_scope"):
        return False
    if not extract.get("is_resolved"):
        return False
    if extract.get("label_class") != "individual":
        return False
    return bool((extract.get("primary_entity_name") or "").strip())


def _unix_to_iso_date(raw_date: Any) -> str | None:
    if not raw_date:
        return None
    try:
        return datetime.date.fromtimestamp(int(raw_date)).isoformat()
    except (ValueError, TypeError, OSError, OverflowError):
        return None


def to_release(row: dict, raw_cache: dict) -> dict:
    """Build the {source, url, body} dict extract_venue_state/
    resolve_twin_candidates expect, carrying the original row + raw date
    along as extra (harmless) keys for the final row-builder to read back."""
    raw = raw_cache.get(row.get("uuid"), {}) or {}
    return {
        "source": "doj",
        "url": row.get("url", "") or "",
        "body": raw.get("body", "") or "",
        "_row": row,
        "_raw_date": raw.get("date"),
    }


def build_grounded_row(name: str, match: OrgMatch, rel: dict, *, label_class: str,
                        surface_confirmed: str, identity_flag: str | None,
                        identity_candidates: list[dict] | None) -> dict:
    orig_row = rel["_row"]
    extract = orig_row.get("extract") or {}
    venue_state = extract_venue_state(rel)
    venue_match = classify_venue_match(venue_state, match.state)
    identity_confidence = compute_identity_confidence(
        match.confidence, venue_match, ambiguous=bool(identity_flag))
    return {
        "label_class": label_class,
        "defendant_name": name,
        "org_npi": match.npi,
        "state": match.state,
        "scheme_summary": (extract.get("scheme_summary") or "")[:480],
        "source_url": orig_row.get("url", ""),
        "disposition": {
            "type": extract.get("disposition"),
            "date": _unix_to_iso_date(rel.get("_raw_date")),
            "amount_usd": extract.get("amount_usd"),
        },
        # Always "resolved" here by construction (passes_ground_filter already
        # required is_resolved==True) — this reflects the upstream LLM
        # extraction's claim; labelgen_confirm.py's G1 gate independently
        # re-verifies disposition against the public record.
        "disposition_finality": "resolved",
        "venue_match": venue_match,
        "surface_confirmed": surface_confirmed,
        "dedupe_checked": True,
        "confidence": match.confidence,
        "identity_confidence": identity_confidence,
        "identity_flag": identity_flag,
        "identity_candidates": identity_candidates,
        "nppes_org_name": match.nppes_org_name,
        "source": "doj",
        "uuid": orig_row.get("uuid"),
    }


def build_individual_row(row: dict) -> dict:
    extract = row.get("extract") or {}
    return {
        "uuid": row.get("uuid"),
        "defendant_name": extract.get("primary_entity_name", ""),
        "all_defendants": extract.get("all_defendants", []),
        "payer": extract.get("payer"),
        "disposition": extract.get("disposition"),
        "amount_usd": extract.get("amount_usd"),
        "scheme_summary": extract.get("scheme_summary"),
        "source_url": row.get("url", ""),
        "confidence": extract.get("confidence"),
    }


def render_md(candidates: list[dict], funnel: dict, *, generated: str) -> str:
    lines = [
        f"# Grounded org/Medicaid/facility label candidates — {generated}",
        "",
        "Deterministic NPPES-identity + surface-confirm grounding of "
        "labelgen_extract.py (Haiku) / labelgen_escalate.py (Sonnet) LLM label "
        "extractions, reusing scripts/mine_entity_labels.py's identity/"
        "surface-confirm logic (see labelgen_ground.py's ORIGIN note). "
        "**Proposal-only — NOT a gold_set/entity_labels write.** For "
        "labelgen_confirm.py's Opus 3-gate review.",
        "",
        "## Funnel",
        f"- n_in: {funnel['n_in']}",
        f"- n_filtered: {funnel['n_filtered']}",
        f"- n_nppes_none: {funnel['n_nppes_none']}",
        f"- n_dup: {funnel['n_dup']}",
        f"- n_venue_dropped: {funnel['n_venue_dropped']}",
        f"- n_no_surface: {funnel['n_no_surface']}",
        f"- n_written: {funnel['n_written']}",
        "",
        f"## Candidates ({len(candidates)})",
        "",
    ]
    by_class: dict[str, list[dict]] = {}
    for c in candidates:
        by_class.setdefault(c["label_class"], []).append(c)
    for label_class in ("org", "medicaid", "facility"):
        rows = by_class.get(label_class, [])
        if not rows:
            continue
        lines.append(f"### {label_class} ({len(rows)})")
        for c in rows:
            excerpt = (c.get("scheme_summary") or "")[:200]
            lines.append(
                f"- `{c['org_npi']}` **{c['defendant_name']}** ({c['label_class']}, "
                f"{c.get('state') or '?'}, conf={c['identity_confidence']}, "
                f"venue={c['venue_match']}) — {excerpt} — {c.get('source_url', '')}"
            )
        lines.append("")
    return "\n".join(lines)


def run(*, extractions_path: Path, escalated_path: Path | None, existing_labels_path: Path,
        gold_set_path: Path, raw_cache_path: Path, limit: int | None,
        log=print) -> tuple[list[dict], list[dict], dict]:
    """Returns (grounded_candidates, individual_proposals, funnel)."""
    rows = load_jsonl(extractions_path)
    escalated_rows = load_jsonl(escalated_path) if escalated_path else []
    rows = apply_escalation_overlay(rows, escalated_rows)
    if limit is not None:
        rows = rows[:limit]
    n_in = len(rows)

    raw_cache: dict = {}
    if raw_cache_path.exists():
        for raw in load_jsonl(raw_cache_path):
            if raw.get("uuid"):
                raw_cache[raw["uuid"]] = raw

    filtered = [r for r in rows if passes_ground_filter(r.get("extract"))]
    n_filtered = len(filtered)

    individual_rows = [r for r in rows if is_individual_candidate(r.get("extract"))]

    # --- NPPES-resolve every filtered candidate --------------------------
    resolved: list[tuple[str, OrgMatch, dict]] = []
    n_nppes_none = 0
    for row in filtered:
        name = row["extract"]["primary_entity_name"].strip()
        match = resolve_org_npi(name, log=log)
        if match is None:
            n_nppes_none += 1
            continue
        resolved.append((name, match, to_release(row, raw_cache)))

    # --- group by normalized name; collapse twins -------------------------
    groups: dict[str, list[tuple[str, OrgMatch, dict]]] = {}
    for name, match, rel in resolved:
        groups.setdefault(_norm_org_name(name), []).append((name, match, rel))

    n_dup = 0
    n_venue_dropped = 0
    winners: list[tuple[str, OrgMatch, dict, str | None, list[dict] | None]] = []
    for members in groups.values():
        distinct_npis = {m.npi for _, m, _ in members}
        winner = resolve_twin_candidates(members)
        winners.append(winner)
        if len(distinct_npis) == 1:
            n_dup += len(members) - 1
        else:
            venue_hits = [classify_venue_match(extract_venue_state(rel), m.state)
                          for _, m, rel in members]
            if venue_hits.count("match") == 1:
                n_venue_dropped += len(members) - 1
            else:
                n_dup += len(members) - 1   # ambiguous_multi_candidate: folded into 1 row

    # --- dedupe vs already-landed / gold_set / within-run -----------------
    existing_npis = load_existing_proposal_npis(existing_labels_path)
    gold_npis = load_gold_npis(gold_set_path)
    seen_this_run: set[str] = set()
    survivors: list[tuple[str, OrgMatch, dict, str | None, list[dict] | None]] = []
    for name, match, rel, identity_flag, identity_candidates in winners:
        if match.npi in existing_npis or match.npi in gold_npis or match.npi in seen_this_run:
            n_dup += 1
            continue
        seen_this_run.add(match.npi)
        survivors.append((name, match, rel, identity_flag, identity_candidates))

    # --- surface-confirm ---------------------------------------------------
    facility_idx = FacilityIndex()
    medicaid_surf = MedicaidSurface()
    medicare_surf = MedicareSurface()

    n_no_surface = 0
    grounded: list[dict] = []
    for name, match, rel, identity_flag, identity_candidates in survivors:
        facility_hit = facility_idx.match(name, match.state)
        medicaid_hit = medicaid_surf.has_surface(match.npi)
        medicare_hit = medicare_surf.has_surface(match.npi)
        picked = pick_label_class(facility_hit, medicaid_hit, medicare_hit)
        if picked is None:
            n_no_surface += 1
            continue
        label_class, surface_confirmed = picked
        grounded.append(build_grounded_row(
            name, match, rel, label_class=label_class, surface_confirmed=surface_confirmed,
            identity_flag=identity_flag, identity_candidates=identity_candidates))

    # --- individuals side file: drop names already in gold_set -----------
    gold_names = {
        _norm_org_name(d["defendant_name"])
        for d in load_jsonl(gold_set_path)
        if d.get("defendant_name")
    }
    individual_proposals = [
        build_individual_row(r) for r in individual_rows
        if _norm_org_name(r["extract"]["primary_entity_name"]) not in gold_names
    ]

    funnel = {
        "n_in": n_in,
        "n_filtered": n_filtered,
        "n_nppes_none": n_nppes_none,
        "n_dup": n_dup,
        "n_venue_dropped": n_venue_dropped,
        "n_no_surface": n_no_surface,
        "n_written": len(grounded),
    }
    return grounded, individual_proposals, funnel


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("extractions", type=Path, help="labelgen_extract.py output JSONL")
    ap.add_argument("--escalated", type=Path, default=None,
                    help="labelgen_escalate.py output JSONL (optional overlay)")
    ap.add_argument("--existing-labels", type=Path, default=ENTITY_LABELS_PATH,
                    help="landed entity_labels.jsonl (dedupe target)")
    ap.add_argument("--gold-set", type=Path, default=GOLD_SET_PATH, help="read-only")
    ap.add_argument("--raw-cache", type=Path, default=doj_pr_lookup.LOCAL_CACHE)
    ap.add_argument("--out", type=Path, default=None,
                    help="default: validation/curator/grounded_candidates_<today>.jsonl")
    ap.add_argument("--individuals-out", type=Path, default=None,
                    help="default: validation/curator/individuals_for_curator_<today>.jsonl")
    ap.add_argument("--md-out", type=Path, default=None, help="default: --out with .md extension")
    ap.add_argument("--limit", type=int, default=None, help="cap input rows (testing)")
    args = ap.parse_args(argv)

    today = datetime.date.today().isoformat()
    out_path = args.out or (REPO_ROOT / "validation" / "curator" / f"grounded_candidates_{today}.jsonl")
    individuals_out = args.individuals_out or (
        REPO_ROOT / "validation" / "curator" / f"individuals_for_curator_{today}.jsonl")
    md_out = args.md_out or out_path.with_suffix(".md")

    grounded, individual_proposals, funnel = run(
        extractions_path=args.extractions,
        escalated_path=args.escalated,
        existing_labels_path=args.existing_labels,
        gold_set_path=args.gold_set,
        raw_cache_path=args.raw_cache,
        limit=args.limit,
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as f:
        for row in grounded:
            f.write(json.dumps(row) + "\n")

    with individuals_out.open("w") as f:
        for row in individual_proposals:
            f.write(json.dumps(row) + "\n")

    md_out.write_text(render_md(grounded, funnel, generated=today))

    print(f"funnel: {funnel}")
    print(f"grounded candidates -> {out_path} ({len(grounded)} rows)")
    print(f"individuals for curator -> {individuals_out} ({len(individual_proposals)} rows)")
    print(f"review .md -> {md_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
