"""Disk cache for the X feature matrix.

Building X from a 3.8M-row training dataframe takes ~240s. The matrix is
deterministic given (years, rules version, satellite extras), so subsequent
ablation runs with the same configuration can load from disk in seconds.

Files live at ``data/cache/feature_matrices/`` as ``.npz`` bundles:
    {X: float32 (N, F), feat_cols: object (F,), meta: dict}
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

MATRIX_CACHE = Path("data/cache/feature_matrices")


def cache_key(role: str, years: list[int], rules_version: str, extras: bool) -> str:
    """Build a stable filename stem for the X cache."""
    yr_lo, yr_hi = min(years), max(years)
    yr_part = f"{yr_lo}" if yr_lo == yr_hi else f"{yr_lo}-{yr_hi}"
    extras_part = "true" if extras else "false"
    return f"xmat_{role}_{yr_part}_rules-{rules_version}_extras-{extras_part}"


def load_or_build_matrix(
    df: pd.DataFrame,
    role: str,
    years: list[int],
    rules_version: str,
    extras: bool = True,
    verbose: bool = True,
) -> tuple[np.ndarray, list[str]]:
    """Return (X, feat_cols), reading from disk if a matching cache exists.

    On miss, computes via the same column-selection logic used by
    ``train_filtered_ranker._feature_matrix`` and writes to disk.
    """
    from scripts.experiments.train_filtered_ranker import _feature_matrix

    MATRIX_CACHE.mkdir(parents=True, exist_ok=True)
    path = MATRIX_CACHE / f"{cache_key(role, years, rules_version, extras)}.npz"

    if path.exists():
        if verbose:
            print(f"  X[{role}]: loaded cache ({path.name})")
        bundle = np.load(path, allow_pickle=True)
        X = bundle["X"]
        feat_cols = list(bundle["feat_cols"])
        return X, feat_cols

    X_df, feat_cols = _feature_matrix(df)
    X = X_df.reindex(columns=feat_cols, fill_value=0).fillna(0).values.astype(np.float32)

    np.savez(
        path,
        X=X,
        feat_cols=np.array(feat_cols, dtype=object),
        meta=np.array({"role": role, "years": years, "rules_version": rules_version,
                       "extras": extras, "n_rows": int(X.shape[0]),
                       "n_cols": int(X.shape[1])}, dtype=object),
    )
    if verbose:
        print(f"  X[{role}]: built + saved ({path.name}, {X.shape[0]:,} x {X.shape[1]})")
    return X, feat_cols


def drop_satellite_columns(X: np.ndarray, feat_cols: list[str]) -> tuple[np.ndarray, list[str]]:
    """Drop cl_*, dme_*, op_* columns. Used by --extras without runs."""
    keep_idx = [i for i, c in enumerate(feat_cols)
                if not (c.startswith("cl_") or c.startswith("dme_") or c.startswith("op_"))]
    kept = [feat_cols[i] for i in keep_idx]
    return X[:, keep_idx], kept
