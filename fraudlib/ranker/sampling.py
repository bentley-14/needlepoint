"""Class-imbalance handling used across papers 15, 17, 18, 14.

The FAU group's canonical toolbox:
  * Random Undersampling (RUS) at a target majority:minority ratio
  * SMOTE synthetic oversampling
  * Cost-sensitive class weights

Paper 15 sweeps RUS ratios {50:50, 65:35, 75:25, 80:20, 90:10} and finds
80:20 optimal — we keep that sweep as the default space.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from imblearn.over_sampling import SMOTE
from sklearn.utils import resample

RUS_RATIOS = ((50, 50), (65, 35), (75, 25), (80, 20), (90, 10))


def random_undersample(
    x: pd.DataFrame,
    y: pd.Series,
    majority_pct: int = 80,
    random_state: int = 0,
) -> tuple[pd.DataFrame, pd.Series]:
    """Downsample the majority class to ``majority_pct:100-majority_pct``.

    Works for heavily imbalanced CMS data where fraud prevalence is well
    below 1%. Returns a new frame/Series (does not mutate input).
    """
    if not 50 <= majority_pct < 100:
        raise ValueError("majority_pct must be in [50, 100).")

    idx_pos = y[y == 1].index
    idx_neg = y[y == 0].index
    n_pos = len(idx_pos)
    if n_pos == 0:
        raise ValueError("No positive (fraud) rows in y.")

    # Target size for the negatives such that neg/(neg+pos) = majority_pct/100.
    minority_pct = 100 - majority_pct
    target_neg = int(round(n_pos * majority_pct / minority_pct))
    target_neg = min(target_neg, len(idx_neg))

    idx_neg_sampled = resample(
        idx_neg,
        replace=False,
        n_samples=target_neg,
        random_state=random_state,
    )
    keep = idx_pos.union(idx_neg_sampled)
    return x.loc[keep].copy(), y.loc[keep].copy()


def smote_resample(
    x: pd.DataFrame,
    y: pd.Series,
    random_state: int = 0,
) -> tuple[pd.DataFrame, pd.Series]:
    """SMOTE the minority class to parity. Columns preserved."""
    sm = SMOTE(random_state=random_state)
    x_new, y_new = sm.fit_resample(x.values, y.values)
    return pd.DataFrame(x_new, columns=x.columns), pd.Series(y_new, name=y.name)


def class_weight_dict(y: pd.Series) -> dict[int, float]:
    """Inverse-frequency class weights suitable for cost-sensitive learning."""
    counts = y.value_counts()
    total = counts.sum()
    return {int(c): float(total / (2.0 * counts[c])) for c in counts.index}
