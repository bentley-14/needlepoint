"""
Top-level CLI for the case-corpus raw-archive scrapers.

Usage:
    python -m scripts.scrape.cli stats
    python -m scripts.scrape.cli scrape doj --max-pages 50
    python -m scripts.scrape.cli scrape oig --max-detail-pages 50
    python -m scripts.scrape.cli scrape all
"""

from __future__ import annotations

import argparse
import sys

from .raw_archive import archive_stats
from .sources import doj_press, oig_enforcement


def cmd_stats(args: argparse.Namespace) -> int:
    for source in ("doj_press", "oig_enforcement"):
        s = archive_stats(source)
        print(f"=== {source} ===")
        if s["count"] == 0:
            print("  (empty)")
            continue
        print(f"  total entries:  {s['count']}")
        print(f"  by year:        {s['by_year']}")
        print(f"  by HTTP status: {s['by_status']}")
        print(f"  first fetch:    {s['first_fetch']}")
        print(f"  last fetch:     {s['last_fetch']}")
        print()
    return 0


def cmd_scrape(args: argparse.Namespace) -> int:
    target = args.target
    if target in ("doj", "all"):
        print("\n>>> DOJ press releases <<<")
        doj_stats = doj_press.run(
            discover_listings=args.discover_listings,
            listing_max_pages=args.listing_max_pages,
            max_pages=args.max_pages,
        )
        print(
            f"\nDOJ done: +{doj_stats.urls_newly_archived} newly archived, "
            f"{doj_stats.urls_failed} failed "
            f"(failure modes: {doj_stats.failure_modes or 'none'})"
        )
    if target in ("oig", "all"):
        print("\n>>> OIG enforcement actions <<<")
        oig_stats = oig_enforcement.run(
            max_listing_pages=args.listing_max_pages,
            max_detail_pages=args.max_pages,
        )
        print(
            f"\nOIG done: +{oig_stats.detail_urls_newly_archived} newly archived, "
            f"{oig_stats.detail_urls_failed} failed"
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Healthcare-fraud case raw archiver")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("stats", help="Print archive statistics")

    p_scrape = sub.add_parser("scrape", help="Run scrapers")
    p_scrape.add_argument("target", choices=["doj", "oig", "all"])
    p_scrape.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Stop after this many newly archived pages",
    )
    p_scrape.add_argument(
        "--listing-max-pages",
        type=int,
        default=50,
        help="Max listing pages to traverse for discovery",
    )
    p_scrape.add_argument(
        "--discover-listings",
        action="store_true",
        help="(DOJ only) Walk /news?topic=tt117 listings to find new URLs",
    )

    args = parser.parse_args()

    if args.cmd == "stats":
        return cmd_stats(args)
    if args.cmd == "scrape":
        return cmd_scrape(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
