"""One-shot backfill: stamp legacy cms_dmepos parquets for BaseScraper compatibility.

Existing ``data/panels/cms_dmepos_*.parquet`` files predate the
``pipeline.base.BaseScraper`` ABC. They lack the seven columns the new
``DMEPOSSchema`` (inheriting from ``ProviderRecordSchema``) will require
and have no sibling ``.manifest.json``.

This script adds the missing columns in-place and writes a manifest, so
the legacy files round-trip through ``BaseScraper._validate()`` once the
real scraper lands.

Workflow:

    make pull-data
    python -m pipeline.scripts.backfill_cms_dmepos_manifest --dry-run
    python -m pipeline.scripts.backfill_cms_dmepos_manifest
    make push-data            # maintainer only

Idempotent: re-running on an already-stamped parquet is a no-op (detected
by presence of the ``schema_version`` column).
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from pipeline.base import format_provider_name
from pipeline.scrapers.cms_dmepos import DMEPOSScraper, DMEPOSSchema

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXPECTED_COLS = frozenset(DMEPOSSchema.to_schema().columns.keys())
PANELS_DIR = PROJECT_ROOT / "data" / "panels"

SOURCE = "cms_dmepos"
SCHEMA_VERSION = "v1.0"
SCHEMA_CLASS = "DMEPOSSchema"

YEAR_RE = re.compile(r"cms_dmepos_(\d{4})\.parquet$")

# Mirrors what BaseScraper would have stamped at fetch time.
BASE_COLS = ("source", "fetched_at_utc", "schema_version")
PROVIDER_COLS = ("npi", "provider_name", "state", "data_year")


def _parse_year(path: Path) -> int:
    m = YEAR_RE.search(path.name)
    if not m:
        raise ValueError(f"cannot parse year from {path.name}")
    return int(m.group(1))


def _file_mtime_utc(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


def _original_fetched_at_utc(path: Path) -> datetime:
    """Prefer the timestamp from an existing manifest over current mtime.

    Once a parquet has been rewritten by a prior backfill pass, its
    mtime no longer reflects the original-download moment. The first
    backfill captured that signal into the manifest's ``started_at_utc``;
    we preserve it across reprocess passes so the historical provenance
    isn't lost when we re-stamp pre-2017 years to pad CC columns.
    """
    manifest_path = path.with_suffix(".manifest.json")
    if manifest_path.exists():
        try:
            data = json.loads(manifest_path.read_text())
            return datetime.fromisoformat(data["started_at_utc"])
        except (KeyError, ValueError):
            pass
    return _file_mtime_utc(path)


def _already_stamped(df: pd.DataFrame) -> bool:
    """True iff the parquet already matches the canonical DMEPOSSchema.

    Tighter than "has the seven stamp columns" — also checks the 25
    Bene_CC_* columns added in DY17, so older years that were stamped
    against the pre-CC schema get reprocessed.
    """
    return EXPECTED_COLS.issubset(df.columns)


def _stamp(df: pd.DataFrame, *, year: int, fetched_at_utc: datetime) -> pd.DataFrame:
    out = df.copy()
    out["source"] = SOURCE
    out["fetched_at_utc"] = fetched_at_utc
    out["schema_version"] = SCHEMA_VERSION

    out["npi"] = out["Rfrg_NPI"].astype("int64").astype(str).str.zfill(10)
    out["provider_name"] = [
        format_provider_name(
            entity_code=ec,
            last_or_org=last,
            first=first,
            middle=mi,
        )
        for ec, last, first, mi in zip(
            out["Rfrg_Prvdr_Ent_Cd"],
            out["Rfrg_Prvdr_Last_Name_Org"],
            out["Rfrg_Prvdr_First_Name"],
            out["Rfrg_Prvdr_MI"],
        )
    ]
    out["state"] = out["Rfrg_Prvdr_State_Abrvtn"].astype("string")
    out["data_year"] = year

    # Pre-2017 lacks the 25 Bene_CC_* cols; pad with NaN to match the
    # canonical schema. Uses DMEPOSScraper._align_to_schema so the rule
    # stays in one place.
    out = DMEPOSScraper()._align_to_schema(out)
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
        "inputs": {"backfilled_from": "legacy parquet, pre-BaseScraper"},
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
    parser.add_argument("--year", type=int, help="restrict to one year (default: all aggregated cms_dmepos year files)")
    args = parser.parse_args()

    # Anchor the year glob to four digits so the aggregated DMEPOS panel
    # (cms_dmepos_2014.parquet …) is matched but cms_dmepos_detailed_<year>
    # — the by-Referring-Provider-and-Service cut, which ships its own
    # manifest from DMEPOSDetailedScraper — is NOT swept into this stamper.
    pattern = f"cms_dmepos_{args.year}.parquet" if args.year else "cms_dmepos_[0-9][0-9][0-9][0-9].parquet"
    paths = sorted(PANELS_DIR.glob(pattern))
    if not paths:
        raise SystemExit(f"no parquets match {PANELS_DIR / pattern}; did you run `make pull-data`?")

    for path in paths:
        result = backfill_one(path, dry_run=args.dry_run)
        print(json.dumps(result))


if __name__ == "__main__":
    main()
