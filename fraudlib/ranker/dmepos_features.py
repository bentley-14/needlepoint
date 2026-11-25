"""DMEPOS referring-provider features.

Extracts fraud-relevant signals from the DMEPOS (Durable Medical Equipment,
Prosthetics, Orthotics, and Supplies) referring-provider dataset. Each row
is one physician who ordered DME for Medicare beneficiaries.

Key fraud signals:
  - DME ordering volume relative to Part B billing (cross-program anomaly)
  - Number of distinct suppliers used (spread = normal; concentration = suspect)
  - DME-to-Part-B payment ratio (high = possible DME scheme certification)
  - Prosthetics/orthotics vs DME split

CMS publishes this data for 2014–2023. 2013 is unavailable (referring-provider
cut first appears at DY14).

Usage:
    from fraudlib.ranker.dmepos_features import compute_dmepos_features
    df = compute_dmepos_features(2023)
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

PROCESSED_DIR = Path("data/panels")
CACHE_DIR = Path("data/cache/dmepos_features")


def compute_dmepos_features(year: int) -> pd.DataFrame:
    """Compute per-referring-NPI DMEPOS features.

    Returns NPI-indexed DataFrame. Caches to
    ``data/cache/dmepos_features/dmepos_{year}.parquet``.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / f"dmepos_{year}.parquet"
    if cache_path.exists():
        return pd.read_parquet(cache_path)

    src = PROCESSED_DIR / f"cms_dmepos_{year}.parquet"
    if not src.exists():
        print(f"[dmepos] No DMEPOS file for {year}")
        return pd.DataFrame()

    print(f"[dmepos] Computing features for {year}...")
    raw = pd.read_parquet(src)

    raw["npi"] = raw["Rfrg_NPI"].astype(str).str.zfill(10)

    for c in ["Tot_Suplrs", "Tot_Suplr_Benes", "Tot_Suplr_Clms",
              "Tot_Suplr_Srvcs", "Suplr_Mdcr_Pymt_Amt", "Suplr_Sbmtd_Chrgs",
              "DME_Tot_Suplrs", "DME_Tot_Suplr_Benes", "DME_Tot_Suplr_Clms",
              "DME_Tot_Suplr_Srvcs", "DME_Suplr_Mdcr_Pymt_Amt",
              "Tot_Suplr_HCPCS_Cds", "DME_Tot_Suplr_HCPCS_Cds"]:
        if c in raw.columns:
            raw[c] = pd.to_numeric(raw[c], errors="coerce").fillna(0)

    features = pd.DataFrame({"npi": raw["npi"]})

    # Total DMEPOS ordering
    features["dme_total_payment"] = raw["Suplr_Mdcr_Pymt_Amt"]
    features["dme_total_claims"] = raw["Tot_Suplr_Clms"]
    features["dme_total_benes"] = raw["Tot_Suplr_Benes"]
    features["dme_total_services"] = raw["Tot_Suplr_Srvcs"]
    features["dme_n_suppliers"] = raw["Tot_Suplrs"]
    features["dme_n_hcpcs"] = raw["Tot_Suplr_HCPCS_Cds"]

    # DME-specific (vs prosthetics/orthotics/supplies)
    features["dme_dme_payment"] = raw["DME_Suplr_Mdcr_Pymt_Amt"]
    features["dme_dme_claims"] = raw["DME_Tot_Suplr_Clms"]
    features["dme_dme_benes"] = raw["DME_Tot_Suplr_Benes"]

    # Derived ratios
    total_pay = features["dme_total_payment"].clip(lower=1)
    total_benes = features["dme_total_benes"].clip(lower=1)
    total_claims = features["dme_total_claims"].clip(lower=1)

    features["dme_pay_per_bene"] = features["dme_total_payment"] / total_benes
    features["dme_claims_per_bene"] = features["dme_total_claims"] / total_benes
    features["dme_dme_share"] = features["dme_dme_payment"] / total_pay

    # Supplier concentration (1 supplier = concentrated; many = distributed)
    features["dme_svcs_per_supplier"] = features["dme_total_services"] / features["dme_n_suppliers"].clip(lower=1)

    # Charge-to-allowed ratio (overcharging signal)
    submitted = pd.to_numeric(raw.get("Suplr_Sbmtd_Chrgs", 0), errors="coerce").fillna(0)
    allowed = pd.to_numeric(raw.get("Suplr_Mdcr_Alowd_Amt", total_pay), errors="coerce").fillna(0).clip(lower=1)
    features["dme_chrg_to_allowed"] = submitted / allowed

    features = features.fillna(0)
    features.to_parquet(cache_path, index=False)
    print(f"[dmepos] {year}: {len(features):,} NPIs, "
          f"{len([c for c in features.columns if c.startswith('dme_')])} features → {cache_path}")
    return features


def attach_dmepos_features(
    provider_df: pd.DataFrame,
    year: int,
    npi_col: str = "npi",
) -> pd.DataFrame:
    """LEFT JOIN DMEPOS features onto a provider frame."""
    feats = compute_dmepos_features(year)
    if feats.empty:
        return provider_df

    provider_df[npi_col] = provider_df[npi_col].astype(str).str.zfill(10)
    feats["npi"] = feats["npi"].astype(str).str.zfill(10)

    feat_cols = [c for c in feats.columns if c.startswith("dme_")]
    return provider_df.merge(
        feats[["npi"] + feat_cols],
        left_on=npi_col,
        right_on="npi",
        how="left",
        suffixes=("", "_dmepos"),
    ).drop(columns=["npi_dmepos"] if "npi_dmepos" in provider_df.columns else [], errors="ignore")
