"""Bauder 2019 features for non-volume fraud detection.

Per Bauder, Herland & Khoshgoftaar (2019, *Health Information Science
and Systems*): procedure-mix concentration entropy, place-of-service
distribution, and beneficiary-to-service ratios are the features that
catch HHA-referral PCPs and phantom-E/M billers — the archetypes our
ranker currently misses.

Built from cms_partb_svc_<year>.parquet (the per-NPI × HCPCS × POS
service-level file). Output is NPI-keyed, LEFT-JOINable onto the
multi-source provider frame.

Features produced (per NPI):
  bauder_full_hcpcs_entropy     — Shannon entropy over all HCPCS codes
                                  (low = narrow billing, phantom signal)
  bauder_top3_code_share        — fraction of total services from top-3
                                  HCPCS codes (high = narrow focus)
  bauder_n_distinct_hcpcs       — count of distinct codes billed
  bauder_facility_share         — fraction of services in facility POS
  bauder_office_share           — fraction in non-facility (office) POS
  bauder_services_per_bene      — already exists in Part B, re-derived
                                  here for consistency
  bauder_bene_per_hcpcs         — beneficiaries / distinct codes
  bauder_svc_per_bene_per_code  — (total svc / total bene) / n_codes
                                  — bills many codes per patient = legit;
                                    few codes × many services = phantom
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

SVC_TEMPLATE = "data/panels/cms_partb_svc_{year}.parquet"


def compute_bauder_features(year: int, svc_path: Path | str | None = None) -> pd.DataFrame:
    """Return NPI-indexed Bauder features for ``year``.

    Reads the CMS Part B service-level PUF and aggregates to one row
    per NPI.
    """
    path = Path(svc_path or SVC_TEMPLATE.format(year=year))
    if not path.exists():
        raise FileNotFoundError(f"Missing Part B service-level file: {path}")

    # Read only needed columns (10M+ rows — keep lean)
    cols = ["Rndrng_NPI", "HCPCS_Cd", "Place_Of_Srvc", "Tot_Benes", "Tot_Srvcs"]
    df = pd.read_parquet(path, columns=cols)
    df["Rndrng_NPI"] = df["Rndrng_NPI"].astype("Int64").astype("string").str.zfill(10)

    # --- Per-NPI aggregation ---

    # 1. Place of service shares
    pos_agg = (
        df.groupby(["Rndrng_NPI", "Place_Of_Srvc"])["Tot_Srvcs"]
        .sum()
        .unstack(fill_value=0)
    )
    pos_total = pos_agg.sum(axis=1)
    facility_col = "F" if "F" in pos_agg.columns else None
    office_col = "O" if "O" in pos_agg.columns else None
    facility_share = (pos_agg[facility_col] / pos_total) if facility_col else 0.0
    office_share = (pos_agg[office_col] / pos_total) if office_col else 0.0

    # 2. HCPCS distribution features (vectorized — no per-group apply)
    code_agg = df.groupby(["Rndrng_NPI", "HCPCS_Cd"])["Tot_Srvcs"].sum()
    npi_total_svc = code_agg.groupby(level=0).sum()
    n_distinct_codes = code_agg.groupby(level=0).count()

    # Shannon entropy — vectorized: p = val/group_sum, entropy = -Σ(p log2 p)
    npi_total_expanded = code_agg.groupby(level=0).transform("sum")
    p = (code_agg / npi_total_expanded).clip(lower=1e-15)
    info = -p * np.log2(p)
    entropy = info.groupby(level=0).sum()

    # Top-3 code concentration — vectorized via rank
    rank_within = code_agg.groupby(level=0).rank(ascending=False, method="first")
    top3_mask = rank_within <= 3
    top3_sum = code_agg[top3_mask].groupby(level=0).sum()
    top3_share = (top3_sum / npi_total_svc).fillna(1.0)

    # 3. Bene aggregates
    npi_total_bene = df.groupby("Rndrng_NPI")["Tot_Benes"].sum()

    # Assemble
    out = pd.DataFrame({
        "NPI": npi_total_svc.index,
        "bauder_full_hcpcs_entropy": entropy.values,
        "bauder_top3_code_share": top3_share.values,
        "bauder_n_distinct_hcpcs": n_distinct_codes.values,
        "bauder_facility_share": (
            facility_share.reindex(npi_total_svc.index).fillna(0.0).values
            if isinstance(facility_share, pd.Series) else 0.0
        ),
        "bauder_office_share": (
            office_share.reindex(npi_total_svc.index).fillna(0.0).values
            if isinstance(office_share, pd.Series) else 0.0
        ),
        "bauder_services_per_bene": (
            npi_total_svc / npi_total_bene.reindex(npi_total_svc.index).replace(0, np.nan)
        ).fillna(0.0).values,
        "bauder_bene_per_hcpcs": (
            npi_total_bene.reindex(npi_total_svc.index) / n_distinct_codes.replace(0, np.nan)
        ).fillna(0.0).values,
        "bauder_svc_per_bene_per_code": (
            (npi_total_svc / npi_total_bene.reindex(npi_total_svc.index).replace(0, np.nan))
            / n_distinct_codes.replace(0, np.nan)
        ).fillna(0.0).values,
    })
    return out.reset_index(drop=True)


CACHE_DIR = Path("data/cache/bauder_features")


def attach_bauder_features(
    provider_df: pd.DataFrame, year: int, npi_col: str = "npi"
) -> pd.DataFrame:
    """LEFT JOIN Bauder features onto a provider-level frame.

    Uses cached parquet at ``data/cache/bauder_features/bauder_<year>.parquet``
    if available; otherwise computes from the service-level file.
    """
    cache_path = CACHE_DIR / f"bauder_{year}.parquet"
    if cache_path.exists():
        bauder = pd.read_parquet(cache_path)
    else:
        bauder = compute_bauder_features(year)
    provider_df = provider_df.copy()
    provider_df["_npi_join"] = provider_df[npi_col].astype(str).str.zfill(10)
    merged = provider_df.merge(
        bauder.rename(columns={"NPI": "_npi_join"}),
        on="_npi_join",
        how="left",
    )
    merged = merged.drop(columns=["_npi_join"])
    # Fill missing (providers not in svc file) with 0
    bauder_cols = [c for c in merged.columns if c.startswith("bauder_")]
    merged[bauder_cols] = merged[bauder_cols].fillna(0.0)
    return merged
