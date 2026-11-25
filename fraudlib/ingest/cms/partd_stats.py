"""Part D prescriber-level aggregation helper.

Reads the year's Part D prescriber parquet and returns per-NPI ratios
and shares that capture drug-prescribing signatures the Part B aggregate
panel is blind to — pill-mill detection, psych-med overprescribers,
brand-vs-generic arbitrage, antipsychotic overuse in dementia, etc.

Intended to be joined onto the existing `cms_partb_<year>.parquet` panel
on `Rndrng_NPI == PRSCRBR_NPI`.

Available for 2013-2023 via the CMS Direct URL table in
`pipeline.scrapers.download` (RY25 V20 release for 2013-2022; V10 for 2023).

The features below were chosen to sharpen specific case-library fraud
archetypes:

  * ``opioid_clm_share`` / ``opioid_la_clm_share`` — pill-mill detection
    (Ruan, Couch, Argeropoulos-style). Long-acting opioid share is the
    sharper of the two because LA formulations over-prescription is a
    classic diversion fingerprint.
  * ``antipsychotic_ge65_clm_share`` — antipsychotic overuse in the 65+
    population (nursing-home / dementia-chemical-restraint cases).
  * ``antibiotic_clm_share`` — unnecessary-antibiotic schemes, relevant
    to urgent-care / telemedicine fraud.
  * ``brand_clm_share`` — brand-over-generic steering (kickback-to-
    manufacturer schemes).
  * ``cost_per_clm`` and ``cost_per_bene`` — headline intensity metrics
    layered on top of the shares.
  * ``clms_per_bene`` — high claims-per-beneficiary is a direct phantom-
    script or doctor-shopping signal.
  * ``ge65_bene_share`` — concentration in the 65+ sub-population.
  * ``risk_score`` — CMS-provided average beneficiary risk score, used
    to contextualize the other shares (a high-opioid share on a high-
    risk-score panel is less alarming than the same share on a
    low-risk-score panel).
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd


PROC_DIR = Path("data/panels")


def _ratio(num: pd.Series, den: pd.Series) -> pd.Series:
    """Null-safe ratio: 0 where denominator is missing or non-positive."""
    out = num / den.where(den > 0)
    return out.fillna(0.0)


def _safe(series: pd.Series | None) -> pd.Series:
    """Coerce a possibly-missing column to a float64 zero-filled Series."""
    if series is None:
        return pd.Series(0.0, dtype=float)
    return series.fillna(0.0).astype(float)


def compute_partd_features(year: int) -> pd.DataFrame:
    """Per-NPI Part D-derived features for ``year``.

    Returns a DataFrame indexed on ``Rndrng_NPI`` (renamed from
    ``PRSCRBR_NPI`` for join-compatibility with the Part B panel) with
    columns documented at the module top.
    """
    path = PROC_DIR / f"cms_partd_{year}.parquet"
    if not path.exists():
        raise FileNotFoundError(
            f"{path} — Part D {year} parquet not present. "
            f"Run `make fetch-data YEAR={year}` or import "
            f"pipeline.scrapers.download.fetch_cms_partd({year}) directly."
        )

    wanted = [
        "PRSCRBR_NPI",
        "Tot_Clms", "Tot_Drug_Cst", "Tot_Benes", "Tot_Day_Suply",
        "Brnd_Tot_Clms", "Gnrc_Tot_Clms",
        "Opioid_Tot_Clms", "Opioid_LA_Tot_Clms",
        "Antbtc_Tot_Clms",
        "Antpsyct_GE65_Tot_Clms",
        "GE65_Tot_Benes",
        "Bene_Avg_Risk_Scre",
    ]
    # Column-name casing drifted across release years (2019 uses title case,
    # 2023 upper-cases the NPI field). Resolve case-insensitively.
    import pyarrow.parquet as pq
    schema_names = pq.read_schema(path).names
    lookup = {n.lower(): n for n in schema_names}
    resolved = {w: lookup.get(w.lower()) for w in wanted}
    to_read = [v for v in resolved.values() if v is not None]
    df = pd.read_parquet(path, columns=to_read)
    # Normalize back to the canonical names used below.
    rename = {v: k for k, v in resolved.items() if v is not None and v != k}
    if rename:
        df = df.rename(columns=rename)

    feats = pd.DataFrame(index=df["PRSCRBR_NPI"])
    feats.index.name = "Rndrng_NPI"

    tot_clms = _safe(df["Tot_Clms"]).values
    tot_cost = _safe(df["Tot_Drug_Cst"]).values
    tot_benes = _safe(df["Tot_Benes"]).values
    tot_days = _safe(df["Tot_Day_Suply"]).values

    brnd_clms = _safe(df.get("Brnd_Tot_Clms")).values
    opioid_clms = _safe(df.get("Opioid_Tot_Clms")).values
    opioid_la_clms = _safe(df.get("Opioid_LA_Tot_Clms")).values
    antbtc_clms = _safe(df.get("Antbtc_Tot_Clms")).values
    antipsy_ge65 = _safe(df.get("Antpsyct_GE65_Tot_Clms")).values
    ge65_benes = _safe(df.get("GE65_Tot_Benes")).values

    # Shares (of total claims)
    feats["opioid_clm_share"] = _ratio(pd.Series(opioid_clms), pd.Series(tot_clms)).values
    feats["opioid_la_clm_share"] = _ratio(
        pd.Series(opioid_la_clms), pd.Series(tot_clms)
    ).values
    feats["antipsychotic_ge65_clm_share"] = _ratio(
        pd.Series(antipsy_ge65), pd.Series(tot_clms)
    ).values
    feats["antibiotic_clm_share"] = _ratio(
        pd.Series(antbtc_clms), pd.Series(tot_clms)
    ).values
    feats["brand_clm_share"] = _ratio(pd.Series(brnd_clms), pd.Series(tot_clms)).values

    # Intensity
    feats["cost_per_clm"] = _ratio(pd.Series(tot_cost), pd.Series(tot_clms)).values
    feats["cost_per_bene"] = _ratio(pd.Series(tot_cost), pd.Series(tot_benes)).values
    feats["clms_per_bene"] = _ratio(pd.Series(tot_clms), pd.Series(tot_benes)).values
    feats["days_per_clm"] = _ratio(pd.Series(tot_days), pd.Series(tot_clms)).values

    feats["ge65_bene_share"] = _ratio(
        pd.Series(ge65_benes), pd.Series(tot_benes)
    ).values

    feats["tot_clms"] = tot_clms
    feats["tot_drug_cst"] = tot_cost
    feats["risk_score"] = _safe(df.get("Bene_Avg_Risk_Scre")).values

    feats = feats[~feats.index.duplicated(keep="first")]
    return feats


def write_year(year: int) -> Path:
    """Compute and persist per-NPI Part D features for ``year``."""
    feats = compute_partd_features(year)
    out = PROC_DIR / f"cms_partd_features_{year}.parquet"
    feats.to_parquet(out)
    return out
