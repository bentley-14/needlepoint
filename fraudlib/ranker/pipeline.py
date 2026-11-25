"""load_training_frame + train + FraudModel.

Flow
----
load_training_frame(year, sources)
  → calls each DataSource.load()  (fetch + feature engineering per source)
  → joins on NPI
  → applies DOJ-case labels (LEIE deprecated as truth metric 2026-04-25 —
    see docs/LEIE_DEPRECATION_PLAN.md)
  → casts categorical columns to pd.CategoricalDtype
  → returns combined DataFrame

train(df, eval_npis)
  → builds feature matrix from the combined DataFrame
  → holds out eval NPIs (never train on them)
  → fits XGBClassifier with literature-supported hyperparams
  → returns FraudModel

FraudModel.score(df)
  → aligns df to training feature columns
  → returns fraud probabilities as pd.Series
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import joblib
import numpy as np
import pandas as pd
import xgboost as xgb

from fraudlib.ranker import features
from fraudlib.labels.labels_v2 import load_cases, label_providers_with_cases
from fraudlib.eval.evalkit import load_positives
from fraudlib.ranker.sources import SOURCE_REGISTRY

DEFAULT_CASES_PATH = Path("data/extracted/gold_set.jsonl")
DEFAULT_EVAL_SPLIT_PATH = Path("data/source/cases/eval_split.json")

# Column set + order returned by score(). Exposed as a module-level
# constant so the mock Layer 1 (fraudlib.ranker.mocks) and its schema
# test can assert exact parity without re-executing score().
SCORE_KEEP_COLS: tuple[str, ...] = (
    "npi",
    "risk_score",
    "risk_rank",
    "label_bucket",
    "fraud_case",
    "drivers",
    "flag_summary",
)

# Columns that carry labels or identifiers — excluded from the feature matrix.
_NON_FEATURE_COLS = {
    "npi", "_npi", "fraud_case", "y",
    "label_bucket", "case_ids", "case_fraud_types", "case_tier",
    # LEIE-augmentation label columns (added 2026-04-27); never features
    "fraud_leie", "fraud_label", "label_source", "label_confidence",
    # Diagnostic / experiment-bookkeeping columns that must not leak into
    # the model: puf_year splits by year for held-out-year validation,
    # peer_group_size / peer_parent_size are peer-stats context already
    # covered by _peer_z features. y_eval is the test-time label override.
    "puf_year", "y_eval",
}

# String columns produced by load_training_frame that should be categorical
# features (not labels) — cast to CategoricalDtype so XGBoost enable_categorical
# works without extra steps on the caller side.
_CATEGORICAL_FEATURE_COLS = [
    "nppes_credential_bucket",
    "nppes_taxonomy_bucket",
]


# ---------------------------------------------------------------------------
# Feature matrix helper
# ---------------------------------------------------------------------------

def _feature_matrix(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """Extract the ML feature matrix from a load_training_frame() output.

    Selects:
    - All numeric columns not in _NON_FEATURE_COLS
    - All CategoricalDtype columns not in _NON_FEATURE_COLS

    Fills numeric NaNs with 0 (same convention as features.multisource_matrix).
    Returns (X, feature_cols).
    """
    numeric_cols = [
        c for c in df.select_dtypes(include="number").columns
        if c not in _NON_FEATURE_COLS
    ]
    cat_cols = [
        c for c in df.select_dtypes(include="category").columns
        if c not in _NON_FEATURE_COLS
    ]
    feature_cols = numeric_cols + cat_cols
    X = df[feature_cols].copy()
    X[numeric_cols] = X[numeric_cols].fillna(0.0)
    return X, feature_cols


# ---------------------------------------------------------------------------
# FraudModel
# ---------------------------------------------------------------------------

@dataclass
class FraudModel:
    """A trained XGBoost fraud ranker.

    Attributes
    ----------
    clf : xgb.XGBClassifier
        Fitted classifier.
    feature_cols : list[str]
        Ordered list of columns used during training.  score() aligns any
        input DataFrame to this list before calling predict_proba.
    train_year : int
        CMS year the model was trained on.
    sources : list[str]
        Data sources included in training (e.g. ["partb", "partd"]).
    """

    clf: xgb.XGBClassifier
    feature_cols: list[str]
    train_year: int
    sources: list[str] = field(default_factory=list)

    def score(self, df: pd.DataFrame) -> pd.Series:
        """Return fraud probability [0, 1] for every row in df.

        Handles missing columns gracefully (filled with 0) so the model
        can score a Part-B-only frame even if it was trained on Part B + D.
        """
        X, _ = _feature_matrix(df)
        X = X.reindex(columns=self.feature_cols, fill_value=0)
        for col in X.select_dtypes(include="object").columns:
            X[col] = X[col].astype("category")
        return pd.Series(
            self.clf.predict_proba(X)[:, 1],
            index=df.index,
            name="fraud_score",
        )

    def rank_providers(self, df: pd.DataFrame) -> pd.DataFrame:
        """Return df sorted by fraud score descending with rank column added."""
        out = df.copy()
        out["fraud_score"] = self.score(df)
        out = out.sort_values("fraud_score", ascending=False).reset_index(drop=True)
        out["fraud_rank"] = out.index + 1
        return out


# ---------------------------------------------------------------------------
# load_training_frame
# ---------------------------------------------------------------------------

def load_training_frame(
    year: int,
    sources: Sequence[str] = ("partb", "partd"),
    cases_path: Path | str = DEFAULT_CASES_PATH,
    min_confidence: str = "high",
    augment_leie_lookahead: int | None = None,
    label_col: str = "fraud_case",
    verbose: bool = True,
) -> pd.DataFrame:
    """Fetch, join, label, and return a training-ready provider DataFrame.

    Parameters
    ----------
    year : int
        CMS release year (e.g. 2013).
    sources : sequence of str
        Source names to load. ``"partb"`` is always included. Unknown names
        raise ValueError. Sources whose cache is missing are skipped with a
        warning so the rest of the pipeline still runs.
    cases_path : path-like
        Path to gold_set.jsonl — the 202 DOJ-case ground truth.
    min_confidence : str
        Minimum defendant NPI confidence for case labels.
    verbose : bool
        Print per-step progress to stdout.

    Returns
    -------
    pd.DataFrame
        One row per provider. Key columns:

        npi                 — provider identifier (str)
        partb_*             — Part B features (always present)
        partd_*             — Part D features (when loaded)
        nppes_*             — NPPES features (when loaded)
        has_partd           — 1 if provider appears in Part D
        fraud_case          — DOJ-case label (0/1)
        label_bucket        — binary: "case_only" or "neither"
                              (LEIE-derived 4-way bucket deprecated 2026-04-25)
        y                   — training target (1 if fraud_case == 1)

        Categorical columns (nppes_credential_bucket, nppes_taxonomy_bucket)
        are cast to pd.CategoricalDtype so XGBoost enable_categorical=True
        works without extra steps.

        Pass to train() or _feature_matrix() to get the model / feature matrix.
    """
    requested = list(sources)
    if "partb" not in requested:
        requested = ["partb"] + requested

    unknown = set(requested) - set(SOURCE_REGISTRY)
    if unknown:
        raise ValueError(f"Unknown sources: {unknown}. Registered: {set(SOURCE_REGISTRY)}")

    # ── 1. Load each source (fetch + feature engineering) ────────────────
    multisource: dict[str, pd.DataFrame | None] = {"partb": None, "partd": None, "dmepos": None}
    left_joins: list[tuple[str, pd.DataFrame]] = []

    for name in requested:
        source = SOURCE_REGISTRY[name](year=year)
        if not source.is_available():
            warnings.warn(f"{name} ({year}): not available. Skipping.")
            continue
        try:
            if verbose:
                print(f"Loading {name}...")
            df_src = source.load()
            if verbose:
                print(f"  {len(df_src):,} rows, {len(df_src.columns)} cols")
            if source.join_strategy == "multisource":
                multisource[name] = df_src
            else:
                left_joins.append((name, df_src))
        except FileNotFoundError as exc:
            warnings.warn(f"{name} ({year}): cache missing — {exc}. Skipping.")
        except Exception as exc:
            warnings.warn(f"{name} ({year}): load failed — {exc}. Skipping.")

    # ── 2. Outer-join Part B / D / DMEPOS on NPI ─────────────────────────
    if multisource["partb"] is None:
        raise RuntimeError("Part B failed to load — cannot build the feature matrix.")

    combined = features.concat_multisource(
        multisource["partb"],
        partd=multisource["partd"],
        dmepos=multisource["dmepos"],
    )
    if verbose:
        print(f"Joined: {len(combined):,} rows × {len(combined.columns)} cols")

    # ── 3. Left-join auxiliary sources (NPPES, future registry data) ──────
    for name, frame in left_joins:
        before = len(combined.columns)
        combined = combined.merge(frame, on="npi", how="left")
        if verbose:
            added = len(combined.columns) - before
            first_feat = next((c for c in frame.columns if c != "npi"), None)
            matched = combined[first_feat].notna().sum() if first_feat else 0
            print(f"  {name}: +{added} cols, {matched:,} rows matched")

    # ── 4. Labels (DOJ cases ± LEIE-historical augmentation) ─────────────
    # LEIE was deprecated as a *truth* metric on 2026-04-25, but
    # LEIE-post-year exclusion (within a few years of the test year) is
    # different: it's a label-augmentation derived from federal action
    # AGAINST a provider.
    combined = label_providers_with_cases(
        combined,
        load_cases(Path(cases_path)),
        puf_year=year,
        min_confidence=min_confidence,
        augment_leie_lookahead=augment_leie_lookahead,
    )
    if label_col not in combined.columns:
        raise ValueError(
            f"label_col={label_col!r} not in label output. "
            f"Available: {[c for c in combined.columns if c.startswith(('fraud_', 'label_'))]}"
        )
    combined["y"] = (combined[label_col] == 1).astype(int)

    if verbose:
        print(f"\nLabel buckets:\n{combined['label_bucket'].value_counts().to_string()}")
        print(f"Training positives ({label_col}=1): {combined['y'].sum()}")
        if augment_leie_lookahead and "label_source" in combined.columns:
            src_counts = combined.loc[combined["y"] == 1, "label_source"].value_counts()
            print(f"By source:\n{src_counts.to_string()}")

    # ── 5. Cast string feature columns to CategoricalDtype ────────────────
    for col in _CATEGORICAL_FEATURE_COLS:
        if col in combined.columns:
            combined[col] = combined[col].astype("category")

    return combined


# ---------------------------------------------------------------------------
# train
# ---------------------------------------------------------------------------

def train(
    df: pd.DataFrame,
    year: int,
    sources: list[str] | None = None,
    n_estimators: int = 400,
    cases_path: Path | str = DEFAULT_CASES_PATH,
    verbose: bool = True,
) -> FraudModel:
    """Train an XGBoost fraud ranker on the output of load_training_frame.

    Literature-supported choices (mirrors paper reproductions in this repo):
    - scale_pos_weight = N_neg / N_pos  — corrects for ~0.016 % fraud base rate
    - max_depth=6, lr=0.05, subsample=0.8, colsample_bytree=0.8  — from ranker.py
    - enable_categorical=True  — uses XGBoost native splits for credential /
                                  taxonomy bucket columns; better tail ranking
                                  than one-hot for the P@K metric
    - tree_method="hist"        — required for enable_categorical; also faster
    - Eval NPIs are held out before fitting  — never train on the honest test set

    Parameters
    ----------
    df : pd.DataFrame
        Output of load_training_frame().
    year : int
        The year df was built for (used to load the eval holdout set).
    sources : list[str], optional
        Source names in df — recorded on the returned FraudModel for bookkeeping.
    n_estimators : int
        Number of boosting rounds.  Reduce to ~50 for smoke tests.
    cases_path : path-like
        Path to gold_set.jsonl, needed to load the eval split for holdout.
    verbose : bool
        Print training progress.

    Returns
    -------
    FraudModel
        Fitted model with .score(df) and .rank_providers(df) methods.
    """
    # ── Hold out test-split NPIs (never train on the honest eval set) ────
    eval_npis = set(load_positives(year, cases_path=cases_path, split="test").keys())
    train_mask = ~df["npi"].isin(eval_npis)
    train_df = df[train_mask]

    if verbose:
        n_held = (~train_mask).sum()
        print(f"Held out {n_held} eval NPIs. Training on {train_mask.sum():,} rows.")

    # ── Feature matrix ────────────────────────────────────────────────────
    X_train, feature_cols = _feature_matrix(train_df)
    y_train = train_df["y"]

    n_pos = int(y_train.sum())
    n_neg = int((y_train == 0).sum())
    scale = n_neg / max(n_pos, 1)

    if verbose:
        print(f"Positives: {n_pos}  |  Negatives: {n_neg:,}  |  scale_pos_weight: {scale:.0f}")
        print(f"Feature matrix: {X_train.shape}")

    # ── XGBoost — literature-supported hyperparameters ────────────────────
    # Parameters match ranker.py reproductions; enable_categorical is new.
    clf = xgb.XGBClassifier(
        n_estimators=n_estimators,
        max_depth=6,
        learning_rate=0.05,
        subsample=0.8,
        colsample_bytree=0.8,
        scale_pos_weight=scale,
        tree_method="hist",         # required for enable_categorical
        enable_categorical=True,
        n_jobs=-1,
        random_state=0,
        verbosity=0,
    )

    if verbose:
        print(f"Training XGBoost (n_estimators={n_estimators})...")

    clf.fit(X_train, y_train)

    if verbose:
        print("Done.")

    return FraudModel(
        clf=clf,
        feature_cols=feature_cols,
        train_year=year,
        sources=list(sources or []),
    )


# ---------------------------------------------------------------------------
# Feature metadata: labels + fraud context for SHAP driver summaries
# ---------------------------------------------------------------------------

# Plain-English name for each feature column.
_FEATURE_LABELS: dict[str, str] = {
    "partb_total_medicare_payment":     "total Medicare payment",
    "partb_n_beneficiaries":            "number of patients",
    "partb_n_services":                 "number of services billed",
    "partb_payment_per_service":        "avg payment per service",
    "partb_services_per_beneficiary":   "services per patient",
    "partb_submitted_to_allowed_ratio": "submitted-to-allowed charge ratio",
    "partb_hcpcs_diversity":            "billing code diversity",
    "partb_n_hcpcs_codes":              "unique billing codes used",
    "partb_bene_avg_age":               "avg patient age",
    "partb_bene_avg_risk_score":        "avg patient risk score",
    "partd_opioid_clm_share":           "opioid prescription share",
    "partd_opioid_la_clm_share":        "long-acting opioid share",
    "partd_antipsychotic_ge65_share":   "antipsychotic share (65+)",
    "partd_brand_clm_share":            "brand-name drug share",
    "partd_cost_per_clm":               "avg drug cost per prescription",
    "partd_clms_per_bene":              "prescriptions per patient",
    "nppes_years_enumerated":           "years since NPI issued",
    "nppes_is_sole_proprietor":         "sole proprietor flag",
    "nppes_is_organization":            "organization NPI",
    "nppes_credential_bucket":          "provider credential",
    "nppes_taxonomy_bucket":            "provider specialty",
}

# Domain interpretation of each feature keyed by (feature, shap_direction).
# shap_direction: "+" = this value pushes fraud score UP, "-" = pushes DOWN.
# The investigator agent uses this to reason about WHY the model flagged someone.
_FEATURE_FRAUD_CONTEXT: dict[str, dict[str, str]] = {
    "partb_total_medicare_payment": {
        "+": "unusually high total billing — common across all fraud types; high-volume schemes need scale",
        "-": "lower billing volume — less consistent with high-volume fraud schemes",
    },
    "partb_payment_per_service": {
        "+": "expensive procedures per visit — upcoding (billing 99215 for routine visits), J-code drug infusions (chemo, Lucentis), or unnecessary procedures",
        "-": "cheap procedures per visit — consistent with routine primary care, not a procedure-upcoding pattern",
    },
    "partb_services_per_beneficiary": {
        "+": "too many procedures per patient — phantom billing (services never rendered) or medically unnecessary services",
        "-": "few procedures per patient — not a high-utilisation fraud pattern",
    },
    "partb_submitted_to_allowed_ratio": {
        "+": "submits far above Medicare's fee schedule — aggressive overbilling; common in DME and infusion fraud",
        "-": "conservative billing — submits close to Medicare's allowed amount",
    },
    "partb_hcpcs_diversity": {
        "+": "wide billing code mix — may indicate legitimate broad practice (investigate further with code-level data)",
        "-": "narrow billing code concentration — focused scheme billing a small set of high-paying codes (e.g. all J-codes, all 99215)",
    },
    "partb_n_beneficiaries": {
        "+": "very large patient panel — possible patient recruiting or phantom beneficiary lists",
        "-": "small patient panel",
    },
    "partd_opioid_clm_share": {
        "+": "high fraction of prescriptions are opioids — pill mill signal; often paired with cash payments and patients traveling long distances",
        "-": "low opioid prescribing — not a pill mill pattern",
    },
    "partd_opioid_la_clm_share": {
        "+": "high long-acting opioid share — highest diversion/abuse risk; OxyContin, fentanyl patches; strong pill mill indicator",
        "-": "low long-acting opioid prescribing",
    },
    "partd_antipsychotic_ge65_share": {
        "+": "high antipsychotic prescribing in elderly patients — nursing home fraud signal; antipsychotics are often used as chemical restraints and billed fraudulently",
        "-": "low elderly antipsychotic prescribing",
    },
    "partd_brand_clm_share": {
        "+": "high brand-name drug share — potential kickback from pharma manufacturer; brand preference without clinical justification",
        "-": "generic-heavy prescribing — lower kickback risk",
    },
    "partd_cost_per_clm": {
        "+": "expensive prescriptions per script — high-cost biologics, brand oncology drugs; may indicate kickback or inappropriate specialty prescribing",
        "-": "cheap prescriptions — routine generics",
    },
    "partd_clms_per_bene": {
        "+": "excessive prescriptions per patient — phantom scripts (prescribing for patients who don't exist or didn't receive drugs)",
        "-": "few prescriptions per patient",
    },
    "nppes_years_enumerated": {
        "+": "long-established provider — elevated risk may reflect combined scheme signals rather than newness",
        "-": "recently enrolled provider — phantom billing / straw NPI signal; new NPI with high billing volume is near-diagnostic of shell practice",
    },
    "nppes_is_sole_proprietor": {
        "+": "sole proprietor entity — easier to set up shell practices; common structure for kickback and phantom billing schemes",
        "-": "not a sole proprietor",
    },
    "nppes_credential_bucket": {
        "+": "credential type associated with elevated fraud risk in training data",
        "-": "credential type associated with lower fraud risk",
    },
    "nppes_taxonomy_bucket": {
        "+": "specialty associated with elevated fraud risk (pain management, cardiology, oncology, podiatry, chiropractic)",
        "-": "specialty associated with lower fraud risk in training data",
    },
}


def _readable(feature: str) -> str:
    return _FEATURE_LABELS.get(feature, feature.replace("_", " "))


def _fraud_context(feature: str, shap_val: float) -> str:
    ctx = _FEATURE_FRAUD_CONTEXT.get(feature)
    if ctx is None:
        return ""
    return ctx["+" if shap_val >= 0 else "-"]


# ---------------------------------------------------------------------------
# score
# ---------------------------------------------------------------------------

def score(
    year: int,
    model: FraudModel,
    sources: Sequence[str] | None = None,
    top_k: int = 1000,
    n_drivers: int = 5,
    cases_path: Path | str = DEFAULT_CASES_PATH,
    verbose: bool = True,
) -> pd.DataFrame:
    """Load a year's provider data, rank by fraud risk, and explain top-K.

    Uses the same load_training_frame() call as train() so features are
    computed identically. SHAP explanations are computed only for the
    top-K providers (computing SHAP for all 956k rows would be slow).
    Percentiles are computed against the full population so the context
    column ("97th percentile for payment_per_service") is meaningful.

    Parameters
    ----------
    year : int
        CMS year to score.
    model : FraudModel
        Trained model from train().
    sources : sequence of str, optional
        Sources to load. Defaults to model.sources. Missing sources are
        skipped with a warning (same graceful degradation as load_training_frame).
    top_k : int
        Number of top-ranked providers to return.
    n_drivers : int
        Number of SHAP drivers to attach to each provider.
    cases_path : path-like
        Path to gold_set.jsonl (used to attach label_bucket for evaluation).
    verbose : bool
        Print progress to stdout.

    Returns
    -------
    pd.DataFrame
        top_k rows, sorted by risk_rank ascending. Columns:

        npi             — provider identifier
        risk_score      — fraud probability [0, 1]  (matches ranker.py naming)
        risk_rank       — 1 = highest risk
        label_bucket    — binary: "case_only" or "neither"
                          (LEIE-derived 4-way bucket deprecated 2026-04-25)
        fraud_case      — 1 if known DOJ defendant
        drivers         — list of n_drivers dicts, each:
                          {
                            "feature":     "partb_payment_per_service",
                            "label":       "avg payment per service",
                            "value":       1247.3,
                            "shap":        0.31,    # positive = pushes toward fraud
                            "percentile":  97,      # vs all providers in this year
                          }
        flag_summary    — one-line plain-English summary of the top drivers,
                          ready for the investigator agent's prompt context
    """
    load_sources = list(sources or model.sources) or ["partb"]
    if verbose:
        print(f"Scoring year={year}, sources={load_sources}, top_k={top_k}")

    df = load_training_frame(
        year,
        sources=load_sources,
        cases_path=cases_path,
        verbose=verbose,
    )

    # ── Rank all providers ────────────────────────────────────────────────
    X_all, _ = _feature_matrix(df)
    X_all = X_all.reindex(columns=model.feature_cols, fill_value=0)
    for col in X_all.select_dtypes(include="object").columns:
        X_all[col] = X_all[col].astype("category")

    df["risk_score"] = model.clf.predict_proba(X_all)[:, 1]
    # Capture sort order BEFORE reset_index so we can reorder X_all in lockstep.
    sort_pos = df["risk_score"].argsort()[::-1].values   # positional indices into X_all
    df = df.iloc[sort_pos].reset_index(drop=True)
    X_all = X_all.iloc[sort_pos].reset_index(drop=True)
    df["risk_rank"] = df.index + 1

    # ── Precompute percentiles for numeric features (full population) ─────
    numeric_feat_cols = [
        c for c in model.feature_cols
        if c in df.columns and pd.api.types.is_numeric_dtype(df[c])
    ]
    percentile_ranks: dict[str, np.ndarray] = {}
    for col in numeric_feat_cols:
        vals = df[col].fillna(0).values
        # percentile of each value within the full population
        order = np.argsort(np.argsort(vals))
        percentile_ranks[col] = (order / max(len(vals) - 1, 1) * 100).round(1)

    # ── SHAP for top-K only ───────────────────────────────────────────────
    top = df.head(top_k).copy()
    X_top = X_all.iloc[:top_k]   # aligned with top after sort_pos reorder above

    if verbose:
        print(f"Computing SHAP for top {top_k} providers...")

    # Use XGBoost native pred_contribs — correctly handles enable_categorical trees.
    # shap.TreeExplainer misreads the internal encoding and produces values that
    # don't reconstruct to the predicted probability.
    dmat_top = xgb.DMatrix(X_top, enable_categorical=True)
    # pred_contribs shape: (top_k, n_features + 1); last col is the bias term
    contribs = model.clf.get_booster().predict(dmat_top, pred_contribs=True)
    shap_matrix = contribs[:, :-1]   # drop bias column; shape: (top_k, n_features)

    # ── Build driver lists ────────────────────────────────────────────────
    drivers_col: list[list[dict[str, Any]]] = []
    summaries: list[str] = []

    for i in range(len(top)):
        sv = shap_matrix[i]
        # top n_drivers by absolute SHAP contribution
        top_idx = np.argsort(np.abs(sv))[::-1][:n_drivers]

        drivers: list[dict[str, Any]] = []
        for j in top_idx:
            feat = model.feature_cols[j]
            raw_val = X_top.iloc[i, j]
            pct = (
                float(percentile_ranks[feat][i])
                if feat in percentile_ranks
                else None
            )
            shap_val = float(sv[j])
            drivers.append({
                "feature":       feat,
                "label":         _readable(feat),
                "value":         float(raw_val) if pd.api.types.is_number(raw_val) else str(raw_val),
                "shap":          round(shap_val, 4),
                "percentile":    pct,
                "fraud_context": _fraud_context(feat, shap_val),
            })
        drivers_col.append(drivers)

        # One-line summary for the investigator agent — includes domain context
        parts = []
        for d in drivers:
            direction = "high" if d["shap"] > 0 else "low"
            pct_str = f" (P{d['percentile']:.0f})" if d["percentile"] is not None else ""
            ctx = f": {d['fraud_context']}" if d["fraud_context"] else ""
            parts.append(f"{direction} {d['label']}{pct_str}{ctx}")
        summaries.append("; ".join(parts))

    top["drivers"]     = drivers_col
    top["flag_summary"] = summaries

    available = [c for c in SCORE_KEEP_COLS if c in top.columns]
    return top[available].reset_index(drop=True)


# ---------------------------------------------------------------------------
# Save / load
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Pairwise ranker (peer-aware)
# ---------------------------------------------------------------------------

@dataclass
class PairwiseFraudModel:
    """XGBoost LambdaMART ranker trained with peer groups as query blocks.

    Groups are peer buckets (e.g. taxonomy × state); within each group
    positives are ranked above negatives. Forces the model to learn
    "what separates a fraudulent cardiologist from a clean one" rather
    than the dominant-specialty archetype the classifier memorises.

    `score(df)` returns raw ranking scores (not probabilities); they are
    only meaningful for ordering, which is what P@K / R@K care about.
    """

    ranker: "xgb.XGBRanker"
    feature_cols: list[str]
    train_year: tuple[int, ...]
    group_col: str

    def score(self, df: pd.DataFrame) -> pd.Series:
        X, _ = _feature_matrix(df)
        X = X.reindex(columns=self.feature_cols, fill_value=0)
        for col in X.select_dtypes(include="object").columns:
            X[col] = X[col].astype("category")
        return pd.Series(
            self.ranker.predict(X),
            index=df.index,
            name="fraud_score",
        )


def train_pairwise(
    df: pd.DataFrame,
    group_col: str = "nppes_taxonomy_bucket",
    eval_npis: set[str] | None = None,
    sample_weight_col: str | None = None,
    n_estimators: int = 400,
    train_year: int | tuple[int, ...] = 0,
    verbose: bool = True,
) -> PairwiseFraudModel:
    """Train a LambdaMART pairwise ranker with peer groups as query blocks.

    Only groups that contain at least one positive AND one negative
    contribute to training — an all-negative group produces no pairs and
    XGBoost will still accept the group but it wastes compute.

    `sample_weight_col` lets the caller down-weight weak positives
    (e.g. borrowed corporate-only cases) without touching the feature matrix.
    """
    if eval_npis:
        df = df[~df["npi"].isin(eval_npis)]

    df = df.dropna(subset=[group_col]).copy()
    df = df.sort_values(group_col, kind="stable").reset_index(drop=True)

    X, feature_cols = _feature_matrix(df)
    y = df["y"].astype(int).values

    # group sizes, in sort order
    group_sizes = df.groupby(group_col, observed=True, sort=False).size().values

    sample_weight = None
    if sample_weight_col and sample_weight_col in df.columns:
        sample_weight = df[sample_weight_col].astype(float).values

    n_pos = int(y.sum())
    if verbose:
        print(
            f"Pairwise train: {len(df):,} rows, {len(group_sizes)} peer groups, "
            f"{n_pos} positives."
        )

    ranker = xgb.XGBRanker(
        objective="rank:pairwise",
        n_estimators=n_estimators,
        max_depth=6,
        learning_rate=0.05,
        subsample=0.8,
        colsample_bytree=0.8,
        tree_method="hist",
        enable_categorical=True,
        n_jobs=-1,
        random_state=0,
        verbosity=0,
    )
    ranker.fit(X, y, group=group_sizes, sample_weight=sample_weight)

    year_tuple = (train_year,) if isinstance(train_year, int) else tuple(train_year)
    return PairwiseFraudModel(
        ranker=ranker,
        feature_cols=feature_cols,
        train_year=year_tuple,
        group_col=group_col,
    )


# ---------------------------------------------------------------------------
# Save / load
# ---------------------------------------------------------------------------

def save_model(model: FraudModel, path: Path | str) -> Path:
    """Persist a trained ``FraudModel`` to ``path`` via joblib.

    The parent directory is created on demand so callers can drop model
    artifacts into fresh paths like ``data/models/foo.joblib``.
    Returns the resolved :class:`Path` for convenience.
    """
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, out)
    return out


def load_model(path: Path | str) -> FraudModel:
    """Load a trained ``FraudModel`` previously written via :func:`save_model`.

    Raises :class:`FileNotFoundError` when the artifact is missing so
    callers (e.g. ``scripts/backtest.py``) can fail loudly rather than
    silently dropping to a mock path.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"FraudModel artifact not found: {p}")
    model = joblib.load(p)
    if not isinstance(model, FraudModel):
        raise TypeError(
            f"Loaded object is {type(model).__name__}, not FraudModel: {p}"
        )
    return model
