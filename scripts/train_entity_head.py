#!/usr/bin/env python3
"""P4 — org/entity scoring head: train + walk-forward eval + score export.

The SEPARATE head that ranks organizations (and the wider Medicaid billing
universe) by fraud-label likelihood, trained on data/extracted/entity_labels.jsonl
(DOJ org/Medicaid/facility defendants, Opus-confirmed). The frozen individual
Part-B ranker and its R@10k walk-forward are untouched — this script lives
entirely outside that pipeline.

Scoring unit (empirical pivot from the first spec draft — see the spec's
addendum): the ORG NPI itself, over the T-MSIS Medicaid billing universe
(~360k billing NPIs/year, Type-1 + Type-2, tagged), NOT the Part-B entity
cluster. Verified 2026-07-16: 42/48 confirmed labels have Medicaid features
(they were surface-confirmed there by construction), while only 7-17/48 are
members of a Part-B entity graph (corporate NPIs mostly don't render Part-B).
The Part-B entity-cluster features join on as an OPTIONAL feature block via
direct membership where it exists.

Label semantics (predict-before-enforcement, strict point-in-time):
- enforcement_year = year of disposition.date (min across label rows per NPI).
- A (npi, feature_year) row is POSITIVE iff feature_year in
  [enf-3, enf-1]  (equivalently: enforcement lands in (fy, fy+3]).
- Rows with feature_year >= enforcement_year are DROPPED (post-enforcement
  billing collapse must not leak into train or test as either class).
- TRAIN-time positives additionally require enf <= test_year (at wall-clock
  time Y you cannot know about a 2026 case; unlabeled-at-Y stays negative —
  the same PU reality the individual ranker lives with).

Leakage controls:
- pct_members_anomalous (entity block) is EXCLUDED from the primary model —
  it derives from the individual ranker, which was trained on later-year
  cases. --with-ranker-features re-adds it for a secondary, clearly-labeled
  number.
- Walk-forward by year; the same NPI never contributes a positive to both
  train and test of one fold (enforcement windows don't overlap folds given
  the strict-known-by-Y rule + post-enf drop).

Outputs (results/entity_head/):
- eval_<date>.md          — per-test-year n_pos, R@1k/5k/10k, mean rank pctile.
- entity_scores_{year}.parquet — npi, score, rank, entity_type, service_class,
  medicaid_total_pay, labeled, enforcement_year, entity_id (when bridged) —
  the artifact the target screen consumes (labeled rows are EXCLUDED from any
  target shortlist by that consumer: government-known == zero qui-tam value).

Usage:
    python scripts/train_entity_head.py --test-years 2021 2022 2023 2024
    python scripts/train_entity_head.py --score-year 2024        # export scores
    python scripts/train_entity_head.py --smoke                  # tiny fast run
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
LOG = logging.getLogger("entity_head")

LABELS_PATH = REPO_ROOT / "data" / "extracted" / "entity_labels.jsonl"
MEDICAID_DIR = REPO_ROOT / "data" / "processed"
ENTITY_DIR = REPO_ROOT / "data" / "processed"
NPPES_PATH = REPO_ROOT / "data" / "registries" / "nppes_bulk.parquet"
OUT_DIR = REPO_ROOT / "results" / "entity_head"

FEATURE_YEARS = [2018, 2019, 2020, 2021, 2022, 2023, 2024]
PREDICT_WINDOW = 3          # enforcement in (fy, fy+3] => positive
MIN_TEST_POS = 5            # skip a test year with fewer positives (report it)
SEED = 76

# Medicaid feature-frame columns used as model inputs (v1 frames; the v2
# state-percentile columns are picked up automatically when present so the
# same script works before and after PR #354's frames rebuild).
MEDICAID_NUM_COLS = [
    "medicaid_total_pay", "medicaid_claim_lines", "medicaid_n_hcpcs",
    "top1_code_share", "code_hhi", "yoy_growth",
    "top1_code_share_pctile_in_class", "code_hhi_pctile_in_class",
    "medicaid_total_pay_pctile_in_class", "yoy_growth_pctile_in_class",
    "cross_program_ratio", "medicare_partb_pay",
]
MEDICAID_V2_EXTRA = [
    "top1_code_share_pctile_in_class_state", "code_hhi_pctile_in_class_state",
    "medicaid_total_pay_pctile_in_class_state", "yoy_growth_pctile_in_class_state",
]
# Single-sourced from the feature builder so the one-hot columns always match
# the frame's actual class partition (v3 widened the taxonomy). Path bootstrap
# so `python scripts/train_entity_head.py` works without an installed package.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
from scripts.compute_medicaid_features import SERVICE_CLASSES  # noqa: E402

# Entity-cluster block (optional join; NaN where the org NPI isn't a Part-B
# entity-graph member). pct_members_anomalous is ranker-derived — leakage.
ENTITY_COLS_SAFE = [
    "n_members", "n_billing_npis", "entity_total_pay",
    "n_distinct_addresses", "cross_member_code_uniformity",
]
ENTITY_COLS_RANKER = ["pct_members_anomalous"]


# ─────────────────────────────────────────────────────────────────────────────
# Labels — pure functions (unit-tested)
# ─────────────────────────────────────────────────────────────────────────────
def load_enforcement_years(labels_path: Path, *,
                           usable_only: bool = True) -> dict[str, int]:
    """org_npi -> earliest enforcement year (disposition.date year).

    ``usable_only`` (default) drops labels annotated
    ``tmsis_window_activity.training_usable in {none_*}`` — the activity audit
    (2026-07-16) found ~90/162 labels have NO Medicaid billing in their
    predict-before-enforcement window (pre-2018 T-MSIS floor, pharmacy NCPDP
    gap, or Medicare-only orgs). Those contribute no signal to THIS head and
    only add label noise; they remain valid labels for the facility/Medicare
    heads where their activity lives. Un-annotated labels are kept (fail-open).
    """
    enf: dict[str, int] = {}
    for line in labels_path.open():
        if not line.strip():
            continue
        row = json.loads(line)
        npi = str(row.get("org_npi") or "")
        disp = row.get("disposition") or {}
        date = disp.get("date") if isinstance(disp, dict) else None
        if not npi or not date:
            continue
        if usable_only:
            u = (row.get("tmsis_window_activity") or {}).get("training_usable")
            if u and str(u).startswith("none"):
                continue
        year = int(str(date)[:4])
        enf[npi] = min(enf.get(npi, 9999), year)
    return enf


def assign_labels(df: pd.DataFrame, enf: dict[str, int],
                  window: int = PREDICT_WINDOW) -> pd.DataFrame:
    """Adds is_positive + enforcement_year; DROPS post-enforcement rows.

    Positive: feature year in [enf-window, enf-1]. Rows with year >= enf are
    removed entirely (never negatives — billing collapses after indictment).
    """
    out = df.copy()
    out["enforcement_year"] = out["npi"].map(enf)
    post = out["enforcement_year"].notna() & (out["year"] >= out["enforcement_year"])
    out = out[~post].copy()
    e = out["enforcement_year"]
    out["is_positive"] = (
        e.notna() & (out["year"] >= e - window) & (out["year"] <= e - 1)
    ).astype(int)
    return out


def train_mask_known_by(df: pd.DataFrame, test_year: int) -> pd.Series:
    """Strict point-in-time: at wall-clock test_year, a training positive must
    already be public (enf <= test_year). Unknown-future positives stay
    negative in training (PU reality)."""
    known = df["enforcement_year"].isna() | (df["enforcement_year"] <= test_year)
    demote = (df["is_positive"] == 1) & ~known
    mask = df["is_positive"].copy()
    mask[demote] = 0
    return mask


def rank_metrics(scores: np.ndarray, is_pos: np.ndarray,
                 ks: tuple[int, ...] = (1_000, 5_000, 10_000)) -> dict:
    """Recall@k + mean rank percentile of positives (rank 1 = highest score)."""
    order = np.argsort(-scores, kind="stable")
    pos_ranks = np.nonzero(is_pos[order])[0] + 1          # 1-indexed ranks
    n_pos = int(is_pos.sum())
    out = {"n_pos": n_pos, "n_universe": int(len(scores))}
    for k in ks:
        out[f"recall@{k}"] = float((pos_ranks <= k).sum() / n_pos) if n_pos else float("nan")
    out["mean_rank_pctile"] = (
        float(1.0 - (pos_ranks.mean() / len(scores))) if n_pos else float("nan")
    )
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Frame assembly (DuckDB over parquet — no full-panel pandas loads)
# ─────────────────────────────────────────────────────────────────────────────
def medicaid_path(year: int) -> Path:
    return MEDICAID_DIR / f"medicaid_features_{year}.parquet"


def build_year_frame(con: duckdb.DuckDBPyConnection, year: int,
                     smoke: bool = False) -> pd.DataFrame:
    """(npi, year) rows for the Medicaid billing universe + feature blocks."""
    mpath = medicaid_path(year)
    if not mpath.exists():
        raise FileNotFoundError(mpath)
    have_cols = con.execute(
        f"select * from read_parquet('{mpath}') limit 0").df().columns.tolist()
    med_cols = [c for c in MEDICAID_NUM_COLS + MEDICAID_V2_EXTRA if c in have_cols]
    sel = ", ".join(f"m.{c}" for c in med_cols)
    limit = "USING SAMPLE 20000 ROWS (reservoir, 76)" if smoke else ""
    q = f"""
    SELECT CAST(m.npi AS VARCHAR)                       AS npi,
           {year}                                        AS year,
           m.service_class                               AS service_class,
           {sel},
           n.Entity_Type_Code                            AS entity_type,
           TRY_STRPTIME(n.Enumeration_Date, '%m/%d/%Y')::DATE AS enum_date,
           g.entity_id                                   AS entity_id
    FROM read_parquet('{mpath}') AS m
    LEFT JOIN read_parquet('{NPPES_PATH}') AS n
           ON CAST(n.npi AS VARCHAR) = CAST(m.npi AS VARCHAR)
    LEFT JOIN read_parquet('{ENTITY_DIR / f"entity_graph_{year}.parquet"}') AS g
           ON CAST(g.npi AS VARCHAR) = CAST(m.npi AS VARCHAR)
    {limit}
    """
    df = con.execute(q).fetchdf()
    # entity-cluster feature block (safe cols + ranker col, joined by entity_id)
    epath = ENTITY_DIR / f"entity_features_{year}.parquet"
    if epath.exists() and df["entity_id"].notna().any():
        ecols = ", ".join(f"e.{c}" for c in ENTITY_COLS_SAFE + ENTITY_COLS_RANKER)
        ent = con.execute(
            f"select e.entity_id, {ecols} from read_parquet('{epath}') e").fetchdf()
        df = df.merge(ent, on="entity_id", how="left")
    else:
        for c in ENTITY_COLS_SAFE + ENTITY_COLS_RANKER:
            df[c] = np.nan
    # enumeration age (years) at the feature year's end
    df["enum_age_years"] = year + 1 - pd.to_datetime(df["enum_date"]).dt.year
    df.drop(columns=["enum_date"], inplace=True)
    return df


def feature_matrix(df: pd.DataFrame, with_ranker: bool = False
                   ) -> tuple[pd.DataFrame, list[str]]:
    """Numeric model matrix. service_class one-hot; entity_type -> is_org;
    pct_members_anomalous EXCLUDED unless with_ranker (leakage control)."""
    med = [c for c in MEDICAID_NUM_COLS + MEDICAID_V2_EXTRA if c in df.columns]
    cols = med + ENTITY_COLS_SAFE + ["enum_age_years"]
    if with_ranker:
        cols += ENTITY_COLS_RANKER
    # reindex (not df[cols]): a block absent from this frame (e.g. entity cols
    # in a fixture, v2 medicaid cols pre-#354) becomes NaN instead of KeyError —
    # HistGB handles NaN natively.
    X = df.reindex(columns=cols).apply(pd.to_numeric, errors="coerce")
    X["is_org"] = (df["entity_type"].astype("string") == "2").astype(float)
    for sc in SERVICE_CLASSES:
        X[f"sc_{sc}"] = (df["service_class"] == sc).astype(float)
    return X, X.columns.tolist()


def fit_model(X: pd.DataFrame, y: pd.Series):
    """XGBoost (the repo's ranker stack; native NaN handling) with positive
    upweighting. Falls back to sklearn HistGB if xgboost is unavailable."""
    n_pos = max(int(y.sum()), 1)
    spw = min(len(y) / n_pos, 2_000.0)
    try:
        from xgboost import XGBClassifier
        model = XGBClassifier(
            n_estimators=300, learning_rate=0.1, max_depth=5,
            min_child_weight=25, subsample=0.9, colsample_bytree=0.9,
            scale_pos_weight=spw, tree_method="hist",
            eval_metric="aucpr", random_state=SEED, n_jobs=4)
        model.fit(X, y)
    except ImportError:
        from sklearn.ensemble import HistGradientBoostingClassifier
        w = np.where(y == 1, spw, 1.0)
        model = HistGradientBoostingClassifier(
            max_iter=300, learning_rate=0.1, max_leaf_nodes=31,
            min_samples_leaf=50, random_state=SEED)
        model.fit(X, y, sample_weight=w)
    return model


# ─────────────────────────────────────────────────────────────────────────────
# Walk-forward + score export
# ─────────────────────────────────────────────────────────────────────────────
def walk_forward(con, enf: dict[str, int], test_years: list[int],
                 with_ranker: bool, smoke: bool) -> list[dict]:
    frames: dict[int, pd.DataFrame] = {}

    def frame(y: int) -> pd.DataFrame:
        if y not in frames:
            LOG.info("building frame %d ...", y)
            frames[y] = assign_labels(build_year_frame(con, y, smoke=smoke), enf)
        return frames[y]

    results = []
    for ty in test_years:
        train_years = [y for y in FEATURE_YEARS if y < ty]
        if not train_years:
            continue
        train = pd.concat([frame(y) for y in train_years], ignore_index=True)
        test = frame(ty)
        y_train = train_mask_known_by(train, ty)
        X_train, cols = feature_matrix(train, with_ranker)
        X_test, _ = feature_matrix(test, with_ranker)
        n_test_pos = int(test["is_positive"].sum())
        row = {"test_year": ty, "n_train": len(train),
               "n_train_pos": int(y_train.sum()), "n_test_pos": n_test_pos}
        if n_test_pos < MIN_TEST_POS:
            row["skipped"] = f"<{MIN_TEST_POS} positives"
            results.append(row)
            LOG.info("  %d: skipped (%d positives)", ty, n_test_pos)
            continue
        model = fit_model(X_train, y_train)
        scores = model.predict_proba(X_test)[:, 1]
        row.update(rank_metrics(scores, test["is_positive"].to_numpy()))
        results.append(row)
        LOG.info("  %d: %s", ty, {k: v for k, v in row.items() if "recall" in k})
    return results


def export_scores(con, enf: dict[str, int], score_year: int,
                  with_ranker: bool, out_dir: Path, smoke: bool) -> Path:
    train_years = [y for y in FEATURE_YEARS if y < score_year]
    train = pd.concat(
        [assign_labels(build_year_frame(con, y, smoke=smoke), enf) for y in train_years],
        ignore_index=True)
    target = assign_labels(build_year_frame(con, score_year, smoke=smoke), enf)
    y_train = train_mask_known_by(train, score_year)
    X_train, _ = feature_matrix(train, with_ranker)
    X_t, _ = feature_matrix(target, with_ranker)
    model = fit_model(X_train, y_train)
    target = target.copy()
    target["score"] = model.predict_proba(X_t)[:, 1]
    target["rank"] = target["score"].rank(ascending=False, method="first").astype(int)
    target["labeled"] = target["npi"].isin(enf.keys())
    out_cols = ["npi", "year", "score", "rank", "entity_type", "service_class",
                "medicaid_total_pay", "entity_id", "labeled", "enforcement_year"]
    out_path = out_dir / f"entity_scores_{score_year}.parquet"
    target.sort_values("rank")[out_cols].to_parquet(out_path, index=False)
    LOG.info("scores -> %s (%d rows)", out_path, len(target))
    return out_path


def write_report(results: list[dict], out_dir: Path, with_ranker: bool,
                 n_labels: int, frames_version_note: str) -> Path:
    date = datetime.date.today().isoformat()
    p = out_dir / f"eval_{date}.md"
    lines = [
        f"# P4 entity/org head — walk-forward eval ({date})",
        "",
        f"- labels: {n_labels} org NPIs (entity_labels.jsonl; enforcement-dated)",
        f"- unit: (npi, year) over the T-MSIS Medicaid billing universe",
        f"- label window: enforcement in (fy, fy+{PREDICT_WINDOW}]; post-enforcement rows dropped;"
        " strict known-by-test-year training labels",
        f"- ranker-derived features: {'INCLUDED (secondary run)' if with_ranker else 'EXCLUDED (primary; leakage control)'}",
        f"- {frames_version_note}",
        "",
        "| test year | n_train | train pos | test pos | R@1k | R@5k | R@10k | mean rank pctile |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        if r.get("skipped"):
            lines.append(f"| {r['test_year']} | {r['n_train']} | {r['n_train_pos']} | "
                         f"{r['n_test_pos']} | — | — | — | skipped ({r['skipped']}) |")
        else:
            lines.append(
                f"| {r['test_year']} | {r['n_train']} | {r['n_train_pos']} | {r['n_test_pos']} "
                f"| {r['recall@1000']:.3f} | {r['recall@5000']:.3f} | {r['recall@10000']:.3f} "
                f"| {r['mean_rank_pctile']:.4f} |")
    lines += ["", "Frozen Part-B individual baseline untouched; this head is separate.", ""]
    p.write_text("\n".join(lines))
    return p


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--labels", type=Path, default=LABELS_PATH)
    ap.add_argument("--test-years", type=int, nargs="+",
                    default=[2020, 2021, 2022, 2023, 2024])
    ap.add_argument("--score-year", type=int, default=None,
                    help="also export entity_scores_{year}.parquet")
    ap.add_argument("--with-ranker-features", action="store_true",
                    help="secondary run incl. pct_members_anomalous (leaky for eval)")
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    ap.add_argument("--smoke", action="store_true", help="20k-row/year sample, fast")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if not args.verbose else logging.DEBUG,
                        format="%(asctime)s %(levelname)s %(message)s")

    enf = load_enforcement_years(args.labels)
    LOG.info("%d labeled org NPIs (enforcement years %s..%s)", len(enf),
             min(enf.values()), max(enf.values()))
    args.out_dir.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()

    # frames-version note for the report (provisional until PR #354 frames land)
    have = con.execute(f"select * from read_parquet('{medicaid_path(2023)}') limit 0"
                       ).df().columns.tolist()
    note = ("medicaid frames: v2 (state percentiles present)"
            if "feature_frame_version" in have or MEDICAID_V2_EXTRA[0] in have
            else "medicaid frames: v1 PROVISIONAL_PRE_P1 (rebuild after PR #354 merges)")

    results = walk_forward(con, enf, args.test_years,
                           args.with_ranker_features, args.smoke)
    report = write_report(results, args.out_dir, args.with_ranker_features,
                          len(enf), note)
    LOG.info("report -> %s", report)
    if args.score_year:
        export_scores(con, enf, args.score_year, args.with_ranker_features,
                      args.out_dir, args.smoke)
    return 0


if __name__ == "__main__":
    sys.exit(main())
