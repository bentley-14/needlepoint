"""
OIG Enforcement Actions scraper.

Source: https://oig.hhs.gov/fraud/enforcement/

The OIG enforcement archive is unprotected (no Akamai bot challenge). Each
enforcement action lives at /fraud/enforcement/{slug} and indexes paginate
via ?page=N.

This scraper:
1. Walks all listing pages to discover individual enforcement-action URLs.
2. For each discovered URL, fetches the page and writes to the raw archive
   (idempotent — skips if already archived).
3. Returns a summary.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..fetcher import fetch_url
from ..raw_archive import (
    archive_paths,
    archived_urls,
    url_slug,
    write_archive,
)

SOURCE = "oig_enforcement"
LISTING_BASE = "https://oig.hhs.gov/fraud/enforcement/"
LISTING_URL_TEMPLATE = "https://oig.hhs.gov/fraud/enforcement/?page={page}"


@dataclass
class ScrapeStats:
    listing_pages_fetched: int = 0
    detail_urls_discovered: int = 0
    detail_urls_already_archived: int = 0
    detail_urls_newly_archived: int = 0
    detail_urls_failed: int = 0


def discover_detail_urls(max_listing_pages: int = 600) -> list[str]:
    """
    Walk listing pages and collect all detail-page URLs.

    OIG pagination quirk: `?page=1` returns the same content as the base
    URL (1-indexed display, but page-1 == default view). Subsequent pages
    (?page=2, ?page=3, ...) return distinct content. Final page is page=546
    as of 2026-05.

    We iterate base + page=2..N and bail out when consecutive pages produce
    no new URLs OR we hit the cap.
    """
    discovered: list[str] = []
    seen: set[str] = set()

    # Page 0 = base URL (no param)
    # Page 1 = same as base, skip
    # Pages 2..max = distinct
    page_indices = [0] + list(range(2, max_listing_pages + 1))

    consecutive_empty_pages = 0
    for page in page_indices:
        listing_url = (
            LISTING_BASE if page == 0 else LISTING_URL_TEMPLATE.format(page=page)
        )
        result = fetch_url(listing_url)
        if result.http_status != 200 or not result.body:
            consecutive_empty_pages += 1
            if consecutive_empty_pages >= 3:
                break
            continue

        # Find all detail URLs on this page
        page_urls = set(
            re.findall(
                r'href="(/fraud/enforcement/[^"#?]+?)"',
                result.body,
            )
        )
        # Exclude the listing page itself + the /about/ static page
        page_urls = {
            u
            for u in page_urls
            if u != "/fraud/enforcement/"
            and u != "/fraud/enforcement/about/"
            and not u.startswith("/fraud/enforcement/?")
            and "/fraud/enforcement/" in u
        }
        page_urls_abs = {f"https://oig.hhs.gov{u}" for u in page_urls}

        new_urls = page_urls_abs - seen
        if not new_urls:
            consecutive_empty_pages += 1
            if consecutive_empty_pages >= 3:
                break
            continue

        consecutive_empty_pages = 0
        seen.update(new_urls)
        discovered.extend(sorted(new_urls))

    return discovered


def fetch_and_archive_detail(url: str) -> tuple[bool, Optional[str]]:
    """
    Fetch a single detail page and write to the archive.

    Returns (success, error_message).
    Idempotent: if already archived, returns (True, None) without re-fetching.
    """
    # Skip if already archived (cheap check via index)
    already = archived_urls(SOURCE)
    if url in already:
        return True, None

    result = fetch_url(url)
    if result.http_status != 200:
        return False, f"HTTP {result.http_status}: {result.error}"

    # Try to extract a year from the page metadata for foldering.
    # OIG enforcement pages usually have a posted date; extract YYYY.
    year_match = re.search(
        r'(?:Enforcement\s+Action|Posted|Date)[^0-9]{0,50}(\d{4})',
        result.body,
        re.IGNORECASE,
    )
    year = int(year_match.group(1)) if year_match else None

    try:
        write_archive(
            source=SOURCE,
            url=url,
            raw_html=result.body,
            http_status=result.http_status,
            final_url=result.final_url,
            scrape_method="urllib",
            year=year,
            redirect_chain=result.redirect_chain,
            extra={"duration_ms": result.duration_ms},
        )
    except FileExistsError:
        # Race condition / concurrent run — treat as success
        return True, None

    return True, None


def run(
    max_listing_pages: int = 200,
    max_detail_pages: Optional[int] = None,
) -> ScrapeStats:
    """
    Full OIG enforcement scrape.

    max_listing_pages: stop after this many listing pages even if more exist
    max_detail_pages: stop after fetching this many detail pages (None = all)
    """
    stats = ScrapeStats()

    print(f"[{SOURCE}] Discovering detail URLs from listing pages...")
    detail_urls = discover_detail_urls(max_listing_pages=max_listing_pages)
    stats.detail_urls_discovered = len(detail_urls)
    stats.listing_pages_fetched = max_listing_pages  # approximate
    print(f"[{SOURCE}] Discovered {len(detail_urls)} detail URLs")

    already = archived_urls(SOURCE)

    fetched = 0
    for url in detail_urls:
        if max_detail_pages is not None and fetched >= max_detail_pages:
            break
        if url in already:
            stats.detail_urls_already_archived += 1
            continue
        success, error = fetch_and_archive_detail(url)
        if success:
            stats.detail_urls_newly_archived += 1
            fetched += 1
            if fetched % 25 == 0:
                print(
                    f"[{SOURCE}] progress: {fetched} new pages archived "
                    f"({stats.detail_urls_already_archived} already-archived skipped)"
                )
        else:
            stats.detail_urls_failed += 1
            print(f"[{SOURCE}] FAILED {url}: {error}")

    return stats


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="OIG enforcement archive scraper")
    parser.add_argument("--max-listing-pages", type=int, default=200)
    parser.add_argument("--max-detail-pages", type=int, default=None)
    args = parser.parse_args()

    stats = run(
        max_listing_pages=args.max_listing_pages,
        max_detail_pages=args.max_detail_pages,
    )
    print(f"\n[{SOURCE}] Done.")
    print(f"  Detail URLs discovered:        {stats.detail_urls_discovered}")
    print(f"  Already archived (skipped):    {stats.detail_urls_already_archived}")
    print(f"  Newly archived:                {stats.detail_urls_newly_archived}")
    print(f"  Failed:                        {stats.detail_urls_failed}")
