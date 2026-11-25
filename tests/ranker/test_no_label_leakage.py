"""Leakage regression test for the ranker pipeline.

Asserts that no feature column passed to the model has high correlation
with the label `y_eval`. This catches the class of bug PR #58 fixed
(fraud_label was leaking with correlation 0.92).

Run with: pytest tests/ranker/test_no_label_leakage.py

Skips automatically if the feature_frames cache or rules file isn't
available locally — full audit must be run in an environment with the
data fixtures (CI on a workstation with `data/cache/feature_frames`
populated).
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

# Hard threshold: anything above this is treated as leakage.
# 0.5 is well above any plausible legitimate signal (the strongest
# legitimate feature in the May 2026 baseline was ~0.06) but well
# below the 0.92 that fraud_label produced.
MAX_FEAT_CORR = 0.5

# Suspect column names that should NEVER appear in feat_cols
LABEL_COLS_FORBIDDEN = {
    "y", "y_eval", "y_doj", "y_leie_only",
    "fraud_case", "fraud_label", "fraud_leie",
    "archetype", "label_bucket", "label_source", "label_confidence",
    "case_tier", "case_ids", "case_fraud_types",
    # Model output scores must not re-enter as features
    "score_xgb", "score_anomaly", "score_neural",
    "score_rrf", "rrf_rank",
}

REPO_ROOT = Path(__file__).resolve().parents[2]
FEATURE_CACHE = REPO_ROOT / "data" / "cache" / "feature_frames"
RULES_PATH = REPO_ROOT / "results" / "rule_candidates_v3" / "set_cover_rules_v3.json"


def _have_fixtures(year: int) -> bool:
    return (FEATURE_CACHE / f"features_{year}.parquet").exists() and RULES_PATH.exists()


@pytest.mark.skipif(not _have_fixtures(2023),
                    reason="feature_frames cache or rules file not available")
def test_no_label_columns_in_feat_cols():
    """No suspect label columns survive _feature_matrix exclusion."""
    import sys
    sys.path.insert(0, str(REPO_ROOT))
    from fraudlib.ranker.feature_frames import load_feature_frame, stamp_labels_and_filter
    from fraudlib.eval.archetypes import archetype_lookup
    from scripts.experiments.run_ablation import _feature_matrix

    rules = json.loads(RULES_PATH.read_text())["rules"]
    fraud_npis: set[str] = set()
    for y in range(2013, 2023):
        fraud_npis |= set(archetype_lookup(y).keys())

    df = load_feature_frame(2023)
    df = stamp_labels_and_filter(df, 2023, rules, fraud_npis=fraud_npis)
    _, feat_cols = _feature_matrix(df)

    forbidden_in_feats = LABEL_COLS_FORBIDDEN & set(feat_cols)
    assert not forbidden_in_feats, (
        f"Label/score columns leaked into feat_cols: {sorted(forbidden_in_feats)}. "
        f"They must be added to scripts.experiments.train_filtered_ranker._NON_FEATURE_COLS."
    )


@pytest.mark.skipif(not _have_fixtures(2023),
                    reason="feature_frames cache or rules file not available")
def test_no_high_correlation_features():
    """No feat_col has |Pearson corr| > MAX_FEAT_CORR with y_eval.

    Leakage like fraud_label (r=0.92) is caught immediately. Real signal
    in the May 2026 baseline maxes around 0.06, leaving a 7x margin to
    the threshold.
    """
    import sys
    sys.path.insert(0, str(REPO_ROOT))
    from fraudlib.ranker.feature_frames import load_feature_frame, stamp_labels_and_filter
    from fraudlib.eval.archetypes import archetype_lookup
    from scripts.experiments.run_ablation import _feature_matrix

    rules = json.loads(RULES_PATH.read_text())["rules"]
    fraud_npis: set[str] = set()
    for y in range(2013, 2023):
        fraud_npis |= set(archetype_lookup(y).keys())

    df = load_feature_frame(2023)
    df = stamp_labels_and_filter(df, 2023, rules, fraud_npis=fraud_npis)
    _, feat_cols = _feature_matrix(df)
    y = df["y_eval"].astype(float).values

    high_corr_feats: list[tuple[str, float]] = []
    for col in feat_cols:
        s = df[col]
        if not pd.api.types.is_numeric_dtype(s):
            continue
        with np.errstate(invalid="ignore"):
            c = float(np.corrcoef(s.fillna(0).values, y)[0, 1])
        if np.isnan(c):
            continue
        if abs(c) > MAX_FEAT_CORR:
            high_corr_feats.append((col, c))

    assert not high_corr_feats, (
        f"{len(high_corr_feats)} feat_col(s) exceeded |corr|={MAX_FEAT_CORR}: "
        f"{high_corr_feats}. Highest correlations indicate label leakage. "
        f"Investigate which column is mirroring y_eval."
    )


@pytest.mark.skipif(not _have_fixtures(2023),
                    reason="feature_frames cache not available")
def test_feature_frame_cache_is_label_free():
    """Cached feature frames must not contain any label columns."""
    df = pd.read_parquet(FEATURE_CACHE / "features_2023.parquet", columns=None)
    leaked = LABEL_COLS_FORBIDDEN & set(df.columns)
    assert not leaked, (
        f"feature_frames cache leaked label columns: {sorted(leaked)}. "
        f"The cache build (load_feature_frame) must strip these before saving."
    )
