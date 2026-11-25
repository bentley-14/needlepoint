#!/usr/bin/env python3
"""Build authoritative clinical-code ground-truth references from CMS PUFs.

Why this exists
---------------
Agents were *hallucinating* code identities — e.g. pleading "J3304
(methylprednisolone)" when J3304 is triamcinolone, and reading "83-90 services"
off a per-1mg drug code as "83-90 injections". The fix is to give them a
ground-truth lookup so they resolve every code instead of guessing.

Outputs (under data/ground_truth/)
----------------------------------
  hcpcs_codes.{parquet,jsonl}
      Every HCPCS/CPT code billed under Medicare Part B, with:
        - descriptor   : CMS's own published HCPCS_Desc (most recent year)
        - is_drug       : from the authoritative HCPCS_Drug_Ind flag
        - unit_basis    : parsed billing unit — "1 mg" / "40 mg" / "per dose"
                          for drugs, "per service" for procedures. THIS is what
                          distinguishes per-mg drug units from per-event counts.
        - code_type     : CPT (procedure/E&M) vs HCPCS-II drug/DME/etc.
        - prevalence    : first/last year, n_years, total services + benes
  partd_drugs.{parquet,jsonl}
      Distinct Part D brand -> generic drug-name pairs (drug-identity truth).

Source of truth: data/processed/cms_partb_svc_*.parquet (HCPCS_Desc /
HCPCS_Drug_Ind are CMS-published) and cms_partd_*.parquet.

Rebuild: python scripts/ingest/build_hcpcs_reference.py
"""
from __future__ import annotations

import glob
import json
import os
import re
from pathlib import Path

import duckdb
import pandas as pd

REPO = Path(__file__).resolve().parents[2]
OUTDIR = REPO / "data" / "ground_truth"
OUTDIR.mkdir(parents=True, exist_ok=True)

DOSE_UNITS = r"(mcg|mg|ml|gm|g|iu|meq|units|unit|billion units|million units|cc)"


def _files_sql(paths) -> str:
    return "[" + ",".join(repr(str(p)) for p in paths) + "]"


def parse_dose_unit(desc: str) -> str | None:
    """Parse a drug-dose billing unit from the CMS descriptor, or None.

    Drug J-codes encode their unit in the descriptor (e.g. "..., 1 mg",
    "per 5 mg", "per dose"). Returning this regardless of the CMS drug flag
    makes the reference robust to years where HCPCS_Drug_Ind is wrongly 'N'
    (observed on J3300/J3302/J3303). None => no dose unit (i.e. a procedure).
    """
    d = (desc or "").lower().strip().rstrip(".")
    if "per dose" in d:
        return "per dose"
    m = re.search(rf"(\d+(?:\.\d+)?)\s*{DOSE_UNITS}\b", d)
    if m:
        return f"{m.group(1)} {m.group(2)}"
    return None


def code_type(code: str, is_drug: bool) -> str:
    if code[:1].isdigit():
        return "CPT (procedure/E&M)"
    c0 = code[:1].upper()
    if is_drug:
        return "HCPCS-II drug/biological"
    fam = {
        "A": "HCPCS-II supplies/transport", "E": "HCPCS-II DME", "K": "HCPCS-II DME",
        "L": "HCPCS-II orthotics/prosthetics", "G": "HCPCS-II temporary (G)",
        "Q": "HCPCS-II temporary (Q)", "C": "HCPCS-II outpatient (C)",
        "P": "HCPCS-II pathology", "J": "HCPCS-II (J)", "V": "HCPCS-II vision/hearing",
    }
    return fam.get(c0, "HCPCS-II")


def _write_atomic(df, stem: str) -> None:
    """Write parquet + jsonl via temp-file + atomic rename."""
    pq, jl = OUTDIR / f"{stem}.parquet", OUTDIR / f"{stem}.jsonl"
    pq_tmp, jl_tmp = pq.with_suffix(".parquet.tmp"), jl.with_suffix(".jsonl.tmp")
    df.to_parquet(pq_tmp, index=False)
    with jl_tmp.open("w") as f:
        for rec in df.to_dict(orient="records"):
            f.write(json.dumps(rec, default=str) + "\n")
    os.replace(pq_tmp, pq)
    os.replace(jl_tmp, jl)


def build_hcpcs():
    src = sorted((REPO / "data" / "processed").glob("cms_partb_svc_*.parquet"))
    if not src:
        raise SystemExit("no cms_partb_svc_*.parquet found under data/processed/")
    # One row per (code, year): the descriptor / drug-flag / volume CMS published
    # that year. Identities DO change across years (codes reassigned, descriptors
    # revised, doses reclassified, drug flag flipped), so we keep the FULL per-year
    # history and let callers resolve the year contemporaneous with the claim
    # instead of anachronistically applying the current descriptor to an old claim.
    by_year = duckdb.sql(
        """
        WITH base AS (
          SELECT HCPCS_Cd AS code, HCPCS_Desc AS descriptor, HCPCS_Drug_Ind AS drug_ind,
                 CAST(regexp_extract(filename, '([0-9]{4})') AS INT) AS year,
                 Tot_Srvcs AS srvcs, Tot_Benes AS benes
          FROM read_parquet(""" + _files_sql(src) + """, filename=true)
        )
        SELECT code, year,
               max(descriptor) AS descriptor,
               bool_or(drug_ind = 'Y') AS drug_ind_y,
               SUM(srvcs) AS total_services, SUM(benes) AS total_beneficiaries
        FROM base GROUP BY code, year ORDER BY code, year
        """
    ).df()
    by_year["dose_unit"] = [parse_dose_unit(d) for d in by_year["descriptor"]]
    by_year["is_drug"] = by_year["drug_ind_y"].fillna(False) | by_year["dose_unit"].notna()
    by_year["unit_basis"] = [u if u else "per service" for u in by_year["dose_unit"]]
    by_year["code_type"] = [code_type(c, b) for c, b in zip(by_year["code"], by_year["is_drug"])]
    by_year = by_year[[
        "code", "year", "descriptor", "is_drug", "unit_basis", "code_type",
        "total_services", "total_beneficiaries",
    ]]
    _write_atomic(by_year, "hcpcs_codes_by_year")

    # Canonical rollup: one row per code (most-recent identity) + change flags so a
    # caller knows when the per-year table must be consulted for a historical claim.
    canon_rows = []
    for code, sub in by_year.sort_values(["code", "year"]).groupby("code", sort=True):
        recent = sub.iloc[-1]
        n_desc, n_unit, n_drug_states = (sub["descriptor"].nunique(),
                                         sub["unit_basis"].nunique(), sub["is_drug"].nunique())
        canon_rows.append({
            "code": code,
            "descriptor": recent["descriptor"],
            "is_drug": bool(sub["is_drug"].any()),
            "unit_basis": recent["unit_basis"],
            "code_type": recent["code_type"],
            "first_year": int(sub["year"].min()),
            "last_year": int(sub["year"].max()),
            "n_years": int(sub["year"].nunique()),
            "n_distinct_descriptors": int(n_desc),
            "n_distinct_units": int(n_unit),
            "varies_by_year": bool(n_desc > 1 or n_unit > 1 or n_drug_states > 1),
            "total_services": float(sub["total_services"].sum()),
            "total_beneficiaries": float(sub["total_beneficiaries"].sum()),
        })
    canon = pd.DataFrame(canon_rows)
    _write_atomic(canon, "hcpcs_codes")
    n_drug = int(canon["is_drug"].sum())
    n_vary = int(canon["varies_by_year"].sum())
    print(f"hcpcs_codes: {len(canon)} codes ({n_drug} drug) + "
          f"hcpcs_codes_by_year: {len(by_year)} (code,year) rows; "
          f"{n_vary} codes vary across years")
    return canon


def build_partd_drugs():
    src = sorted((REPO / "data" / "processed").glob("cms_partd_prescriber_drug_*.parquet"))
    if not src:
        print("partd_drugs: no cms_partd_prescriber_drug_*.parquet — skipping")
        return None
    df = duckdb.sql(
        "SELECT DISTINCT Brnd_Name AS brand_name, Gnrc_Name AS generic_name FROM read_parquet("
        + _files_sql(src) + ", union_by_name=true) WHERE Brnd_Name IS NOT NULL ORDER BY 1, 2"
    ).df()
    _write_atomic(df, "partd_drugs")
    print(f"partd_drugs: {len(df)} brand/generic pairs -> partd_drugs.parquet + .jsonl")
    return df


if __name__ == "__main__":
    build_hcpcs()
    build_partd_drugs()
