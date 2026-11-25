"""Normalize California CHHS Open Data CSVs into parquet for runtime lookup.

Sources:
  1. Provider Suspended and Ineligible List (S&I List) — Medi-Cal exclusion list
     ~23K entries; the Welfare & Institutions Code 14043.6/14123 list.
  2. Profile of Enrolled Medi-Cal Fee-for-Service (FFS) Providers — current snapshot
     ~357K rows; has dedicated NPI column.

Outputs:
  - data/panels/ca_si_list.parquet            (S&I with NPI extracted from
                                                  Provider Number; license + name
                                                  fallback indexes)
  - data/panels/ca_medi_cal_providers.parquet (FFS provider profile)

These are runtime-only data — the investigator agent queries them via the CLI
``scripts/ca_exclusion_lookup.py``. Not used in ML training.

Usage:
    python scripts/ingest/normalize_ca_chhs.py
"""
from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
RAW_DIR = REPO_ROOT / "data/raw/ca"
OUT_DIR = REPO_ROOT / "data/panels"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# Match a valid 10-digit NPI (starts with 1 or 2). Captures from any string,
# including comma-separated lists in the Provider Number field.
NPI_RE = re.compile(r"\b([12]\d{9})\b")


def _extract_npis(s: str) -> list[str]:
    """Extract all 10-digit NPIs from a string. Returns sorted unique list."""
    if not s or not isinstance(s, str):
        return []
    return sorted({m.group(1) for m in NPI_RE.finditer(s)})


def normalize_si_list(src: Path, dst: Path) -> None:
    """Normalize S&I List CSV.

    The Provider Number field is mixed — sometimes contains the NPI, sometimes
    the license number, sometimes both comma-separated, sometimes 'N/A'.
    We extract all 10-digit NPI patterns into a list column.
    """
    df = pd.read_csv(src, dtype=str, keep_default_na=False, na_values=[""],
                     encoding="latin-1")
    # Clean column names — strip whitespace + newlines
    df.columns = [c.strip().replace("\n", " ") for c in df.columns]
    # Rename for downstream consistency
    df = df.rename(columns={
        "Last Name":         "last_name",
        "First Name":        "first_name",
        "Middle Name":       "middle_name",
        "Address(es)":       "address",
        "Provider Type":     "provider_type",
        "License Number":    "license_number",
        "Provider Number":   "provider_number_raw",
        "Date of Suspension": "date_of_suspension",
        "Active Period":     "active_period",
    })
    # A/K/A column has a long header; find it dynamically
    aka_col = next((c for c in df.columns if "A/K/A" in c.upper() or "ALSO KNOWN" in c.upper()), None)
    if aka_col:
        df = df.rename(columns={aka_col: "aka_dba"})

    # Extract NPIs from the Provider Number field
    df["npis"] = df["provider_number_raw"].apply(_extract_npis)
    df["has_npi"] = df["npis"].apply(lambda v: len(v) > 0)
    df["primary_npi"] = df["npis"].apply(lambda v: v[0] if v else None)
    df["n_npis"] = df["npis"].apply(len)

    # Normalize names for case-insensitive matching
    df["last_name_norm"] = df["last_name"].fillna("").str.upper().str.strip()
    df["first_name_norm"] = df["first_name"].fillna("").str.upper().str.strip()

    # Drop the truly empty rows
    df = df[~df["last_name"].isna() | ~df["first_name"].isna() | df["has_npi"]]

    df.to_parquet(dst, index=False)
    print(f"  S&I List: {len(df):,} rows → {dst}")
    print(f"    With NPI: {df['has_npi'].sum():,} ({100*df['has_npi'].mean():.1f}%)")
    print(f"    Provider types: {df['provider_type'].value_counts().head(5).to_dict()}")


def normalize_provider_profile(src: Path, dst: Path) -> None:
    """Normalize the Medi-Cal FFS Provider Profile CSV."""
    df = pd.read_csv(src, dtype=str, keep_default_na=False, na_values=[""],
                     encoding="latin-1")
    # Clean column names — first one has BOM
    df.columns = [c.strip().replace("﻿", "") for c in df.columns]
    # Rename for downstream consistency
    df = df.rename(columns={
        "OBJECTID":               "objectid",
        "Provider_Source":        "provider_source",
        "Provider_Number":        "provider_number",
        "NPI":                    "npi",
        "Owner_Number":           "owner_number",
        "Service_Location_Number": "service_location_number",
        "Legal_Name":             "legal_name",
        "Enroll_Status_Eff_DT":   "enroll_status_eff_dt",
        "Provider_Taxonomy":      "taxonomy",
        "FI_Provider_Type_CD":    "fi_provider_type_cd",
        "FI_Provider_Type":       "fi_provider_type",
        "Provider_License":       "license",
        "FI_Provider_Specialty_CD": "fi_provider_specialty_cd",
        "FI_Provider_Specialty":  "fi_provider_specialty",
        "Out_of_State_Indicator": "out_of_state_indicator",
        "Address":                "address1",
        "Address2":               "address2",
        "City":                   "city",
        "State":                  "state",
        "ZIP":                    "zip5",
        "ZIP_4":                  "zip4",
    })
    # Only keep rows with a valid 10-digit NPI
    df = df[df["npi"].fillna("").astype(str).str.match(r"^[12]\d{9}$")].copy()
    df["npi"] = df["npi"].astype(str)
    df.to_parquet(dst, index=False)
    print(f"  Medi-Cal FFS Providers: {len(df):,} rows → {dst}")
    print(f"    Distinct NPIs: {df['npi'].nunique():,}")
    print(f"    Top specialties: {df['fi_provider_specialty'].value_counts().head(5).to_dict()}")


def main() -> int:
    print("=== CHHS Normalization ===")
    si_src = RAW_DIR / "si_list_2026-05-20.csv"
    ffs_src = RAW_DIR / "medi_cal_ffs_providers_2026-05-20.csv"
    if not si_src.exists():
        raise FileNotFoundError(f"Missing source: {si_src}")
    if not ffs_src.exists():
        raise FileNotFoundError(f"Missing source: {ffs_src}")

    normalize_si_list(si_src, OUT_DIR / "ca_si_list.parquet")
    normalize_provider_profile(ffs_src, OUT_DIR / "ca_medi_cal_providers.parquet")
    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
