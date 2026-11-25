"""
DOJ press release scraper with Akamai bypass.

DOJ.gov is protected by Akamai bot mitigation that returns a JS interstitial
on first contact. The fetcher in `scripts.scrape.fetcher` automatically
detects the interstitial and falls back to the curl_cffi-based solver
(see `akamai_solver.py`). With that bypass in place, every URL pattern
is reachable:

- /opa/pr/{slug}            DOJ HQ recent press releases
- /archives/opa/pr/{slug}   DOJ HQ archived press releases
- /usao-{district}/pr/{slug} USAO district press releases
- /news?topic=tt117&page=N  health-care-fraud topic listing (paginated)

Discovery:
- SEED MODE (default): pull URLs from v1 corpus + scrape archive index
- LISTING MODE: walk /news?topic=tt117 paginated listings to find new URLs
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

from ..fetcher import fetch_url
from ..raw_archive import (
    REPO_ROOT,
    archived_urls,
    write_archive,
)

SOURCE = "doj_press"
V1_CORPUS_PATH = REPO_ROOT / "data" / "source" / "cases" / "gold_set.jsonl"

# DOJ News topic codes (URL parameter ?topic=...)
TOPIC_HEALTH_CARE_FRAUD = "tt117"

LISTING_BASE = "https://www.justice.gov/news"


@dataclass
class ScrapeStats:
    seed_urls_found: int = 0
    listing_urls_discovered: int = 0
    urls_already_archived: int = 0
    urls_newly_archived: int = 0
    urls_failed: int = 0
    failure_modes: dict[str, int] = field(default_factory=dict)


def extract_seed_urls_from_v1() -> list[str]:
    """
    Extract all DOJ source URLs from the v1 corpus.

    Returns deduplicated, normalized list. Filters non-press-release URLs.
    """
    if not V1_CORPUS_PATH.exists():
        return []

    urls: set[str] = set()
    with V1_CORPUS_PATH.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                case = json.loads(line)
            except json.JSONDecodeError:
                continue
            for src in case.get("sources", []):
                url = src.get("url", "") if isinstance(src, dict) else str(src)
                if not url:
                    continue
                url = re.sub(r"^http://", "https://", url)
                if url.startswith("https://justice.gov"):
                    url = url.replace(
                        "https://justice.gov", "https://www.justice.gov", 1
                    )
                if "justice.gov" not in url or "/pr/" not in url:
                    continue
                url = re.sub(r"[?#].*$", "", url)
                url = re.sub(r"/(USAO|Usao)-", "/usao-", url)
                urls.add(url)
    return sorted(urls)


def extract_year_from_body(body: str) -> Optional[int]:
    """Extract publication year from DOJ press release body."""
    m = re.search(r'article:published_time"\s+content="(\d{4})-', body)
    if m:
        return int(m.group(1))
    m = re.search(
        r"(?:January|February|March|April|May|June|July|August|"
        r"September|October|November|December)\s+\d{1,2},?\s+(\d{4})",
        body,
    )
    if m:
        return int(m.group(1))
    return None


def fetch_and_archive(url: str) -> tuple[bool, Optional[str]]:
    """Fetch a single DOJ URL and archive it. Idempotent.

    Applies the in-scope slug filter (Layer 1) BEFORE the HTTP request — pages
    whose URL slug clearly indicates non-healthcare-fraud content are skipped
    cheap. The deeper title+body filter runs later in the pipeline (Layer 2).
    """
    if url in archived_urls(SOURCE):
        return True, None

    # Layer 1: cheap slug-only filter
    try:
        from scripts.scrape.in_scope_filter import slug_in_scope

        ok, reason = slug_in_scope(url)
        if not ok:
            return False, f"filtered_pre_fetch_{reason}"
    except ImportError:
        # Filter module not present — fall through to fetch (backward compat)
        pass

    result = fetch_url(url)

    if result.http_status != 200:
        return False, f"http_{result.http_status}"

    if len(result.body) < 8000:
        if "akamai" in result.body.lower() or "bm-verify" in result.body:
            return False, "akamai_unsolved"
        return False, f"body_too_small_{len(result.body)}"

    year = extract_year_from_body(result.body)

    try:
        write_archive(
            source=SOURCE,
            url=url,
            raw_html=result.body,
            http_status=result.http_status,
            final_url=result.final_url,
            scrape_method=result.method,
            year=year,
            redirect_chain=result.redirect_chain,
            extra={"duration_ms": result.duration_ms},
        )
    except FileExistsError:
        return True, None

    return True, None


# ----------------------------------------------------------------------
# Listing-page discovery (for delta — finding URLs not yet in v1 seed)
# ----------------------------------------------------------------------


def discover_from_listing(
    topic: str = TOPIC_HEALTH_CARE_FRAUD,
    max_pages: int = 50,
) -> list[str]:
    """
    Walk /news?topic={topic}&page=N pages and extract press-release URLs.

    DOJ listings paginate 1-indexed via ?page=N. Each page typically
    contains 10-25 result links. We stop when a page has no new URLs.

    Returns absolute press-release URLs in discovery order (which is
    reverse-chronological per DOJ's default sort).
    """
    discovered: list[str] = []
    seen: set[str] = set()

    for page in range(0, max_pages):
        listing_url = f"{LISTING_BASE}?topic={topic}"
        if page > 0:
            listing_url += f"&page={page}"

        result = fetch_url(listing_url)
        if result.http_status != 200 or len(result.body) < 8000:
            break

        # Press release links: /opa/pr/{slug}, /usao-*/pr/{slug},
        # /archives/opa/pr/{slug}
        page_urls = set(
            re.findall(
                r'href="(/(?:opa|archives/opa|usao-[a-z\-]+)/pr/[^"#?]+)"',
                result.body,
            )
        )
        page_urls_abs = {f"https://www.justice.gov{u}" for u in page_urls}

        new_urls = page_urls_abs - seen
        if not new_urls:
            break
        seen.update(new_urls)
        discovered.extend(sorted(new_urls))

    return discovered


# ----------------------------------------------------------------------
# Main entry
# ----------------------------------------------------------------------


def run(
    seed_urls: Optional[Iterable[str]] = None,
    discover_listings: bool = False,
    listing_max_pages: int = 50,
    max_pages: Optional[int] = None,
) -> ScrapeStats:
    """
    Scrape DOJ press releases.

    seed_urls: explicit list of URLs to fetch. If None, derives from v1 corpus.
    discover_listings: if True, also walk /news?topic=tt117 pages to find new URLs.
    listing_max_pages: max listing pages to traverse for discovery.
    max_pages: stop after this many newly archived pages (None = unlimited).
    """
    stats = ScrapeStats()

    # Build target URL set
    targets: list[str] = []
    if seed_urls is None:
        seed_urls = extract_seed_urls_from_v1()
    seeds = list(seed_urls)
    stats.seed_urls_found = len(seeds)
    targets.extend(seeds)

    if discover_listings:
        print(
            f"[{SOURCE}] Discovering URLs from /news?topic={TOPIC_HEALTH_CARE_FRAUD} "
            f"({listing_max_pages} pages max)..."
        )
        listing_urls = discover_from_listing(max_pages=listing_max_pages)
        new_from_listing = [u for u in listing_urls if u not in set(seeds)]
        stats.listing_urls_discovered = len(listing_urls)
        targets.extend(new_from_listing)
        print(
            f"[{SOURCE}] Listing discovery: {len(listing_urls)} URLs found, "
            f"{len(new_from_listing)} not in v1 seed"
        )

    # Dedup while preserving order
    targets = list(dict.fromkeys(targets))
    print(f"[{SOURCE}] Total target URLs: {len(targets)}")

    already = archived_urls(SOURCE)

    fetched = 0
    for i, url in enumerate(targets, 1):
        if max_pages is not None and fetched >= max_pages:
            break
        if url in already:
            stats.urls_already_archived += 1
            continue

        success, error = fetch_and_archive(url)
        if success:
            stats.urls_newly_archived += 1
            fetched += 1
            if fetched % 25 == 0:
                print(
                    f"[{SOURCE}] progress: {fetched} new pages archived "
                    f"({i}/{len(targets)} targets processed; "
                    f"{stats.urls_failed} failures)"
                )
        else:
            stats.urls_failed += 1
            mode = (error or "unknown").split(":")[0]
            stats.failure_modes[mode] = stats.failure_modes.get(mode, 0) + 1

    return stats


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="DOJ press release scraper")
    parser.add_argument(
        "--max-pages", type=int, default=None, help="Stop after N new archives"
    )
    parser.add_argument(
        "--seed-file",
        type=str,
        default=None,
        help="File with one URL per line (overrides v1 corpus seed)",
    )
    parser.add_argument(
        "--discover-listings",
        action="store_true",
        help="Also walk /news?topic=tt117 listings to find new URLs",
    )
    parser.add_argument(
        "--listing-max-pages",
        type=int,
        default=50,
        help="Max listing pages to traverse",
    )
    args = parser.parse_args()

    seeds: Optional[list[str]] = None
    if args.seed_file:
        from pathlib import Path

        seeds = [
            line.strip()
            for line in Path(args.seed_file).read_text().splitlines()
            if line.strip() and not line.startswith("#")
        ]

    stats = run(
        seed_urls=seeds,
        discover_listings=args.discover_listings,
        listing_max_pages=args.listing_max_pages,
        max_pages=args.max_pages,
    )
    print(f"\n[{SOURCE}] Done.")
    print(f"  Seed URLs:                {stats.seed_urls_found}")
    print(f"  Listing URLs discovered:  {stats.listing_urls_discovered}")
    print(f"  Already archived:         {stats.urls_already_archived}")
    print(f"  Newly archived:           {stats.urls_newly_archived}")
    print(f"  Failed:                   {stats.urls_failed}")
    if stats.failure_modes:
        print(f"  Failure modes:            {stats.failure_modes}")
