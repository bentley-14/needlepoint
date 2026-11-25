"""One-shot backfill: stamp the legacy nppes_bulk parquet for BaseScraper compatibility.

The existing ``data/registries/nppes_bulk.parquet`` predates the
``pipeline.base.BaseScraper`` ABC. It was produced by
``fraudlib.ingest.providers.nppes_ingest.download_nppes_bulk()`` and
carries only the 10-column ``_KEEP_COLUMNS`` projection — no canonical
NPI columns, no base stamps, no sibling manifest.

This script:

* reads the legacy parquet,
* renames the legacy projection's columns to match v1.0's ``_RENAMES``,
* adds the three v1.0 column expansions
  (``Provider_Business_Practice_Location_Address_State``,
  ``NPI_Deactivation_Reason_Code``, ``NPI_Deactivation_Date``) as NaN if
  the legacy file doesn't have them (it won't),
* populates the four ``ProviderRecordSchema`` canonical columns
  (``npi``, ``provider_name``, ``state``, ``data_year``),
* stamps ``source`` / ``fetched_at_utc`` / ``schema_version``,
* validates against ``NppesSchema`` and writes the manifest.

Workflow:

    make pull-data
    python -m pipeline.scripts.backfill_nppes_manifest --dry-run
    python -m pipeline.scripts.backfill_nppes_manifest
    rclone copy data/registries/nppes_bulk.parquet r2:needlepoint-data/processed/v1/
    rclone copy data/registries/nppes_bulk.manifest.json r2:needlepoint-data/processed/v1/

Idempotent: re-running on an already-stamped parquet is a no-op (detected
by the full canonical column set already being present).

Caveat: the legacy projection lacks the state + deactivation columns, so
the backfilled parquet carries ``state=None`` on every row and the
deactivation columns are all-NaN until the file is re-pulled from CMS
via ``NppesScraper().fetch()``. That's intentional — the backfill is for
manifest hygiene, not data re-download.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from pipeline.base import format_provider_name
from pipeline.scrapers.nppes import (
    _RENAMES,
    _STATE_NAME_TO_ABBR,
    NppesScraper,
    NppesSchema,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REGISTRIES_DIR = PROJECT_ROOT / "data" / "registries"
PARQUET_PATH = REGISTRIES_DIR / "nppes_bulk.parquet"
MANIFEST_PATH = PARQUET_PATH.with_suffix(".manifest.json")

EXPECTED_COLS = frozenset(NppesSchema.to_schema().columns.keys())

SOURCE = "nppes"
SCHEMA_VERSION = "v1.0"
SCHEMA_CLASS = "NppesSchema"


def _file_mtime_utc(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


def _original_fetched_at_utc(path: Path) -> datetime:
    """Prefer the timestamp from an existing manifest over current mtime.

    Once a parquet has been rewritten by a prior backfill pass its mtime
    no longer reflects the original-download moment. If a previous
    manifest exists, its ``started_at_utc`` is the canonical provenance
    we want to preserve.
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
    """True iff the parquet already matches the canonical NppesSchema."""
    return EXPECTED_COLS.issubset(df.columns)


def _stamp(df: pd.DataFrame, *, fetched_at_utc: datetime) -> pd.DataFrame:
    out = df.copy()

    # The legacy parquet was already renamed to snake_case by
    # nppes_ingest.py's _RENAME dict — the columns we see here are
    # already in the v1.0 snake_case form (Entity_Type_Code,
    # Provider_Organization_Name, etc.). But to be defensive against a
    # legacy file that pre-dates that rename, run the v1.0 _RENAMES map
    # too — it's a no-op for already-snake-case columns.
    raw_to_canonical = {raw: canonical for raw, canonical in _RENAMES.items() if raw in out.columns}
    if raw_to_canonical:
        out = out.rename(columns=raw_to_canonical)

    # Pad the v1.0 expansions if the legacy file doesn't have them.
    # state will then be None on every row (the legacy projection didn't
    # capture practice-location state) and the deactivation columns will
    # be all-NaN. A subsequent re-pull via NppesScraper().fetch() will
    # fill them in.
    for col in ("Provider_Business_Practice_Location_Address_State",
                "NPI_Deactivation_Reason_Code",
                "NPI_Deactivation_Date"):
        if col not in out.columns:
            out[col] = pd.NA

    # Base stamps
    out["source"] = SOURCE
    out["fetched_at_utc"] = fetched_at_utc
    out["schema_version"] = SCHEMA_VERSION

    # Canonical NPI columns
    npi_numeric = pd.to_numeric(out["NPI"], errors="coerce")
    out["NPI"] = npi_numeric.astype("Int64")
    out["npi"] = npi_numeric.astype("Int64").astype("string").str.zfill(10)

    _entity_lookup = {"1": "I", "2": "O"}
    entity_mapped = [_entity_lookup.get(v) for v in out["Entity_Type_Code"]]
    out["provider_name"] = [
        format_provider_name(
            entity_code=ec,
            last_or_org=org if ec == "O" else last_indiv,
            first=first,
            middle=None,
        )
        for ec, org, last_indiv, first in zip(
            entity_mapped,
            out["Provider_Organization_Name"],
            out["Provider_Last_Name_Legal_Name"],
            out["Provider_First_Name"],
        )
    ]

    state_name = (
        out["Provider_Business_Practice_Location_Address_State"]
        .astype("string")
        .fillna("")
        .str.upper()
        .str.strip()
    )
    out["state"] = state_name.map(_STATE_NAME_TO_ABBR).astype("string")
    out["data_year"] = pd.Series([pd.NA] * len(out), dtype="Int64")

    # Drop any non-schema columns the legacy projection carried that
    # NppesSchema doesn't declare — the strict validator would reject.
    out = out[[c for c in out.columns if c in EXPECTED_COLS]]

    # Run alignment as a safety net (no-op when all expected cols are
    # already present).
    out = NppesScraper()._align_to_schema(out)
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


def backfill(path: Path, *, dry_run: bool) -> dict:
    if not path.exists():
        raise SystemExit(
            f"{path} not found. Run `make pull-data` first or run "
            "fraudlib.ingest.providers.nppes_ingest.download_nppes_bulk()."
        )

    fetched_at_utc = _original_fetched_at_utc(path)
    df = pd.read_parquet(path)

    if _already_stamped(df):
        return {"path": str(path), "status": "skipped (already stamped)", "rows": len(df)}

    stamped = _stamp(df, fetched_at_utc=fetched_at_utc)

    # Run the scraper's strict validator before writing.
    NppesScraper()._validate(stamped)

    if dry_run:
        return {
            "path": str(path),
            "status": "would stamp",
            "rows": len(stamped),
            "added_cols": sorted(set(stamped.columns) - set(df.columns)),
            "fetched_at_utc": fetched_at_utc.isoformat(),
        }

    stamped.to_parquet(path, index=False)
    manifest_path = _write_manifest(path, stamped, fetched_at_utc)
    return {
        "path": str(path),
        "manifest": str(manifest_path),
        "status": "stamped",
        "rows": len(stamped),
        "fetched_at_utc": fetched_at_utc.isoformat(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report what would change, write nothing")
    args = parser.parse_args()

    result = backfill(PARQUET_PATH, dry_run=args.dry_run)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
