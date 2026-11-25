"""Claim-level features from Part B by Service (per NPI × HCPCS).

Extracts fraud-relevant signals from the service-level data that are
invisible in provider-level aggregates:

  1. E&M level distribution — upcoding signal
  2. J-code (drug infusion) patterns — dispensing/infusion fraud
  3. Specialty-specific code ratios — podiatry, ophthalmology, cataract
  4. Telehealth code concentration — post-COVID fraud signal
  5. Place-of-service patterns — facility vs office vs home vs telehealth

These features require the ``cms_partb_svc_{year}.parquet`` files
(~9-10M rows per year, one row per NPI × HCPCS × POS).

Usage:
    from fraudlib.ranker.claim_level_features import compute_claim_features
    df = compute_claim_features(2023)  # returns NPI-indexed DataFrame
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

PANELS_DIR = Path("data/panels")
CACHE_DIR = Path("data/cache/claim_level_features")

# E&M office visit codes (outpatient, new + established)
EM_CODES_HIGH = {"99215", "99205"}   # Level 5 (highest complexity)
EM_CODES_MID  = {"99214", "99204"}   # Level 4
EM_CODES_LOW  = {"99213", "99203", "99212", "99202", "99211", "99201"}  # Level 1-3

# Telehealth codes (post-COVID expansion)
TELEHEALTH_CODES = {
    "99441", "99442", "99443",  # telephone E&M
    "G2010", "G2012",           # virtual check-in
    "G2250", "G2251", "G2252",  # remote patient monitoring
    "99421", "99422", "99423",  # online digital E&M
}

# Podiatry fraud signature codes
POD_DEBRIDEMENT = {"11721", "11720"}  # nail debridement (full)
POD_TRIMMING = {"11719"}               # trimming (routine)

# Cataract fraud signature codes
CATARACT_COMPLEX = {"66982"}  # complex cataract
CATARACT_ROUTINE = {"66984"}  # routine cataract

# Skin substitute codes (podiatry fraud — extreme per-bene revenue)
SKIN_SUB_CODES = {f"Q{n}" for n in range(4100, 4300)}  # Q41xx-Q42xx range
SKIN_SUB_CODES |= {"15271", "15272", "15273", "15274", "15275", "15276", "15277", "15278"}


def compute_claim_features(year: int) -> pd.DataFrame:
    """Compute claim-level features from Part B Service file.

    Returns NPI-indexed DataFrame with ~15 features per provider.
    Caches to ``data/cache/claim_level_features/claim_{year}.parquet``.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / f"claim_{year}.parquet"
    if cache_path.exists():
        return pd.read_parquet(cache_path)

    svc_path = PANELS_DIR / f"cms_partb_svc_{year}.parquet"
    if not svc_path.exists():
        print(f"[claim_features] No service file for {year}")
        return pd.DataFrame()

    print(f"[claim_features] Computing features for {year}...")
    svc = pd.read_parquet(svc_path)

    # Map to standard column names (CMS uses exact names across years)
    col_map = {
        "Rndrng_NPI": "npi",
        "HCPCS_Cd": "hcpcs_cd",
        "Place_Of_Srvc": "pos",
        "Tot_Benes": "benes",
        "Tot_Srvcs": "svcs",
        "Avg_Mdcr_Pymt_Amt": "avg_payment",
    }
    svc = svc.rename(columns=col_map)

    # Compute total payment per row (avg × svcs)
    svc["benes"] = pd.to_numeric(svc["benes"], errors="coerce").fillna(0)
    svc["svcs"] = pd.to_numeric(svc["svcs"], errors="coerce").fillna(0)
    svc["avg_payment"] = pd.to_numeric(svc["avg_payment"], errors="coerce").fillna(0)
    svc["payment"] = svc["avg_payment"] * svc["svcs"]

    svc["npi"] = svc["npi"].astype(str).str.zfill(10)
    svc["hcpcs_cd"] = svc["hcpcs_cd"].astype(str).str.strip().str.upper()
    svc["pos"] = svc["pos"].astype(str).str.strip()

    # ── Per-NPI aggregations ──
    # Total services per NPI (for share calculations)
    npi_totals = svc.groupby("npi").agg(
        total_svcs=("svcs", "sum"),
        total_payment=("payment", "sum"),
    )

    features = npi_totals[[]].copy()  # empty df with NPI index

    # ── 1. E&M level distribution ──
    em_high = svc[svc["hcpcs_cd"].isin(EM_CODES_HIGH)].groupby("npi")["svcs"].sum()
    em_mid = svc[svc["hcpcs_cd"].isin(EM_CODES_MID)].groupby("npi")["svcs"].sum()
    em_low = svc[svc["hcpcs_cd"].isin(EM_CODES_LOW)].groupby("npi")["svcs"].sum()

    em_total = (em_high.add(em_mid, fill_value=0)
                       .add(em_low, fill_value=0))

    features["cl_em_high_share"] = em_high.div(em_total).fillna(0)
    features["cl_em_mid_share"] = em_mid.div(em_total).fillna(0)
    features["cl_em_high_ratio"] = (
        em_high.add(em_mid, fill_value=0)
        .div(em_low.clip(lower=1))
        .fillna(0)
    )

    # ── 2. J-code (drug infusion) patterns ──
    j_mask = svc["hcpcs_cd"].str.startswith("J")
    j_svcs = svc[j_mask].groupby("npi")["svcs"].sum()
    j_payment = svc[j_mask].groupby("npi")["payment"].sum()
    features["cl_j_code_svc_share"] = j_svcs.div(npi_totals["total_svcs"]).fillna(0)
    features["cl_j_code_pay_share"] = j_payment.div(npi_totals["total_payment"]).fillna(0)

    # Top J-code concentration
    j_by_code = svc[j_mask].groupby(["npi", "hcpcs_cd"])["svcs"].sum().reset_index()
    j_top = j_by_code.sort_values("svcs", ascending=False).groupby("npi").first()
    j_top_share = j_top["svcs"].div(j_svcs).fillna(0)
    features["cl_j_code_top1_concentration"] = j_top_share

    # ── 3. Telehealth code share ──
    th_mask = svc["hcpcs_cd"].isin(TELEHEALTH_CODES)
    th_svcs = svc[th_mask].groupby("npi")["svcs"].sum()
    features["cl_telehealth_code_share"] = th_svcs.div(npi_totals["total_svcs"]).fillna(0)

    # ── 4. Podiatry code ratios ──
    pod_debride = svc[svc["hcpcs_cd"].isin(POD_DEBRIDEMENT)].groupby("npi")["svcs"].sum()
    pod_trim = svc[svc["hcpcs_cd"].isin(POD_TRIMMING)].groupby("npi")["svcs"].sum()
    features["cl_pod_debride_to_trim"] = pod_debride.div(pod_trim.clip(lower=1)).fillna(0)

    # Skin substitute share
    skin_mask = svc["hcpcs_cd"].isin(SKIN_SUB_CODES)
    skin_payment = svc[skin_mask].groupby("npi")["payment"].sum()
    features["cl_skin_sub_pay_share"] = skin_payment.div(npi_totals["total_payment"]).fillna(0)

    # ── 5. Cataract code ratio ──
    cat_complex = svc[svc["hcpcs_cd"].isin(CATARACT_COMPLEX)].groupby("npi")["svcs"].sum()
    cat_routine = svc[svc["hcpcs_cd"].isin(CATARACT_ROUTINE)].groupby("npi")["svcs"].sum()
    features["cl_cataract_complex_ratio"] = cat_complex.div(
        cat_routine.clip(lower=1)
    ).fillna(0)

    # ── 6. Place-of-service patterns ──
    # CMS Part B by-service uses "O" (office) / "F" (facility)
    pos_svcs = svc.groupby(["npi", "pos"])["svcs"].sum().unstack(fill_value=0)
    pos_total = pos_svcs.sum(axis=1).clip(lower=1)

    features["cl_pos_office_share"] = pos_svcs.get("O", pd.Series(0, index=pos_svcs.index)).div(pos_total).fillna(0)
    features["cl_pos_facility_share"] = pos_svcs.get("F", pd.Series(0, index=pos_svcs.index)).div(pos_total).fillna(0)

    # Home and telehealth not available in this format — drop from feature set
    features["cl_pos_home_share"] = 0.0
    features["cl_pos_telehealth_share"] = 0.0

    # ── Finalize ──
    features = features.fillna(0).reset_index()
    features = features.rename(columns={"index": "npi"} if "index" in features.columns else {})

    # Ensure npi is string
    if "npi" not in features.columns and features.index.name == "npi":
        features = features.reset_index()
    features["npi"] = features["npi"].astype(str).str.zfill(10)

    features.to_parquet(cache_path, index=False)
    print(f"[claim_features] {year}: {len(features):,} NPIs, "
          f"{len([c for c in features.columns if c.startswith('cl_')])} features "
          f"→ {cache_path}")
    return features


def attach_claim_features(
    provider_df: pd.DataFrame,
    year: int,
    npi_col: str = "npi",
) -> pd.DataFrame:
    """LEFT JOIN claim-level features onto a provider frame."""
    feats = compute_claim_features(year)
    if feats.empty:
        return provider_df

    # Ensure join keys match
    provider_df[npi_col] = provider_df[npi_col].astype(str).str.zfill(10)
    feats["npi"] = feats["npi"].astype(str).str.zfill(10)

    feat_cols = [c for c in feats.columns if c.startswith("cl_")]
    return provider_df.merge(
        feats[["npi"] + feat_cols],
        left_on=npi_col,
        right_on="npi",
        how="left",
        suffixes=("", "_claim"),
    ).drop(columns=["npi_claim"] if "npi_claim" in provider_df.columns else [], errors="ignore")
