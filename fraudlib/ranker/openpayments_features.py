"""Open Payments (Sunshine Act) per-NPI features.

Aggregates raw CMS Open Payments General Payments data into per-NPI
features capturing industry payment patterns — the key signal for
kickback-adjacent fraud that billing-volume features miss entirely.

Features:
  op_total_received_usd     — total industry payments received
  op_n_payments             — number of distinct payment records
  op_n_manufacturers        — number of distinct paying companies
  op_pay_per_manufacturer   — avg payment per manufacturer (concentration)
  op_n_meals                — meals/beverages count
  op_n_travel               — travel/lodging count
  op_n_consulting           — consulting fee count
  op_n_speaker              — speaker/faculty count
  op_n_education            — education count
  op_n_royalty              — royalty/license count
  op_has_royalty            — binary: any royalty income
  op_speaker_share          — speaker payments as fraction of total count

Data source: data/panels/open_payments_general_{year}.parquet
Coverage: 2016–2023 (2013–2015 lack NPI column)

Usage:
    from fraudlib.ranker.openpayments_features import compute_op_features
    df = compute_op_features(2023)
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

PANELS_DIR = Path("data/panels")
CACHE_DIR = Path("data/cache/openpayments_features")

_NATURE_BUCKET = {
    "Food and Beverage": "meals",
    "Travel and Lodging": "travel",
    "Consulting Fee": "consulting",
    "Compensation for services other than consulting, including serving as faculty or as a speaker at a venue other than a continuing education program": "speaker",
    "Compensation for serving as faculty or as a speaker for a non-accredited and noncertified continuing education program": "speaker",
    "Compensation for serving as faculty or as a speaker for an accredited or certified continuing education program": "speaker",
    "Education": "education",
    "Royalty or License": "royalty",
    "Current or prospective ownership or investment interest": "royalty",
    "Honoraria": "speaker",
    "Gift": "meals",
    "Entertainment": "meals",
    "Charitable Contribution": "education",
    "Space rental or facility fees (teaching hospital only)": "education",
    "Grant": "education",
}


def compute_op_features(year: int) -> pd.DataFrame:
    """Compute per-NPI Open Payments features.

    Returns NPI-indexed DataFrame. Caches to
    ``data/cache/openpayments_features/op_{year}.parquet``.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / f"op_{year}.parquet"
    if cache_path.exists():
        return pd.read_parquet(cache_path)

    src = PANELS_DIR / f"open_payments_general_{year}.parquet"
    if not src.exists():
        print(f"[op_features] No Open Payments file for {year}")
        return pd.DataFrame()

    raw = pd.read_parquet(src)

    npi_col = "Covered_Recipient_NPI"
    if npi_col not in raw.columns:
        print(f"[op_features] {year}: no NPI column — cannot join (pre-2016 schema)")
        return pd.DataFrame()

    print(f"[op_features] Computing features for {year}...")

    raw[npi_col] = pd.to_numeric(raw[npi_col], errors="coerce")
    raw = raw[raw[npi_col].notna() & (raw[npi_col] > 0)].copy()
    raw["npi"] = raw[npi_col].astype(int).astype(str).str.zfill(10)

    amt_col = "Total_Amount_of_Payment_USDollars"
    raw["amount"] = pd.to_numeric(raw[amt_col], errors="coerce").fillna(0)

    nature_col = "Nature_of_Payment_or_Transfer_of_Value"
    if nature_col in raw.columns:
        raw["bucket"] = raw[nature_col].map(_NATURE_BUCKET).fillna("other")
    else:
        raw["bucket"] = "other"

    mfr_col = "Applicable_Manufacturer_or_Applicable_GPO_Making_Payment_Name"

    g = raw.groupby("npi")

    features = pd.DataFrame({
        "npi": g["npi"].first(),
        "op_total_received_usd": g["amount"].sum(),
        "op_n_payments": g["amount"].count(),
    })

    if mfr_col in raw.columns:
        mfr_counts = raw.groupby("npi")[mfr_col].nunique()
        features["op_n_manufacturers"] = mfr_counts
    else:
        features["op_n_manufacturers"] = 1

    features["op_pay_per_manufacturer"] = (
        features["op_total_received_usd"] /
        features["op_n_manufacturers"].clip(lower=1)
    )

    # Nature-of-payment bucket counts
    bucket_counts = raw.pivot_table(
        index="npi", columns="bucket", values="amount",
        aggfunc="count", fill_value=0,
    )
    for b in ["meals", "travel", "consulting", "speaker", "education", "royalty"]:
        col = f"op_n_{b}"
        features[col] = bucket_counts.get(b, 0)

    features["op_has_royalty"] = (features["op_n_royalty"] > 0).astype(int)

    total_counts = features["op_n_payments"].clip(lower=1)
    features["op_speaker_share"] = features["op_n_speaker"] / total_counts

    features = features.fillna(0).reset_index(drop=True)
    features["npi"] = features["npi"].astype(str).str.zfill(10)

    features.to_parquet(cache_path, index=False)
    n_feats = len([c for c in features.columns if c.startswith("op_")])
    print(f"[op_features] {year}: {len(features):,} NPIs, {n_feats} features → {cache_path}")
    return features


def attach_op_features(
    provider_df: pd.DataFrame,
    year: int,
    npi_col: str = "npi",
) -> pd.DataFrame:
    """LEFT JOIN Open Payments features onto a provider frame."""
    feats = compute_op_features(year)
    if feats.empty:
        return provider_df

    provider_df[npi_col] = provider_df[npi_col].astype(str).str.zfill(10)
    feats["npi"] = feats["npi"].astype(str).str.zfill(10)

    feat_cols = [c for c in feats.columns if c.startswith("op_")]
    return provider_df.merge(
        feats[["npi"] + feat_cols],
        left_on=npi_col,
        right_on="npi",
        how="left",
        suffixes=("", "_op"),
    ).drop(columns=["npi_op"] if "npi_op" in provider_df.columns else [], errors="ignore")
