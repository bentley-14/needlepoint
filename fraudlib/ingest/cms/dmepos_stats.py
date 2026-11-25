"""DMEPOS-level feature engineering — referring-provider DME/POS/Drug ratios.

Reads ``data/panels/cms_dmepos_<year>.parquet`` (Medicare DMEPOS by
Referring Provider PUF) and returns a per-NPI feature frame indexed by
``Rfrg_NPI``. Engineered ratios capture brace-mill, DME-fraud, and
supplier-concentration patterns the existing partb/partd panels can't see.

Available years: 2014–2023 (CMS publishes the Referring Provider PUF
from 2014 onward).

Feature families
----------------
* **Supplier concentration** — ``avg_clms_per_supplier``,
  ``benes_per_clm``, ``services_per_clm``. High clms-per-supplier with
  few suppliers is the classic brace-mill referral pattern.
* **Submitted-to-allowed ratio** — by category (DME / POS / Drug) and
  overall. Aggressive DME billing.
* **Category mix** — share of total spend in DME, POS, Drug. A
  primary-care doctor with 95% of DMEPOS volume in DME is suspicious.
* **Suppression flags** — CMS publishes ``DME_Sprsn_Ind``,
  ``POS_Sprsn_Ind``, ``Drug_Sprsn_Ind`` (suppressed if too few cells);
  presence of suppression itself is a feature (CMS withheld for
  privacy = unusually low n).
* **Volume aggregates** — total suppliers, total HCPCS codes,
  beneficiaries, claims, services, $ amounts. Kept raw; the peer-z
  layer in the ranker handles scale normalisation.

Note: this module follows the pattern of ``partd_stats.py`` —
``compute_dmepos_features(year)`` returns the engineered frame indexed
by NPI.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd


PANELS_DIR = Path("data/panels")


# Columns we surface as features (subset of the 97 in the raw panel —
# others are name/address/identifier and not predictive).
_BASE_NUMERIC = [
    "Tot_Suplrs", "Tot_Suplr_HCPCS_Cds", "Tot_Suplr_Benes",
    "Tot_Suplr_Clms", "Tot_Suplr_Srvcs",
    "Suplr_Sbmtd_Chrgs", "Suplr_Mdcr_Alowd_Amt",
    "Suplr_Mdcr_Pymt_Amt", "Suplr_Mdcr_Stdzd_Pymt_Amt",
    # Per-category $: DME / POS (prosthetics & orthotics) / Drug
    "DME_Suplr_Mdcr_Pymt_Amt", "POS_Suplr_Mdcr_Pymt_Amt",
    "Drug_Suplr_Mdcr_Pymt_Amt",
    "DME_Suplr_Mdcr_Alowd_Amt", "POS_Suplr_Mdcr_Alowd_Amt",
    "Drug_Suplr_Mdcr_Alowd_Amt",
    "DME_Suplr_Sbmtd_Chrgs", "POS_Suplr_Sbmtd_Chrgs",
    "Drug_Suplr_Sbmtd_Chrgs",
    "DME_Tot_Suplrs", "POS_Tot_Suplrs", "Drug_Tot_Suplrs",
    # Beneficiary mix
    "Bene_Avg_Age", "Bene_Avg_Risk_Scre",
    "Bene_Dual_Cnt", "Bene_Ndual_Cnt",
]

# Categorical / string columns we keep on the frame for peer grouping +
# downstream feature engineering. ``dmepos_specialty`` is distinct from
# the NPPES taxonomy bucket — it's the specialty CMS observed on this
# provider's DMEPOS-claim volume, which can differ from their NPPES self-
# declared specialty.
_KEEP_STRING = [
    "Rfrg_Prvdr_Spclty_Desc",
    "Rfrg_Prvdr_State_Abrvtn",
    "Rfrg_Prvdr_RUCA",
]


def compute_dmepos_features(year: int) -> pd.DataFrame:
    """Per-NPI engineered DMEPOS feature frame.

    Parameters
    ----------
    year
        Year of the DMEPOS PUF to load (2014-2023).

    Returns
    -------
    pd.DataFrame
        One row per ``Rfrg_NPI``. Columns include the raw aggregates
        listed above plus engineered ratios. NPI returned as string for
        join compatibility with the rest of the ranker pipeline.
    """
    path = PANELS_DIR / f"cms_dmepos_{year}.parquet"
    if not path.exists():
        raise FileNotFoundError(f"DMEPOS PUF not found: {path}")

    df = pd.read_parquet(path)

    # ── Build the output frame ─────────────────────────────────────────
    out = pd.DataFrame(index=df.index)
    out["npi"] = df["Rfrg_NPI"].astype(str)

    # Numeric base columns (copy through; coerce non-numeric to NaN)
    for col in _BASE_NUMERIC:
        if col in df.columns:
            out[col.lower()] = pd.to_numeric(df[col], errors="coerce")

    # Suppression flags as binary features (Y/N → 1/0; NaN → 0)
    for col in ("DME_Sprsn_Ind", "POS_Sprsn_Ind", "Drug_Sprsn_Ind"):
        if col in df.columns:
            out[col.lower()] = (df[col].astype(str).str.upper() == "Y").astype(int)

    # ── Engineered ratios ──────────────────────────────────────────────
    # Avoid divide-by-zero; pandas handles NaN propagation for us.
    eps = 1e-9
    if {"tot_suplr_clms", "tot_suplrs"}.issubset(out.columns):
        out["clms_per_supplier"] = out["tot_suplr_clms"] / (out["tot_suplrs"].replace(0, np.nan))
    if {"tot_suplr_benes", "tot_suplr_clms"}.issubset(out.columns):
        out["benes_per_clm"] = out["tot_suplr_benes"] / (out["tot_suplr_clms"].replace(0, np.nan))
    if {"tot_suplr_srvcs", "tot_suplr_clms"}.issubset(out.columns):
        out["srvcs_per_clm"] = out["tot_suplr_srvcs"] / (out["tot_suplr_clms"].replace(0, np.nan))
    if {"suplr_sbmtd_chrgs", "suplr_mdcr_alowd_amt"}.issubset(out.columns):
        out["sbmtd_to_alowd_ratio"] = out["suplr_sbmtd_chrgs"] / (
            out["suplr_mdcr_alowd_amt"].replace(0, np.nan)
        )
    # Category share of payment (DME vs POS vs Drug)
    pay_cols = ("dme_suplr_mdcr_pymt_amt", "pos_suplr_mdcr_pymt_amt", "drug_suplr_mdcr_pymt_amt")
    if all(c in out.columns for c in pay_cols):
        denom = out[list(pay_cols)].sum(axis=1).replace(0, np.nan)
        out["dme_pay_share"] = out["dme_suplr_mdcr_pymt_amt"] / denom
        out["pos_pay_share"] = out["pos_suplr_mdcr_pymt_amt"] / denom
        out["drug_pay_share"] = out["drug_suplr_mdcr_pymt_amt"] / denom

    # ── String columns (for peer grouping / context) ───────────────────
    rename_str = {
        "Rfrg_Prvdr_Spclty_Desc": "specialty",
        "Rfrg_Prvdr_State_Abrvtn": "state",
        "Rfrg_Prvdr_RUCA": "ruca",
    }
    for src, dst in rename_str.items():
        if src in df.columns:
            out[dst] = df[src]

    # Inf → NaN, then NaN → 0 for the numeric block (matches features.py
    # convention so concat_multisource doesn't choke).
    num_block = out.select_dtypes(include=[np.number]).columns
    out[num_block] = out[num_block].replace([np.inf, -np.inf], np.nan)
    return out
