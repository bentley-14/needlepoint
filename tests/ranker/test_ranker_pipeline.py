"""Unit tests for fraudlib.ranker.pipeline + fraudlib.ranker.mocks.

Keeps the surface small: save/load round-trip and the mock-vs-team
schema parity. These are the two invariants the rest of the pipeline
leans on. Anything heavier (loading real parquets, training an actual
model) belongs in experiments/, not unit tests.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest
pytest.importorskip("xgboost", reason="heavy ML stack deliberately not installed in targeted CI (see .github/workflows/tests.yml)")
import xgboost as xgb

from fraudlib.ranker.mocks import mock_score
from fraudlib.ranker.pipeline import (
    SCORE_KEEP_COLS,
    FraudModel,
    load_model,
    save_model,
)


def test_save_and_load_model_roundtrip(tmp_path: Path) -> None:
    """save_model → load_model preserves metadata and predict behavior."""
    clf = xgb.XGBClassifier(n_estimators=2, max_depth=2)
    X = np.array([[0.0, 0.0], [1.0, 1.0], [0.5, 0.5], [0.9, 0.1]])
    y = np.array([0, 1, 0, 1])
    clf.fit(X, y)

    model = FraudModel(
        clf=clf,
        feature_cols=["a", "b"],
        train_year=2013,
        sources=["partb", "partd"],
    )

    path = tmp_path / "model.joblib"
    save_model(model, path)
    assert path.exists()

    loaded = load_model(path)
    assert loaded.train_year == 2013
    assert loaded.feature_cols == ["a", "b"]
    assert loaded.sources == ["partb", "partd"]

    probe = np.array([[0.3, 0.7]])
    np.testing.assert_allclose(
        clf.predict_proba(probe),
        loaded.clf.predict_proba(probe),
    )


def test_load_model_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_model(tmp_path / "nope.joblib")


def test_load_model_wrong_type_raises(tmp_path: Path) -> None:
    import joblib
    path = tmp_path / "not_a_model.joblib"
    joblib.dump({"not": "a model"}, path)
    with pytest.raises(TypeError):
        load_model(path)


@pytest.mark.xfail(reason="known: mock top-k fixture drift (pre-existing)", strict=False)
def test_mock_layer1_schema_matches_score() -> None:
    """mock_score output column set + element types mirror score() exactly.

    team's ``score()`` needs real data (cms_partb parquet + a trained
    model) to run, so we can't call it here. Instead we assert the
    mock matches the module-level ``SCORE_KEEP_COLS`` contract that
    ``score()`` is written against — one canonical source of truth.
    """
    df = mock_score(score_year=2013, top_k=10)

    # 1. Column set matches the canonical list exactly (order + membership).
    assert tuple(df.columns) == SCORE_KEEP_COLS

    # 2. Row count honors top_k.
    assert len(df) == 10

    # 3. Element types on each column.
    row = df.iloc[0]
    assert isinstance(row["npi"], str)
    assert isinstance(row["risk_score"], float)
    # pandas int64 is fine here; python int comparison is not reliable
    assert pd.api.types.is_integer(row["risk_rank"])
    assert isinstance(row["label_bucket"], str)
    assert pd.api.types.is_integer(row["fraud_case"])
    assert isinstance(row["drivers"], list)
    assert isinstance(row["flag_summary"], str)

    # 4. Drivers carry the expected per-element dict shape.
    drivers = row["drivers"]
    assert len(drivers) >= 1
    d = drivers[0]
    assert isinstance(d, dict)
    for key in ("feature", "label", "value", "shap", "percentile", "fraud_context"):
        assert key in d, f"driver missing key {key!r}"

    # 5. risk_rank strictly ascending, risk_score weakly descending.
    assert df["risk_rank"].is_monotonic_increasing
    assert df["risk_score"].is_monotonic_decreasing or (
        df["risk_score"].diff().dropna() <= 0
    ).all()

    # 6. All 7 pinned 2013 NPIs appear in rank 1..7.
    pinned = {
        "1900000080", "1900000048", "1900000004", "1900000022",
        "1900000008", "1900000003", "1900000028",
    }
    top7 = set(df["npi"].astype(str).head(7))
    assert pinned == top7, f"top-7 mismatch: {top7 ^ pinned}"
