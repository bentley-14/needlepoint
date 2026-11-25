"""Re-ingest a panel source-year from the CMS source and push it to R2.

Why this exists (vs. ``recast_r2_dtypes.py``)
---------------------------------------------
``recast_r2_dtypes`` only *re-types* columns whose values survived on R2. It
cannot help the columns flagged ``REINGEST_ONLY`` — FIPS/ZIP that were stored
as floats already lost their leading zeros (``"06"``→``6.0``) and any
alphanumeric code (FIPS ``"9C"``→``NaN``); no cast recovers destroyed
information. The only fix is to read the column from the authoritative CMS
source again, now that the scraper reads those code columns as ``string``
(see ``pipeline/scrapers/*.py``). This tool does exactly that:

    fetch(year=…)  →  build + validate locally  →  verify  →  (execute) push to R2

It also covers the partd-2024 case (the R2 panel was built from the retracted
February release; re-fetching the current URL repopulates the empty columns and
the missing ~277k rows).

Fresh-runner advantage: run this in the ``reingest`` GitHub Action, not on a
laptop — datacenter bandwidth for the multi-GB CSVs, and no stale-cache trap
(``download._download`` skips a re-download if a CSV is already on disk, which
is how the 2024 panel got built from a stale file in the first place).

Modes
-----
* ``dry`` (default): download, build, validate, and verify the local parquet.
  Touches nothing on R2. Use this to confirm the rebuild looks right.
* ``--execute``: also back up the existing R2 parquet to ``.pre-reingest.bak``
  and push the rebuilt parquet + manifest over it (outward-facing).

Usage
-----
    python -m pipeline.scripts.reingest_r2 --source cms_partd --year 2024
    python -m pipeline.scripts.reingest_r2 --source cms_partd --year 2013,2014,2015 --execute
"""
from __future__ import annotations

import argparse
import subprocess
from pathlib import Path
from typing import Type

import pyarrow.parquet as pq

from pipeline.base import BaseScraper
from pipeline.scrapers.cms_dmepos import DMEPOSScraper
from pipeline.scrapers.cms_dmepos_detailed import DMEPOSDetailedScraper
from pipeline.scrapers.cms_partb import PartBScraper
from pipeline.scrapers.cms_partb_svc import PartBSvcScraper
from pipeline.scrapers.cms_partd import PartDScraper
from pipeline.scrapers.open_payments import OpenPaymentsScraper

# Year-partitioned panel sources (NPPES is a single year-less snapshot and is
# not re-ingested per-year, so it is intentionally excluded here;
# physician_compare is snapshot-keyed, not year-keyed — it has its own
# backfill path in pipeline/scripts/backfill_physician_compare.py).
SCRAPERS: dict[str, Type[BaseScraper]] = {
    c.source: c for c in [
        OpenPaymentsScraper, PartBScraper, PartBSvcScraper,
        PartDScraper, DMEPOSScraper, DMEPOSDetailedScraper,
    ]
}

BUCKET, PREFIX = "needlepoint-data", "processed/v1"

# Substrings that mark a zero-padded / alphanumeric code column that MUST be a
# string after the rebuild. The whole point of the re-ingest is that these come
# back as text, not float — so we assert it before pushing.
_CODE_COL_HINTS = ("FIPS", "Zip5", "zip5")


def _rclone(*args: str) -> str:
    return subprocess.run(["rclone", *args], capture_output=True, text=True,
                          check=True).stdout.strip()


def _r2_key_exists(fname: str) -> bool:
    out = _rclone("lsf", f"r2:{BUCKET}/{PREFIX}/")
    return fname in out.splitlines()


def _verify_local(parquet_path: Path) -> dict:
    """Read the built parquet's schema + row count and assert the code columns
    came back as strings. Returns a small report; raises on a hard failure.
    """
    pf = pq.ParquetFile(parquet_path)
    schema = pf.schema_arrow
    n_rows = pf.metadata.num_rows
    code_cols = [n for n in schema.names
                 if any(h in n for h in _CODE_COL_HINTS)]
    bad = []
    for name in code_cols:
        t = schema.field(name).type
        # pyarrow string types are large_string/string; anything numeric here
        # means the read still coerced the code to a number — the bug.
        if not (t == "string" or t == "large_string" or str(t).startswith("string")):
            bad.append((name, str(t)))
    if n_rows == 0:
        raise SystemExit(f"VERIFY FAILED: {parquet_path.name} has 0 rows")
    if bad:
        raise SystemExit(
            f"VERIFY FAILED: code columns are not string in {parquet_path.name}: "
            f"{bad}. The scraper must read FIPS/ZIP with dtype='string'."
        )
    return {"rows": n_rows, "code_cols": code_cols}


def reingest_one(source: str, scraper: Type[BaseScraper], year: int,
                 *, execute: bool) -> dict:
    fname = f"{source}_{year}.parquet"
    mfname = f"{source}_{year}.manifest.json"

    # 1. fetch + build + validate (writes data/panels/<source>_<year>.parquet)
    result = scraper().fetch(year=year)
    local_parquet = Path(result.artifact_path)
    local_manifest = local_parquet.parent / f"{local_parquet.stem}.manifest.json"

    # 2. verify the rebuild
    report = _verify_local(local_parquet)
    report.update({"source": source, "year": year,
                   "local_parquet": str(local_parquet)})

    if not execute:
        report["status"] = "built + verified (dry — NOT pushed to R2)"
        return report

    # 3. back up the existing R2 object (if any), then push parquet + manifest
    if _r2_key_exists(fname):
        _rclone("copyto", "--s3-no-check-bucket",
                f"r2:{BUCKET}/{PREFIX}/{fname}",
                f"r2:{BUCKET}/{PREFIX}/{fname}.pre-reingest.bak")
        report["backup"] = f"{fname}.pre-reingest.bak"
    _rclone("copyto", "--s3-no-check-bucket",
            str(local_parquet), f"r2:{BUCKET}/{PREFIX}/{fname}")
    if local_manifest.exists():
        _rclone("copyto", "--s3-no-check-bucket",
                str(local_manifest), f"r2:{BUCKET}/{PREFIX}/{mfname}")
    report["status"] = "re-ingested + pushed to R2"
    return report


def _parse_years(raw: str) -> list[int]:
    return [int(y) for y in raw.replace(",", " ").split()]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", required=True, choices=sorted(SCRAPERS),
                    help="panel source to re-ingest")
    ap.add_argument("--year", required=True,
                    help="year, or comma/space-separated list (e.g. 2013,2014)")
    ap.add_argument("--execute", action="store_true",
                    help="back up + push the rebuilt parquet/manifest to R2 "
                         "(default: dry — build + verify locally only)")
    args = ap.parse_args()

    scraper = SCRAPERS[args.source]
    mode = "execute" if args.execute else "dry"
    for year in _parse_years(args.year):
        print(f"::group::{args.source} {year} (mode={mode})")
        res = reingest_one(args.source, scraper, year, execute=args.execute)
        print(res)
        print("::endgroup::")


if __name__ == "__main__":
    main()
