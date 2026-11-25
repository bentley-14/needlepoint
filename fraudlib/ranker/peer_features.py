"""Peer-relative feature transform for the fraud ranker.

Replaces raw feature values with robust z-scores computed within peer groups
(specialty × state), with empirical-Bayes shrinkage toward the parent group
(specialty alone) and then the global population when a peer group is too small
to estimate median / MAD reliably.

Rationale: the global-percentile ranker memorizes specialty archetypes — a 99th-percentile
oncologist on $/service looks identical to a 99th-percentile podiatrist. Peer-
relative z-scores strip the specialty signature so the model learns deviations
from peers rather than which specialty most often bills fraudulently.

Public API
----------
fit_peer_stats(df, group_cols, numeric_cols, min_group_n=30)
    Compute median / MAD per peer group + parent + global. Fit on training only.

peer_relative_transform(df, stats, suffix='_peer_z')
    Apply the robust z-score transform using previously-fit stats. Original
    columns are retained; one new column per numeric feature is appended.

PeerStats
    The fitted statistics object.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

import numpy as np
import pandas as pd

# 1 / scipy.stats.norm.ppf(0.75); converts MAD to a robust std estimate.
MAD_TO_STD = 1.4826

# Prevent division-by-zero when MAD == 0 (feature constant within a peer group).
# When triggered, the resulting z-score is meaningless — we fall through to
# parent / global stats first (use_peer / use_parent require mad > 0), and
# clamp the final output to ±Z_CAP as a safety net.
_MAD_FLOOR = 1e-9
Z_CAP = 10.0


# Raw billing-volume + bene-count features that anchor the model on practice
# scale rather than fraud-specific signal. The 2026-04-26 SHAP analysis showed
# these as the dominant top-10 drivers on 2019 — almost all false positives
# were "very-high-billing" providers. Drop them via drop_raw_scale_features()
# to force the model onto ratios + peer-relative deviations. Their _peer_z
# versions are kept (they are the only scale signal that should remain).
_RAW_SCALE_FEATURES: tuple[str, ...] = (
    # Part B raw $ / volume
    "partb_total_medicare_allowed",
    "partb_total_medicare_payment",
    "partb_total_medicare_standardized",
    "partb_n_services",
    "partb_n_beneficiaries",
    "partb_n_hcpcs_codes",
    "partb_tot_sbmtd_chrg",
    # Part B median amounts
    "partb_med_mdcr_alowd_amt",
    "partb_med_mdcr_pymt_amt",
    "partb_med_mdcr_stdzd_amt",
    "partb_med_tot_benes",
    "partb_med_tot_hcpcs_cds",
    # Part B drug aggregates
    "partb_drug_tot_benes",
    "partb_drug_tot_hcpcs_cds",
    # Part B beneficiary-mix counts (all variants — not all may be present
    # in any given panel; missing names are silently skipped by the helper)
    "partb_bene_dual_cnt",
    "partb_bene_ndual_cnt",
    "partb_bene_male_cnt",
    "partb_bene_female_cnt",
    "partb_bene_age_lt_65_cnt",
    "partb_bene_age_65_74_cnt",
    "partb_bene_age_75_84_cnt",
    "partb_bene_age_gt_84_cnt",
    "partb_bene_race_wht_cnt",
    "partb_bene_race_blck_cnt",
    "partb_bene_race_hspnc_cnt",
    "partb_bene_race_api_cnt",
    "partb_bene_race_natind_cnt",
    "partb_bene_race_othr_cnt",
    # Part D raw scale
    "partd_tot_drug_cst",
    "partd_tot_clms",
    # Carry-over: taxonomy bucket as a model split feature
    # (still used to define peer groups; just not as a model input)
    "nppes_taxonomy_bucket",
)


def drop_raw_scale_features(df: pd.DataFrame) -> pd.DataFrame:
    """Return ``df`` with all raw billing-volume / bene-count columns dropped.

    Columns in ``_RAW_SCALE_FEATURES`` not present on ``df`` are silently
    skipped, so the helper is safe across panel-schema changes.
    """
    drop = [c for c in _RAW_SCALE_FEATURES if c in df.columns]
    if not drop:
        return df
    return df.drop(columns=drop)


@dataclass
class PeerStats:
    """Fitted median / MAD tables for peer, parent, and global scopes.

    Attributes
    ----------
    numeric_cols : list[str]
        Columns the transform will produce peer-z scores for.
    group_cols : list[str]
        Columns that define the finest peer group (e.g. ['taxonomy', 'state']).
    parent_cols : list[str]
        Columns that define the parent group (e.g. ['taxonomy']). Used as
        fallback when the peer group is thin.
    peer : pd.DataFrame
        One row per peer group, indexed by `group_cols`, columns
        `{feat}_median`, `{feat}_mad`, `n`.
    parent : pd.DataFrame
        Same shape, indexed by `parent_cols`.
    global_ : pd.Series
        Columns `{feat}_median`, `{feat}_mad`, `n` (scalar row).
    min_group_n : int
        Minimum peer-group size to trust peer stats; below this, shrink toward
        parent. Same threshold applies to parent → global.
    """

    numeric_cols: list[str]
    group_cols: list[str]
    parent_cols: list[str]
    peer: pd.DataFrame
    parent: pd.DataFrame
    global_: pd.Series
    min_group_n: int = 30


def _robust_stats(frame: pd.DataFrame, cols: Sequence[str]) -> dict[str, float]:
    """Compute median + MAD for every numeric column. Returns a flat dict.

    MAD = median(|x - median(x)|). Robust to skew + outliers (fraudulent
    providers are themselves tail cases — using the mean would let them
    shift the centre of the peer group).
    """
    out: dict[str, float] = {"n": float(len(frame))}
    for col in cols:
        if col not in frame.columns:
            out[f"{col}_median"] = np.nan
            out[f"{col}_mad"] = np.nan
            continue
        vals = pd.to_numeric(frame[col], errors="coerce").dropna().values
        if len(vals) == 0:
            out[f"{col}_median"] = np.nan
            out[f"{col}_mad"] = np.nan
            continue
        med = float(np.median(vals))
        mad = float(np.median(np.abs(vals - med)))
        out[f"{col}_median"] = med
        out[f"{col}_mad"] = mad
    return out


def fit_peer_stats(
    df: pd.DataFrame,
    group_cols: Sequence[str],
    numeric_cols: Sequence[str],
    parent_cols: Sequence[str] | None = None,
    min_group_n: int = 30,
) -> PeerStats:
    """Fit peer / parent / global median + MAD tables on the training frame.

    Fit stats on training data only. Passing the combined train+test frame
    would leak test values into the peer centre; stats derived from training
    generalise to 2019 rows naturally because `state` + `taxonomy` are static.
    """
    group_cols = list(group_cols)
    parent_cols = list(parent_cols) if parent_cols is not None else group_cols[:1]
    numeric_cols = list(numeric_cols)

    peer_rows: list[dict] = []
    for key, sub in df.groupby(group_cols, observed=True, dropna=False):
        stats = _robust_stats(sub, numeric_cols)
        if not isinstance(key, tuple):
            key = (key,)
        for col, val in zip(group_cols, key):
            stats[col] = val
        peer_rows.append(stats)
    peer = pd.DataFrame(peer_rows).set_index(group_cols) if peer_rows else pd.DataFrame()

    parent_rows: list[dict] = []
    for key, sub in df.groupby(parent_cols, observed=True, dropna=False):
        stats = _robust_stats(sub, numeric_cols)
        if not isinstance(key, tuple):
            key = (key,)
        for col, val in zip(parent_cols, key):
            stats[col] = val
        parent_rows.append(stats)
    parent = (
        pd.DataFrame(parent_rows).set_index(parent_cols) if parent_rows else pd.DataFrame()
    )

    global_ = pd.Series(_robust_stats(df, numeric_cols))

    return PeerStats(
        numeric_cols=numeric_cols,
        group_cols=group_cols,
        parent_cols=parent_cols,
        peer=peer,
        parent=parent,
        global_=global_,
        min_group_n=min_group_n,
    )


def _lookup(table: pd.DataFrame, keys: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    """Left-join per-row stats from `table` onto `keys[cols]`; missing → NaN."""
    if table.empty:
        return pd.DataFrame(index=keys.index, columns=table.columns)
    idx_keys = keys[cols].copy()
    # align dtypes with the fitted index to avoid NaN joins on category mismatch
    for c, lvl in zip(cols, table.index.names):
        level_vals = table.index.get_level_values(lvl)
        idx_keys[c] = idx_keys[c].astype(level_vals.dtype, errors="ignore")
    if len(cols) == 1:
        idx = pd.Index(idx_keys[cols[0]].values)
    else:
        idx = pd.MultiIndex.from_frame(idx_keys)
    return table.reindex(idx).set_axis(keys.index)


def peer_relative_transform(
    df: pd.DataFrame,
    stats: PeerStats,
    suffix: str = "_peer_z",
) -> pd.DataFrame:
    """Add peer-relative robust z-score columns to `df`.

    For each numeric column `c`, emit `c{suffix}` = (x - centre) / scale, where
    (centre, scale) come from the first scope whose group size >= min_group_n:
        1. peer group (e.g. taxonomy × state)
        2. parent group (e.g. taxonomy alone)
        3. global

    Also adds three peer-group context features (once per frame, not per
    numeric column): `peer_group_size`, `peer_parent_size`. These let the
    downstream model learn to weight peer-relative deviations by how well-
    estimated the peer centre is.

    The input frame's raw columns are retained unchanged.
    """
    out = df.copy()
    out_index = out.index

    peer_lookup = _lookup(stats.peer, out, stats.group_cols)
    parent_lookup = _lookup(stats.parent, out, stats.parent_cols)

    peer_n = pd.to_numeric(peer_lookup.get("n"), errors="coerce").fillna(0.0)
    parent_n = pd.to_numeric(parent_lookup.get("n"), errors="coerce").fillna(0.0)

    # Group-size masks. The MAD-zero check is applied per-feature below
    # because mad_peer differs across columns (a peer group can have MAD=0
    # for one feature and a healthy MAD for another).
    peer_n_ok = peer_n >= stats.min_group_n
    parent_n_ok = parent_n >= stats.min_group_n

    # Build all new columns in a flat dict, then concat once at the end.
    # Column-by-column ``out["new_col"] = ...`` fragments the DataFrame into
    # one block per column; on a frame with 100+ peer-z columns this slows
    # downstream operations (XGBoost DMatrix conversion, .to_numpy())
    # by 30-50%. Single pd.concat at the end keeps the output frame
    # consolidated.
    new_cols: dict[str, np.ndarray] = {}
    for col in stats.numeric_cols:
        if col not in out.columns:
            continue
        med_peer = pd.to_numeric(peer_lookup.get(f"{col}_median"), errors="coerce")
        mad_peer = pd.to_numeric(peer_lookup.get(f"{col}_mad"), errors="coerce")
        med_parent = pd.to_numeric(parent_lookup.get(f"{col}_median"), errors="coerce")
        mad_parent = pd.to_numeric(parent_lookup.get(f"{col}_mad"), errors="coerce")
        med_global = float(stats.global_.get(f"{col}_median", np.nan))
        mad_global = float(stats.global_.get(f"{col}_mad", np.nan))

        # Per-feature MAD>0 gates. A peer group with MAD=0 on this column
        # produces meaningless z-scores; cascade to parent, then global.
        # Without this gate, the prior 1e-9 floor produced |z| ≈ 10^10 and
        # those spurious values landed in the top-K (2026-04-26 audit).
        peer_mad_ok = peer_n_ok & (mad_peer.fillna(0) > 0)
        parent_mad_ok = (~peer_mad_ok) & parent_n_ok & (mad_parent.fillna(0) > 0)

        centre = pd.Series(med_global, index=out_index, dtype="float64")
        scale = pd.Series(mad_global, index=out_index, dtype="float64")
        centre = centre.where(~parent_mad_ok, med_parent)
        scale = scale.where(~parent_mad_ok, mad_parent)
        centre = centre.where(~peer_mad_ok, med_peer)
        scale = scale.where(~peer_mad_ok, mad_peer)

        x = pd.to_numeric(out[col], errors="coerce")
        scale_denom = MAD_TO_STD * scale.replace(0, np.nan).fillna(_MAD_FLOOR)
        z = (x - centre) / scale_denom
        # Clip |z| ≤ Z_CAP — robust z beyond ±10 is float / floor artefact,
        # not signal. XGBoost splits ±10 the same as ±10^10; smaller value
        # keeps SHAP outputs human-readable and avoids float-precision noise.
        z = z.clip(lower=-Z_CAP, upper=Z_CAP)
        new_cols[f"{col}{suffix}"] = z.fillna(0.0).astype(np.float32).to_numpy()

    new_cols["peer_group_size"] = peer_n.astype(np.float32).to_numpy()
    new_cols["peer_parent_size"] = parent_n.astype(np.float32).to_numpy()

    # Single allocation: concat the new block once. This keeps `out`
    # contiguous-blocked instead of fragmenting it column-by-column.
    new_block = pd.DataFrame(new_cols, index=out.index, copy=False)
    return pd.concat([out, new_block], axis=1, copy=False)


def default_numeric_features(df: pd.DataFrame) -> list[str]:
    """Helper: pick numeric feature columns from a load_training_frame() output.

    Excludes label / identifier columns and any column that already ends in
    a peer-z suffix (idempotency when the transform is re-run).
    """
    skip = {
        "npi", "y", "y_eval",
        # All label columns produced by labels_v2.label_providers_with_cases.
        # Peer-z'ing a 0/1 label produces a +10σ value for every positive in
        # a peer group — a perfect leak (fraud_label_peer_z had val AUC=1.0
        # in a 2026-04-27 audit). Skip them all.
        "fraud", "fraud_case", "fraud_leie", "fraud_label",
        "peer_group_size", "peer_parent_size",
        "puf_year",  # diagnostic column — must not become a feature (leaks year identity)
    }
    cols = []
    for c in df.select_dtypes(include=[np.number]).columns:
        if c in skip or c.endswith("_peer_z"):
            continue
        cols.append(c)
    return cols
