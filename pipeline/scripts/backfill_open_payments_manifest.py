"""One-shot backfill: stamp legacy open_payments parquets for BaseScraper compatibility.

Existing ``data/panels/open_payments_general_*.parquet`` files (in
particular the PY2024 cut the maintainer landed via PR #96) predate the
``pipeline.base.BaseScraper`` ABC. They lack the seven columns the new
``OpenPaymentsSchema`` (inheriting from ``ProviderRecordSchema``)
declares, and may have no sibling ``.manifest.json``.

This script adds the missing columns in-place and writes a manifest, so
the legacy files round-trip through ``BaseScraper._validate()`` once the
real scraper runs.

Workflow — year-by-year pull→stamp→push→delete loop (OpenPayments
parquets are large: ~80 MB for PY2013 partial, ~500 MB for PY2023+
full years, ~6 GB total for all 12 years if held on disk at once):

    for y in 2013 2014 2015 2016 2017 2018 2019 2020 2021 2022 2023 2024; do
        rclone copy r2:hcfd/panels/open_payments_general_${y}.parquet data/panels/
        rclone copy r2:hcfd/panels/open_payments_general_${y}.manifest.json data/panels/ || true
        .venv/bin/python -m pipeline.scripts.backfill_open_payments_manifest --year $y
        rclone copy data/panels/open_payments_general_${y}.parquet r2:hcfd/panels/
        rclone copy data/panels/open_payments_general_${y}.manifest.json r2:hcfd/panels/
        rm data/panels/open_payments_general_${y}.parquet
        rm data/panels/open_payments_general_${y}.manifest.json
    done

Idempotent: re-running on an already-stamped parquet is a no-op (detected
by the full canonical column set already being present).

rclone tip (carried over from PR #95): multi-thread rclone choked on R2
on 2026-05-29. If a transfer stalls > ~3 min, retry with
``--transfers 1 --multi-thread-streams 1``.
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from pipeline.base import format_provider_name
from pipeline.scrapers.open_payments import (
    OpenPaymentsScraper,
    OpenPaymentsSchema,
    _recipient_entity_code,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
# OpenPaymentsSchema is a Pandera DataFrameModel (was a dataclass pre-migration);
# its column set comes from to_schema() rather than dataclasses.fields().
EXPECTED_COLS = frozenset(OpenPaymentsSchema.to_schema().columns.keys())
PANELS_DIR = PROJECT_ROOT / "data" / "panels"

SOURCE = "open_payments_general"
SCHEMA_VERSION = "v1.0"
SCHEMA_CLASS = "OpenPaymentsSchema"

YEAR_RE = re.compile(r"open_payments_general_(\d{4})\.parquet$")


def _file_mtime_utc(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


def _original_fetched_at_utc(path: Path) -> datetime:
    """Prefer the timestamp from an existing manifest over current mtime.

    Once a parquet has been rewritten by a prior backfill pass, its
    mtime no longer reflects the original-download moment. The first
    backfill captured that signal into the manifest's
    ``started_at_utc``; we preserve it across reprocess passes so the
    historical provenance isn't lost.
    """
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
    """True iff the parquet already matches the canonical OpenPaymentsSchema."""
    return EXPECTED_COLS.issubset(df.columns)


def _stamp(df: pd.DataFrame, *, year: int, fetched_at_utc: datetime) -> pd.DataFrame:
    """Add the seven canonical + stamp columns and align to the schema.

    The pre-stamp parquets from PR #96 carry the 15 raw CMS columns
    plus, in some cases, a few legacy lowercase columns (``NPI``,
    ``recipient_type``, ``amount_usd``, …) from the previous
    ``fraudlib/ingest/providers/openpayments.py`` `_RENAME` map. The
    legacy columns are aliased back to their raw CMS names before
    stamping so downstream validation sees the canonical schema.
    """
    out = df.copy()

    # If the legacy ingest module's renamed columns are present, alias
    # them back to the canonical raw-CMS names declared in the schema.
    legacy_rename = {
        "NPI": "Covered_Recipient_NPI",
        "recipient_type": "Covered_Recipient_Type",
        "specialty": "Covered_Recipient_Specialty_1",
        "state": "Recipient_State",
        "manufacturer": "Applicable_Manufacturer_or_Applicable_GPO_Making_Payment_Name",
        "amount_usd": "Total_Amount_of_Payment_USDollars",
        "date_of_payment": "Date_of_Payment",
        "nature_of_payment": "Nature_of_Payment_or_Transfer_of_Value",
        "product_name": "Name_of_Drug_or_Biological_or_Device_or_Medical_Supply_1",
        "program_year": "Program_Year",
        "record_id": "Record_ID",
    }
    to_rename = {k: v for k, v in legacy_rename.items() if k in out.columns and v not in out.columns}
    if to_rename:
        out = out.rename(columns=to_rename)

    # Match the scraper's parse-time contract: drop rows with null NPI.
    # Pre-2018 legacy parquets carry NaN NPIs (teaching-hospital-only
    # payments, early-year reporting gaps); the canonical schema makes
    # `npi` non-nullable.
    out = out[out["Covered_Recipient_NPI"].notna()].copy()

    out["source"] = SOURCE
    out["fetched_at_utc"] = fetched_at_utc
    out["schema_version"] = SCHEMA_VERSION

    out["npi"] = (
        out["Covered_Recipient_NPI"].astype("int64").astype(str).str.zfill(10)
    )

    # Provider-name format: individuals get "LAST, FIRST MI"; teaching
    # hospitals get the org name (Teaching_Hospital_Name). If the
    # legacy parquet doesn't carry the name fields, fall back to None
    # (provider_name is Optional on ProviderRecordSchema).
    has_name_fields = all(
        c in out.columns
        for c in (
            "Covered_Recipient_First_Name",
            "Covered_Recipient_Middle_Name",
            "Covered_Recipient_Last_Name",
        )
    )
    has_org_name = "Teaching_Hospital_Name" in out.columns

    if has_name_fields:
        ent_codes = [_recipient_entity_code(t) for t in out["Covered_Recipient_Type"]]
        org_col = out["Teaching_Hospital_Name"] if has_org_name else [None] * len(out)
        out["provider_name"] = [
            format_provider_name(
                entity_code=ec,
                last_or_org=org if ec == "O" else last,
                first=first,
                middle=mid,
            )
            for ec, org, last, first, mid in zip(
                ent_codes,
                org_col,
                out["Covered_Recipient_Last_Name"],
                out["Covered_Recipient_First_Name"],
                out["Covered_Recipient_Middle_Name"],
            )
        ]
    else:
        out["provider_name"] = None

    out["state"] = out["Recipient_State"].astype("string") if "Recipient_State" in out.columns else None
    out["data_year"] = year

    # Align to canonical schema (NaN-pad declared-but-absent columns).
    out = OpenPaymentsScraper()._align_to_schema(out)
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
        # Even on an already-stamped parquet, a missing manifest sidecar
        # is worth fixing — write it from the parquet column set.
        manifest_path = path.with_suffix(".manifest.json")
        if not manifest_path.exists() and not dry_run:
            _write_manifest(path, df, fetched_at_utc)
            return {
                "path": str(path),
                "manifest": str(manifest_path),
                "status": "manifest-only (parquet already stamped)",
                "rows": len(df),
            }
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
    parser.add_argument("--year", type=int, help="restrict to one year (default: all open_payments_general_*.parquet)")
    args = parser.parse_args()

    if args.year:
        paths = sorted(PANELS_DIR.glob(f"open_payments_general_{args.year}.parquet"))
    else:
        paths = sorted(p for p in PANELS_DIR.glob("open_payments_general_*.parquet") if YEAR_RE.search(p.name))
    if not paths:
        raise SystemExit(
            f"no parquets match open_payments_general_*.parquet in {PANELS_DIR}; "
            f"did you `rclone copy r2:hcfd/panels/open_payments_general_<year>.parquet data/panels/` first?"
        )

    for path in paths:
        result = backfill_one(path, dry_run=args.dry_run)
        print(json.dumps(result))


if __name__ == "__main__":
    main()
