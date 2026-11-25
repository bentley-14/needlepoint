"""One-shot backfill: stamp legacy cms_partb_svc parquets for BaseScraper compatibility.

Existing ``data/panels/cms_partb_svc_*.parquet`` files on R2 predate the
``PartBSvcScraper`` (PR #109). They lack the seven canonical columns the
new schema requires and have no sibling ``.manifest.json``.

This script adds the missing columns in-place and writes a manifest, so
the legacy files round-trip through ``BaseScraper._validate()``.

Workflow (single year, what the loop driver runs per iteration):

    rclone copyto r2:needlepoint-data/processed/v1/cms_partb_svc_${Y}.parquet \\
      data/panels/cms_partb_svc_${Y}.parquet \\
      --transfers 1 --multi-thread-streams 1 --s3-no-check-bucket

    python -m pipeline.scripts.backfill_cms_partb_svc_manifest --year ${Y}

    rclone copy data/panels/ r2:needlepoint-data/processed/v1/ \\
      --include "cms_partb_svc_${Y}*" --s3-no-check-bucket

    rm data/panels/cms_partb_svc_${Y}*

Idempotent: re-running on an already-stamped parquet is a no-op (detected
by the full canonical column set already being present).
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from pipeline.scrapers.cms_partb_svc import PartBSvcScraper, PartBSvcSchema

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXPECTED_COLS = frozenset(PartBSvcSchema.to_schema().columns.keys())
PANELS_DIR = PROJECT_ROOT / "data" / "panels"

SOURCE = "cms_partb_svc"
SCHEMA_VERSION = "v1.0"
SCHEMA_CLASS = "PartBSvcSchema"

YEAR_RE = re.compile(r"cms_partb_svc_(\d{4})\.parquet$")


def _file_mtime_utc(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


def _original_fetched_at_utc(path: Path) -> datetime:
    manifest_path = path.with_suffix(".manifest.json")
    if manifest_path.exists():
        try:
            data = json.loads(manifest_path.read_text())
            return datetime.fromisoformat(data["started_at_utc"])
        except (KeyError, ValueError):
            pass
    return _file_mtime_utc(path)


def _parse_year(path: Path) -> int:
    m = YEAR_RE.search(path.name)
    if not m:
        raise ValueError(f"cannot parse year from {path.name}")
    return int(m.group(1))


def _already_stamped(df: pd.DataFrame) -> bool:
    return EXPECTED_COLS.issubset(df.columns)


def _stamp(df: pd.DataFrame, *, year: int, fetched_at_utc: datetime) -> pd.DataFrame:
    out = PartBSvcScraper._add_canonical_columns(df, year=year)
    out["source"] = SOURCE
    out["fetched_at_utc"] = fetched_at_utc
    out["schema_version"] = SCHEMA_VERSION
    return out


def _write_manifest(parquet_path: Path, df: pd.DataFrame, fetched_at_utc: datetime) -> Path:
    manifest = {
        "source": SOURCE,
        "artifact_path": str(parquet_path),
        "rows": len(df),
        "bytes": parquet_path.stat().st_size,
        "started_at_utc": fetched_at_utc.isoformat(),
        "ended_at_utc": fetched_at_utc.isoformat(),
        "schema_class": SCHEMA_CLASS,
        "schema_version": SCHEMA_VERSION,
        "schema_columns": sorted(df.columns),
        "inputs": {"backfilled_from": "legacy parquet, pre-BaseScraper (the maintainer PR #96 + earlier)"},
    }
    manifest_path = parquet_path.with_suffix(".manifest.json")
    manifest_path.write_text(json.dumps(manifest, indent=2))
    return manifest_path


def backfill_one(path: Path, *, dry_run: bool) -> dict:
    year = _parse_year(path)
    fetched_at_utc = _original_fetched_at_utc(path)
    df = pd.read_parquet(path)

    if _already_stamped(df):
        return {"path": str(path), "status": "skipped (already stamped)", "rows": len(df)}

    stamped = _stamp(df, year=year, fetched_at_utc=fetched_at_utc)

    # Strict validation via the scraper itself — same path the live scraper takes.
    scraper = PartBSvcScraper()
    scraper._validate(stamped)

    if dry_run:
        return {
            "path": str(path),
            "status": "would stamp",
            "rows": len(stamped),
            "added_cols": sorted(set(stamped.columns) - set(df.columns)),
            "mtime_utc": fetched_at_utc.isoformat(),
        }

    stamped.to_parquet(path, index=False)
    manifest_path = _write_manifest(path, stamped, fetched_at_utc)
    return {
        "path": str(path),
        "manifest": str(manifest_path),
        "status": "stamped",
        "rows": len(stamped),
        "mtime_utc": fetched_at_utc.isoformat(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report what would change, write nothing")
    parser.add_argument("--year", type=int, help="restrict to one year")
    args = parser.parse_args()

    if args.year:
        paths = sorted(PANELS_DIR.glob(f"cms_partb_svc_{args.year}.parquet"))
    else:
        paths = sorted(p for p in PANELS_DIR.glob("cms_partb_svc_*.parquet") if YEAR_RE.search(p.name))
    if not paths:
        raise SystemExit(f"no parquets match cms_partb_svc_*.parquet in {PANELS_DIR}")

    for path in paths:
        result = backfill_one(path, dry_run=args.dry_run)
        print(json.dumps(result))


if __name__ == "__main__":
    main()
