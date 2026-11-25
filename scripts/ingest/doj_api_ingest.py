"""Systematic DOJ press release ingestion for healthcare fraud cases.

DEPRECATED (2026-06-10) — LEGACY regex bulk ingester. The CANONICAL DOJ extraction path
is now the **corpus_curator pipeline**: discovery via
``pipeline/scrapers/doj_press_releases.py`` → LLM extraction + deterministic gate + merge
via ``pipeline/scrapers/gold_set_builder.py`` (the ``corpus_curator`` agent). The Curator
reads the prose and extracts the per-case amount/defendants/scheme, so it does NOT hit the
``amount_usd = max(amounts)`` Strike-Force-boilerplate trap this script had (that regex path
is kept fixed — PR #233 — only as a safety net). This file is RETAINED because it built the
historical corpus and ``doj_press_releases.py`` reuses its ``_is_healthcare_fraud`` classifier.
Do not use it to refresh gold_set — run the curator pipeline (``gold_set_builder``) instead.

Pulls ALL healthcare fraud press releases from the DOJ public API,
extracts case details, deduplicates against existing gold_set.jsonl,
and outputs new cases ready for NPI resolution.

DOJ API: https://www.justice.gov/developer/api-documentation/api_v1
  - Endpoint: /api/v1/press_releases.json
  - Rate limit: 4 req/sec, max 50 results/page
  - No authentication required
  - Filters: title, date, component

Search strategy — 6 query terms to maximize recall:
  1. "health care fraud"     (~3,097 results)
  2. "medicare fraud"        (~1,088 results)
  3. "medicaid fraud"        (~469 results)
  4. "opioid"                (~251+ results)
  5. "False Claims Act"      (~272+ results matching health)
  6. "kickback"              (~300+ results matching health)

After dedup by URL, expect ~4,000-5,000 unique press releases.

Usage:
    # Pull all healthcare fraud press releases
    python scripts/ingest/doj_api_ingest.py --pull

    # Extract cases from pulled press releases
    python scripts/ingest/doj_api_ingest.py --extract

    # Deduplicate against existing gold_set.jsonl
    python scripts/ingest/doj_api_ingest.py --dedup

    # Full pipeline
    python scripts/ingest/doj_api_ingest.py --all
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote

import requests

ROOT = Path(__file__).resolve().parents[2]
RAW_DIR = ROOT / "data" / "source" / "doj_api"
CASES_V0 = ROOT / "data" / "source" / "cases" / "gold_set.jsonl"
OUTPUT = RAW_DIR / "new_cases.jsonl"

API_BASE = "https://www.justice.gov/api/v1/press_releases.json"
PAGE_SIZE = 50
RATE_LIMIT_DELAY = 0.3  # seconds between requests (conservative, <4/sec)

# Search terms — focused on Medicare fraud and False Claims Act
# (not generic "opioid" or "controlled substance" which pull non-Medicare cases)
SEARCH_TERMS = [
    "medicare fraud",
    "health care fraud",
    "False Claims Act",
    "medicare kickback",
    "anti-kickback statute",
    "medicare home health",
    "medicare hospice fraud",
    "medicare telemedicine fraud",
    "medicare durable medical equipment",
    "medicare pill mill",
    "medicare genetic testing",
    "medicare phantom billing",
    "TRICARE fraud",
    "health care fraud takedown",
    "Medicare Strike Force",
]

# REQUIRED: press release must mention at least one of these (Medicare/FCA specific)
MEDICARE_FCA_REQUIRED = {
    "medicare", "false claims act", "anti-kickback",
    "tricare", "health care fraud", "healthcare fraud",
    "cms", "hhs-oig", "oig",
}

# Additional healthcare billing keywords (need >=2 of these on top of required)
HC_BILLING_KEYWORDS = {
    "billing", "claims", "reimbursement", "beneficiary",
    "provider", "physician", "doctor", "nurse practitioner",
    "hospital", "clinic", "pharmacy", "home health",
    "hospice", "telemedicine", "telehealth", "dme",
    "durable medical equipment", "laboratory", "diagnostic",
    "medically unnecessary", "upcoding", "phantom",
    "kickback", "stark law", "self-referral",
    "opioid", "controlled substance", "pill mill",
    "genetic testing", "compounding",
}


def _is_healthcare_fraud(pr: dict) -> bool:
    """Filter press release to Medicare fraud / False Claims Act only.

    Must mention Medicare/FCA/OIG explicitly, plus at least 2 billing keywords.
    """
    text = (
        (pr.get("title") or "") + " " +
        (pr.get("body") or "") + " " +
        (pr.get("teaser") or "")
    ).lower()

    # MUST mention Medicare, FCA, or OIG
    has_required = any(kw in text for kw in MEDICARE_FCA_REQUIRED)
    if not has_required:
        return False

    # Plus at least 2 billing-specific keywords
    billing_matches = sum(1 for kw in HC_BILLING_KEYWORDS if kw in text)
    return billing_matches >= 2


def _clean_html(text: str) -> str:
    """Strip HTML tags and decode entities."""
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _parse_date(date_str: str) -> str | None:
    """Parse DOJ API date (Unix timestamp string) to ISO date."""
    try:
        ts = int(date_str)
        from datetime import datetime, timezone
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return None


# Medicare Fraud Strike Force press releases routinely cite the program's CUMULATIVE
# billings ("...has charged X defendants who collectively billed approximately $14
# billion...") — NOT this case's amount. The old `max(amounts)` grabbed that boilerplate
# and contaminated 141 gold_set rows (decontaminated in PR #166; this stops the ingester
# from re-introducing it on the next run). Two guards below: a per-case ceiling and a
# cumulative-context filter.
_PER_CASE_CEILING_USD = 1_000_000_000  # no single Medicare-fraud case is >= $1B; those are program totals
_CUMULATIVE_CONTEXT = re.compile(
    r"strike force|since its inception|collectively|in the aggregate", re.IGNORECASE,
)


def _extract_dollar_amounts(text: str) -> list[float]:
    """Extract per-case dollar amounts from text, EXCLUDING Strike-Force cumulative
    boilerplate (larger than any single case; was contaminating gold_set via max())."""
    amounts = []
    for m in re.finditer(r"\$\s*([\d,.]+)\s*(million|billion|thousand)?", text, re.IGNORECASE):
        try:
            val = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        mult = (m.group(2) or "").lower()
        if mult == "billion":
            val *= 1_000_000_000
        elif mult == "million":
            val *= 1_000_000
        elif mult == "thousand":
            val *= 1_000
        if val >= _PER_CASE_CEILING_USD:
            continue  # cumulative program/strike-force total, never a single case
        # Cumulative-context markers PRECEDE the figure ("...collectively billed $14
        # billion"); look backward + the match itself (no forward, to avoid bridging
        # into an adjacent sentence and dropping a legitimate per-case amount).
        window = text[max(0, m.start() - 140):m.end()]
        if _CUMULATIVE_CONTEXT.search(window):
            continue  # Strike-Force cumulative-billings boilerplate, not this case's amount
        amounts.append(val)
    return amounts


def _extract_names(text: str) -> list[str]:
    """Extract potential defendant names from press release text."""
    names = []

    # Pattern: "FirstName LastName, age XX" or "FirstName LastName, XX, of City"
    for m in re.finditer(
        r"([A-Z][a-z]+(?:\s+[A-Z]\.?)?\s+[A-Z][a-z]+(?:-[A-Z][a-z]+)?)"
        r"(?:,\s*(?:age\s*)?\d{2,3}|,\s*\d{2,3},\s*of\s)",
        text,
    ):
        names.append(m.group(1).strip())

    # Pattern: "Dr. FirstName LastName" or "Dr. FirstName M. LastName"
    for m in re.finditer(
        r"Dr\.\s+([A-Z][a-z]+(?:\s+[A-Z]\.?)?\s+[A-Z][a-z]+(?:-[A-Z][a-z]+)?)",
        text,
    ):
        names.append(m.group(1).strip())

    return list(dict.fromkeys(names))  # dedup preserving order


def _classify_fraud_types(text: str) -> list[str]:
    """Classify fraud types from press release text."""
    text_lower = text.lower()
    types = []

    fraud_patterns = {
        "phantom_billing": [
            "phantom", "never rendered", "not provided", "fabricated",
            "false claims", "bogus", "sham",
        ],
        "upcoding": [
            "upcod", "higher-level", "higher level", "inflated codes",
            "99215", "99214",
        ],
        "kickback": [
            "kickback", "anti-kickback", "illegal remuneration",
            "referral fee", "inducement",
        ],
        "stark_violation": [
            "stark law", "stark violation", "self-referral",
        ],
        "opioid_diversion": [
            "opioid", "controlled substance", "pill mill",
            "fentanyl", "oxycodone", "hydrocodone", "drug diversion",
            "unlawful distribution", "unlawful prescri",
        ],
        "medically_unnecessary": [
            "medically unnecessary", "not medically necessary",
            "unnecessary procedure", "unnecessary test",
            "unnecessary service", "unnecessary treatment",
        ],
        "telemedicine_fraud": [
            "telemedicine", "telehealth", "tele-medicine",
        ],
        "genetic_testing_fraud": [
            "genetic test", "dna test", "cancer screening",
            "pharmacogenomic",
        ],
        "dme_scheme": [
            "durable medical equipment", "dme", "brace",
            "wheelchair", "ortho", "prosthe",
        ],
        "lab_requisition_fraud": [
            "lab test", "laboratory", "urine drug test",
            "toxicology", "blood test",
        ],
        "hospice_eligibility": [
            "hospice", "not terminally ill", "hospice eligib",
        ],
        "hha_poc_falsification": [
            "home health", "home care", "hha",
            "plan of care", "certif",
        ],
        "addiction_treatment_fraud": [
            "addiction treatment", "substance abuse treatment",
            "sober home", "sober living", "rehab",
        ],
        "compounding_fraud": [
            "compounding", "compound pharmacy", "compound cream",
        ],
        "npi_theft": [
            "stolen identit", "identity theft", "stolen npi",
            "used the name", "impersonat",
        ],
    }

    for ftype, patterns in fraud_patterns.items():
        if any(p in text_lower for p in patterns):
            types.append(ftype)

    if not types:
        types.append("phantom_billing")  # default

    return types


def _extract_states(text: str) -> list[str]:
    """Extract US state references from text."""
    state_patterns = {
        "AL": r"\bAlabama\b", "AK": r"\bAlaska\b", "AZ": r"\bArizona\b",
        "AR": r"\bArkansas\b", "CA": r"\bCalifornia\b", "CO": r"\bColorado\b",
        "CT": r"\bConnecticut\b", "DE": r"\bDelaware\b", "FL": r"\bFlorida\b",
        "GA": r"\bGeorgia\b", "HI": r"\bHawaii\b", "ID": r"\bIdaho\b",
        "IL": r"\bIllinois\b", "IN": r"\bIndiana\b", "IA": r"\bIowa\b",
        "KS": r"\bKansas\b", "KY": r"\bKentucky\b", "LA": r"\bLouisiana\b",
        "ME": r"\bMaine\b", "MD": r"\bMaryland\b", "MA": r"\bMassachusetts\b",
        "MI": r"\bMichigan\b", "MN": r"\bMinnesota\b", "MS": r"\bMississippi\b",
        "MO": r"\bMissouri\b", "MT": r"\bMontana\b", "NE": r"\bNebraska\b",
        "NV": r"\bNevada\b", "NH": r"\bNew Hampshire\b", "NJ": r"\bNew Jersey\b",
        "NM": r"\bNew Mexico\b", "NY": r"\bNew York\b", "NC": r"\bNorth Carolina\b",
        "ND": r"\bNorth Dakota\b", "OH": r"\bOhio\b", "OK": r"\bOklahoma\b",
        "OR": r"\bOregon\b", "PA": r"\bPennsylvania\b", "RI": r"\bRhode Island\b",
        "SC": r"\bSouth Carolina\b", "SD": r"\bSouth Dakota\b", "TN": r"\bTennessee\b",
        "TX": r"\bTexas\b", "UT": r"\bUtah\b", "VT": r"\bVermont\b",
        "VA": r"\bVirginia\b", "WA": r"\bWashington\b", "WV": r"\bWest Virginia\b",
        "WI": r"\bWisconsin\b", "WY": r"\bWyoming\b",
    }
    states = []
    for code, pattern in state_patterns.items():
        if re.search(pattern, text):
            states.append(code)
    return states


# ============================================================
# Phase 1: Pull all press releases from DOJ API
# ============================================================

def pull_press_releases() -> list[dict]:
    """Pull all healthcare fraud press releases from DOJ API."""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = RAW_DIR / "raw_press_releases.jsonl"

    # Check cache
    if cache_path.exists():
        print(f"Loading cached press releases from {cache_path}")
        prs = []
        with open(cache_path) as f:
            for line in f:
                prs.append(json.loads(line))
        print(f"  {len(prs)} cached")
        return prs

    all_prs = {}  # dedup by URL

    for term in SEARCH_TERMS:
        print(f"\nSearching: \"{term}\"")
        page = 0
        term_count = 0

        while True:
            url = (
                f"{API_BASE}?pagesize={PAGE_SIZE}"
                f"&page={page}"
                f"&parameters%5Btitle%5D={quote(term)}"
            )
            try:
                resp = requests.get(url, timeout=30)
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                print(f"  Error on page {page}: {e}")
                break

            results = data.get("results", [])
            if not results:
                break

            total = int(data["metadata"]["resultset"]["count"])
            for pr in results:
                pr_url = pr.get("url", "")
                if pr_url and pr_url not in all_prs:
                    all_prs[pr_url] = pr
                    term_count += 1

            page += 1
            fetched = page * PAGE_SIZE
            print(f"  Page {page}: {len(results)} results "
                  f"({fetched}/{total}, {term_count} new)", end="\r")

            if fetched >= total:
                break

            time.sleep(RATE_LIMIT_DELAY)

        print(f"  \"{term}\": {term_count} new (total unique: {len(all_prs)})")

    # Save to cache
    print(f"\nTotal unique press releases: {len(all_prs)}")
    with open(cache_path, "w") as f:
        for pr in all_prs.values():
            f.write(json.dumps(pr) + "\n")
    print(f"Saved to {cache_path}")

    return list(all_prs.values())


# ============================================================
# Phase 2: Filter to healthcare fraud and extract case details
# ============================================================

def extract_cases(press_releases: list[dict]) -> list[dict]:
    """Extract structured case info from healthcare fraud press releases."""
    cases = []

    for pr in press_releases:
        # Filter to healthcare fraud
        if not _is_healthcare_fraud(pr):
            continue

        title = _clean_html(pr.get("title", ""))
        body = _clean_html(pr.get("body", ""))
        full_text = f"{title} {body}"
        date = _parse_date(pr.get("date", ""))
        url = pr.get("url", "")

        # Extract case details
        defendants = _extract_names(body)
        fraud_types = _classify_fraud_types(full_text)
        amounts = _extract_dollar_amounts(full_text)
        states = _extract_states(full_text)

        # Parse year from date
        year = int(date[:4]) if date else None

        # Build case ID
        if defendants:
            name_slug = re.sub(r"[^a-z0-9]", "-", defendants[0].lower())[:30]
        else:
            name_slug = re.sub(r"[^a-z0-9]", "-", title.lower())[:30]
        case_id = f"DOJ-{year or 'unknown'}-{name_slug}"

        case = {
            "case_id": case_id,
            "case_name": title[:200],
            "scheme_summary": body[:500] if body else title,
            "defendants": [
                {
                    "name": name,
                    "role": "unknown",
                    "state": states[0] if states else "",
                    "specialty": "",
                    "npi": None,
                    "npi_confidence": "unresolved",
                }
                for name in defendants[:10]  # cap at 10 defendants
            ],
            "fraud_types": fraud_types,
            "active_years": [year] if year else [],
            "payer_lines": [],
            "outcome": {
                "type": "indictment",
                "date": date,
                "amount_usd": max(amounts) if amounts else None,
            },
            "sources": [
                {
                    "url": url,
                    "kind": "doj_press_release",
                    "accessed": "2026-04-30",
                }
            ],
            "tier": "C",  # unverified until NPI resolution
            "inclusion_notes": "Auto-extracted from DOJ API",
            "ingest_source": "doj_api",
            "split": None,  # assigned during NPI resolution
        }

        # Upgrade tier based on detail level
        if defendants and amounts:
            case["tier"] = "B"
        if len(defendants) >= 2 and amounts and states:
            case["tier"] = "A"

        cases.append(case)

    return cases


# ============================================================
# Phase 3: Deduplicate against existing cases
# ============================================================

def deduplicate(new_cases: list[dict]) -> tuple[list[dict], int]:
    """Remove cases already in gold_set.jsonl."""
    # Load existing cases
    existing_urls = set()
    existing_names = set()
    existing_ids = set()

    if CASES_V0.exists():
        with open(CASES_V0) as f:
            for line in f:
                case = json.loads(line)
                existing_ids.add(case.get("case_id", ""))
                for src in case.get("sources", []):
                    existing_urls.add(src.get("url", ""))
                for d in case.get("defendants", []):
                    name = d.get("name", "").lower().strip()
                    if name:
                        existing_names.add(name)

    print(f"Existing corpus: {len(existing_ids)} cases, "
          f"{len(existing_urls)} URLs, {len(existing_names)} defendant names")

    novel = []
    dupes = 0

    for case in new_cases:
        # Check URL match
        case_urls = {s["url"] for s in case.get("sources", [])}
        if case_urls & existing_urls:
            dupes += 1
            continue

        # Check defendant name overlap (fuzzy)
        case_names = {d["name"].lower().strip() for d in case.get("defendants", []) if d.get("name")}
        if case_names and (case_names & existing_names):
            dupes += 1
            continue

        novel.append(case)

    return novel, dupes


# ============================================================
# Main
# ============================================================

def main():
    ap = argparse.ArgumentParser(description="DOJ API healthcare fraud case ingestion")
    ap.add_argument("--pull", action="store_true", help="Pull press releases from DOJ API")
    ap.add_argument("--extract", action="store_true", help="Extract cases from pulled PRs")
    ap.add_argument("--dedup", action="store_true", help="Deduplicate against existing corpus")
    ap.add_argument("--all", action="store_true", help="Run full pipeline")
    ap.add_argument("--stats", action="store_true", help="Print stats only")
    args = ap.parse_args()

    if args.all:
        args.pull = args.extract = args.dedup = True

    if not any([args.pull, args.extract, args.dedup, args.stats]):
        args.all = True
        args.pull = args.extract = args.dedup = True

    print("=" * 70)
    print("DOJ API HEALTHCARE FRAUD CASE INGESTION")
    print("=" * 70)

    # Phase 1: Pull
    raw_path = RAW_DIR / "raw_press_releases.jsonl"
    if args.pull:
        print("\n=== Phase 1: Pull press releases ===")
        prs = pull_press_releases()
    elif raw_path.exists():
        prs = []
        with open(raw_path) as f:
            for line in f:
                prs.append(json.loads(line))
        print(f"Loaded {len(prs)} cached press releases")
    else:
        print("No cached press releases. Run with --pull first.")
        return 1

    # Phase 2: Extract
    extracted_path = RAW_DIR / "extracted_cases.jsonl"
    if args.extract:
        print(f"\n=== Phase 2: Extract cases from {len(prs)} press releases ===")
        cases = extract_cases(prs)
        print(f"  Healthcare fraud press releases: {len(cases)}")
        print(f"  With named defendants: {sum(1 for c in cases if c['defendants'])}")
        print(f"  With dollar amounts: {sum(1 for c in cases if c['outcome']['amount_usd'])}")

        # Fraud type distribution
        from collections import Counter
        ft_counts = Counter()
        for c in cases:
            ft_counts.update(c["fraud_types"])
        print(f"\n  Fraud type distribution:")
        for ft, n in ft_counts.most_common(15):
            print(f"    {ft}: {n}")

        # Year distribution
        year_counts = Counter()
        for c in cases:
            for y in c.get("active_years", []):
                year_counts[y] += 1
        print(f"\n  Year distribution:")
        for y in sorted(year_counts.keys())[-10:]:
            print(f"    {y}: {year_counts[y]}")

        # Save extracted
        with open(extracted_path, "w") as f:
            for case in cases:
                f.write(json.dumps(case) + "\n")
        print(f"\n  Saved {len(cases)} cases to {extracted_path}")
    elif extracted_path.exists():
        cases = []
        with open(extracted_path) as f:
            for line in f:
                cases.append(json.loads(line))
        print(f"Loaded {len(cases)} extracted cases")
    else:
        print("No extracted cases. Run with --extract first.")
        return 1

    # Phase 3: Deduplicate
    if args.dedup:
        print(f"\n=== Phase 3: Deduplicate against gold_set.jsonl ===")
        novel, dupes = deduplicate(cases)
        print(f"  Total extracted: {len(cases)}")
        print(f"  Duplicates (already in corpus): {dupes}")
        print(f"  NOVEL cases: {len(novel)}")

        if novel:
            # Tier distribution
            tier_counts = Counter(c["tier"] for c in novel)
            print(f"\n  Novel cases by tier:")
            for t in ("A", "B", "C"):
                print(f"    Tier {t}: {tier_counts.get(t, 0)}")

            # With defendants
            with_defendants = sum(1 for c in novel if c["defendants"])
            print(f"  With named defendants: {with_defendants}")
            print(f"  Ready for NPI resolution: {with_defendants}")

            # Save
            with open(OUTPUT, "w") as f:
                for case in novel:
                    f.write(json.dumps(case) + "\n")
            print(f"\n  Saved {len(novel)} novel cases to {OUTPUT}")
            print(f"\n  NEXT STEP: Run NPI resolution on {OUTPUT}")
            print(f"  to match defendant names to NPIs via NPPES lookup.")
        else:
            print("\n  No novel cases found — corpus is up to date.")

    print(f"\n{'=' * 70}")
    print(f"DONE")
    print(f"{'=' * 70}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
