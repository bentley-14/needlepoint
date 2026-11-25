"""V3 data-driven rules — 59 set-cover rules used by the walk-forward pipeline.

Replaces the 14 hand-authored registry rules (now in stale/registry_rules/).
These rules were validated in the post-audit walk-forward (PRs #58–63,
mean R@10K = 12.5%).

For the agent layer, ``rule_hits()`` fires v3 rules against a single NPI
using cached feature frames + peer percentiles.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pandas as pd

_RULES_PATH = Path(__file__).parent / "rules_v3.json"

FraudType = Literal[
    "upcoding", "phantom_billing", "medically_unnecessary",
    "kickback", "opioid_diversion", "unbundling", "unknown",
]


@dataclass
class Rule:
    name: str
    bucket: str
    predicate_type: str
    threshold: float | None
    threshold_2: float | None
    feature_pct: str | None
    feature_pct_2: str | None
    volume_col: str
    volume_floor: int
    precision: float


def _load_v3_rules() -> list[dict]:
    with open(_RULES_PATH) as f:
        data = json.load(f)
    return data["rules"]


def _to_rule(d: dict) -> Rule:
    ptype = d["predicate_type"]
    return Rule(
        name=d["id"],
        bucket=d["bucket"],
        predicate_type=ptype,
        threshold=d.get("threshold") or d.get("threshold_1"),
        threshold_2=d.get("threshold_2"),
        feature_pct=d.get("feature_pct") or d.get("feature_pct_1"),
        feature_pct_2=d.get("feature_pct_2"),
        volume_col=d.get("volume_col", "partb_n_beneficiaries"),
        volume_floor=d.get("volume_floor", 25),
        precision=d.get("precision", 0.0),
    )


_V3_RAW: list[dict] = _load_v3_rules()
RULES: list[Rule] = [_to_rule(r) for r in _V3_RAW]
RULES_RAW: list[dict] = _V3_RAW


def _rule_mask(df: pd.DataFrame, rule: dict) -> pd.Series:
    """Apply a v3 rule predicate to df, return boolean mask.

    Imported logic from scripts/experiments/measure_top_k_v2.py.
    """
    from scripts.experiments.discover_multisource_rules_v2 import (
        SPECIALTY_BUCKETS,
        _bucket_mask,
    )
    GENERAL_BUCKET = "GENERAL"

    if rule["bucket"] == GENERAL_BUCKET:
        bm = pd.Series(True, index=df.index)
    else:
        terms = SPECIALTY_BUCKETS.get(rule["bucket"], [])
        bm = _bucket_mask(df, terms) if terms else pd.Series(True, index=df.index)

    ptype = rule["predicate_type"]
    if ptype == "single_hi":
        col = rule["feature_pct"]
        if col not in df.columns:
            return pd.Series(False, index=df.index)
        return (
            bm
            & (df[col] >= rule["threshold"])
            & (df[rule["volume_col"]] >= rule["volume_floor"])
        )
    if ptype == "single_lo":
        col = rule["feature_pct"]
        if col not in df.columns:
            return pd.Series(False, index=df.index)
        return (
            bm
            & (df[col] <= rule["threshold"])
            & (df[rule["volume_col"]] >= rule["volume_floor"])
        )
    if ptype == "pair_hi_hi":
        c1, c2 = rule["feature_pct_1"], rule["feature_pct_2"]
        if c1 not in df.columns or c2 not in df.columns:
            return pd.Series(False, index=df.index)
        return (
            bm
            & (df[c1] >= rule["threshold_1"])
            & (df[c2] >= rule["threshold_2"])
            & (df[rule["volume_col"]] >= rule["volume_floor"])
        )
    if ptype == "pair_hi_lo":
        c1, c2 = rule["feature_pct_1"], rule["feature_pct_2"]
        if c1 not in df.columns or c2 not in df.columns:
            return pd.Series(False, index=df.index)
        return (
            bm
            & (df[c1] >= rule["threshold_1"])
            & (df[c2] <= rule["threshold_2"])
            & (df[rule["volume_col"]] >= rule["volume_floor"])
        )
    return pd.Series(False, index=df.index)


def rule_hits(npi: str, year: int) -> list[dict]:
    """Which v3 rules fire for this NPI? Uses cached feature frames."""
    from fraudlib.ranker.feature_frames import load_feature_frame

    df = load_feature_frame(year)
    npi_str = str(npi).zfill(10)
    if "npi_padded" in df.columns:
        row = df[df["npi_padded"] == npi_str]
    elif "npi" in df.columns:
        row = df[df["npi"].astype(str).str.zfill(10) == npi_str]
    else:
        return []

    if row.empty:
        return []

    hits = []
    for raw in _V3_RAW:
        mask = _rule_mask(row, raw)
        if mask.any():
            hits.append({
                "rule": raw["id"],
                "bucket": raw["bucket"],
                "predicate_type": raw["predicate_type"],
                "precision": raw.get("precision", 0.0),
            })
    return hits


def add_peer_percentiles(
    df: pd.DataFrame,
    group: str = "specialty_state",
) -> pd.DataFrame:
    """Add within-group percentile columns to a Part B panel.

    Kept for backward compatibility with query_tools.py agent tooling.
    """
    df = df.copy()
    type_col = "Rndrng_Prvdr_Type" if "Rndrng_Prvdr_Type" in df.columns else "partb_provider_type"
    state_col = "Rndrng_Prvdr_State_Abrvtn" if "Rndrng_Prvdr_State_Abrvtn" in df.columns else "partb_state"

    if type_col not in df.columns or state_col not in df.columns:
        return df

    base_cols = {
        "svcs_per_bene": ["Tot_Srvcs", "Tot_Benes"],
        "pay_per_bene": ["Tot_Mdcr_Pymt_Amt", "Tot_Benes"],
        "pay_per_svc": ["Tot_Mdcr_Pymt_Amt", "Tot_Srvcs"],
    }
    for name, (num, den) in base_cols.items():
        if num in df.columns and den in df.columns:
            df[name] = df[num] / df[den].clip(lower=1)

    pct_candidates = [
        "svcs_per_bene", "pay_per_bene", "pay_per_svc",
        "Tot_Mdcr_Pymt_Amt", "Tot_Srvcs", "Tot_Benes", "Tot_HCPCS_Cds",
        "drug_frac", "chrg_to_allowed",
    ]
    g = df.groupby([type_col, state_col], observed=True)
    for col in pct_candidates:
        if col in df.columns:
            df[f"{col}_pct"] = g[col].rank(pct=True, na_option="keep")

    return df


def evaluate(df: pd.DataFrame, rules: list[Rule] | None = None,
             add_percentiles: bool = True, year: int | None = None) -> pd.DataFrame:
    """Apply rules to panel and add fire columns. Compat shim."""
    if rules is None:
        rules = RULES
    if add_percentiles:
        df = add_peer_percentiles(df)
    for i, r in enumerate(rules):
        raw = _V3_RAW[i] if i < len(_V3_RAW) else {}
        if raw:
            df[f"rule__{i:03d}"] = _rule_mask(df, raw).astype(int)
    df["n_rules_fired"] = sum(
        df.get(f"rule__{i:03d}", 0) for i in range(len(rules))
    )
    return df


def filter_top(df: pd.DataFrame, min_rules: int = 1,
               top_n: int = 1000) -> pd.DataFrame:
    """Filter to providers with >=min_rules fired, return top_n by count."""
    if "n_rules_fired" not in df.columns:
        return df.head(0)
    hits = df[df["n_rules_fired"] >= min_rules]
    return hits.nlargest(top_n, "n_rules_fired")


def summarize(df: pd.DataFrame) -> dict:
    """Quick summary of rule-fire coverage."""
    rule_cols = [c for c in df.columns if c.startswith("rule__")]
    return {
        "n_rules": len(rule_cols),
        "n_providers": len(df),
        "n_any_fire": int((df[rule_cols].sum(axis=1) > 0).sum()) if rule_cols else 0,
    }
