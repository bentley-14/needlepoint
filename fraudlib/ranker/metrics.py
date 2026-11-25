"""Uniform evaluation + side-by-side comparison with paper-reported metrics."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Optional

import numpy as np
import pandas as pd
from sklearn.metrics import (
    average_precision_score,
    f1_score,
    precision_recall_curve,
    precision_score,
    recall_score,
    roc_auc_score,
    roc_curve,
)


@dataclass
class Report:
    auc: float
    ap: float
    f1: float
    precision: float
    recall: float
    threshold: float
    n_pos: int
    n_neg: int

    def as_dict(self) -> dict[str, float]:
        return {
            "AUC": self.auc,
            "AP": self.ap,
            "F1": self.f1,
            "Precision": self.precision,
            "Recall": self.recall,
            "Threshold": self.threshold,
            "N_pos": self.n_pos,
            "N_neg": self.n_neg,
        }


def report(y_true: Iterable[int], y_score: Iterable[float], threshold: Optional[float] = None) -> Report:
    """Core metric report. If ``threshold`` is None, pick the one that
    maximizes F1 (paper 15 explicitly optimizes decision threshold)."""
    y_true = np.asarray(list(y_true))
    y_score = np.asarray(list(y_score))
    auc = float(roc_auc_score(y_true, y_score))
    ap = float(average_precision_score(y_true, y_score))

    if threshold is None:
        prec, rec, thr = precision_recall_curve(y_true, y_score)
        # Align shapes: precision_recall_curve returns one more prec/rec
        # value than thresholds — drop the last.
        f1s = 2 * prec[:-1] * rec[:-1] / np.clip(prec[:-1] + rec[:-1], 1e-9, None)
        best = int(np.argmax(f1s)) if len(f1s) else 0
        threshold = float(thr[best]) if len(thr) else 0.5

    y_pred = (y_score >= threshold).astype(int)
    return Report(
        auc=auc,
        ap=ap,
        f1=float(f1_score(y_true, y_pred, zero_division=0)),
        precision=float(precision_score(y_true, y_pred, zero_division=0)),
        recall=float(recall_score(y_true, y_pred, zero_division=0)),
        threshold=float(threshold),
        n_pos=int((y_true == 1).sum()),
        n_neg=int((y_true == 0).sum()),
    )


def compare_to_reported(
    reproduced: Report | Mapping[str, float],
    reported: Mapping[str, float],
    tolerance: float = 0.05,
) -> pd.DataFrame:
    """Return a side-by-side frame: metric | reported | reproduced | delta | within_tolerance."""
    if isinstance(reproduced, Report):
        reproduced = reproduced.as_dict()

    rows = []
    for metric, reported_val in reported.items():
        repro_val = reproduced.get(metric)
        if repro_val is None:
            rows.append(
                {
                    "metric": metric,
                    "reported": reported_val,
                    "reproduced": None,
                    "delta": None,
                    "within_tol": None,
                }
            )
            continue
        delta = repro_val - reported_val
        within = abs(delta) <= tolerance
        rows.append(
            {
                "metric": metric,
                "reported": reported_val,
                "reproduced": repro_val,
                "delta": delta,
                "within_tol": within,
            }
        )
    return pd.DataFrame(rows)


def threshold_sweep(y_true: Iterable[int], y_score: Iterable[float]) -> pd.DataFrame:
    """Full ROC sweep for plotting and paper-15-style threshold analysis."""
    y_true = np.asarray(list(y_true))
    y_score = np.asarray(list(y_score))
    fpr, tpr, thr = roc_curve(y_true, y_score)
    return pd.DataFrame({"threshold": thr, "fpr": fpr, "tpr": tpr})


def cv_by_npi(
    X: pd.DataFrame,
    y: pd.Series,
    groups: pd.Series,
    model_factory,
    n_splits: int = 5,
) -> dict[str, float]:
    """5-fold by-NPI CV AUC. ``groups`` is the NPI series (same length as X/y).

    Paper 10's headline: stratified CV leaks when multiple rows share an
    NPI. Replacing ``StratifiedKFold`` with ``GroupKFold(on=npi)`` gives an
    honest AUC. This helper implements the honest version.
    """
    from sklearn.model_selection import GroupKFold

    gkf = GroupKFold(n_splits=n_splits)
    X_arr = X.values if hasattr(X, "values") else np.asarray(X)
    y_arr = y.values if hasattr(y, "values") else np.asarray(y)
    g_arr = groups.values if hasattr(groups, "values") else np.asarray(groups)

    fold_aucs: list[float] = []
    for tr, te in gkf.split(X_arr, y_arr, groups=g_arr):
        if y_arr[te].sum() == 0 or y_arr[tr].sum() == 0:
            continue
        clf = model_factory()
        clf.fit(X_arr[tr], y_arr[tr])
        if hasattr(clf, "predict_proba"):
            score = clf.predict_proba(X_arr[te])[:, 1]
        elif hasattr(clf, "decision_function"):
            score = clf.decision_function(X_arr[te])
        else:
            score = clf.predict(X_arr[te])
        fold_aucs.append(float(roc_auc_score(y_arr[te], score)))

    return {
        "mean_auc": float(np.mean(fold_aucs)) if fold_aucs else float("nan"),
        "std_auc": float(np.std(fold_aucs)) if fold_aucs else float("nan"),
        "n_folds": len(fold_aucs),
    }


def precision_at_k(y_true: Iterable[int], y_score: Iterable[float], k: int) -> float:
    """Fraction of the top-k scored items that are true positives."""
    y_true = np.asarray(list(y_true))
    y_score = np.asarray(list(y_score))
    k = min(k, len(y_score))
    if k == 0:
        return float("nan")
    top_idx = np.argsort(-y_score, kind="stable")[:k]
    return float(y_true[top_idx].sum()) / k


def recall_at_k(y_true: Iterable[int], y_score: Iterable[float], k: int) -> float:
    """Fraction of true positives that land in the top-k."""
    y_true = np.asarray(list(y_true))
    y_score = np.asarray(list(y_score))
    n_pos = int(y_true.sum())
    if n_pos == 0:
        return float("nan")
    k = min(k, len(y_score))
    top_idx = np.argsort(-y_score, kind="stable")[:k]
    return float(y_true[top_idx].sum()) / n_pos


def rank_report(
    y_true: Iterable[int],
    y_score: Iterable[float],
    ks: Iterable[int] = (100, 500, 1000),
) -> dict[str, float]:
    """Bundle P@K, R@K for several K values and total positives."""
    y_true = np.asarray(list(y_true))
    y_score = np.asarray(list(y_score))
    out = {"n": int(len(y_true)), "n_pos": int(y_true.sum())}
    for k in ks:
        out[f"P@{k}"] = precision_at_k(y_true, y_score, k)
        out[f"R@{k}"] = recall_at_k(y_true, y_score, k)
    return out


def shap_feature_rank(
    clf,
    X: pd.DataFrame,
    top_pct: float = 0.125,
    sample: int = 10_000,
    random_state: int = 0,
) -> list[str]:
    """Rank features by mean |SHAP value| on a sampled subset; return top-``top_pct``.

    Uses ``shap.TreeExplainer``. Only call on tree-based classifiers
    (XGBoost / RandomForest / LightGBM). Paper 08's "novel feature
    selection" is the SHAP-ranked version; ``feature_importances_`` is a
    weaker proxy.
    """
    import shap

    if len(X) > sample:
        X_sub = X.sample(n=sample, random_state=random_state)
    else:
        X_sub = X

    explainer = shap.TreeExplainer(clf)
    shap_vals = explainer.shap_values(X_sub)
    if isinstance(shap_vals, list):  # binary classifier returns list of 2
        shap_vals = shap_vals[1] if len(shap_vals) > 1 else shap_vals[0]
    mean_abs = np.abs(shap_vals).mean(axis=0)
    imp = pd.Series(mean_abs, index=X.columns).sort_values(ascending=False)
    top_k = max(1, int(round(len(imp) * top_pct)))
    return imp.head(top_k).index.tolist()
