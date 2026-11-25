"""HCPCS-level aggregation helper.

Reads the year's Prov_Svc parquet and returns per-NPI ratios that capture
code-mix signatures the aggregated Part B panel cannot express. Intended
to be joined onto the existing `cms_partb_<year>.parquet` panel on
`Rndrng_NPI`.

Only available for 2019-2023; CMS does not publish Prov_Svc for earlier
years.

The features below group into four families:

  * **E&M / office-visit upcoding** — ``em_high_low_ratio``,
    ``em_new_vs_estab_ratio``, ``em_high_share`` (of all E&M),
    ``em_distribution_entropy`` (distribution across 99211-99215).
  * **Procedure / bundling arbitrage** — ``same_day_procedure_stacking``.
    Modifier-level features (mod-25, mod-59, mod-50) are not available
    in the Prov_Svc PUF; they would require the detailed research-file.
  * **Specialty-specific anchors** — ``podiatry_debride_routine_ratio``,
    ``cataract_complex_ratio``, ``intravitreal_injection_intensity``
    (existing); ``jcode_biologic_share``, ``jcode_chemo_share``,
    ``jcode_eye_injection_share`` (J-code class groupings).
  * **Code-mix shape** — ``n_hcpcs_distinct``, ``top3_code_concentration``
    (revenue share of top-3 codes), ``drug_hcpcs_share``,
    ``hcpcs_paid_to_allowed``.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd


PROC_DIR = Path("data/panels")


# ---------------------------------------------------------------------------
# Code-set constants
# ---------------------------------------------------------------------------

# E&M
_EM_ALL = {"99211", "99212", "99213", "99214", "99215"}
_EM_HIGH = {"99214", "99215"}
_EM_LOW = {"99212", "99213"}
# New-vs-established office visits (upcoding lens): 99201-99205 new,
# 99211-99215 established. 99201 was deleted in 2021 by CMS so we tolerate
# its absence.
_EM_NEW = {"99202", "99203", "99204", "99205"}
_EM_ESTAB = {"99211", "99212", "99213", "99214", "99215"}

# Podiatry
_POD_DEBRIDE_HIGH = {"11720", "11721"}
_POD_DEBRIDE_LOW = {"11719", "G0127"}

# Cataract (ophth)
_CAT_COMPLEX = {"66982"}
_CAT_SIMPLE = {"66984"}

# Retinal injectable J-codes (Eylea, Lucentis, Avastin)
_INTRAVITREAL_JCODES = {"J9145", "J2778", "J2503"}

# J-code class groupings for broad drug-mix analysis. These are small
# curated sets — not exhaustive — chosen to cover the drug-fraud
# mechanisms that show up most often in the case library.
#
# Biologics / infusions (rheumatology, GI, oncology): Remicade, Rituxan,
# Herceptin, Neulasta, and similar high-cost biologics.
_JCODE_BIOLOGICS = {
    "J1745", "J9312", "J9355", "J2505", "J1566", "J1569", "J2357",
    "J9035", "J9041", "J9171", "J9310", "J9312",
}
# Chemotherapy anchors (non-biologic chemo): docetaxel, carboplatin,
# paclitaxel, fluorouracil, leucovorin, gemcitabine, irinotecan,
# bortezomib, pemetrexed.
_JCODE_CHEMO = {
    "J9171", "J9045", "J9267", "J9190", "J0640", "J9201", "J9206",
    "J9041", "J9305",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ratio(num: pd.Series, den: pd.Series) -> pd.Series:
    """Null-safe ratio: 0 where denominator is missing or non-positive."""
    out = num / den.where(den > 0)
    return out.fillna(0.0)


def _svc_sum_by_code(
    df: pd.DataFrame, codes: set[str], value: str = "Tot_Srvcs"
) -> pd.Series:
    return (
        df.loc[df["HCPCS_Cd"].isin(codes), ["Rndrng_NPI", value]]
        .groupby("Rndrng_NPI")[value]
        .sum()
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def compute_hcpcs_features(year: int) -> pd.DataFrame:
    """Per-NPI HCPCS-derived features for ``year``.

    Returns a DataFrame indexed on ``Rndrng_NPI``. See module docstring
    for the column groups.
    """
    path = PROC_DIR / f"cms_partb_svc_{year}.parquet"
    if not path.exists():
        raise FileNotFoundError(
            f"{path} — Prov_Svc not available for {year} "
            "(CMS publishes 2019-2023 only)"
        )

    # Read more columns than the v0 — we need modifier / allowed / mod
    # columns for the new features. We still project early to keep memory
    # bounded; ~60M rows * ~8 cols fits in ~4 GB.
    import pyarrow.parquet as pq
    schema = pq.read_schema(path).names
    want = [
        "Rndrng_NPI", "HCPCS_Cd", "HCPCS_Drug_Ind",
        "Tot_Srvcs", "Tot_Benes", "Tot_Bene_Day_Srvcs",
        "Avg_Mdcr_Pymt_Amt", "Avg_Mdcr_Alowd_Amt",
        "Place_Of_Srvc",
    ]
    # Some PUF vintages don't carry every column. Keep only those present.
    cols = [c for c in want if c in schema]
    df = pd.read_parquet(path, columns=cols)

    # Derive totals from averages — the Prov_Svc PUF ships averages per
    # (provider, HCPCS) row, so we multiply by row-level Tot_Srvcs to get
    # the dollar total before aggregating. pandas keeps this vectorized.
    if "Avg_Mdcr_Pymt_Amt" in df.columns:
        df["_tot_paid"] = df["Avg_Mdcr_Pymt_Amt"] * df["Tot_Srvcs"]
    if "Avg_Mdcr_Alowd_Amt" in df.columns:
        df["_tot_alw"] = df["Avg_Mdcr_Alowd_Amt"] * df["Tot_Srvcs"]

    # Fast per-NPI code-set sums.
    em_hi = _svc_sum_by_code(df, _EM_HIGH)
    em_lo = _svc_sum_by_code(df, _EM_LOW)
    em_all = _svc_sum_by_code(df, _EM_ALL)
    em_new = _svc_sum_by_code(df, _EM_NEW)
    em_estab = _svc_sum_by_code(df, _EM_ESTAB)
    pod_hi = _svc_sum_by_code(df, _POD_DEBRIDE_HIGH)
    pod_lo = _svc_sum_by_code(df, _POD_DEBRIDE_LOW)
    cat_cx = _svc_sum_by_code(df, _CAT_COMPLEX)
    cat_sp = _svc_sum_by_code(df, _CAT_SIMPLE)
    ivt_svcs = _svc_sum_by_code(df, _INTRAVITREAL_JCODES)
    ivt_benes = _svc_sum_by_code(df, _INTRAVITREAL_JCODES, "Tot_Benes")
    biol_svcs = _svc_sum_by_code(df, _JCODE_BIOLOGICS)
    chemo_svcs = _svc_sum_by_code(df, _JCODE_CHEMO)

    # NPI totals
    n_codes = df.groupby("Rndrng_NPI")["HCPCS_Cd"].nunique()
    tot_srvcs = df.groupby("Rndrng_NPI")["Tot_Srvcs"].sum()
    tot_paid = df.groupby("Rndrng_NPI")["_tot_paid"].sum() \
        if "_tot_paid" in df.columns else pd.Series(dtype=float)

    idx = n_codes.index
    feats = pd.DataFrame(index=idx)
    feats.index.name = "Rndrng_NPI"

    # --- E&M family ---
    feats["em_high_low_ratio"] = _ratio(
        em_hi.reindex(idx, fill_value=0.0),
        em_lo.reindex(idx, fill_value=0.0),
    )
    feats["em_high_share"] = _ratio(
        em_hi.reindex(idx, fill_value=0.0),
        em_all.reindex(idx, fill_value=0.0),
    )
    feats["em_new_vs_estab_ratio"] = _ratio(
        em_new.reindex(idx, fill_value=0.0),
        em_estab.reindex(idx, fill_value=0.0),
    )

    # E&M distribution entropy across 99211-99215 — upcoding signal
    # (a provider who only bills 99214/99215 has low entropy; a
    # legitimate-mix practice spans 99212-99215).
    em_rows = df.loc[df["HCPCS_Cd"].isin(_EM_ALL), ["Rndrng_NPI", "HCPCS_Cd", "Tot_Srvcs"]]
    if len(em_rows):
        pivot = (
            em_rows.groupby(["Rndrng_NPI", "HCPCS_Cd"])["Tot_Srvcs"].sum()
            .unstack(fill_value=0.0)
        )
        p = pivot.div(pivot.sum(axis=1).replace(0, np.nan), axis=0).fillna(0.0)
        # entropy with log base e
        with np.errstate(divide="ignore", invalid="ignore"):
            logp = np.where(p > 0, np.log(p), 0.0)
            entropy = -(p.values * logp).sum(axis=1)
        feats["em_distribution_entropy"] = pd.Series(entropy, index=pivot.index).reindex(
            idx, fill_value=0.0
        )
    else:
        feats["em_distribution_entropy"] = 0.0

    # --- Specialty anchors (existing) ---
    feats["podiatry_debride_routine_ratio"] = _ratio(
        pod_hi.reindex(idx, fill_value=0.0),
        pod_lo.reindex(idx, fill_value=0.0),
    )
    feats["cataract_complex_ratio"] = _ratio(
        cat_cx.reindex(idx, fill_value=0.0),
        (cat_cx + cat_sp).reindex(idx, fill_value=0.0),
    )
    feats["intravitreal_injection_intensity"] = _ratio(
        ivt_svcs.reindex(idx, fill_value=0.0),
        ivt_benes.reindex(idx, fill_value=0.0),
    )

    # --- J-code class shares ---
    feats["jcode_biologic_share"] = _ratio(
        biol_svcs.reindex(idx, fill_value=0.0),
        tot_srvcs.reindex(idx, fill_value=0.0),
    )
    feats["jcode_chemo_share"] = _ratio(
        chemo_svcs.reindex(idx, fill_value=0.0),
        tot_srvcs.reindex(idx, fill_value=0.0),
    )
    feats["jcode_eye_injection_share"] = _ratio(
        ivt_svcs.reindex(idx, fill_value=0.0),
        tot_srvcs.reindex(idx, fill_value=0.0),
    )

    # --- Same-day procedure stacking ---
    # CMS publishes Tot_Bene_Day_Srvcs = beneficiary-day-service count.
    # Services-per-bene-day > 1 signals stacking (multiple procedures on
    # the same bene on the same day — unbundling footprint).
    if "Tot_Bene_Day_Srvcs" in df.columns:
        tot_bdays = df.groupby("Rndrng_NPI")["Tot_Bene_Day_Srvcs"].sum()
        feats["same_day_procedure_stacking"] = _ratio(
            tot_srvcs.reindex(idx, fill_value=0.0),
            tot_bdays.reindex(idx, fill_value=0.0),
        )
    else:
        feats["same_day_procedure_stacking"] = 0.0

    # --- Shape / concentration ---
    feats["n_hcpcs_distinct"] = n_codes

    # Top-3 code share of total Medicare paid per NPI (narrow-code
    # concentration is a scheme fingerprint). Vectorized: rank within
    # NPI by paid $ descending, keep ranks <= 3, sum. Avoids the slow
    # groupby-apply(nlargest) path that was >5 min on 10M rows.
    if "_tot_paid" in df.columns:
        per_code = (
            df.groupby(["Rndrng_NPI", "HCPCS_Cd"], sort=False)["_tot_paid"]
            .sum()
            .reset_index()
        )
        per_code["_rnk"] = per_code.groupby("Rndrng_NPI")["_tot_paid"].rank(
            method="first", ascending=False
        )
        top3 = (
            per_code.loc[per_code["_rnk"] <= 3]
            .groupby("Rndrng_NPI")["_tot_paid"].sum()
        )
        feats["top3_code_concentration"] = _ratio(
            top3.reindex(idx, fill_value=0.0),
            tot_paid.reindex(idx, fill_value=0.0),
        )
    else:
        feats["top3_code_concentration"] = 0.0

    # --- Drug vs non-drug share ---
    # The HCPCS_Drug_Ind flag is "Y" for drug J/Q-codes, blank/NaN else.
    if "HCPCS_Drug_Ind" in df.columns:
        drug_svcs = (
            df.loc[df["HCPCS_Drug_Ind"] == "Y"]
            .groupby("Rndrng_NPI")["Tot_Srvcs"].sum()
        )
        feats["drug_hcpcs_share"] = _ratio(
            drug_svcs.reindex(idx, fill_value=0.0),
            tot_srvcs.reindex(idx, fill_value=0.0),
        )
    else:
        feats["drug_hcpcs_share"] = 0.0

    # --- Charge-to-allowed ratio at the HCPCS-weighted level ---
    # Rolls the aggregate-frame's charge_to_allowed into a code-weighted
    # version so narrow-procedure outliers aren't masked by the rest of
    # the practice.
    if "_tot_alw" in df.columns:
        tot_alw = df.groupby("Rndrng_NPI")["_tot_alw"].sum()
        feats["hcpcs_paid_to_allowed"] = _ratio(
            tot_paid.reindex(idx, fill_value=0.0),
            tot_alw.reindex(idx, fill_value=0.0),
        )
    else:
        feats["hcpcs_paid_to_allowed"] = 0.0

    return feats


def write_year(year: int) -> Path:
    """Compute and write per-NPI HCPCS features for ``year``."""
    feats = compute_hcpcs_features(year)
    out = PROC_DIR / f"cms_hcpcs_features_{year}.parquet"
    feats.to_parquet(out)
    return out
