"""P3 label mining — ORG / Medicaid / facility fraud-label PROPOSALS.

The existing corpus_curator sweep (validation/curator/gold_proposals_2026-07-11.jsonl)
mines DOJ press releases for **individual** defendants. gold_set is DOJ-only and
individual-heavy (~1,000 unique NPIs, all NPPES entity_type "1"). The entity/org
scoring head (P4) needs a SEPARATE
label class: organizational defendants (home health, hospice, personal-care/PCS,
adult-day, SNF, lab, clinic entities) resolved to a Type-2 (organization) NPI.

**PROPOSAL-ONLY.** This script NEVER writes data/extracted/gold_set.jsonl or
validation/battery/battery.json — it only appends to the output JSONL below for a
human (a teammate) to keep/reject. See docs/design/execution_roadmap_2026-07.md workstream E.

## Sources (funnel step 1)

1. **DOJ** — reuses doj_pr_lookup.py's two channels: the LOCAL CACHE
   (data/source/doj_api/raw_press_releases.jsonl, healthcare-fraud-scoped, ~6,061
   releases through its last refresh) for the bulk of the [--since, --until] window
   (no network), plus a small live-API title-search supplement for the gap between
   the cache's last date and --until (the cache is refreshed periodically, not live).
2. **NY OMIG** (omig.ny.gov/news-and-announcements) — confirmed reachable, Drupal
   "views" listing, paginated; a genuine state MFCU/IG source.
3. **CA OAG DMFEA** (oag.ca.gov/media/news) — confirmed reachable, same Drupal
   pattern, BUT this is the *entire* CA AG press feed (all topics), not
   DMFEA-scoped — no working taxonomy-filter URL was found in this pass, so
   coverage here is bounded by --max-state-pages and will not reach --since for
   a broad window; title-filtered per page to keep the fetch-body budget small.
   This is a known, reported limitation (see the run report), not a silent gap.

Both state sources were reachable in this pass (HTTP 200, parseable HTML) —
neither is "blocked." If a future run finds one blocked (403/Akamai/JS-wall),
that source degrades to skipped-with-a-logged-reason; the run continues on DOJ
+ the other state source rather than failing closed.

## Funnel (steps 2-5, per candidate)

1. Discover a press release, keyword/entity-relevance-gate it (an org-suffix or
   facility-style name near a disposition marker + a home-health/hospice/PCS/
   lab/clinic domain keyword somewhere in the body).
2. Extract candidate entity name(s) (regex heuristic — no LLM/NLP; recall-oriented,
   since NPPES resolution + surface confirmation below are the real precision gates).
3. NPPES-resolve inline (nppes_lookup._query, organization_name search) to a
   Type-2 (NPI-2) org; keep the best name-similarity match if confident, drop
   ambiguous/no-match candidates (never queue-then-resolve: resolve the NPI inline).
4. Dedupe vs gold_set.jsonl (by NPI; gold_set is individual-only today so this is
   mostly a formality) AND vs rows already in the output file (so a re-run after
   a mid-run death does not reprocess or duplicate finished candidates — the
   resumability IS the resilience feature here).
5. Surface-confirm: facility (facility_hospice/hha/snf/hospital.parquet name+state
   match) OR Medicaid (T-MSIS provider_spending_full.parquet NPI match) OR
   Medicare (check_billing_surface.py's Part B/D/DMEPOS panels, LOCAL-ONLY —
   NP_R2_DISABLE=1 so this script never triggers a surprise multi-GB R2 pull).
   Drop no-surface candidates. label_class is picked by precedence
   facility > medicaid > org (Medicare-only) — see pick_label_class().

## Resilience (the prior run died on a transient connection drop, zero output)

* Every network call goes through `_retry()` (3 tries, exponential backoff) —
  a single flaky request degrades to "skip + log," not a crash.
* Each finished proposal is appended + flushed to the output JSONL IMMEDIATELY
  (see `_append_flush`), not collected and written at the end.
* The output file is READ on startup; NPIs already present are skipped, so
  re-running this script after any death (transient or not) resumes rather
  than reprocessing or duplicating work.
* Heavy local data (Medicaid parquet connection, facility frames, Medicare panel
  NPI sets) is loaded ONCE up front, then every per-candidate surface check is an
  in-memory / single-connection point lookup — batched, not re-opened per row.

## Labeling-audit hardening (2026-07-14, see validation/curator/entity_medicaid_labeled_2026-07-13.md)

A first-pass audit of the 2026-07-13 proposals found 5 systemic bugs affecting
roughly half the batch. Each proposal row now also carries:

* `disposition_finality` (resolved/contested/unknown) — classify_disposition_finality()
  scopes to whichever marker sits closest to the ENTITY's own mention, not a
  whole-body priority scan (which locked onto an incidental "settlement"-flavored
  phrase and mislabeled charging-stage releases as resolved).
* `venue_match` (match/mismatch/unknown) — extract_venue_state() pins the case's
  own venue (state source / usao-<slug> / body prose) independent of the NPPES
  org's address; a facility-class candidate with a mismatched venue is dropped
  rather than proposed (geography-blind NPPES search was proposing the wrong
  real-world org in ~35% of rows).
* `identity_confidence` (high/medium/low) — name-match confidence demoted on a
  venue mismatch, forced to "low" for a twin-NPI tie-break.
* Generic-fragment candidate names ("Pharmacy Inc", "Hospice Inc") are dropped
  before an NPPES call (is_name_too_generic(), requires >=2 distinctive tokens).
* Twin NPIs — the same press-release mention resolving to 2+ different real
  NPPES orgs in different states — are resolved to ONE row (resolve_twin_candidates():
  prefer the venue-matching candidate, else flag `identity_flag=
  "ambiguous_multi_candidate"` with every candidate listed) instead of silently
  proposing more than one guess.

Usage:
    python scripts/mine_entity_labels.py
    python scripts/mine_entity_labels.py --since 2020-01-01 --until 2026-07-14
    python scripts/mine_entity_labels.py --sources doj,ny_omig --limit 25
    python scripts/mine_entity_labels.py --max-state-pages 10   # smoke test
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, Iterator

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))       # sibling-script imports below
sys.path.insert(0, str(REPO_ROOT))        # fraudlib (needed transitively by check_billing_surface)

import doj_pr_lookup                       # noqa: E402  reused: _api, LOCAL_CACHE, _html_to_text
import nppes_lookup                        # noqa: E402  reused: _query, NPPES_URL
import check_billing_surface as billing_surface   # noqa: E402  reused: load_surfaces, verdict_for
import medicaid_lookup                     # noqa: E402  reused: _connect, NPI_FILTER

GOLD_SET_PATH = REPO_ROOT / "data" / "extracted" / "gold_set.jsonl"
FACILITY_FILES = {
    "hospice": REPO_ROOT / "data" / "processed" / "facility_hospice.parquet",
    "hha": REPO_ROOT / "data" / "processed" / "facility_hha.parquet",
    "snf": REPO_ROOT / "data" / "processed" / "facility_snf.parquet",
    "hospital": REPO_ROOT / "data" / "processed" / "facility_hospital.parquet",
}
DEFAULT_OUT = REPO_ROOT / "validation" / "curator" / "entity_medicaid_proposals_2026-07-13.jsonl"
# Medicare surface years: local partb parquet covers 2018-2024 (confirmed on disk);
# partd/dmepos are R2-only locally (manifests, no parquet) — NP_R2_DISABLE=1 below
# keeps this script from ever triggering a first-fetch multi-GB R2 pull for them;
# they gracefully degrade to "no_panel" and Medicaid/facility remain live surfaces.
MEDICARE_SURFACE_YEARS = [2018, 2019, 2020, 2021, 2022, 2023, 2024]

HEADERS = {"User-Agent": "fraud-investigator/1.0 (entity-label-mining)", "Accept": "*/*"}
TIMEOUT_SEC = 30


# ---------------------------------------------------------------------------
# Resilience: one retry wrapper for every network call. doj_pr_lookup._api and
# nppes_lookup._query both return {"_error": ...} on failure (same convention);
# a plain requests.get (state-source HTML fetches) is wrapped directly.
# ---------------------------------------------------------------------------

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


def _http_get(url: str, *, params: dict | None = None, log=print) -> requests.Response | None:
    return _retry(
        lambda: requests.get(url, params=params, headers=HEADERS, timeout=TIMEOUT_SEC),
        log=log, what=f"GET {url}",
    )


# ---------------------------------------------------------------------------
# Entity-name extraction (regex heuristic; recall-oriented — NPPES resolution
# + surface confirmation are the real precision gates downstream)
# ---------------------------------------------------------------------------

_ORG_SUFFIX = (r"(?:Inc\.?|Incorporated|L\.?L\.?C\.?|Corp(?:oration)?\.?|"
               r"Co\.|Company|L\.?L\.?P\.?|P\.?L\.?L\.?C\.?|P\.?C\.?|Ltd\.?)")
# NOTE: "Co" requires a MANDATORY trailing period (Co\. not Co\.?) — deliberately
# excluded from the case-insensitive optional-period pattern the other suffixes
# use. An optional-period, case-insensitive "co" collides with the extremely
# common "co-defendant(s)"/"co-conspirator(s)" prefix in DOJ press releases
# (verified against the local cache: without this, "All co-defendants" and
# "Additional co-conspirators" both false-matched as an entity name ending in
# the "Co." suffix). "Company" (spelled out) has no such collision.
# The suffix alternation is wrapped in a scoped (?i:...) so ALL-CAPS registered
# names ("JACKSON HOME HEALTHCARE INC.") match too — scoped, not global, so the
# preceding Title-Case name-word requirement ([A-Z]...) stays case-SENSITIVE
# (that's what keeps this from matching random lowercase prose).
_SUFFIX_ENTITY_RE = re.compile(
    r"\b((?:[A-Z][A-Za-z0-9&'\-]*\.?\s+){0,5}[A-Z][A-Za-z0-9&'\-]*),?\s+(?i:(" + _ORG_SUFFIX + r"))\b"
)
# Facility-style names commonly carry no corporate suffix in press-release prose
# ("Jackson Home Healthcare", "Compassionate Touch Hospice") — a second,
# domain-keyword-anchored extractor catches those.
_DOMAIN_NAME_RE = re.compile(
    r"\b((?:[A-Z][A-Za-z'&\-]*\s+){1,5}(?:Hospice|Home Health(?:care)?|Home Care|"
    r"Personal Care(?: Services)?|Adult Day (?:Care|Health)?|Laboratory|Labs?|"
    r"Medical Group|Clinic|Health Services|Home Healthcare|Rehabilitation|Rehab|"
    r"Nursing(?: Home)?|Wellness|Health Care))\b"
)

_AGENCY_BLOCKLIST = re.compile(
    r"United States|U\.S\.|Department of Justice|Justice Department|District of|"
    r"Attorney(?:'s|s')? Office|Attorney General|Federal Bureau|FBI|HHS-OIG|"
    r"Office of Inspector General|Health and Human Services|"
    r"Medicare Fraud Strike Force|Centers for Medicare|Drug Enforcement|"
    r"Internal Revenue|Postal Inspection|State Police|Superior Court|"
    r"District Court|Court of|Grand Jury|Circuit Court|Inspector General|"
    r"Medicaid Fraud Control Unit|Bureau of Investigation",
    re.I,
)

_DOMAIN_KEYWORDS = re.compile(
    r"home health|hospice|personal care|\bPCS\b|adult day|skilled nursing|nursing home|"
    r"laboratory|clinical lab|durable medical equipment|\bDME\b|pharmacy|"
    r"behavioral health|substance abuse treatment|addiction treatment|"
    r"telemedicine|telehealth|billing company|management services organization|"
    r"\bMSO\b|medical group|health system|imaging center|diagnostic|urgent care|"
    r"medical clinic|home care",
    re.I,
)

_DISPOSITION_PROXIMITY_RE = re.compile(
    r"agreed to pay|has agreed|will pay|settlement|\bsettle[ds]?\b|resolve allegations|"
    r"pleaded guilty|pled guilty|plead(?:s|ed)? guilty|convicted|found guilty|"
    r"indicted|indictment|\bcharged\b|sentenced|False Claims Act",
    re.I,
)

# Generic capitalized lead-ins that the repeated-word portion of the extractors
# will happily sweep in as if they were part of the entity's name (verified
# against the local cache: "DEFENDANT American Home Health Services Inc",
# "San Diego-based Phamatech Inc"). Stripped (repeatedly — a name can carry more
# than one) before dedup/blocklist so "Defendant X" and "X" collapse to one row.
_LEADING_NOISE_RE = re.compile(
    r"^(?:Defendants?|Additional|All|The|Co-defendants?|Said|Named|Above-named|"
    r"[A-Za-z]+-based)\s+",
    re.I,
)


def _strip_leading_noise(name: str) -> str:
    prev = None
    while prev != name:
        prev = name
        name = _LEADING_NOISE_RE.sub("", name)
    return name


def extract_entity_candidates(body_text: str) -> list[str]:
    """Best-effort org/facility names from a press-release body.

    Union of the suffix-anchored (Inc/LLC/Corp/...) and domain-keyword-anchored
    (...Hospice/Home Health/...) extractors, de-duplicated case-insensitively,
    with known government-agency/court names and generic lead-in words filtered.
    """
    seen: set[str] = set()
    out: list[str] = []
    for m in _SUFFIX_ENTITY_RE.finditer(body_text):
        name = re.sub(r"\s+", " ", f"{m.group(1)} {m.group(2)}").strip(" ,.")
        _maybe_add(_strip_leading_noise(name), seen, out)
    for m in _DOMAIN_NAME_RE.finditer(body_text):
        name = re.sub(r"\s+", " ", m.group(1)).strip(" ,.")
        _maybe_add(_strip_leading_noise(name), seen, out)
    return out


def _maybe_add(name: str, seen: set[str], out: list[str]) -> None:
    key = name.lower()
    if key in seen:
        return
    if _AGENCY_BLOCKLIST.search(name):
        return
    if len(name) < 6 or len(name.split()) > 9:
        return
    seen.add(key)
    out.append(name)


# Truncated-fragment guard (labeling-audit bug #3): a handful of extracted
# names are generic TAIL fragments of the real name -- "Pharmacy Inc" for "NY
# Elm Pharmacy Inc", "Hospice Inc" for "626 Hospice Inc", "Home Health Inc" for
# a company whose real name carries more distinctive words -- and the NPPES
# resolve then confidently (mis)matches the bare fragment to an unrelated real
# org. Corp-suffix words and common domain-descriptor words don't count toward
# the "distinctive" tally; a name needs >=2 non-generic tokens before it's
# trusted with an NPPES call. Deliberately NOT folded into extract_entity_
# candidates/_maybe_add above (which stays recall-oriented) -- this is a
# separate precision gate applied just before resolve_org_npi.
_GENERIC_NAME_TOKENS = {
    "inc", "incorporated", "llc", "corp", "corporation", "co", "company",
    "llp", "pllc", "pc", "ltd",
    "home", "health", "healthcare", "care", "hospice", "pharmacy", "medical",
    "clinic", "center", "centers", "wellness", "rehab", "rehabilitation",
    "nursing", "laboratory", "laboratories", "lab", "labs", "group",
    "services", "service", "solutions", "supply", "supplies", "diagnostic",
    "diagnostics", "management", "mgmt", "behavioral", "adult", "day",
    "personal", "skilled", "living", "senior", "community", "and", "of",
    "the", "&",
}


def _distinctive_token_count(name: str) -> int:
    tokens = re.findall(r"[A-Za-z0-9']+", name)
    return sum(1 for t in tokens if t.lower() not in _GENERIC_NAME_TOKENS)


def is_name_too_generic(name: str) -> bool:
    """True when `name` has fewer than 2 distinctive (non-suffix, non-domain-
    descriptor) tokens -- almost always a truncated fragment rather than a
    safely resolvable identity (labeling-audit bug #3)."""
    return _distinctive_token_count(name) < 2


def release_is_entity_relevant(body_text: str) -> bool:
    """Release-level gate: a domain keyword AND a disposition marker somewhere
    in the body. Cheap pre-filter before spending an NPPES call on any name
    extracted from it."""
    return bool(_DOMAIN_KEYWORDS.search(body_text) and _DISPOSITION_PROXIMITY_RE.search(body_text))


# ---------------------------------------------------------------------------
# Disposition (type / date / amount) — best-effort, single scalar, matches the
# precision bar of the individual curator's own regex-free heuristics (a human
# reviews every row; this is not adjudicated data).
# ---------------------------------------------------------------------------

_DISP_RULES = [
    ("settlement", re.compile(r"agreed to pay|settlement|resolve allegations|consent (?:judgment|order|decree)|civil (?:penalt|settlement)", re.I)),
    ("plea", re.compile(r"plead(?:ed|s)? guilty|guilty plea|plea agreement", re.I)),
    ("sentencing", re.compile(r"\bsentenced\b|sentencing", re.I)),
    ("conviction", re.compile(r"convicted|found guilty|jury (?:trial|verdict)", re.I)),
    ("indictment", re.compile(r"\bindict(?:ed|ment)\b|grand jury", re.I)),
    ("charged", re.compile(r"\bcharged\b|criminal complaint|complaint unsealed", re.I)),
]

_AMOUNT_RE = re.compile(r"\$\s?([\d]{1,3}(?:,\d{3})*(?:\.\d+)?)\s*(million|billion|M\b|B\b)?", re.I)


def classify_disposition_type(body_text: str) -> str:
    for label, rx in _DISP_RULES:
        if rx.search(body_text):
            return label
    return "unknown"


# ---------------------------------------------------------------------------
# disposition_finality (labeling-audit bug #1): classify_disposition_type
# above is a body-WIDE, fixed-priority keyword scan that checks "settlement"
# before plea/sentencing/conviction/indictment/charged -- so in a multi-
# defendant release where one co-defendant pled guilty and ANOTHER (our
# entity) was merely charged, or where generic boilerplate ("...to resolve
# allegations that...") appears anywhere in the body, it locks onto the wrong
# marker and tags a still-contested, charging-stage release as "settlement".
# disposition_finality fixes this by (a) scoping to whichever marker sits
# CLOSEST to the entity's own mention(s), not first-in-body, and (b) using a
# narrower, less ambiguous marker set per bucket (RESOLVED requires a
# terminal marker -- conviction/plea/sentencing/an actual settlement-or-
# judgment phrase, not the bare word "allegations", which appears in charging
# documents just as often as settlements).
# ---------------------------------------------------------------------------

_FINALITY_RESOLVED_RE = re.compile(
    r"pleaded guilty|pled guilty|plea(?:s|ded)? guilty|guilty plea|"
    r"\bconvicted\b|found guilty|jury (?:trial|verdict)|"
    r"\bsentenced\b|sentencing (?:hearing|memorandum)?|"
    r"agreed to pay|has agreed to pay|will pay \$|"
    r"consent (?:judgment|order|decree)|"
    r"settlement agreement|civil settlement|entered (?:into )?a settlement",
    re.I,
)
_FINALITY_CONTESTED_RE = re.compile(
    r"\bindict(?:ed|ment)\b|grand jury (?:indict|return)|"
    r"\bcharged\b|criminal complaint|complaint (?:was |is )?unsealed|"
    r"filed (?:a |an )?(?:civil |federal )?(?:lawsuit|complaint|suit|action)|"
    r"announced (?:the filing|that it (?:has |had )?filed)|"
    r"\ballege(?:d|s)?\b|"
    r"arraign(?:ed|ment)?|ple(?:d|aded) not guilty|"
    r"\barrested\b|is accused of|stands? accused|unsealed (?:indictment|charges)",
    re.I,
)


def _entity_mention_offsets(body_text: str, entity_name: str) -> list[int]:
    """Every (case-insensitive) start offset of entity_name in body_text."""
    if not entity_name:
        return []
    low_body, low_name = body_text.lower(), entity_name.lower()
    offsets, start = [], 0
    while True:
        idx = low_body.find(low_name, start)
        if idx < 0:
            break
        offsets.append(idx)
        start = idx + max(len(low_name), 1)
    return offsets


def classify_disposition_finality(body_text: str, entity_name: str | None = None, *, max_distance: int = 500) -> str:
    """disposition_finality in {resolved, contested, unknown} -- grounded in
    whichever marker sits CLOSEST to the entity's own mention(s), not a fixed
    whole-body keyword priority order (see module note above / labeling-audit
    bug #1). With no entity_name (or when whole-body scanning is desired),
    falls back to first-occurrence-wins over the whole body."""
    offsets = _entity_mention_offsets(body_text, entity_name or "")
    if entity_name and not offsets:
        return "unknown"   # can't ground a disposition to an entity never named in the body

    resolved_hits = [m.start() for m in _FINALITY_RESOLVED_RE.finditer(body_text)]
    contested_hits = [m.start() for m in _FINALITY_CONTESTED_RE.finditer(body_text)]

    def _nearest(hits: list[int]) -> int | None:
        if not hits:
            return None
        if not offsets:
            return min(hits)
        d = min(abs(h - o) for h in hits for o in offsets)
        return d if d <= max_distance else None

    nearest_r, nearest_c = _nearest(resolved_hits), _nearest(contested_hits)
    if nearest_r is None and nearest_c is None:
        return "unknown"
    if nearest_c is None:
        return "resolved"
    if nearest_r is None:
        return "contested"
    return "resolved" if nearest_r < nearest_c else "contested"


def _amount_from_match(m: re.Match) -> float:
    num = float(m.group(1).replace(",", ""))
    mult = (m.group(2) or "").lower()
    if mult.startswith("m"):
        num *= 1_000_000
    elif mult.startswith("b"):
        num *= 1_000_000_000
    return num


def extract_amount_usd(body_text: str, entity_name: str | None = None, *, window: int = 400) -> float | None:
    """Case-specific dollar figure nearest the entity's own mention(s) --
    labeling-audit bug #5: the plain first-dollar-figure-in-body search grabs
    the sweep-wide AGGREGATE in multi-defendant/nationwide-takedown releases
    (several audited rows all showed the same $2.75B/$14.6B/$69.7M headline
    figure regardless of this defendant's actual, much smaller loss amount).
    With no entity_name, preserves the original first-match-in-body behavior.
    Returns None (never a guessed/aggregate fallback) when the entity isn't
    named in the body, or no dollar figure appears within `window` chars of
    any of its mentions."""
    offsets = _entity_mention_offsets(body_text, entity_name or "")
    if entity_name and not offsets:
        return None   # can't ground an amount to an entity never named in the body
    if not offsets:
        m = _AMOUNT_RE.search(body_text)
        return _amount_from_match(m) if m else None
    candidates = sorted(
        ((min(abs(m.start() - o) for o in offsets), m) for m in _AMOUNT_RE.finditer(body_text)),
        key=lambda t: t[0],
    )
    for dist, m in candidates:
        if dist <= window:
            return _amount_from_match(m)
    return None


def scheme_summary_excerpt(body_text: str, entity_name: str, *, window: int = 260, cap: int = 480) -> str:
    """A short excerpt centered on the entity's first mention — not a full
    reproduction of the release. Whitespace-normalized, capped."""
    idx = body_text.lower().find(entity_name.lower())
    if idx < 0:
        text = body_text[:cap]
    else:
        a, b = max(0, idx - window // 2), min(len(body_text), idx + len(entity_name) + window)
        text = ("…" if a else "") + body_text[a:b] + ("…" if b < len(body_text) else "")
    return re.sub(r"\s+", " ", text).strip()[:cap]


# ---------------------------------------------------------------------------
# DOJ discovery — local cache (bulk, no network) + live title-search (the
# cache-to-now gap)
# ---------------------------------------------------------------------------

def discover_doj(since: str, until: str, *, max_title_calls: int = 6, log=print) -> Iterator[dict]:
    cache_max_date = "0000-00-00"
    n_cache_hits = 0
    if doj_pr_lookup.LOCAL_CACHE.exists():
        with doj_pr_lookup.LOCAL_CACHE.open() as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                d = r.get("date") or ""
                iso = _doj_date_to_iso(d)
                if iso and iso > cache_max_date:
                    cache_max_date = iso
                if not iso or iso < since or iso > until:
                    continue
                body = doj_pr_lookup._html_to_text(r.get("body") or "")
                if not release_is_entity_relevant(body):
                    continue
                n_cache_hits += 1
                yield {"title": r.get("title") or "", "url": r.get("url") or "",
                       "date": iso, "body": body, "source": "doj"}
    log(f"  DOJ local cache: {n_cache_hits} entity-relevant hit(s) "
        f"(cache spans through {cache_max_date}, window [{since}, {until}])")

    # Live-API supplement for the gap between the cache's last date and --until.
    gap_start = max(since, cache_max_date)
    if gap_start >= until or max_title_calls <= 0:
        return
    log(f"  DOJ live API supplement for gap [{gap_start}, {until}] "
        f"(cache is refreshed periodically, not live)")
    terms = ["health care fraud", "medicaid fraud", "home health", "hospice",
             "kickback", "false claims act"]
    seen_urls: set[str] = set()
    calls = 0
    for term in terms:
        if calls >= max_title_calls:
            break
        res = _retry(lambda t=term: doj_pr_lookup._api(
            {"pagesize": 50, "title": t, "sort": "date", "direction": "DESC"}),
            log=log, what=f"DOJ title search {term!r}")
        calls += 1
        if not res:
            continue
        for r in res.get("results", []) or []:
            iso = _doj_date_to_iso(r.get("date") or "")
            if not iso or iso < gap_start or iso > until:
                continue
            url = r.get("url") or ""
            if url in seen_urls:
                continue
            seen_urls.add(url)
            body = doj_pr_lookup._html_to_text(r.get("body") or "")
            if not release_is_entity_relevant(body):
                continue
            yield {"title": r.get("title") or "", "url": url, "date": iso,
                   "body": body, "source": "doj"}


def _doj_date_to_iso(d: str) -> str | None:
    try:
        ts = int(d)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()


# ---------------------------------------------------------------------------
# State-source discovery — NY OMIG + CA OAG share a Drupal "views" listing
# structure; parsed with lxml (already a declared dependency) rather than
# hand-rolled regex-over-HTML.
# ---------------------------------------------------------------------------

STATE_SOURCES = {
    "ny_omig": {
        "list_url": "https://omig.ny.gov/news-and-announcements",
        "base_url": "https://omig.ny.gov",
        "date_selector": "time.datetime",
        "date_attr": "datetime",
        # OMIG's whole feed is Medicaid-program news; still gate on a
        # disposition/fraud-flavored title so policy/protocol posts are skipped.
        "title_gate": re.compile(r"fraud|indict|arrest|plea|guilty|settlement|convicted|charged|sentenc", re.I),
    },
    "ca_oag": {
        "list_url": "https://oag.ca.gov/media/news",
        "base_url": "https://oag.ca.gov",
        "date_selector": "span.date-display-single",
        "date_attr": "content",
        # CA OAG's feed is ALL AG topics (not DMFEA-scoped) — no working
        # taxonomy-filter URL was found, so this title gate does the real work.
        "title_gate": re.compile(r"medi-cal fraud|medicaid fraud|health care fraud|healthcare fraud|"
                                  r"hospice fraud|home health fraud|nursing (?:home|facility) fraud", re.I),
    },
}


def _parse_listing(html_text: str, base_url: str, *, date_selector: str, date_attr: str) -> list[dict]:
    from lxml import html as lxml_html
    tree = lxml_html.fromstring(html_text)
    titles = tree.cssselect("div.views-field-title a")
    dates = tree.cssselect(date_selector)
    out = []
    for a, d in zip(titles, dates):
        href = a.get("href") or ""
        url = href if href.startswith("http") else base_url + href
        raw_date = d.get(date_attr) if date_attr else (d.text_content() or "")
        iso = (raw_date or "")[:10]
        title = (a.text_content() or "").strip()
        if title and url and re.match(r"\d{4}-\d{2}-\d{2}", iso):
            out.append({"title": title, "url": url, "date": iso})
    return out


def discover_state_source(name: str, since: str, until: str, *, max_pages: int = 40, log=print) -> Iterator[dict]:
    cfg = STATE_SOURCES[name]
    n_listed = n_title_matched = n_fetched = n_relevant = 0
    oldest_seen = until
    for page in range(max_pages):
        url = cfg["list_url"] if page == 0 else f"{cfg['list_url']}?page={page}"
        resp = _http_get(url, log=log)
        if resp is None or resp.status_code != 200:
            log(f"  {name}: page {page} unreachable — stopping pagination "
                f"({'HTTP ' + str(resp.status_code) if resp is not None else 'no response'})")
            break
        try:
            rows = _parse_listing(resp.text, cfg["base_url"],
                                   date_selector=cfg["date_selector"], date_attr=cfg["date_attr"])
        except Exception as e:
            log(f"  {name}: page {page} parse failed ({e}) — stopping pagination")
            break
        if not rows:
            log(f"  {name}: page {page} had no parseable rows — stopping pagination")
            break
        n_listed += len(rows)
        page_dates = [r["date"] for r in rows if r["date"]]
        if page_dates:
            oldest_seen = min(oldest_seen, min(page_dates))
        stop_paging = all(r["date"] < since for r in rows if r["date"])
        for r in rows:
            if not (since <= r["date"] <= until):
                continue
            if not cfg["title_gate"].search(r["title"]):
                continue
            n_title_matched += 1
            body_resp = _http_get(r["url"], log=log)
            if body_resp is None:
                continue
            n_fetched += 1
            try:
                import trafilatura
                body = trafilatura.extract(body_resp.text, include_comments=False, include_tables=False) or ""
            except Exception as e:
                log(f"  {name}: trafilatura extract failed for {r['url']}: {e}")
                continue
            if not release_is_entity_relevant(body):
                continue
            n_relevant += 1
            yield {"title": r["title"], "url": r["url"], "date": r["date"], "body": body, "source": name}
        if stop_paging:
            break
    log(f"  {name}: {n_listed} listed / {n_title_matched} title-matched / "
        f"{n_fetched} body-fetched / {n_relevant} entity-relevant "
        f"(paged to page={page}, oldest release seen={oldest_seen}"
        f"{', did not reach --since' if oldest_seen > since else ''})")


# ---------------------------------------------------------------------------
# NPPES org resolution
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Venue / geography (labeling-audit bug #2): resolve_org_npi above is a
# nationwide, geography-BLIND name-similarity search -- 35% of the audited
# proposals had an org_npi practice-location state that flatly contradicted
# the case's own venue (e.g. NPPES said TX, the press release said the
# defendant was a Louisiana corporation). The fix isn't to change the NPPES
# search itself (organization_name search has no state parameter worth
# trusting) but to independently extract the case's VENUE and cross-check it
# against whatever state NPPES returned.
#
# The reliable signal is structural, not prose: every DOJ press release URL
# under a usao-<slug> path was issued BY that single US Attorney's Office, and
# a federal judicial district never spans more than one state -- so the slug
# alone pins the venue with no parsing risk. Body-prose "District of X" is a
# fallback for releases without a usao- slug (e.g. main-DOJ /archives/opa/
# URLs); it's only trusted when every district named in the body agrees on
# ONE state, so a genuinely nationwide multi-district takedown release
# correctly falls back to "unknown" rather than a coin-flip guess.
# ---------------------------------------------------------------------------

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
    address. NY OMIG / CA OAG releases ARE the state (deterministic); DOJ
    releases are pinned by their usao-<slug> URL (a federal district never
    crosses state lines); body prose ("District of X") is a last-resort
    fallback for releases without a usao- slug. Returns None when nothing
    reliable is available (e.g. a main-DOJ nationwide takedown release naming
    several different districts) -- never a guess."""
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
    contradicts it (bug #2), forced to "low" when the candidate only survived
    a twin-NPI tie-break (bug #4)."""
    if ambiguous:
        return "low"
    rank = _CONFIDENCE_RANK.get(name_confidence, 0)
    if venue_match == "mismatch":
        rank = max(0, rank - 1)
    return _RANK_TO_CONFIDENCE[rank]


# ---------------------------------------------------------------------------
# Twin-NPI resolution (labeling-audit bug #4): "X Inc" and "X" extracted from
# the SAME release (one mention with the corporate suffix, one without) are
# two different candidate keys in run()'s `candidates` dict, and each
# independently resolves via NPPES to its OWN best-scoring org -- sometimes a
# genuinely different real NPI in a different state. 5/94 audited rows were
# exactly this: the same underlying defendant proposed TWICE, in two states,
# with no way for a reviewer to tell which (if either) was right.
#
# run() groups raw candidates by _norm_org_name() (suffix-stripped) before
# writing anything, so "Trinity Home Health Care Inc" and "Trinity Home
# Health Care" are decided TOGETHER. resolve_twin_candidates is the pure
# decision function: a single distinct NPI in the group is a non-issue
# (just spelling variants); >=2 distinct NPIs is a genuine twin, resolved by
# preferring whichever candidate's own venue corroborates its NPPES state --
# and if venue can't disambiguate either, exactly ONE row is still emitted,
# flagged ambiguous_multi_candidate with every candidate listed, rather than
# silently proposing more than one guess for the same real-world entity.
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Dedupe — gold_set.jsonl (individual-only today; a formality) + resume from a
# partially-written output file (the actual resilience payoff).
# ---------------------------------------------------------------------------

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
    """NPIs already written to the output file — the resume/skip set."""
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


# ---------------------------------------------------------------------------
# Surface confirmation — facility (in-memory fuzzy match) / Medicaid (single
# duckdb connection, local parquet) / Medicare (check_billing_surface, local-only)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def build_proposal_row(*, entity_name: str, match: OrgMatch, label_class: str, surface_confirmed: str,
                        source: str, source_url: str, pr_date: str, body: str,
                        venue_match: str, identity_confidence: str,
                        identity_flag: str | None = None,
                        identity_candidates: list[dict] | None = None) -> dict:
    disp_type = classify_disposition_type(body)
    amount = extract_amount_usd(body, entity_name)
    return {
        "label_class": label_class,
        "defendant_name": entity_name,
        "org_npi": match.npi,
        "state": match.state,
        "scheme_summary": scheme_summary_excerpt(body, entity_name),
        "source_url": source_url,
        "disposition": {"type": disp_type, "date": pr_date, "amount_usd": amount},
        "disposition_finality": classify_disposition_finality(body, entity_name),
        "venue_match": venue_match,
        "surface_confirmed": surface_confirmed,
        "dedupe_checked": True,
        "confidence": match.confidence,
        "identity_confidence": identity_confidence,
        "identity_flag": identity_flag,
        "identity_candidates": identity_candidates,
        "nppes_org_name": match.nppes_org_name,
        "source": source,
    }


def _append_flush(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(row) + "\n")
        f.flush()


def render_md(rows: list[dict], *, generated: str) -> str:
    n = len(rows)
    by_class: dict[str, int] = {}
    by_finality: dict[str, int] = {}
    for r in rows:
        by_class[r["label_class"]] = by_class.get(r["label_class"], 0) + 1
        fin = r.get("disposition_finality", "unknown")
        by_finality[fin] = by_finality.get(fin, 0) + 1
    lines = [
        f"# Entity/Medicaid/facility label-mining PROPOSALS — {generated} sweep",
        "",
        "DOJ + NY OMIG + CA OAG DMFEA press releases, organizational (Type-2 NPI) defendants, "
        "NPPES-resolved, Medicaid/facility/Medicare surface-confirmed, NOT already in "
        "`data/extracted/gold_set.jsonl`. **These are PROPOSALS for maintainer (a teammate) keep/reject "
        "review — nothing has been written to gold_set.jsonl or battery.json.** "
        "`disposition_finality`/`venue_match`/`identity_confidence` are best-effort, entity-scoped "
        "heuristics (see scripts/mine_entity_labels.py) — still proposals, not adjudicated facts.",
        "",
        f"{n} proposal(s): " + " · ".join(f"{k}={by_class.get(k, 0)}" for k in ("facility", "medicaid", "org")),
        "Disposition finality: " + " · ".join(
            f"{k}={by_finality.get(k, 0)}" for k in ("resolved", "contested", "unknown")),
        "",
        "| # | Entity | Org NPI | State | Label class | Disposition | Finality | Venue | Date | "
        "Amount (USD) | Identity conf | Surface | Source |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(rows, 1):
        amt = r["disposition"].get("amount_usd")
        amt_s = f"${amt:,.0f}" if amt else "—"
        lines.append(
            f"| {i} | {r['defendant_name']} | {r['org_npi']} | {r.get('state') or '—'} | "
            f"{r['label_class']} | {r['disposition']['type']} | {r.get('disposition_finality', '—')} | "
            f"{r.get('venue_match', '—')} | {r['disposition']['date']} | {amt_s} | "
            f"{r.get('identity_confidence', r.get('confidence', '—'))} | {r['surface_confirmed']} | "
            f"[{r['source']}]({r['source_url']}) |"
        )
    lines += ["", "## Scheme summaries", ""]
    for i, r in enumerate(rows, 1):
        lines.append(f"### {i}. {r['defendant_name']} (NPI {r['org_npi']})")
        lines.append(f"- **Label class:** {r['label_class']}  ·  **Surface:** {r['surface_confirmed']}")
        lines.append(f"- **NPPES org name:** {r['nppes_org_name']}")
        lines.append(f"- **Scheme:** {r['scheme_summary']}")
        if r.get("identity_flag"):
            cands = ", ".join(f"NPI {c['npi']} ({c.get('state') or '?'})" for c in (r.get("identity_candidates") or []))
            lines.append(f"- **Identity flag:** {r['identity_flag']} — candidates: {cands}")
        lines.append(f"- **Source:** {r['source_url']}")
        lines.append("")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run(*, since: str, until: str, sources: list[str], out_path: Path,
        max_title_calls: int, max_state_pages: int, limit: int | None, log=print) -> dict:
    gold_npis = load_gold_npis()
    seen_npis = load_existing_proposal_npis(out_path)
    n_resumed = len(seen_npis)
    log(f"gold_set NPIs: {len(gold_npis)} (individual-only; formality dedupe) | "
        f"already in {out_path.name}: {n_resumed} (resume-skip set)")

    log("Loading surface indexes once (facility rosters, Medicaid connection, Medicare panels)...")
    facility_idx = FacilityIndex()
    medicaid_surf = MedicaidSurface()
    medicare_surf = MedicareSurface()
    log(f"  facility rosters loaded: {list(facility_idx.by_type)}")
    log(f"  medicaid surface available: {medicaid_surf.con is not None}")
    log(f"  medicare surface years: {medicare_surf.years} (NP_R2_DISABLE=1, local-only)")

    releases: list[dict] = []
    if "doj" in sources:
        log("=== DOJ discovery ===")
        releases.extend(discover_doj(since, until, max_title_calls=max_title_calls, log=log))
    for st in ("ny_omig", "ca_oag"):
        if st in sources:
            log(f"=== {st} discovery ===")
            releases.extend(discover_state_source(st, since, until, max_pages=max_state_pages, log=log))

    log(f"\n{len(releases)} entity-relevant release(s) across sources; extracting candidates...")
    candidates: dict[str, dict] = {}   # lowercased raw name -> first-seen release context
    for rel in releases:
        for name in extract_entity_candidates(rel["body"]):
            key = name.lower()
            if key not in candidates:
                candidates[key] = {"name": name, "release": rel}
    log(f"{len(candidates)} unique candidate entity name(s) extracted")

    # Group by NORMALIZED (suffix-stripped) name so "Trinity Home Health Care
    # Inc" and "Trinity Home Health Care" -- two raw candidate strings from
    # ONE press-release mention -- are resolved and decided TOGETHER rather
    # than as independent candidates (twin-NPI guard, bug #4). Every empirical
    # twin in the audited batch shared a source release; grouping globally
    # (not per-release) is a deliberate, slightly wider net for the rare
    # cross-release case of the same real entity mentioned twice.
    norm_groups: dict[str, list[dict]] = {}
    for item in candidates.values():
        norm_groups.setdefault(_norm_org_name(item["name"]), []).append(item)

    n_nppes_none = n_dropped_dup = n_no_surface = n_written = 0
    n_too_generic = n_venue_dropped = n_ambiguous_multi = 0
    group_items = list(norm_groups.values())
    if limit:
        group_items = group_items[:limit]
    for group in group_items:
        kept = [it for it in group if not is_name_too_generic(it["name"])]
        n_too_generic += len(group) - len(kept)
        if not kept:
            continue

        resolved: list[tuple[str, OrgMatch, dict]] = []
        for it in kept:
            match = resolve_org_npi(it["name"], log=log)
            if match is None:
                n_nppes_none += 1
                continue
            if match.npi in gold_npis or match.npi in seen_npis:
                n_dropped_dup += 1
                continue
            resolved.append((it["name"], match, it["release"]))
        if not resolved:
            continue

        name, match, rel, identity_flag, identity_candidates = resolve_twin_candidates(resolved)
        if identity_flag:
            n_ambiguous_multi += 1

        facility_hit = facility_idx.match(name, match.state)
        medicaid_hit = medicaid_surf.has_surface(match.npi)
        medicare_hit = medicare_surf.has_surface(match.npi)
        picked = pick_label_class(facility_hit, medicaid_hit, medicare_hit)
        if picked is None:
            n_no_surface += 1
            continue
        label_class, surface_confirmed = picked

        venue_state = extract_venue_state(rel)
        venue_match = classify_venue_match(venue_state, match.state)
        # Geography-blind match guard (bug #2): for a single-site facility
        # (hospice/HHA/SNF/hospital), a state that contradicts the case venue
        # is a real wrong-entity red flag -- drop rather than propose. For
        # medicaid/org classes (labs/DME/telehealth), multi-state billing is
        # normal, so a mismatch only demotes identity_confidence below, it
        # doesn't drop the row. A group already flagged ambiguous is exempt
        # from this drop -- the point of that flag is to SURFACE the
        # uncertainty to the human reviewer, not to silently swallow it.
        if label_class == "facility" and venue_match == "mismatch" and not identity_flag:
            n_venue_dropped += 1
            continue
        identity_confidence = compute_identity_confidence(
            match.confidence, venue_match, ambiguous=bool(identity_flag))

        row = build_proposal_row(
            entity_name=name, match=match, label_class=label_class, surface_confirmed=surface_confirmed,
            source=rel["source"], source_url=rel["url"], pr_date=rel["date"], body=rel["body"],
            venue_match=venue_match, identity_confidence=identity_confidence,
            identity_flag=identity_flag, identity_candidates=identity_candidates,
        )
        _append_flush(out_path, row)
        seen_npis.add(match.npi)
        n_written += 1
        log(f"  + [{label_class}] {name} -> NPI {match.npi} ({match.state}) "
            f"conf={match.confidence}/{identity_confidence} venue={venue_match} surface={surface_confirmed}"
            + (f" IDENTITY_FLAG={identity_flag}" if identity_flag else ""))

    funnel = {
        "n_releases": len(releases), "n_candidates": len(candidates), "n_norm_groups": len(norm_groups),
        "n_too_generic": n_too_generic, "n_nppes_none": n_nppes_none, "n_dropped_dup": n_dropped_dup,
        "n_venue_dropped": n_venue_dropped, "n_ambiguous_multi": n_ambiguous_multi,
        "n_no_surface": n_no_surface, "n_written_this_run": n_written,
        "n_resumed_from_prior_run": n_resumed,
    }
    log(f"\nFunnel: {funnel}")
    return funnel


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--since", default="2020-01-01")
    ap.add_argument("--until", default=date.today().isoformat())
    ap.add_argument("--sources", default="doj,ny_omig,ca_oag",
                     help="comma-separated subset of doj,ny_omig,ca_oag")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--max-doj-title-calls", type=int, default=6)
    ap.add_argument("--max-state-pages", type=int, default=40)
    ap.add_argument("--limit", type=int, default=None, help="cap candidates processed (smoke testing)")
    args = ap.parse_args(argv)

    out_path = Path(args.out)
    sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    funnel = run(since=args.since, until=args.until, sources=sources, out_path=out_path,
                 max_title_calls=args.max_doj_title_calls, max_state_pages=args.max_state_pages,
                 limit=args.limit)

    rows = []
    if out_path.exists():
        with out_path.open() as f:
            for line in f:
                if line.strip():
                    rows.append(json.loads(line))
    md_path = out_path.with_suffix(".md")
    md_path.write_text(render_md(rows, generated=date.today().isoformat()))
    print(f"\n{len(rows)} total proposal(s) (cumulative) -> {out_path}")
    print(f"Review table -> {md_path}")
    print(f"This run: {funnel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
