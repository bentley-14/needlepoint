"""Scrape the 3,594 DOJ press release URLs that the OIG enforcement archive
references but our DOJ corpus doesn't yet have.

Driven by `data/extracted/doj_urls_missing_from_oig_xref.txt`. Idempotent —
already-archived URLs are skipped. Resumable on interruption.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from scripts.scrape.sources.doj_press import run as scrape_doj  # noqa: E402

URL_LIST = REPO_ROOT / "data" / "extracted" / "doj_urls_missing_from_oig_xref.txt"


def main() -> int:
    urls = [u.strip() for u in URL_LIST.read_text().splitlines() if u.strip()]
    print(f"loaded {len(urls)} candidate URLs from {URL_LIST.name}", flush=True)
    start = time.time()
    stats = scrape_doj(seed_urls=urls, discover_listings=False)
    duration = time.time() - start
    print(
        f"\nscrape complete in {duration / 60:.1f} min: "
        f"+{stats.urls_newly_archived} newly archived, "
        f"{stats.urls_failed} failed "
        f"(failure modes: {stats.failure_modes or 'none'})",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
