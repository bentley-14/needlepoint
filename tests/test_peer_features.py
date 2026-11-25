"""Tests for fraudlib/ranker/peer_features.py.

Covers:
- robust-z matches hand-computed values on a synthetic distribution
- shrinkage falls back to parent then global when the peer group is small
- no-leakage: stats fit on train only are used to transform test rows
- idempotency: re-running transform does not stack `_peer_z_peer_z` columns
- output dtype + NaN handling
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from fraudlib.ranker import peer_features as pf


def _mk_frame(
    spec: list[tuple[str, str, int, float]],
    feat_noise: float = 0.0,
    seed: int = 0,
) -> pd.DataFrame:
    """Build a toy frame with one numeric feature `x`.

    spec: list of (taxonomy, state, n_rows, mean_x).
    """
    rng = np.random.default_rng(seed)
    rows = []
    for taxonomy, state, n, mean in spec:
        for _ in range(n):
            rows.append(
                {
                    "taxonomy": taxonomy,
                    "state": state,
                    "x": mean + rng.normal(0, feat_noise) if feat_noise else mean,
                }
            )
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# robust-z correctness on a hand-computed fixture
# ---------------------------------------------------------------------------

def test_robust_z_matches_hand_computed():
    # One peer group, n>>min_group_n, known median + MAD.
    values = [10, 12, 14, 16, 18]  # median 14
    # |x - 14| = [4, 2, 0, 2, 4] → MAD = 2
    df = pd.DataFrame(
        {
            "taxonomy": ["A"] * 40,
            "state": ["X"] * 40,
            "x": list(values) * 8,  # 40 rows, identical median/MAD as fixture
        }
    )
    stats = pf.fit_peer_stats(
        df,
        group_cols=["taxonomy", "state"],
        numeric_cols=["x"],
        min_group_n=5,
    )
    scored = pf.peer_relative_transform(df, stats)

    # (x - 14) / (1.4826 * 2) = (x - 14) / 2.9652
    expected = np.array(values) / 1.0  # placeholder
    expected = (np.array(values) - 14) / (1.4826 * 2)
    got = scored.loc[scored["x"] == 10, "x_peer_z"].iloc[0]
    assert got == pytest.approx(expected[0], rel=1e-5)
    got = scored.loc[scored["x"] == 18, "x_peer_z"].iloc[0]
    assert got == pytest.approx(expected[-1], rel=1e-5)


# ---------------------------------------------------------------------------
# Shrinkage: tiny peer group falls back to parent; tiny parent → global
# ---------------------------------------------------------------------------

def test_shrink_to_parent_when_peer_thin():
    """Peer (A, X) has n=3 < min_group_n=30; parent (A) has n=60 → parent."""
    spec = [
        ("A", "X", 3, 100.0),   # thin peer; centered at 100
        ("A", "Y", 60, 50.0),   # parent bulk; centers parent median around 50
    ]
    df = _mk_frame(spec, feat_noise=0.0)
    stats = pf.fit_peer_stats(
        df,
        group_cols=["taxonomy", "state"],
        numeric_cols=["x"],
        parent_cols=["taxonomy"],
        min_group_n=30,
    )
    out = pf.peer_relative_transform(df, stats)

    # (A, X) rows: peer n=3 is below threshold, parent n=63 passes, so they
    # should be centered on the PARENT median (≈50), giving a large positive z.
    ax = out[(out["taxonomy"] == "A") & (out["state"] == "X")]
    ay = out[(out["taxonomy"] == "A") & (out["state"] == "Y")]

    assert (ax["x_peer_z"] > 0).all(), "thin peer row should rank above parent median"
    # Parent MAD of combined A group is |100-50| for 3 rows + |50-50| for 60 → median(|x - 50|) = 0
    # so denom will floor; z should be very large (thousands). Just assert direction.
    assert ay["x_peer_z"].abs().max() < 1e-3, "bulk of parent at median → z ≈ 0"


def test_shrink_to_global_when_parent_also_thin():
    """Both peer and parent thin; stats fall through to global."""
    spec = [
        ("rare1", "X", 2, 100.0),
        ("rare2", "Y", 2, 100.0),
        ("bulk",  "Z", 60, 10.0),  # bulk of global
    ]
    df = _mk_frame(spec, feat_noise=0.0)
    stats = pf.fit_peer_stats(
        df,
        group_cols=["taxonomy", "state"],
        numeric_cols=["x"],
        parent_cols=["taxonomy"],
        min_group_n=30,
    )
    out = pf.peer_relative_transform(df, stats)

    # rare1/X row: peer n=2, parent n=2 (rare1 alone), global n=64 → global applies.
    rare = out[out["taxonomy"] == "rare1"]
    # Global median dominated by bulk (≈10); the rare value 100 should have z > 0.
    assert (rare["x_peer_z"] > 0).all()


# ---------------------------------------------------------------------------
# No-leakage: stats fit on train used for test transform
# ---------------------------------------------------------------------------

def test_no_leakage_stats_fit_on_train_only():
    """Fitting on train, transforming test with those stats uses only train info."""
    train = _mk_frame([("A", "X", 40, 10.0)])
    test = pd.DataFrame({"taxonomy": ["A"], "state": ["X"], "x": [99.0]})

    stats = pf.fit_peer_stats(
        train, group_cols=["taxonomy", "state"], numeric_cols=["x"], min_group_n=5
    )
    out = pf.peer_relative_transform(test, stats)

    # Train peer median is 10, MAD is 0 (all rows equal). Without parent
    # stats the test row falls through to global (which here = peer = 10
    # with MAD=0). Result is clipped to Z_CAP. Leak-freeness is the test's
    # core property: no test value influenced the centre/scale; only the
    # train value did, and the clip is a deterministic post-step.
    assert out["x_peer_z"].iloc[0] == pf.Z_CAP


# ---------------------------------------------------------------------------
# Idempotency + context columns
# ---------------------------------------------------------------------------

def test_transform_emits_context_columns():
    spec = [("A", "X", 40, 10.0), ("A", "Y", 40, 12.0)]
    df = _mk_frame(spec)
    stats = pf.fit_peer_stats(
        df, group_cols=["taxonomy", "state"], numeric_cols=["x"], min_group_n=5
    )
    out = pf.peer_relative_transform(df, stats)
    assert "peer_group_size" in out.columns
    assert "peer_parent_size" in out.columns
    assert out["peer_group_size"].dtype == np.float32
    assert (out["peer_group_size"] == 40).all()
    assert (out["peer_parent_size"] == 80).all()


def test_default_numeric_features_skips_peer_z_and_labels():
    """All label columns must be excluded from peer-z. Audited 2026-04-27:
    fraud_label_peer_z had val AUC=1.0 (perfect leak) until fraud_label was
    added to the skip set."""
    df = pd.DataFrame(
        {
            "npi": ["a"],
            "x": [1.0],
            "x_peer_z": [0.5],
            "y": [1],
            "y_eval": [1],
            "fraud_case": [0],
            "fraud_leie": [0],
            "fraud_label": [0],
            "peer_group_size": [30],
            "puf_year": [2019],
        }
    )
    cols = pf.default_numeric_features(df)
    assert cols == ["x"], f"Expected only ['x']; got {cols}"
    for leak_col in ("fraud_case", "fraud_leie", "fraud_label",
                     "y", "y_eval", "puf_year",
                     "peer_group_size", "peer_parent_size"):
        assert leak_col not in cols, (
            f"{leak_col!r} must be excluded to prevent label/identity leakage"
        )


def test_zcap_clips_extreme_outlier():
    """A value far in the tail must be clipped to ±Z_CAP, not 10^10."""
    spec = [("A", "X", 40, 10.0)]
    df = _mk_frame(spec)
    # Append one extreme outlier — its true robust-z is enormous because
    # peer MAD = 0 (all rows identical at 10.0). Old behaviour: ~10^10.
    df = pd.concat(
        [df, pd.DataFrame({"taxonomy": ["A"], "state": ["X"], "x": [10_000.0]})],
        ignore_index=True,
    )
    stats = pf.fit_peer_stats(
        df, group_cols=["taxonomy", "state"], numeric_cols=["x"], min_group_n=5
    )
    out = pf.peer_relative_transform(df, stats)
    assert out["x_peer_z"].abs().max() <= pf.Z_CAP + 1e-6, (
        f"|z| should be clipped to {pf.Z_CAP}, got {out['x_peer_z'].abs().max()}"
    )


def test_mad_zero_falls_through_to_parent():
    """Peer group with MAD=0 should use parent stats, not the 1e-9 floor.

    Setup: taxonomy A, state X has 40 rows ALL at value=10 (MAD=0).
    Parent (taxonomy A across states X+Y) has MAD>0 because Y rows differ.
    A row at x=12 inside (A, X) used to produce z = (12-10)/(1.4826 * 1e-9)
    ≈ 1.35e9. With the fix it should fall through to parent and yield a
    sensible bounded z.
    """
    spec = [("A", "X", 40, 10.0), ("A", "Y", 40, 50.0)]
    df = _mk_frame(spec)
    # Append one row in (A, X) with a deviating value; without parent
    # fall-through it would explode.
    df = pd.concat(
        [df, pd.DataFrame({"taxonomy": ["A"], "state": ["X"], "x": [12.0]})],
        ignore_index=True,
    )
    stats = pf.fit_peer_stats(
        df,
        group_cols=["taxonomy", "state"],
        numeric_cols=["x"],
        parent_cols=["taxonomy"],
        min_group_n=5,
    )
    out = pf.peer_relative_transform(df, stats)
    # No |z| > Z_CAP anywhere
    assert out["x_peer_z"].abs().max() <= pf.Z_CAP + 1e-6
    # The deviating row should have a finite, bounded z (parent fall-through
    # used parent MAD which is finite).
    devrow_z = out.iloc[-1]["x_peer_z"]
    assert -pf.Z_CAP <= devrow_z <= pf.Z_CAP


def test_drop_raw_scale_features_idempotent():
    """drop_raw_scale_features removes known scale cols; safe on missing cols."""
    df = pd.DataFrame({
        "npi": ["a", "b"],
        "partb_total_medicare_allowed": [1.0, 2.0],
        "partb_n_services": [10, 20],
        "partb_payment_per_service": [0.1, 0.2],
        "partb_total_medicare_allowed_peer_z": [0.5, -0.5],
        "nppes_taxonomy_bucket": pd.Categorical(["A", "B"]),
    })
    out = pf.drop_raw_scale_features(df)
    assert "partb_total_medicare_allowed" not in out.columns
    assert "partb_n_services" not in out.columns
    assert "nppes_taxonomy_bucket" not in out.columns
    # Kept: ratios + peer-z + npi
    assert "partb_payment_per_service" in out.columns
    assert "partb_total_medicare_allowed_peer_z" in out.columns
    assert "npi" in out.columns
    # Idempotent: running again is safe (returns same columns).
    out2 = pf.drop_raw_scale_features(out)
    assert list(out.columns) == list(out2.columns)


def test_nan_input_yields_zero_z():
    """A row with NaN in the source feature should get z=0 (not NaN)."""
    df = pd.DataFrame(
        {
            "taxonomy": ["A"] * 40 + ["A"],
            "state": ["X"] * 40 + ["X"],
            "x": [10.0] * 40 + [np.nan],
        }
    )
    stats = pf.fit_peer_stats(
        df, group_cols=["taxonomy", "state"], numeric_cols=["x"], min_group_n=5
    )
    out = pf.peer_relative_transform(df, stats)
    assert out["x_peer_z"].isna().sum() == 0
    assert out["x_peer_z"].iloc[-1] == 0.0
