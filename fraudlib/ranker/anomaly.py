"""Peer-group-conditioned Isolation Forest for archetype-agnostic fraud signal.

Layer 1 of the IRM + anomaly hybrid ranker. Catches novel
fraud archetypes by construction — fits one IsolationForest per peer
group on the *normal* training population, scores test rows by their
deviation from peer norms. Doesn't learn fraud archetypes, so doesn't
fail when fraud archetypes shift.

Public API
----------
PeerIsolationForest(group_cols, parent_cols, min_group_n, contamination,
                     n_estimators, random_state)
    .fit(df, numeric_cols, label_col='y')
        Fit one IF per peer group on rows where ``label_col == 0``.
        Groups smaller than ``min_group_n`` fall back to a parent-taxonomy
        IF; below parent threshold, fall back to a global IF. Same
        shrinkage cascade as ``peer_features.fit_peer_stats``.
    .score(df) -> pd.Series
        Return a per-row anomaly score (higher = more anomalous).
        Polarity-aligned with the supervised ranker (higher score = more
        suspicious), so it can be stacked with classifier probabilities
        directly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest


# ---------------------------------------------------------------------------
# Default constants — match peer_features for consistency
# ---------------------------------------------------------------------------

DEFAULT_GROUP_COLS = ("nppes_taxonomy_bucket", "partb_state")
DEFAULT_PARENT_COLS = ("nppes_taxonomy_bucket",)
DEFAULT_MIN_GROUP_N = 30


@dataclass
class PeerIsolationForest:
    """One IsolationForest per peer group, with parent + global fallbacks.

    Parameters
    ----------
    group_cols
        Columns defining the finest peer group (default: taxonomy × state).
    parent_cols
        Columns defining the parent group, used when the peer group has
        too few rows to fit a useful IF (default: taxonomy alone).
    min_group_n
        Minimum rows required to fit a peer-level IF. Groups below this
        threshold fall through to the parent IF; parents below this
        threshold fall through to the global IF.
    contamination
        Expected fraction of anomalies in the *normal* training population.
        Smaller values = stricter anomaly cutoff. Default 0.05 (5%) is
        conservative; 0.1 if we want a wider net.
    n_estimators, random_state
        Standard IsolationForest knobs.

    Attributes (set by .fit)
    ------------------------
    peer_models, parent_models, global_model
        Fitted IF per scope; lookup cascade is peer → parent → global.
    feature_cols
        Columns used at fit time. Score-time inputs must include the same
        columns.
    """

    group_cols: tuple[str, ...] = DEFAULT_GROUP_COLS
    parent_cols: tuple[str, ...] = DEFAULT_PARENT_COLS
    min_group_n: int = DEFAULT_MIN_GROUP_N
    contamination: float = 0.05
    n_estimators: int = 200
    random_state: int = 0

    peer_models: dict[tuple, IsolationForest] = field(default_factory=dict)
    parent_models: dict[tuple, IsolationForest] = field(default_factory=dict)
    global_model: IsolationForest | None = None
    feature_cols: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Fit
    # ------------------------------------------------------------------

    def fit(
        self,
        df: pd.DataFrame,
        numeric_cols: Sequence[str],
        label_col: str = "y",
        verbose: bool = False,
    ) -> "PeerIsolationForest":
        """Fit IFs on the *normal* (label == 0) subset of ``df``.

        Excluding training positives from the fit prevents the "normal"
        envelope from being inflated by known fraud — a typical dataset-
        contamination issue with anomaly detectors. The number of training
        positives is small (~500 across all years) so this barely shrinks
        the per-group fit population.
        """
        self.feature_cols = list(numeric_cols)
        normal_mask = df[label_col].astype(int) == 0
        normal_df = df.loc[normal_mask].reset_index(drop=True)

        # ── Global fallback ──────────────────────────────────────────
        self.global_model = self._fit_one(normal_df, "global", verbose=verbose)

        # ── Parent groups ────────────────────────────────────────────
        parent_groups = list(self.parent_cols)
        for key, sub in normal_df.groupby(parent_groups, observed=True, dropna=False):
            if not isinstance(key, tuple):
                key = (key,)
            if len(sub) < self.min_group_n:
                continue
            self.parent_models[key] = self._fit_one(
                sub, f"parent={key}", verbose=verbose
            )

        # ── Peer groups ──────────────────────────────────────────────
        peer_groups = list(self.group_cols)
        for key, sub in normal_df.groupby(peer_groups, observed=True, dropna=False):
            if not isinstance(key, tuple):
                key = (key,)
            if len(sub) < self.min_group_n:
                continue
            self.peer_models[key] = self._fit_one(
                sub, f"peer={key}", verbose=verbose
            )

        if verbose:
            print(
                f"[PeerIF] Fit complete: {len(self.peer_models)} peer + "
                f"{len(self.parent_models)} parent + 1 global IFs"
            )
        return self

    def _fit_one(
        self, sub: pd.DataFrame, label: str, verbose: bool
    ) -> IsolationForest:
        X = sub[self.feature_cols].fillna(0.0).to_numpy(dtype=np.float32)
        clf = IsolationForest(
            n_estimators=self.n_estimators,
            contamination=self.contamination,
            random_state=self.random_state,
            n_jobs=-1,
        )
        clf.fit(X)
        if verbose:
            print(f"[PeerIF]   {label}: fit on {len(sub):,} rows")
        return clf

    # ------------------------------------------------------------------
    # Score
    # ------------------------------------------------------------------

    def score(self, df: pd.DataFrame) -> pd.Series:
        """Per-row anomaly score (higher = more anomalous).

        We negate sklearn's ``decision_function`` (where higher = more
        normal) so the output polarity matches the supervised ranker
        (higher = more suspicious). This lets the meta-learner fuse
        scores without sign flips.

        Cascade per row:
            1. If peer group has a fitted IF, use it.
            2. Else if parent group has one, use it.
            3. Else use the global IF.
        """
        if self.global_model is None:
            raise RuntimeError("PeerIsolationForest.fit() must be called first.")

        n = len(df)
        scores = np.full(n, np.nan, dtype=np.float64)
        X = df[self.feature_cols].fillna(0.0).to_numpy(dtype=np.float32)

        # Build keys for each row
        peer_keys = self._row_keys(df, self.group_cols)
        parent_keys = self._row_keys(df, self.parent_cols)

        # Step 1: try peer-level IFs (group rows by peer key for batched scoring)
        peer_used = np.zeros(n, dtype=bool)
        for key, model in self.peer_models.items():
            mask = np.array([k == key for k in peer_keys])
            if not mask.any():
                continue
            scores[mask] = -model.decision_function(X[mask])
            peer_used |= mask

        # Step 2: fall back to parent-level IFs for rows without peer
        parent_used = np.zeros(n, dtype=bool)
        remaining = ~peer_used
        for key, model in self.parent_models.items():
            mask = np.array([k == key for k in parent_keys]) & remaining
            if not mask.any():
                continue
            scores[mask] = -model.decision_function(X[mask])
            parent_used |= mask

        # Step 3: global fallback for everything else
        global_mask = ~(peer_used | parent_used)
        if global_mask.any():
            scores[global_mask] = -self.global_model.decision_function(X[global_mask])

        return pd.Series(scores, index=df.index, name="anomaly_iforest_score")

    @staticmethod
    def _row_keys(df: pd.DataFrame, cols: tuple[str, ...]) -> list[tuple]:
        if not cols:
            return [() for _ in range(len(df))]
        # Convert per-row tuple of col values; cast to str to handle categorical
        # / nullable dtypes uniformly.
        as_strings = df[list(cols)].astype(str)
        return [tuple(row) for row in as_strings.itertuples(index=False, name=None)]


# ---------------------------------------------------------------------------
# Convenience factory
# ---------------------------------------------------------------------------

def fit_peer_iforest(
    df: pd.DataFrame,
    numeric_cols: Sequence[str],
    *,
    group_cols: Sequence[str] = DEFAULT_GROUP_COLS,
    parent_cols: Sequence[str] = DEFAULT_PARENT_COLS,
    min_group_n: int = DEFAULT_MIN_GROUP_N,
    contamination: float = 0.05,
    n_estimators: int = 200,
    label_col: str = "y",
    random_state: int = 0,
    verbose: bool = False,
) -> PeerIsolationForest:
    """One-call helper — fit a PeerIsolationForest with sensible defaults.

    Mirrors the signature of ``peer_features.fit_peer_stats`` so the two
    layers of the hybrid ranker can be invoked the same way.
    """
    pif = PeerIsolationForest(
        group_cols=tuple(group_cols),
        parent_cols=tuple(parent_cols),
        min_group_n=min_group_n,
        contamination=contamination,
        n_estimators=n_estimators,
        random_state=random_state,
    )
    return pif.fit(df, numeric_cols=numeric_cols, label_col=label_col, verbose=verbose)
