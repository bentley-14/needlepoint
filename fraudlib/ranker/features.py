"""Provider-aggregate feature engineering.

The CMS PUF "by Provider" flavor is already aggregated, so most work here
is schema normalization + selecting the informative columns. If a notebook
uses the "by Provider and Service" flavor (one row per HCPCS), call
:func:`aggregate_services_to_provider` first.

The resulting feature matrix is used by the supervised (papers 03, 08, 10,
14, 15) and unsupervised (paper 06) notebooks alike.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

import numpy as np
import pandas as pd

# CMS renamed columns in the 2020 release ("Rndrng_..." prefix). We
# normalize both schemas to the older-style snake_case used in most
# published papers to keep the rest of the code stable.
_RENAME_MAP = {
    r"^Rndrng_NPI$": "npi",
    r"^PRSCRBR_NPI$": "npi",
    r"^Rfrg_NPI$": "npi",
    r"^Rndrng_Prvdr_Type$": "provider_type",
    r"^Prscrbr_Type$": "provider_type",
    r"^Rfrg_Prvdr_Type$": "provider_type",
    r"^Prscrbr_State_Abrvtn$": "state",
    r"^Rfrg_Prvdr_State_Abrvtn$": "state",
    r"^Rndrng_Prvdr_Gndr$": "provider_gender",
    r"^Rndrng_Prvdr_Crdntls$": "credentials",
    r"^Rndrng_Prvdr_Ent_Cd$": "entity_code",
    r"^Rndrng_Prvdr_State_Abrvtn$": "state",
    r"^Rndrng_Prvdr_Cntry$": "country",
    r"^Rndrng_Prvdr_MdcrPrtcptg_Ind$": "medicare_participating",
    r"^Tot_HCPCS_Cds$": "n_hcpcs_codes",
    r"^Tot_Benes$": "n_beneficiaries",
    r"^Tot_Srvcs$": "n_services",
    r"^Tot_Sbmtd_Chrg_Amt$": "total_submitted_charge",
    r"^Tot_Mdcr_Alowd_Amt$": "total_medicare_allowed",
    r"^Tot_Mdcr_Pymt_Amt$": "total_medicare_payment",
    r"^Tot_Mdcr_Stdzd_Amt$": "total_medicare_standardized",
    r"^Bene_Avg_Age$": "bene_avg_age",
    r"^Bene_Avg_Risk_Scre$": "bene_avg_risk_score",
    r"^NPI$": "npi",
    r"^Provider Type$": "provider_type",
    r"^Number of HCPCS$": "n_hcpcs_codes",
    r"^Number of Services$": "n_services",
    r"^Number of Medicare Beneficiaries$": "n_beneficiaries",
    r"^Total Submitted Charge Amount$": "total_submitted_charge",
    r"^Total Medicare Allowed Amount$": "total_medicare_allowed",
    r"^Total Medicare Payment Amount$": "total_medicare_payment",
}


def normalize_schema(df: pd.DataFrame) -> pd.DataFrame:
    """Rename CMS columns to a stable snake_case schema. Non-matching
    columns are kept as-is (lower-cased)."""
    renames: dict[str, str] = {}
    for col in df.columns:
        for pat, new in _RENAME_MAP.items():
            if re.match(pat, col):
                renames[col] = new
                break
    out = df.rename(columns=renames)
    out.columns = [c if c in _RENAME_MAP.values() else c.lower() for c in out.columns]
    return out


# Base numeric features shared across papers. Derived columns are appended
# by :func:`engineer`.
BASE_NUMERIC = [
    "n_hcpcs_codes",
    "n_beneficiaries",
    "n_services",
    "total_submitted_charge",
    "total_medicare_allowed",
    "total_medicare_payment",
    "total_medicare_standardized",
    "bene_avg_age",
    "bene_avg_risk_score",
]


def engineer(df: pd.DataFrame) -> pd.DataFrame:
    """Add derived provider-level features on top of the normalized schema."""
    df = normalize_schema(df).copy()
    for col in BASE_NUMERIC:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # Ratios that papers repeatedly highlight as informative.
    if {"total_submitted_charge", "total_medicare_allowed"}.issubset(df.columns):
        df["submitted_to_allowed_ratio"] = (
            df["total_submitted_charge"] / df["total_medicare_allowed"].replace(0, np.nan)
        )
    if {"total_medicare_payment", "n_services"}.issubset(df.columns):
        df["payment_per_service"] = (
            df["total_medicare_payment"] / df["n_services"].replace(0, np.nan)
        )
    if {"n_services", "n_beneficiaries"}.issubset(df.columns):
        df["services_per_beneficiary"] = (
            df["n_services"] / df["n_beneficiaries"].replace(0, np.nan)
        )
    if {"n_hcpcs_codes", "n_services"}.issubset(df.columns):
        df["hcpcs_diversity"] = (
            df["n_hcpcs_codes"] / df["n_services"].replace(0, np.nan)
        )
    return df


def aggregate_service_level(
    df_service: pd.DataFrame,
    group_col: str = "Rndrng_NPI",
    amount_col_candidates: tuple[str, ...] = (
        "Tot_Mdcr_Pymt_Amt",
        "Avg_Mdcr_Pymt_Amt",
        "Tot_Mdcr_Alowd_Amt",
    ),
    service_code_candidates: tuple[str, ...] = ("HCPCS_Cd", "Brnd_Name", "BETOS_Lvl2_Desc"),
) -> pd.DataFrame:
    """Collapse a service-level frame (by-service, by-drug) to provider aggregates.

    Works for Part B "by Provider & Service", Part D "by Provider & Drug",
    and DMEPOS "by Referring Provider & Service". Returns one row per NPI
    with columns: ``npi``, ``n_unique_codes``, ``total_payment``,
    ``mean_payment_per_code``, ``max_payment``, ``n_service_rows``.
    """
    amount_col = next((c for c in amount_col_candidates if c in df_service.columns), None)
    code_col = next((c for c in service_code_candidates if c in df_service.columns), None)
    if group_col not in df_service.columns:
        raise KeyError(f"{group_col} not in columns: {list(df_service.columns)[:20]}")
    if amount_col is None:
        raise KeyError(
            f"No payment amount column found. Tried {amount_col_candidates}."
        )

    df = df_service.copy()
    df[amount_col] = pd.to_numeric(df[amount_col], errors="coerce").fillna(0.0)
    grp = df.groupby(group_col, as_index=False)

    agg = grp.agg(
        total_payment=(amount_col, "sum"),
        mean_payment_per_code=(amount_col, "mean"),
        max_payment=(amount_col, "max"),
        n_service_rows=(amount_col, "size"),
    )
    if code_col:
        codes = grp[code_col].nunique().rename(columns={code_col: "n_unique_codes"})
        agg = agg.merge(codes, on=group_col, how="left")
    agg = agg.rename(columns={group_col: "npi"})
    agg["npi"] = agg["npi"].astype(str)
    return agg


def concat_multisource(
    partb: pd.DataFrame,
    partd: Optional[pd.DataFrame] = None,
    dmepos: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    """Outer-join Part B / D / DMEPOS provider frames on NPI with source prefixes.

    Expects each frame to be already normalized and include a ``npi``
    column. Source columns are prefixed ``partb_``, ``partd_``,
    ``dmepos_``. Missing-source providers get zeros + a presence indicator
    column (``has_partd``, ``has_dmepos``).
    """
    def _prep(frame: pd.DataFrame, prefix: str) -> pd.DataFrame:
        f = frame.copy()
        if "npi" not in f.columns:
            raise KeyError(f"{prefix} frame has no 'npi' column; call engineer() first.")
        f["npi"] = f["npi"].astype(str)
        f = f.drop_duplicates("npi")
        rename = {c: f"{prefix}_{c}" for c in f.columns if c != "npi"}
        return f.rename(columns=rename)

    out = _prep(partb, "partb")
    if partd is not None:
        pd_ = _prep(partd, "partd")
        out = out.merge(pd_, on="npi", how="outer")
        out["has_partd"] = out[[c for c in out.columns if c.startswith("partd_")]].notna().any(axis=1).astype(int)
    if dmepos is not None:
        dm = _prep(dmepos, "dmepos")
        out = out.merge(dm, on="npi", how="outer")
        out["has_dmepos"] = out[[c for c in out.columns if c.startswith("dmepos_")]].notna().any(axis=1).astype(int)
    out = out.fillna(0.0)
    return out


def multi_year_provider_panel(
    fetch_fn,
    years: Iterable[int],
    engineer_fn=None,
) -> pd.DataFrame:
    """Concatenate yearly PUFs into one long frame, one row per (NPI, year).

    Parameters
    ----------
    fetch_fn
        Callable taking ``year`` kwarg, returning a provider-level DataFrame
        (e.g. :func:`pipeline.scrapers.download.fetch_cms_partb`).
    years
        Iterable of calendar years to concatenate.
    engineer_fn
        Optional per-year transformation applied before concat
        (defaults to :func:`engineer`).

    Returns
    -------
    Long-format DataFrame with a ``puf_year`` column identifying source
    year. Useful for paper 10's real multi-row-per-NPI structure.
    """
    if engineer_fn is None:
        engineer_fn = engineer
    parts = []
    for y in years:
        frame = fetch_fn(year=y)
        frame = engineer_fn(frame)
        frame = frame.copy()
        frame["puf_year"] = y
        parts.append(frame)
    combined = pd.concat(parts, ignore_index=True)
    return combined


def provider_matrix(
    df: pd.DataFrame,
    *,
    include_categorical: bool = True,
    fillna: float = 0.0,
) -> tuple[pd.DataFrame, pd.Series]:
    """Return ``(X, y)`` where ``y`` is the ``fraud`` column and ``X`` is
    numeric + optionally one-hot-encoded categorical features. Infinities
    and NaNs in ``X`` are replaced with ``fillna``.
    """
    df = engineer(df)
    if "fraud" not in df.columns:
        raise KeyError(
            "DataFrame has no 'fraud' column. This matrix builder is for the "
            "legacy LEIE-labeled paper-reproduction path. For active ranker "
            "training use load_training_frame() + _feature_matrix() from "
            "fraudlib.ranker.pipeline (target column is 'y' from fraud_case)."
        )

    y = df["fraud"].astype(int)
    numeric_cols = [c for c in BASE_NUMERIC if c in df.columns]
    derived = [
        c
        for c in [
            "submitted_to_allowed_ratio",
            "payment_per_service",
            "services_per_beneficiary",
            "hcpcs_diversity",
        ]
        if c in df.columns
    ]
    x = df[numeric_cols + derived].copy()

    if include_categorical:
        cat_cols = [c for c in ["provider_type", "state", "provider_gender", "entity_code"] if c in df.columns]
        if cat_cols:
            # Limit cardinality: keep top-50 per column; rest → "OTHER".
            for col in cat_cols:
                top = df[col].astype(str).value_counts().nlargest(50).index
                df[col] = df[col].astype(str).where(df[col].astype(str).isin(top), other="OTHER")
            x = pd.concat(
                [x, pd.get_dummies(df[cat_cols], prefix=cat_cols, dummy_na=False)],
                axis=1,
            )

    x = x.replace([np.inf, -np.inf], np.nan).fillna(fillna)
    return x, y


def multisource_matrix(
    df: pd.DataFrame,
    *,
    fillna: float = 0.0,
) -> tuple[pd.DataFrame, pd.Series]:
    """Matrix extractor for the prefixed output of :func:`concat_multisource`.

    Selects every numeric column except ``fraud``/``npi``/bookkeeping, so
    ``partb_n_services``, ``partd_total_payment``, ``has_partd``, etc., are
    all included. Use this for papers that train on the concatenated B+D
    (+DMEPOS) panel where :func:`provider_matrix` would return 0 columns.
    """
    if "fraud" not in df.columns:
        raise KeyError(
            "DataFrame has no 'fraud' column. This matrix builder is for the "
            "legacy LEIE-labeled paper-reproduction path. For active ranker "
            "training use load_training_frame() + _feature_matrix() from "
            "fraudlib.ranker.pipeline (target column is 'y' from fraud_case)."
        )
    drop = {"fraud", "npi", "excldate", "_npi"}
    num_cols = [c for c in df.select_dtypes(include=[np.number]).columns if c not in drop]
    x = df[num_cols].replace([np.inf, -np.inf], np.nan).fillna(fillna).astype(np.float32)
    y = df["fraud"].astype(int)
    return x, y
