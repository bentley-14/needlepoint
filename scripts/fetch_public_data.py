#!/usr/bin/env python3
"""Download public CMS data into data/processed/ so the investigation tools can run.

Everything comes straight from data.cms.gov (public domain). Nothing here needs an
account or a key.

    python3 scripts/fetch_public_data.py --years 2023                 # Part B (both cuts)
    python3 scripts/fetch_public_data.py --years 2022 2023 --datasets partb partb_svc dmepos
    python3 scripts/fetch_public_data.py --list

Sizes are large: the Part B by-provider-and-service file is several GB of CSV per year
(about 10M rows). The by-provider file is far smaller. Start with one recent year.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

DATASETS = {
    "partb": ("pipeline.scrapers.cms_partb", "PartBScraper",
              "Part B by provider (one row per clinician per year)"),
    "partb_svc": ("pipeline.scrapers.cms_partb_svc", "PartBSvcScraper",
                  "Part B by provider and service (one row per clinician x HCPCS x place of service)"),
    "partd": ("pipeline.scrapers.cms_partd", "PartDScraper", "Part D prescribers by provider"),
    "dmepos": ("pipeline.scrapers.cms_dmepos", "DMEPOSScraper", "DMEPOS by referring provider"),
    "open_payments": ("pipeline.scrapers.open_payments", "OpenPaymentsScraper",
                      "Open Payments general payments (manufacturer to clinician)"),
}


def ensure_data_dirs() -> None:
    """Tools read data/processed/; scrapers write data/panels/. Make them the same place."""
    processed = REPO / "data" / "processed"
    processed.mkdir(parents=True, exist_ok=True)
    panels = REPO / "data" / "panels"
    if panels.is_dir() and not panels.is_symlink() and not any(p.name != ".gitkeep" for p in panels.iterdir()):
        for p in panels.iterdir():
            p.unlink()
        panels.rmdir()  # an empty real dir would split writes from reads
    if not panels.exists() and not panels.is_symlink():
        panels.symlink_to("processed")
    (REPO / "data" / "raw").mkdir(parents=True, exist_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--years", type=int, nargs="+", default=[])
    ap.add_argument("--datasets", nargs="+", default=["partb", "partb_svc"], choices=sorted(DATASETS))
    ap.add_argument("--list", action="store_true", help="list datasets and available years")
    ap.add_argument("--keep-raw", action="store_true", help="keep the downloaded CSVs (deleted by default)")
    args = ap.parse_args()

    ensure_data_dirs()
    import importlib

    if args.list or not args.years:
        for key, (mod, _cls, desc) in DATASETS.items():
            m = importlib.import_module(mod)
            years = getattr(m, "AVAILABLE_YEARS", None) or sorted(getattr(m, "URLS", {}))
            print(f"{key:14s} {desc}\n{'':14s} years: {', '.join(map(str, years)) or 'see module'}")
        if not args.years:
            print("\nPass --years to download, e.g. --years 2023")
        return 0

    failures = 0
    for key in args.datasets:
        mod, cls, desc = DATASETS[key]
        scraper = getattr(importlib.import_module(mod), cls)()
        for year in args.years:
            print(f"==> {key} {year}: {desc}", flush=True)
            try:
                res = scraper.fetch(year=year)
                print(f"    wrote {res.artifact_path} ({res.rows:,} rows)")
                if not args.keep_raw:  # the parquet is what tools read; the CSV is several times larger
                    for raw in (REPO / "data" / "raw" / scraper.source).glob(f"*{year}*"):
                        raw.unlink()
            except Exception as e:  # keep going; report at the end
                failures += 1
                print(f"    FAILED: {type(e).__name__}: {e}")
    print(f"\nData directory: {REPO / 'data' / 'processed'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
