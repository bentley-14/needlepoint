"""Top-K evaluation with precision-weighted (log-LR) rule scoring.

Replaces the naive `n_rules_fired * 1e10 + payment` score with a
naive-Bayes-style log-likelihood-ratio aggregation:

    score(provider) = Σ_rules-that-fire log(rule.precision / base_rate)

This weights providers who trigger high-precision (rare-but-informative)
rules above those who simply trigger many low-precision rules. Without
this fix, a provider in a rule-rich specialty bucket racks up a high
n_rules_fired purely from bucket membership.

Also reports an alternative scoring (cap rules per bucket to top-N, then
sum n_rules_fired) for comparison.

Usage:
    python scripts/experiments/measure_top_k_v2.py \
        --rules-from results/rule_candidates_v2_2018/selected_rules.json \
        --years 2018,2019,2020 \
        --out results/rule_candidates_v2_2018/top_k_eval_v2/
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.experiments.discover_multisource_rules_v2 import (
    GENERAL_BUCKET,
    SCANNABLE_FEATURES,
    SPECIALTY_BUCKETS,
    _add_peer_pcts,
    _bucket_mask,
    _load_year,
    _stamp_positives,
)

K_VALUES = (100, 250, 500, 1000, 2500, 5000, 10000)


def _rule_mask(df: pd.DataFrame, rule: dict) -> pd.Series:
    """Apply a rule predicate to df, return boolean mask."""
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


def _score_log_lr(df: pd.DataFrame, rules: list[dict]) -> pd.DataFrame:
    """For each provider, score = Σ log(rule.precision / base_rate) for fires.

    base_rate computed from this year's panel (n_pos / n_panel). Rules with
    precision ≤ base_rate contribute 0 (don't penalize).
    """
    n_pos = int(df["y_eval"].sum())
    n_total = len(df)
    base_rate = n_pos / max(n_total, 1)
    log_lr = pd.Series(0.0, index=df.index)
    n_fired = pd.Series(0, index=df.index, dtype=int)
    for rule in rules:
        prec = float(rule.get("precision", 0))
        if prec <= base_rate:
            weight = 0.0
        else:
            weight = math.log(prec / base_rate)
        m = _rule_mask(df, rule)
        log_lr = log_lr + m.astype(float) * weight
        n_fired = n_fired + m.astype(int)
    out = df.copy()
    out["_log_lr"] = log_lr
    out["_n_rules"] = n_fired
    pay = out.get("partb_total_medicare_payment", pd.Series(0.0, index=out.index))
    # Final score: log_lr primary, payment as tiebreaker
    out["_score"] = log_lr + (pay.fillna(0).astype(float) / 1e12)
    out = out.sort_values("_score", ascending=False).reset_index(drop=True)
    out["_rank"] = np.arange(1, len(out) + 1)
    return out


def run(
    rules_path: Path,
    years: tuple[int, ...],
    out_dir: Path,
    verbose: bool = True,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    rules = json.loads(rules_path.read_text())["rules"]
    if verbose:
        print(f"Loaded {len(rules)} rules from {rules_path}")
    all_rows = []
    for year in years:
        if verbose:
            print(f"\n=== year={year} ===")
        df = _load_year(year)
        df = _stamp_positives(df, year)
        df = _add_peer_pcts(df, SCANNABLE_FEATURES)
        n_pos = int(df["y_eval"].sum())
        if verbose:
            print(f"  loading + percentiles done. positives={n_pos}")
            print(f"  scoring with log-LR over {len(rules)} rules...")
        ranked = _score_log_lr(df, rules)
        for k in K_VALUES:
            top_k = ranked.head(k)
            tp_k = int(top_k["y_eval"].sum())
            recall_k = tp_k / n_pos if n_pos else 0.0
            precision_k = tp_k / k
            min_score = float(top_k["_score"].iloc[-1])
            min_n_rules = int(top_k["_n_rules"].iloc[-1])
            all_rows.append({
                "year": year,
                "k": k,
                "n_positives": n_pos,
                "tp_at_k": tp_k,
                "recall_at_k": recall_k,
                "precision_at_k": precision_k,
                "min_score_at_k": min_score,
                "min_n_rules_at_k": min_n_rules,
            })
            if verbose:
                print(
                    f"  K={k:>5}: TP={tp_k:>3} recall={recall_k:.1%} "
                    f"precision={precision_k:.3%} "
                    f"(min_score={min_score:.2f}, min_n_rules={min_n_rules})"
                )
    out = pd.DataFrame(all_rows)
    out.to_csv(out_dir / "top_k_metrics.csv", index=False)
    pivot = out.pivot(index="year", columns="k", values="recall_at_k")
    pivot.columns = [f"recall@{k}" for k in pivot.columns]
    pivot.to_csv(out_dir / "recall_at_k_by_year.csv")
    if verbose:
        print(f"\n## Recall@K by year (log-LR scoring)")
        print(pivot.applymap(lambda x: f"{x:.1%}").to_string())
        print(f"\nWrote {out_dir}/")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rules-from", type=Path, required=True)
    ap.add_argument(
        "--years",
        type=lambda s: tuple(int(x) for x in s.split(",")),
        default=(2018, 2019, 2020),
    )
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    run(args.rules_from, args.years, args.out, verbose=not args.quiet)
    return 0


if __name__ == "__main__":
    sys.exit(main())
