"""
USAO press release discovery — find healthcare-fraud-relevant URLs across
all 94 US Attorneys' Offices.

Each USAO publishes its own press release feed at /usao-{district}/news
and /usao-{district}/pr/{slug}. These are paginated 1-indexed via
?page=N. C.D. Cal alone has ~3,300 press releases across 276 pages.
Across 94 districts that's potentially 200,000+ URLs — most NOT
healthcare-fraud relevant.

Discovery strategy: walk each district's listing pages, extract
press-release URLs, filter by URL slug for healthcare-fraud keywords.
Slugs are descriptive (e.g. "doctor-sentenced-medicare-fraud") so this
catches most relevant cases without fetching the page body.

This is a SEPARATE scraper from doj_press because:
- It produces seed URLs only (not archive entries)
- The output is a JSON file of new URLs to feed back to doj_press
- It's optional (the v1 seed already has ~1,500 USAO URLs)

Usage:
    python -m scripts.scrape.sources.usao_discovery \\
        --districts cdca,edny,sdny,ndga,sdfl \\
        --max-listing-pages 50 \\
        --output data/raw/_index/usao_discovered.jsonl

Then feed back into the DOJ scraper:
    python -m scripts.scrape.cli scrape doj \\
        --seed-file data/raw/_index/usao_discovered.jsonl
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..fetcher import fetch_url


# Healthcare-fraud relevant keywords. URL slug must contain at least one
# of these to be retained. Keywords are deliberately broad — Stage 2 LLM
# extraction does the precise classification.
HEALTHCARE_KEYWORDS = [
    "medicare",
    "medicaid",
    "health-care",
    "healthcare",
    "kickback",
    "fraud",  # very broad — catches general fraud cases too; LLM filters in Stage 2
    "false-claim",
    "qui-tam",
    "opioid",
    "prescri",  # prescriber, prescription
    "doctor",
    "physician",
    "nurse",
    "hospital",
    "hospice",
    "pharmacy",
    "clinic",
    "dental",
    "telehealth",
    "telemedicine",
    "lab",
    "genetic-test",
    "billing",
    "anti-kickback",
    "stark",
    "cms",
    "tricare",
    "champva",
    "pill-mill",
    "dme",  # durable medical equipment
    "addiction-treatment",
]

# All 94 US Attorneys' Offices — district codes.
# Source: justice.gov/usao
ALL_DISTRICTS = [
    # Alabama
    "almd", "alnd", "alsd",
    # Alaska
    "ak",
    # Arizona
    "az",
    # Arkansas
    "are", "arw",
    # California
    "cdca", "edca", "ndca", "sdca",
    # Colorado
    "co",
    # Connecticut
    "ct",
    # Delaware
    "de",
    # Florida
    "flmd", "flnd", "flsd",
    # Georgia
    "gam", "gan", "gas", "ndga", "mdga", "sdga",
    # Guam / NMI
    "gu",
    # Hawaii
    "hi",
    # Idaho
    "id",
    # Illinois
    "ilcd", "ilnd", "ilsd",
    # Indiana
    "innd", "insd",
    # Iowa
    "ian", "ias", "ndia", "sdia",
    # Kansas
    "ks",
    # Kentucky
    "kyed", "kywd",
    # Louisiana
    "lae", "lam", "law",
    # Maine
    "me",
    # Maryland
    "md",
    # Massachusetts
    "ma",
    # Michigan
    "mie", "miw",
    # Minnesota
    "mn",
    # Mississippi
    "msn", "mss",
    # Missouri
    "mow", "moe",
    # Montana
    "mt",
    # Nebraska
    "ne",
    # Nevada
    "nv",
    # New Hampshire
    "nh",
    # New Jersey
    "nj",
    # New Mexico
    "nm",
    # New York
    "nyed", "nynd", "nysd", "nywd", "edny", "ndny", "sdny", "wdny",
    # North Carolina
    "nce", "ncm", "ncw", "ednc", "mdnc", "wdnc",
    # North Dakota
    "nd",
    # Ohio
    "ohn", "ohs", "ndoh", "sdoh",
    # Oklahoma
    "oke", "okn", "okw",
    # Oregon
    "or",
    # Pennsylvania
    "pae", "pam", "paw",
    # Puerto Rico
    "pr",
    # Rhode Island
    "ri",
    # South Carolina
    "sc",
    # South Dakota
    "sd",
    # Tennessee
    "tnm", "tne", "tnw",
    # Texas
    "txe", "txn", "txs", "txw", "edtx", "ndtx", "sdtx", "wdtx",
    # Utah
    "ut",
    # Vermont
    "vt",
    # Virgin Islands
    "vi",
    # Virginia
    "vae", "vaw",
    # Washington
    "wae", "waw",
    # West Virginia
    "wvn", "wvs",
    # Wisconsin
    "wie", "wiw", "wdwi",
    # Wyoming
    "wy",
]


@dataclass
class DiscoveryStats:
    districts_walked: int = 0
    listing_pages_fetched: int = 0
    total_pr_urls: int = 0
    healthcare_pr_urls: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)


def is_healthcare_relevant(url: str) -> bool:
    """Check if a press-release URL slug contains a healthcare-fraud keyword."""
    slug = url.lower()
    return any(kw in slug for kw in HEALTHCARE_KEYWORDS)


def discover_district(
    district: str, max_listing_pages: int = 50
) -> tuple[set[str], int, int]:
    """
    Walk one USAO district's listing pages, return (filtered_urls, total_seen,
    pages_fetched).

    Pagination quirk (same as OIG): ?page=1 returns the same content as the
    base URL. Iterate [base, ?page=2, ?page=3, ...] and bail after 3
    consecutive empty/duplicate pages.
    """
    base = f"https://www.justice.gov/usao-{district}/news"

    discovered_pr: set[str] = set()
    total_seen: set[str] = set()
    pages_fetched = 0
    consecutive_empty = 0

    page_indices = [0] + list(range(2, max_listing_pages + 1))

    for page in page_indices:
        url = base if page == 0 else f"{base}?page={page}"
        result = fetch_url(url)
        pages_fetched += 1

        if result.http_status != 200 or len(result.body) < 8000:
            consecutive_empty += 1
            if consecutive_empty >= 3:
                break
            continue

        pr_links = set(
            re.findall(
                rf'href="(/usao-{district}/pr/[^"#?]+)"',
                result.body,
            )
        )
        new_links = pr_links - total_seen
        if not new_links:
            consecutive_empty += 1
            if consecutive_empty >= 3:
                break
            continue

        consecutive_empty = 0
        total_seen.update(pr_links)
        for u in pr_links:
            full_url = f"https://www.justice.gov{u}"
            if is_healthcare_relevant(full_url):
                discovered_pr.add(full_url)

    return discovered_pr, len(total_seen), pages_fetched


def run(
    districts: Optional[list[str]] = None,
    max_listing_pages: int = 50,
    output_path: Optional[Path] = None,
) -> tuple[DiscoveryStats, list[str]]:
    """
    Walk USAO listings for the requested districts (or all 94),
    return healthcare-fraud-relevant press release URLs.
    """
    districts = districts or ALL_DISTRICTS
    stats = DiscoveryStats()
    all_urls: set[str] = set()

    for i, district in enumerate(districts, 1):
        print(
            f"[usao_discovery] [{i}/{len(districts)}] usao-{district}: "
            f"walking up to {max_listing_pages} listing pages...",
            flush=True,
        )
        try:
            urls, total, pages = discover_district(
                district, max_listing_pages=max_listing_pages
            )
            stats.districts_walked += 1
            stats.listing_pages_fetched += pages
            stats.total_pr_urls += total
            stats.healthcare_pr_urls += len(urls)
            all_urls.update(urls)
            print(
                f"[usao_discovery]   pages={pages} total_pr={total} "
                f"healthcare_relevant={len(urls)}"
            )
        except Exception as e:  # noqa: BLE001
            stats.failures.append((district, str(e)))
            print(f"[usao_discovery]   FAILED: {e}")

    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8") as f:
            for url in sorted(all_urls):
                f.write(url + "\n")
        print(
            f"\n[usao_discovery] Wrote {len(all_urls)} URLs to {output_path}"
        )

    return stats, sorted(all_urls)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="USAO discovery")
    parser.add_argument(
        "--districts",
        type=str,
        default=None,
        help="Comma-separated list of district codes (default: all 94)",
    )
    parser.add_argument(
        "--max-listing-pages",
        type=int,
        default=50,
        help="Max listing pages per district (default: 50)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="data/raw/_index/usao_discovered.txt",
        help="Output file (one URL per line)",
    )
    args = parser.parse_args()

    districts = (
        args.districts.split(",")
        if args.districts
        else None
    )
    output = Path(args.output) if args.output else None

    stats, urls = run(
        districts=districts,
        max_listing_pages=args.max_listing_pages,
        output_path=output,
    )

    print(f"\n[usao_discovery] Done.")
    print(f"  Districts walked:        {stats.districts_walked}")
    print(f"  Listing pages fetched:   {stats.listing_pages_fetched}")
    print(f"  Total PR URLs seen:      {stats.total_pr_urls}")
    print(f"  Healthcare-relevant:     {stats.healthcare_pr_urls}")
    print(f"  Failures:                {len(stats.failures)}")
    sys.exit(0)
