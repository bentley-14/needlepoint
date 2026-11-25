"""Year-over-year temporal trajectory features.

For a scoring year Y, computes how each provider's billing changed
relative to prior years. Catches fraud onset patterns invisible in
single-year snapshots.

CRITICAL LEAKAGE GUARD: For scoring year Y, only uses data from
years ≤ Y-1. Never looks forward.

Features:
  - payment/services/beneficiary growth (1yr and 3yr)
  - opioid prescribing trajectory
  - brand-name drug trajectory
  - panel stability (years present, payment volatility)
  - new entrant detection

Usage:
    from fraudlib.ranker.temporal_features import compute_temporal_features
    df = compute_temporal_features(2023)  # uses only 2013-2022 data
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

PANELS_DIR = Path("data/panels")
CACHE_DIR = Path("data/cache/temporal_features")


def _load_partb_slim(year: int) -> pd.DataFrame | None:
    """Load minimal Part B columns for one year."""
    path = PANELS_DIR / f"cms_partb_{year}.parquet"
    if not path.exists():
        return None
    cols = [
        "Rndrng_NPI",
        "Tot_Mdcr_Pymt_Amt",
        "Tot_Srvcs",
        "Tot_Benes",
    ]
    df = pd.read_parquet(path, columns=cols)
    df = df.rename(columns={
        "Rndrng_NPI": "npi",
        "Tot_Mdcr_Pymt_Amt": "payment",
        "Tot_Srvcs": "services",
        "Tot_Benes": "beneficiaries",
    })
    df["npi"] = df["npi"].astype(str).str.zfill(10)
    for c in ["payment", "services", "beneficiaries"]:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
    return df


def _load_partd_slim(year: int) -> pd.DataFrame | None:
    """Load minimal Part D columns for opioid trajectory."""
    path = PANELS_DIR / f"cms_partd_features_{year}.parquet"
    if not path.exists():
        path = PANELS_DIR / f"cms_partd_{year}.parquet"
    if not path.exists():
        return None
    df = pd.read_parquet(path)
    # Find opioid and brand columns
    opioid_col = next((c for c in df.columns if "opioid" in c.lower() and "share" in c.lower()), None)
    brand_col = next((c for c in df.columns if "brand" in c.lower() and "share" in c.lower()), None)
    npi_col = next((c for c in df.columns if "npi" in c.lower()), None)
    if not npi_col:
        return None

    result = pd.DataFrame()
    result["npi"] = df[npi_col].astype(str).str.zfill(10)
    if opioid_col:
        result["opioid_share"] = pd.to_numeric(df[opioid_col], errors="coerce").fillna(0)
    if brand_col:
        result["brand_share"] = pd.to_numeric(df[brand_col], errors="coerce").fillna(0)
    return result


def compute_temporal_features(
    scoring_year: int,
    lookback: int = 5,
) -> pd.DataFrame:
    """Compute temporal trajectory features for a scoring year.

    LEAKAGE GUARD: Only uses data from years < scoring_year.

    Parameters
    ----------
    scoring_year : int
        The year being scored (features computed relative to this year).
    lookback : int
        How many prior years to consider (default 5).
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / f"temporal_{scoring_year}.parquet"
    if cache_path.exists():
        return pd.read_parquet(cache_path)

    print(f"[temporal] Computing features for scoring year {scoring_year} "
          f"(using {scoring_year - lookback}-{scoring_year - 1} only)...")

    # Load Part B for prior years (NEVER the scoring year itself)
    prior_years = list(range(scoring_year - lookback, scoring_year))
    partb_by_year: dict[int, pd.DataFrame] = {}
    for y in prior_years:
        df = _load_partb_slim(y)
        if df is not None:
            partb_by_year[y] = df.set_index("npi")

    if not partb_by_year:
        print(f"[temporal] No prior Part B data for {scoring_year}")
        return pd.DataFrame()

    # All NPIs seen in any prior year
    all_npis = set()
    for df in partb_by_year.values():
        all_npis |= set(df.index)

    features = pd.DataFrame(index=sorted(all_npis))
    features.index.name = "npi"

    # ── Billing trajectory ──
    most_recent = max(partb_by_year.keys())
    y_minus_1 = partb_by_year.get(most_recent)
    y_minus_3_key = most_recent - 2
    y_minus_3 = partb_by_year.get(y_minus_3_key)

    if y_minus_1 is not None:
        # Need y_minus_2 for 1-year growth
        y_minus_2_key = most_recent - 1
        y_minus_2 = partb_by_year.get(y_minus_2_key)

        if y_minus_2 is not None:
            pay_1 = y_minus_1["payment"].reindex(features.index, fill_value=0)
            pay_2 = y_minus_2["payment"].reindex(features.index, fill_value=0)
            features["tp_payment_growth_1yr"] = (pay_1 / pay_2.clip(lower=1)) - 1
            features["tp_payment_growth_1yr"] = features["tp_payment_growth_1yr"].clip(-1, 10)

            svc_1 = y_minus_1["services"].reindex(features.index, fill_value=0)
            svc_2 = y_minus_2["services"].reindex(features.index, fill_value=0)
            features["tp_svcs_growth_1yr"] = (svc_1 / svc_2.clip(lower=1)) - 1
            features["tp_svcs_growth_1yr"] = features["tp_svcs_growth_1yr"].clip(-1, 10)

            bene_1 = y_minus_1["beneficiaries"].reindex(features.index, fill_value=0)
            bene_2 = y_minus_2["beneficiaries"].reindex(features.index, fill_value=0)
            features["tp_bene_growth_1yr"] = (bene_1 / bene_2.clip(lower=1)) - 1
            features["tp_bene_growth_1yr"] = features["tp_bene_growth_1yr"].clip(-1, 10)

        if y_minus_3 is not None:
            pay_1 = y_minus_1["payment"].reindex(features.index, fill_value=0)
            pay_3 = y_minus_3["payment"].reindex(features.index, fill_value=0)
            features["tp_payment_growth_3yr"] = (pay_1 / pay_3.clip(lower=1)) - 1
            features["tp_payment_growth_3yr"] = features["tp_payment_growth_3yr"].clip(-1, 20)

    # ── Prescribing trajectory ──
    partd_recent = _load_partd_slim(most_recent)
    partd_prior = _load_partd_slim(most_recent - 1)
    if partd_recent is not None and partd_prior is not None:
        pr = partd_recent.set_index("npi")
        pp = partd_prior.set_index("npi")
        if "opioid_share" in pr.columns and "opioid_share" in pp.columns:
            op_r = pr["opioid_share"].reindex(features.index, fill_value=0)
            op_p = pp["opioid_share"].reindex(features.index, fill_value=0)
            features["tp_opioid_change_1yr"] = op_r - op_p
        if "brand_share" in pr.columns and "brand_share" in pp.columns:
            br_r = pr["brand_share"].reindex(features.index, fill_value=0)
            br_p = pp["brand_share"].reindex(features.index, fill_value=0)
            features["tp_brand_change_1yr"] = br_r - br_p

    # ── Stability ──
    # Years present in prior window
    presence = pd.DataFrame(index=features.index)
    for y, df in partb_by_year.items():
        presence[y] = df["payment"].reindex(features.index).notna().astype(int)
    features["tp_years_in_panel"] = presence.sum(axis=1)

    # Payment volatility
    pay_matrix = pd.DataFrame(index=features.index)
    for y, df in partb_by_year.items():
        pay_matrix[y] = df["payment"].reindex(features.index)
    pay_mean = pay_matrix.mean(axis=1).clip(lower=1)
    pay_std = pay_matrix.std(axis=1).fillna(0)
    features["tp_payment_volatility"] = (pay_std / pay_mean).clip(0, 10)

    # ── New entrant ──
    if y_minus_1 is not None:
        in_recent = set(y_minus_1.index)
        y_minus_2_key = most_recent - 1
        y_m2 = partb_by_year.get(y_minus_2_key)
        if y_m2 is not None:
            in_prior = set(y_m2.index)
            features["tp_is_new_entrant"] = (
                features.index.isin(in_recent) & ~features.index.isin(in_prior)
            ).astype(int)
        else:
            features["tp_is_new_entrant"] = 0
    else:
        features["tp_is_new_entrant"] = 0

    # ── Finalize ──
    features = features.fillna(0).reset_index()
    features.to_parquet(cache_path, index=False)
    tp_cols = [c for c in features.columns if c.startswith("tp_")]
    print(f"[temporal] {scoring_year}: {len(features):,} NPIs, {len(tp_cols)} features → {cache_path}")
    return features


def attach_temporal_features(
    provider_df: pd.DataFrame,
    scoring_year: int,
    npi_col: str = "npi",
) -> pd.DataFrame:
    """LEFT JOIN temporal features onto a provider frame."""
    feats = compute_temporal_features(scoring_year)
    if feats.empty:
        return provider_df

    provider_df[npi_col] = provider_df[npi_col].astype(str).str.zfill(10)
    feats["npi"] = feats["npi"].astype(str).str.zfill(10)

    tp_cols = [c for c in feats.columns if c.startswith("tp_")]
    return provider_df.merge(
        feats[["npi"] + tp_cols],
        left_on=npi_col,
        right_on="npi",
        how="left",
        suffixes=("", "_temporal"),
    ).drop(columns=["npi_temporal"] if "npi_temporal" in provider_df.columns else [], errors="ignore")
