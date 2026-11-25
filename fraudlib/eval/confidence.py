"""Confidence intervals for recall@K metrics."""
from __future__ import annotations

import numpy as np
from scipy import stats


def binomial_ci(tp: int, n: int, alpha: float = 0.05) -> tuple[float, float]:
    """Wilson score interval for recall = tp/n."""
    if n == 0:
        return (0.0, 0.0)
    return stats.binom.interval(1 - alpha, n, tp / n, loc=0) if tp > 0 else (0.0, stats.binom.ppf(1 - alpha / 2, n, 0.5) / n)


def wilson_ci(tp: int, n: int, alpha: float = 0.05) -> tuple[float, float]:
    """Wilson score CI — better for small proportions."""
    if n == 0:
        return (0.0, 0.0)
    z = stats.norm.ppf(1 - alpha / 2)
    p = tp / n
    denom = 1 + z**2 / n
    centre = (p + z**2 / (2 * n)) / denom
    spread = z * np.sqrt((p * (1 - p) + z**2 / (4 * n)) / n) / denom
    return (max(0.0, centre - spread), min(1.0, centre + spread))


def bootstrap_recall_ci(
    scores: np.ndarray,
    y_eval: np.ndarray,
    k: int,
    n_boot: int = 1000,
    alpha: float = 0.05,
    seed: int = 0,
) -> tuple[float, float, float]:
    """Bootstrap CI for recall@K. Returns (mean, lo, hi)."""
    rng = np.random.default_rng(seed)
    n = len(scores)
    recalls = []
    for _ in range(n_boot):
        idx = rng.choice(n, size=n, replace=True)
        s = scores[idx]
        y = y_eval[idx]
        order = np.argsort(-s)
        tp_k = int(y[order][:k].sum())
        n_pos = max(int(y.sum()), 1)
        recalls.append(tp_k / n_pos)
    recalls = np.array(recalls)
    lo = float(np.percentile(recalls, 100 * alpha / 2))
    hi = float(np.percentile(recalls, 100 * (1 - alpha / 2)))
    return (float(recalls.mean()), lo, hi)
