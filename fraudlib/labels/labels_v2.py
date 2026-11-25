"""Case-based labels — the sole training-label source for the active ranker.

A provider is labeled ``fraud_case=1`` if their NPI appears as a
high/medium-confidence defendant in ``gold_set.jsonl`` with an
``active_years`` window that intersects the PUF year. ``label_bucket``
is binary {``case_only``, ``neither``} computed from ``fraud_case`` alone.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Literal, Union

import numpy as np
import pandas as pd

NPI_COLUMN_CANDIDATES = ("NPI", "Rndrng_NPI", "PRSCRBR_NPI", "REFERRING_NPI", "Rfrg_NPI")


def _find_npi_column(df: pd.DataFrame, candidates: tuple[str, ...]) -> str:
    upper = {c.upper(): c for c in df.columns}
    for cand in candidates:
        if cand.upper() in upper:
            return upper[cand.upper()]
    raise KeyError(
        f"No NPI column found in DataFrame. Tried {candidates}. "
        f"Present columns: {list(df.columns)[:20]}..."
    )


def _to_npi_series(s: pd.Series) -> pd.Series:
    """Coerce an NPI column to clean 10-digit strings. Missing → NaN."""
    out = s.astype(str).str.replace(r"\D", "", regex=True)
    out = out.where(out.str.len() == 10, other=np.nan)
    return out

# NOTE: Union[...] not `X | Y` — this is a runtime assignment (not a deferred
# annotation), so PEP-604 `|` would TypeError on Python 3.9. (fixed 2026-05-28)
CasesSource = Union[str, Path, Iterable[dict]]


def load_cases(src: CasesSource) -> pd.DataFrame:
    """Load gold_set.jsonl into a flat per-defendant frame.

    One row per (case, defendant) with resolved NPI. Rows lacking an NPI
    (corporate defendants, unresolved names) are kept but will have NaN
    in ``npi``.
    """
    if isinstance(src, (str, Path)):
        rows = [json.loads(line) for line in Path(src).read_text().splitlines() if line.strip()]
    else:
        rows = list(src)

    records = []
    for case in rows:
        for d in case.get("defendants") or [{}]:
            records.append({
                "case_id": case.get("case_id"),
                "case_name": case.get("case_name"),
                "fraud_types": case.get("fraud_types") or [],
                "primary_fraud_type": (case.get("fraud_types") or [None])[0],
                "active_years": case.get("active_years", []),
                "tier": case.get("tier"),
                "detectability": case.get("detectability"),
                "outcome_type": (case.get("outcome") or {}).get("type"),
                "outcome_date": (case.get("outcome") or {}).get("date"),
                "amount_usd": (case.get("outcome") or {}).get("amount_usd"),
                "defendant_name": d.get("name"),
                "defendant_role": d.get("role"),
                "npi": d.get("npi") or None,
                "npi_confidence": d.get("npi_confidence"),
            })
    df = pd.DataFrame(records)
    if not df.empty:
        df["npi"] = df["npi"].astype(str).where(df["npi"].notna() & (df["npi"] != ""), other=pd.NA)
    return df


def label_providers_with_cases(
    provider_df: pd.DataFrame,
    cases_df: pd.DataFrame,
    puf_year: int,
    min_confidence: Literal["high", "medium", "low"] = "high",
) -> pd.DataFrame:
    """Join provider panel against the case list; add case-based labels.

    Parameters
    ----------
    provider_df
        Provider-year panel (one row per NPI for ``puf_year``).
    cases_df
        Output of :func:`load_cases`.
    puf_year
        PUF calendar year. A case contributes only if ``puf_year in
        case.active_years``.
    min_confidence
        Minimum ``npi_confidence`` to accept. ``high`` is recommended
        for training labels; ``medium`` can be used for eval only.

    Returns
    -------
    ``provider_df`` with added columns:
      - ``fraud_case`` (int, 0/1) — DOJ-defendant match
      - ``case_ids`` (pipe-joined case_ids; empty if no match)
      - ``case_fraud_types`` (pipe-joined primary_fraud_type values)
      - ``case_tier`` (best tier A>B>C if multiple)
      - ``label_bucket`` (binary: ``"case_only"`` / ``"neither"``)
      - ``fraud_label`` (int, 0/1) — alias of ``fraud_case``
      - ``label_source`` (str) — ``"doj"`` or ``""``
      - ``label_confidence`` (str) — ``"high"`` or ``""``
    """
    rank = {"high": 0, "medium": 1, "low": 2}
    if cases_df.empty or "npi_confidence" not in cases_df.columns:
        eligible = pd.DataFrame({
            "case_id": [], "primary_fraud_type": [], "tier": [], "npi": [],
        })
    else:
        eligible = cases_df[
            cases_df["npi_confidence"].map(lambda c: rank.get(c, 99)) <= rank[min_confidence]
        ].copy()
        eligible = eligible[eligible["npi"].notna()]
        eligible = eligible[eligible["active_years"].apply(lambda yrs: puf_year in (yrs or []))]

    prov_col = _find_npi_column(provider_df, NPI_COLUMN_CANDIDATES)
    out = provider_df.copy()
    out["_npi"] = _to_npi_series(out[prov_col])

    if eligible.empty or "npi" not in eligible.columns:
        grouped = pd.DataFrame(columns=["_npi", "case_ids", "case_fraud_types", "case_tier"])
    else:
        grouped = (
            eligible.groupby("npi", dropna=True)
            .agg(
                case_ids=("case_id", lambda s: "|".join(sorted(set(s)))),
                case_fraud_types=("primary_fraud_type", lambda s: "|".join(sorted(set(s)))),
                case_tier=("tier", lambda s: sorted(set(s))[0]),
            )
            .reset_index()
            .rename(columns={"npi": "_npi"})
        )
    merged = out.merge(grouped, on="_npi", how="left")
    merged["fraud_case"] = merged["case_ids"].notna().astype(int)
    merged["case_ids"] = merged["case_ids"].fillna("")
    merged["case_fraud_types"] = merged["case_fraud_types"].fillna("")
    merged["case_tier"] = merged["case_tier"].fillna("")

    merged["fraud_label"] = merged["fraud_case"].copy()

    src = pd.Series("", index=merged.index, dtype=object)
    src.loc[merged["fraud_case"] == 1] = "doj"
    merged["label_source"] = src

    conf = pd.Series("", index=merged.index, dtype=object)
    conf.loc[merged["fraud_case"] == 1] = "high"
    merged["label_confidence"] = conf

    bucket = pd.Series("neither", index=merged.index)
    bucket.loc[merged["fraud_case"] == 1] = "case_only"
    merged["label_bucket"] = bucket

    return merged.drop(columns=["_npi"])


def summarize_buckets(df: pd.DataFrame) -> dict[str, int]:
    if "label_bucket" not in df.columns:
        raise ValueError("Run label_providers_with_cases first to produce label_bucket.")
    return df["label_bucket"].value_counts().to_dict()
