"""Tier 1.2: per-archetype rankers with max-pool aggregation — LIBRARY.

STATUS 2026-06-09: kept as a LIBRARY (run_ablation/run_tier1_combined import
helpers from here; see scripts/REGISTRY.yaml). Stale-claim notes: the production
archetype set no longer includes STATE_EXCLUSION, and rules_v3 has 59 rules
(not 27); in v4 the panel filter is the AGENT filter, not the rule filter.

Historical design (as originally run): trains one XGBoost binary classifier per
fraud archetype (OPIOID_MILL, BILLING_FRAUD, KICKBACK, UPCODING,
MEDICALLY_UNNECESSARY, STATE_EXCLUSION) on the multi-source frame restricted
to the set-cover rule filter. Each archetype model treats only its own positives
as 1, everything else (including other-archetype positives) as 0
(Hancock & Khoshgoftaar 2022 stratification).

Scores are aggregated via max-pool: rank candidates by max(score across
archetype models). Reports top-K recall + precision for max-pool,
sum-pool, and the per-archetype top-N union.

Adds PECOS features (LEFT JOIN on NPI):
  pecos_partb_eligible, pecos_dme_eligible, pecos_hha_eligible,
  pecos_pmd_eligible, pecos_hospice_eligible,
  pecos_has_ordering_privilege, pecos_has_referring_privilege,
  pecos_n_privileges, pecos_in_directory (NEW: True if NPI in PECOS
  Order_and_Referring file at all — absence is a straw-NPI signal).

Usage:
    python scripts/experiments/train_per_archetype_rankers.py \
        --rules-from results/rule_candidates_v2_2018/set_cover_rules.json \
        --train-years 2014,2016,2017 \
        --val-year 2018 \
        --test-years 2019,2020 \
        --out results/per_archetype_rankers_v1
"""
from __future__ import annotations

import argparse
import json
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fraudlib.eval.archetypes import ARCHETYPES, archetype_lookup
from fraudlib.ingest.providers.pecos import load_pecos_features
from scripts.experiments.discover_multisource_rules_v2 import (
    SCANNABLE_FEATURES,
    _add_peer_pcts,
)
from scripts.experiments.train_filtered_ranker import (
    K_VALUES,
    _NON_FEATURE_COLS,
    _apply_rules_get_fires,
    _load_year,
)

DEFAULT_OUT_DIR = Path("results/per_archetype_rankers_v1")
PECOS_PARQUET = Path("data/registries/pecos_ordering_referring.parquet")


def _stamp_archetype(df: pd.DataFrame, year: int) -> pd.DataFrame:
    """Add y_eval (any verified positive) + y_archetype (per-archetype label)."""
    lookup = archetype_lookup(year)
    npi_str = df["npi"].astype(str).str.zfill(10)
    df = df.copy()
    df["npi_padded"] = npi_str
    df["archetype"] = npi_str.map(lookup).fillna("")
    df["y_eval"] = (df["archetype"] != "").astype(int)
    return df


def _attach_pecos(df: pd.DataFrame) -> pd.DataFrame:
    """LEFT JOIN PECOS features onto df. Adds pecos_in_directory as a
    new straw-NPI signal: True if NPI is in PECOS Order_and_Referring.
    """
    if not PECOS_PARQUET.exists():
        return df
    pecos = load_pecos_features(parquet_path=PECOS_PARQUET)
    pecos["pecos_in_directory"] = True
    df = df.copy()
    df["npi_padded"] = df.get("npi_padded", df["npi"].astype(str).str.zfill(10))
    df = df.merge(
        pecos.rename(columns={"NPI": "npi_padded"}),
        on="npi_padded", how="left",
    )
    df["pecos_in_directory"] = df["pecos_in_directory"].fillna(False).astype(int)
    # Cast the privilege flags from bool → int (XGBoost handles this cleanly)
    for c in ("pecos_partb_eligible", "pecos_dme_eligible",
              "pecos_hha_eligible", "pecos_pmd_eligible",
              "pecos_hospice_eligible", "pecos_has_ordering_privilege",
              "pecos_has_referring_privilege"):
        if c in df.columns:
            df[c] = df[c].fillna(False).astype(int)
    if "pecos_n_privileges" in df.columns:
        df["pecos_n_privileges"] = df["pecos_n_privileges"].fillna(0).astype(int)
    return df


def _prepare_year(year: int, rules: list[dict]) -> pd.DataFrame:
    df = _load_year(year)
    df = _stamp_archetype(df, year)
    df = _attach_pecos(df)
    df = _add_peer_pcts(df, SCANNABLE_FEATURES)
    rule_df, any_fires = _apply_rules_get_fires(df, rules)
    df = pd.concat([df, rule_df], axis=1)
    df = df[any_fires].reset_index(drop=True)
    df["puf_year"] = year
    return df


_NON_FEATURE_COLS_EXTENDED = _NON_FEATURE_COLS | {"archetype"}


def _feature_matrix(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    numeric_cols = [
        c for c in df.select_dtypes(include="number").columns
        if c not in _NON_FEATURE_COLS_EXTENDED
    ]
    cat_cols = [
        c for c in df.select_dtypes(include="category").columns
        if c not in _NON_FEATURE_COLS_EXTENDED
    ]
    feature_cols = numeric_cols + cat_cols
    X = df[feature_cols].copy()
    X[numeric_cols] = X[numeric_cols].fillna(0.0)
    return X, feature_cols


def _train_one_archetype(
    archetype: str,
    train_df: pd.DataFrame,
    val_df: pd.DataFrame,
    feat_cols: list[str],
    verbose: bool = True,
) -> tuple[object, dict]:
    """Train binary XGBoost for one archetype. Returns (model, metrics)."""
    import xgboost as xgb

    scheme_col = f"scheme_{archetype}"
    if scheme_col in train_df.columns:
        y_train = train_df[scheme_col].astype(int).values
        y_val = val_df[scheme_col].astype(int).values if scheme_col in val_df.columns else (val_df["archetype"] == archetype).astype(int).values
    else:
        y_train = (train_df["archetype"] == archetype).astype(int).values
        y_val = (val_df["archetype"] == archetype).astype(int).values
    X_train, _ = _feature_matrix(train_df)
    X_train = X_train.reindex(columns=feat_cols, fill_value=0)
    X_val, _ = _feature_matrix(val_df)
    X_val = X_val.reindex(columns=feat_cols, fill_value=0)

    n_pos = int(y_train.sum())
    n_neg = int((y_train == 0).sum())
    if n_pos < 5:
        if verbose:
            print(f"  [{archetype}] skipped (only {n_pos} train positives)")
        return None, {"archetype": archetype, "skipped": True, "n_train_pos": n_pos}
    scale = (n_neg / max(n_pos, 1)) * 2.0
    if verbose:
        print(f"  [{archetype}] train_pos={n_pos} val_pos={int(y_val.sum())} "
              f"scale_pos_weight={scale:.0f}")

    clf = xgb.XGBClassifier(
        n_estimators=300, max_depth=8, learning_rate=0.1,
        min_child_weight=1, subsample=0.8, colsample_bytree=0.8,
        scale_pos_weight=scale, tree_method="hist",
        enable_categorical=True, early_stopping_rounds=20,
        eval_metric="aucpr", n_jobs=-1, verbosity=0, random_state=0,
    )
    clf.fit(X_train, y_train, eval_set=[(X_val, y_val)], verbose=False)
    return clf, {
        "archetype": archetype,
        "n_train_pos": n_pos,
        "n_val_pos": int(y_val.sum()),
        "best_iteration": clf.best_iteration,
        "scale_pos_weight": scale,
    }


def _score_topk(
    scored: pd.DataFrame, score_col: str, n_pos: int
) -> list[dict]:
    rows = []
    ranked = scored.sort_values(score_col, ascending=False).reset_index(drop=True)
    for k in K_VALUES:
        top_k = ranked.head(k)
        tp_k = int(top_k["y_eval"].sum())
        rows.append({
            "k": k,
            "tp": tp_k,
            "recall_at_k": tp_k / n_pos if n_pos else 0.0,
            "precision_at_k": tp_k / k,
        })
    return rows


def run(
    rules_path: Path,
    train_years: tuple[int, ...],
    val_year: int,
    test_years: tuple[int, ...],
    out_dir: Path,
    verbose: bool = True,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    rules = json.loads(rules_path.read_text())["rules"]
    if verbose:
        print(f"Loaded {len(rules)} set-cover rules\n")

    if verbose:
        print(f"Preparing train years {train_years}...")
    train_frames = []
    for y in train_years:
        if verbose:
            print(f"  {y}...")
        train_frames.append(_prepare_year(y, rules))
    train_df = pd.concat(train_frames, ignore_index=True)
    if verbose:
        print(f"  train rows: {len(train_df):,}, "
              f"positives: {int(train_df['y_eval'].sum())}")
        print(f"  archetype mix: {dict(train_df.loc[train_df.y_eval==1, 'archetype'].value_counts())}")

    if verbose:
        print(f"\nPreparing val year {val_year}...")
    val_df = _prepare_year(val_year, rules)
    if verbose:
        print(f"  val rows: {len(val_df):,}, positives: {int(val_df['y_eval'].sum())}")

    if verbose:
        print(f"\nPreparing test years {test_years}...")
    test_dfs = {}
    for y in test_years:
        test_dfs[y] = _prepare_year(y, rules)

    # Symmetric NPI holdout: drop val + test positive NPIs from train
    val_pos = set(val_df.loc[val_df["y_eval"] == 1, "npi_padded"])
    test_pos: set[str] = set()
    for t in test_dfs.values():
        test_pos |= set(t.loc[t["y_eval"] == 1, "npi_padded"])
    holdout = val_pos | test_pos
    before = len(train_df)
    train_df = train_df[~train_df["npi_padded"].isin(holdout)].reset_index(drop=True)
    if verbose:
        print(f"\nDropped {before - len(train_df):,} train rows (val/test positives)")
        print(f"  after holdout: {len(train_df):,} rows, "
              f"{int(train_df['y_eval'].sum())} positives")

    val_df = val_df[~val_df["npi_padded"].isin(test_pos)].reset_index(drop=True)

    # Build canonical feature schema from train_df
    _, feat_cols = _feature_matrix(train_df)
    if verbose:
        print(f"\nFeature schema: {len(feat_cols)} features (incl. PECOS + rule-fires)")

    # Train one model per archetype
    if verbose:
        print(f"\n=== Training {len(ARCHETYPES)} per-archetype models ===")
    models: dict[str, object] = {}
    train_metrics = []
    for archetype in ARCHETYPES:
        model, m = _train_one_archetype(archetype, train_df, val_df, feat_cols, verbose=verbose)
        if model is not None:
            models[archetype] = model
        train_metrics.append(m)

    # Score every eval-frame row with each archetype model
    eval_frames = {val_year: val_df, **test_dfs}
    score_results: dict[int, pd.DataFrame] = {}
    for y, frame in eval_frames.items():
        if verbose:
            print(f"\n=== Scoring year={y} ===")
        X, _ = _feature_matrix(frame)
        X = X.reindex(columns=feat_cols, fill_value=0)
        scored = frame.copy()
        for archetype, model in models.items():
            scored[f"score_{archetype}"] = model.predict_proba(X)[:, 1]
        # Aggregate
        score_cols = [c for c in scored.columns if c.startswith("score_")]
        scored["score_max"] = scored[score_cols].max(axis=1)
        scored["score_sum"] = scored[score_cols].sum(axis=1)
        score_results[y] = scored

    # Top-K eval per aggregation strategy
    all_topk = []
    for y, scored in score_results.items():
        n_pos = int(scored["y_eval"].sum())
        if verbose:
            print(f"\n## year={y} (n_pos={n_pos})")
        for agg, col in (("max_pool", "score_max"), ("sum_pool", "score_sum")):
            rows = _score_topk(scored, col, n_pos)
            for r in rows:
                r.update({"year": y, "agg": agg, "n_positives": n_pos})
                all_topk.append(r)
            if verbose:
                line = f"  {agg:>8}: " + "  ".join(
                    f"K={r['k']}: {r['recall_at_k']:.1%}" for r in rows if r["k"] in (100, 1000, 5000, 10000)
                )
                print(line)
        # Per-archetype top-K (recall@K from the single archetype's model)
        for archetype in models:
            col = f"score_{archetype}"
            n_arch_pos = int((scored["archetype"] == archetype).sum())
            ranked = scored.sort_values(col, ascending=False).reset_index(drop=True)
            for k in (100, 500, 1000, 5000):
                top_k = ranked.head(k)
                # Total positives caught (any archetype)
                tp_k = int(top_k["y_eval"].sum())
                # Same archetype caught
                tp_arch = int((top_k["archetype"] == archetype).sum())
                all_topk.append({
                    "year": y, "agg": f"per_arch_{archetype}",
                    "k": k, "tp": tp_k,
                    "tp_archetype_specific": tp_arch,
                    "n_positives": n_pos,
                    "n_archetype_positives": n_arch_pos,
                    "recall_at_k": tp_k / n_pos if n_pos else 0.0,
                    "recall_archetype_at_k": tp_arch / n_arch_pos if n_arch_pos else 0.0,
                    "precision_at_k": tp_k / k,
                })
        # Top-N-per-archetype union: take top-200 from each model, dedupe
        topn_per_arch = 200
        union_npis: set[str] = set()
        for archetype in models:
            col = f"score_{archetype}"
            top_n = scored.nlargest(topn_per_arch, col)["npi_padded"].tolist()
            union_npis |= set(top_n)
        union_df = scored[scored["npi_padded"].isin(union_npis)]
        all_topk.append({
            "year": y, "agg": f"union_top{topn_per_arch}",
            "k": len(union_npis),
            "tp": int(union_df["y_eval"].sum()),
            "n_positives": n_pos,
            "recall_at_k": int(union_df["y_eval"].sum()) / n_pos if n_pos else 0.0,
            "precision_at_k": int(union_df["y_eval"].sum()) / max(len(union_npis), 1),
        })
        if verbose:
            print(f"  union_top{topn_per_arch}: |union|={len(union_npis):,} "
                  f"recall={int(union_df['y_eval'].sum())/max(n_pos,1):.1%}")
        # Save top-1000 max-pool for diagnostic
        if y in test_years:
            scored.sort_values("score_max", ascending=False).head(1000).to_csv(
                out_dir / f"top1000_max_pool_{y}.csv", index=False,
            )

    pd.DataFrame(all_topk).to_csv(out_dir / "top_k_metrics.csv", index=False)
    (out_dir / "metrics.json").write_text(json.dumps({
        "train_years": list(train_years),
        "val_year": val_year,
        "test_years": list(test_years),
        "archetypes_trained": list(models.keys()),
        "n_features": len(feat_cols),
        "train_metrics": train_metrics,
    }, indent=2, default=str))
    if verbose:
        print(f"\nWrote {out_dir}/")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rules-from", type=Path, required=True)
    ap.add_argument(
        "--train-years",
        type=lambda s: tuple(int(x) for x in s.split(",")),
        default=(2014, 2016, 2017),
    )
    ap.add_argument("--val-year", type=int, default=2018)
    ap.add_argument(
        "--test-years",
        type=lambda s: tuple(int(x) for x in s.split(",")),
        default=(2019, 2020),
    )
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=FutureWarning)
        run(
            rules_path=args.rules_from,
            train_years=args.train_years,
            val_year=args.val_year,
            test_years=args.test_years,
            out_dir=args.out,
            verbose=not args.quiet,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
