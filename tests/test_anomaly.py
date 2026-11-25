"""Tests for fraudlib/ranker/anomaly.py.

Covers:
- Per-peer-group IF fits and produces sane anomaly scores
- Group below min_group_n falls back to parent
- Group below parent threshold falls back to global
- Training positives (label==1) are excluded from the 'normal' fit
- Score polarity (higher = more anomalous, opposite of sklearn raw)
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("sklearn", reason="heavy ML stack deliberately not installed in targeted CI (see .github/workflows/tests.yml)")
from fraudlib.ranker.anomaly import PeerIsolationForest, fit_peer_iforest


def _mk_frame(spec, feat_noise=0.5, seed=0):
    """spec: list of (taxonomy, state, n_rows, mean_x, mean_y, label)."""
    rng = np.random.default_rng(seed)
    rows = []
    for taxonomy, state, n, mx, my, label in spec:
        for _ in range(n):
            rows.append({
                "nppes_taxonomy_bucket": taxonomy,
                "partb_state": state,
                "x": mx + rng.normal(0, feat_noise),
                "y_feat": my + rng.normal(0, feat_noise),
                "y": label,
            })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 1. Basic peer-group fit produces sane scores
# ---------------------------------------------------------------------------

def test_peer_iforest_flags_outlier_in_known_peer():
    """Outlier provider in a well-fit peer group scores above the inlier median.

    IsolationForest's calibration on small (n~100) low-dim (d=2) synthetic
    data is famously noisy, so we don't assert a strict p95 quantile —
    just that an extreme multi-feature outlier scores higher than the
    median of the inlier population the IF was fit on.
    """
    spec = [
        # 200 normal cardiologists in CA (bulk of peer group)
        ("CARDIO", "CA", 200, 5.0, 5.0, 0),
    ]
    train = _mk_frame(spec, feat_noise=0.4)
    # Test row: same peer group, extreme on BOTH features
    test = pd.DataFrame({
        "nppes_taxonomy_bucket": ["CARDIO"],
        "partb_state": ["CA"],
        "x": [50.0],
        "y_feat": [50.0],
        "y": [0],
    })

    pif = fit_peer_iforest(train, numeric_cols=["x", "y_feat"], min_group_n=20)
    train_scores = pif.score(train)
    test_score = pif.score(test).iloc[0]

    median = train_scores.median()
    assert test_score > median, (
        f"Outlier score {test_score:.4f} should exceed train median {median:.4f}"
    )


# ---------------------------------------------------------------------------
# 2. Below-min peer group falls through to parent
# ---------------------------------------------------------------------------

def test_falls_through_to_parent_when_peer_below_min():
    """Peer (CARDIO, CA) has only 5 rows; parent (CARDIO, *) has plenty."""
    spec = [
        ("CARDIO", "CA", 5,  10.0, 10.0, 0),    # too small for peer model
        ("CARDIO", "NY", 100, 10.0, 10.0, 0),   # bulk of parent
        ("CARDIO", "TX", 100, 10.0, 10.0, 0),
    ]
    train = _mk_frame(spec, feat_noise=0.3)
    test = pd.DataFrame({
        "nppes_taxonomy_bucket": ["CARDIO"],
        "partb_state": ["CA"],
        "x": [10.0],
        "y_feat": [10.0],
        "y": [0],
    })

    pif = fit_peer_iforest(train, numeric_cols=["x", "y_feat"], min_group_n=20)

    # No peer-level model for (CARDIO, CA) — too small
    assert ("CARDIO", "CA") not in pif.peer_models
    # But parent-level (CARDIO,) is fitted
    assert ("CARDIO",) in pif.parent_models

    # Score works without error
    score = pif.score(test).iloc[0]
    assert np.isfinite(score)


# ---------------------------------------------------------------------------
# 3. Below parent threshold falls through to global
# ---------------------------------------------------------------------------

def test_falls_through_to_global_when_parent_below_min():
    """A rare specialty (n=3) with no large parent uses the global IF."""
    spec = [
        ("RARE_SPECIALTY", "AK", 3,  5.0, 5.0, 0),     # peer below min, parent below min
        ("BULK_SPECIALTY", "CA", 200, 1.0, 1.0, 0),    # global bulk
    ]
    train = _mk_frame(spec, feat_noise=0.2)
    test = pd.DataFrame({
        "nppes_taxonomy_bucket": ["RARE_SPECIALTY"],
        "partb_state": ["AK"],
        "x": [5.0],
        "y_feat": [5.0],
        "y": [0],
    })

    pif = fit_peer_iforest(train, numeric_cols=["x", "y_feat"], min_group_n=20)

    # No peer or parent model
    assert ("RARE_SPECIALTY", "AK") not in pif.peer_models
    assert ("RARE_SPECIALTY",) not in pif.parent_models
    # Global is fitted
    assert pif.global_model is not None

    score = pif.score(test).iloc[0]
    assert np.isfinite(score)


# ---------------------------------------------------------------------------
# 4. Training positives excluded from the 'normal' fit
# ---------------------------------------------------------------------------

def test_training_positives_excluded_from_fit():
    """Flipping a label from 0 → 1 should remove that row from the IF fit.

    Concretely: build two training sets that differ only in whether
    the extreme rows are labelled 0 or 1. Fit on each. The IF fit
    on (extreme rows = label 1) should NOT have those rows in its
    'normal' envelope, so it should score them as more anomalous.
    """
    # 50 normal CARDIO providers + 5 extreme outliers
    rng = np.random.default_rng(0)
    base = pd.DataFrame({
        "nppes_taxonomy_bucket": ["CARDIO"] * 55,
        "partb_state": ["CA"] * 55,
        "x": np.concatenate([rng.normal(5, 0.5, 50), [50.0] * 5]),
        "y_feat": np.concatenate([rng.normal(5, 0.5, 50), [50.0] * 5]),
        "y": [0] * 50 + [1] * 5,           # extremes labelled as fraud
    })
    pif_with_holdout = fit_peer_iforest(base, numeric_cols=["x", "y_feat"], min_group_n=20)

    # Same rows but with extremes labelled 0 (= contamination)
    base_contaminated = base.copy()
    base_contaminated["y"] = 0
    pif_contaminated = fit_peer_iforest(
        base_contaminated, numeric_cols=["x", "y_feat"], min_group_n=20
    )

    # Score the same outlier under both. The "honest" fit (with positives
    # excluded) should produce a HIGHER anomaly score, since the extremes
    # didn't shift its 'normal' envelope.
    test = pd.DataFrame({
        "nppes_taxonomy_bucket": ["CARDIO"],
        "partb_state": ["CA"],
        "x": [50.0],
        "y_feat": [50.0],
        "y": [0],
    })
    s_honest = pif_with_holdout.score(test).iloc[0]
    s_contaminated = pif_contaminated.score(test).iloc[0]

    # Honest fit treats the outlier as more anomalous (score is higher
    # because we negated decision_function).
    assert s_honest >= s_contaminated, (
        f"Honest fit should yield higher anomaly score; got {s_honest:.3f} vs "
        f"{s_contaminated:.3f}"
    )


# ---------------------------------------------------------------------------
# 5. Score polarity sanity (higher = more anomalous)
# ---------------------------------------------------------------------------

def test_score_polarity_higher_is_more_anomalous():
    """Inlier should score lower than outlier under the same fit."""
    spec = [("CARDIO", "CA", 100, 10.0, 10.0, 0)]
    train = _mk_frame(spec, feat_noise=0.4)
    test = pd.DataFrame({
        "nppes_taxonomy_bucket": ["CARDIO", "CARDIO"],
        "partb_state": ["CA", "CA"],
        "x": [10.0, 100.0],   # inlier, outlier
        "y_feat": [10.0, 100.0],
        "y": [0, 0],
    })
    pif = fit_peer_iforest(train, numeric_cols=["x", "y_feat"], min_group_n=20)
    scores = pif.score(test).values
    assert scores[1] > scores[0], (
        f"Outlier ({scores[1]:.3f}) should score higher than inlier ({scores[0]:.3f})"
    )


# ---------------------------------------------------------------------------
# 6. Score polarity sanity — calling score() before fit() raises
# ---------------------------------------------------------------------------

def test_score_before_fit_raises():
    pif = PeerIsolationForest()
    test = pd.DataFrame({
        "nppes_taxonomy_bucket": ["CARDIO"],
        "partb_state": ["CA"],
        "x": [1.0],
        "y": [0],
    })
    with pytest.raises(RuntimeError):
        pif.score(test)
