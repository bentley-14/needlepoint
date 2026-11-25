"""Backfill a Physician Compare (DAC) snapshot and push it to R2.

Physician Compare is snapshot-keyed (``YYYY-MM``), not year-keyed, so it
doesn't fit the year-partitioned ``reingest_r2`` tool. This is its
equivalent: run ``PhysicianCompareScraper().fetch(snapshot=…)`` on a fresh
runner (datacenter bandwidth, no stale-cache trap), verify the rebuild, and
— in execute mode — push the dated parquet + manifest to R2 and repoint the
``physician_compare.parquet`` alias when this is the newest snapshot.

Modes
-----
* ``dry`` (default): download, build, validate, verify locally. Touches
  nothing on R2.
* ``--execute``: back up any existing R2 object, push the dated parquet +
  manifest, and (when newest) copy it to the ``physician_compare.parquet``
  alias on R2.

Usage
-----
    # current snapshot (default DAC URL), local dry run
    python -m pipeline.scripts.backfill_physician_compare --snapshot 2026-05

    # a historical snapshot from the archived-data page, pushed to R2
    python -m pipeline.scripts.backfill_physician_compare \
        --snapshot 2022-09 \
        --url https://data.cms.gov/provider-data/.../DAC_NationalDownloadableFile.csv \
        --execute
"""
from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import pyarrow.parquet as pq

from pipeline.scrapers.physician_compare import PhysicianCompareScraper

BUCKET, PREFIX = "needlepoint-data", "processed/v1"
POINTER = "physician_compare.parquet"


def _rclone(*args: str) -> str:
    return subprocess.run(["rclone", *args], capture_output=True, text=True,
                          check=True).stdout.strip()


def _r2_key_exists(fname: str) -> bool:
    return fname in _rclone("lsf", f"r2:{BUCKET}/{PREFIX}/").splitlines()


def _verify_local(parquet_path: Path) -> dict:
    """Assert non-empty + the Aspen known-NPI lookup resolves with a
    populated Org_PAC_ID. Raises SystemExit on a hard failure.
    """
    pf = pq.ParquetFile(parquet_path)
    if pf.metadata.num_rows == 0:
        raise SystemExit(f"VERIFY FAILED: {parquet_path.name} has 0 rows")
    # Reuse the shared known-NPI check (reads the pointer or newest dated file;
    # the just-written snapshot is the newest, so it is what gets checked).
    from pipeline.scripts.verify_new_sources import verify_physician_compare
    errors = verify_physician_compare()
    if errors:
        raise SystemExit(f"VERIFY FAILED: {parquet_path.name}: {errors}")
    return {"rows": pf.metadata.num_rows}


def backfill_one(snapshot: str, *, url: str | None, local_csv: str | None,
                 execute: bool) -> dict:
    fname = f"physician_compare_{snapshot}.parquet"
    mfname = f"physician_compare_{snapshot}.manifest.json"

    result = PhysicianCompareScraper().fetch(
        snapshot=snapshot, url=url, local_csv=local_csv
    )
    local_parquet = Path(result.artifact_path)
    local_manifest = local_parquet.parent / f"{local_parquet.stem}.manifest.json"
    local_pointer = local_parquet.parent / POINTER

    report = _verify_local(local_parquet)
    report.update({"source": "physician_compare", "snapshot": snapshot,
                   "local_parquet": str(local_parquet)})

    if not execute:
        report["status"] = "built + verified (dry — NOT pushed to R2)"
        return report

    if _r2_key_exists(fname):
        _rclone("copyto", "--s3-no-check-bucket",
                f"r2:{BUCKET}/{PREFIX}/{fname}",
                f"r2:{BUCKET}/{PREFIX}/{fname}.pre-backfill.bak")
        report["backup"] = f"{fname}.pre-backfill.bak"
    _rclone("copyto", "--s3-no-check-bucket",
            str(local_parquet), f"r2:{BUCKET}/{PREFIX}/{fname}")
    if local_manifest.exists():
        _rclone("copyto", "--s3-no-check-bucket",
                str(local_manifest), f"r2:{BUCKET}/{PREFIX}/{mfname}")

    # Repoint the R2 alias only when this snapshot is the newest on disk
    # (the scraper applies the same rule to the local symlink).
    if local_pointer.is_symlink() and Path(local_pointer.readlink()).name == fname:
        _rclone("copyto", "--s3-no-check-bucket",
                str(local_parquet), f"r2:{BUCKET}/{PREFIX}/{POINTER}")
        report["pointer"] = f"{POINTER} -> {fname}"
    report["status"] = "backfilled + pushed to R2"
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--snapshot", required=True, help="snapshot date as YYYY-MM")
    ap.add_argument("--url", default=None,
                    help="DAC CSV URL (default: current distribution); pass an "
                         "archived URL for a historical snapshot")
    ap.add_argument("--local-csv", default=None,
                    help="ingest a locally-downloaded DAC CSV instead of fetching")
    ap.add_argument("--execute", action="store_true",
                    help="back up + push the parquet/manifest/pointer to R2 "
                         "(default: dry — build + verify locally only)")
    args = ap.parse_args()

    mode = "execute" if args.execute else "dry"
    print(f"::group::physician_compare {args.snapshot} (mode={mode})")
    res = backfill_one(args.snapshot, url=args.url, local_csv=args.local_csv,
                       execute=args.execute)
    print(res)
    print("::endgroup::")


if __name__ == "__main__":
    main()
