"""P4 entity-head pure-function tests — label semantics, leakage controls,
rank metrics. No DuckDB/parquet/model fits (those run in the script's smoke)."""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from scripts.train_entity_head import (
    ENTITY_COLS_RANKER,
    assign_labels,
    feature_matrix,
    load_enforcement_years,
    rank_metrics,
    train_mask_known_by,
)


def _frame(rows):
    base = {"service_class": "personal_care", "entity_type": "2",
            "entity_id": None, "enum_age_years": 5.0,
            "medicaid_total_pay": 100.0}
    return pd.DataFrame([{**base, **r} for r in rows])


# ---------------------------------------------------------------------------
# Label semantics
# ---------------------------------------------------------------------------

def test_load_enforcement_years_takes_earliest(tmp_path):
    p = tmp_path / "labels.jsonl"
    p.write_text("\n".join([
        json.dumps({"org_npi": "1111111111", "disposition": {"type": "plea", "date": "2023-05-01"}}),
        json.dumps({"org_npi": "1111111111", "disposition": {"type": "sentencing", "date": "2024-02-01"}}),
        json.dumps({"org_npi": "2222222222", "disposition": {"type": "settlement", "date": "2020-01-15"}}),
        json.dumps({"org_npi": "3333333333"}),   # no disposition date -> skipped
    ]))
    enf = load_enforcement_years(p)
    assert enf == {"1111111111": 2023, "2222222222": 2020}


def test_assign_labels_window_and_post_enf_drop():
    """enf=2022, window=3: positives are 2019-2021; 2022+ rows are DROPPED."""
    df = _frame([{"npi": "1", "year": y} for y in (2018, 2019, 2020, 2021, 2022, 2023)])
    out = assign_labels(df, {"1": 2022}, window=3)
    got = dict(zip(out["year"], out["is_positive"]))
    assert got == {2018: 0, 2019: 1, 2020: 1, 2021: 1}   # 2022/2023 dropped
    assert set(out["year"]) == {2018, 2019, 2020, 2021}


def test_assign_labels_unlabeled_rows_untouched():
    df = _frame([{"npi": "9", "year": 2020}, {"npi": "9", "year": 2024}])
    out = assign_labels(df, {"1": 2022})
    assert len(out) == 2 and (out["is_positive"] == 0).all()


def test_train_mask_strict_point_in_time():
    """A positive whose enforcement is AFTER the test year is unknown at
    train time -> demoted to negative in the training mask."""
    df = _frame([{"npi": "1", "year": 2019}, {"npi": "2", "year": 2023}])
    out = assign_labels(df, {"1": 2021, "2": 2025}, window=3)
    assert out["is_positive"].tolist() == [1, 1]          # both in-window
    mask = train_mask_known_by(out, test_year=2024)
    got = dict(zip(out["npi"], mask))
    assert got["1"] == 1      # enf 2021 <= 2024: known by then
    assert got["2"] == 0      # enf 2025 > 2024: not knowable -> negative


# ---------------------------------------------------------------------------
# Leakage control in the feature matrix
# ---------------------------------------------------------------------------

def test_feature_matrix_excludes_ranker_cols_by_default():
    df = _frame([{"npi": "1", "year": 2020, "pct_members_anomalous": 0.9,
                  "n_members": 3.0}])
    X, cols = feature_matrix(df, with_ranker=False)
    assert all(c not in cols for c in ENTITY_COLS_RANKER)
    X2, cols2 = feature_matrix(df, with_ranker=True)
    assert "pct_members_anomalous" in cols2


def test_feature_matrix_is_org_and_service_class():
    df = _frame([{"npi": "1", "year": 2020, "entity_type": "2"},
                 {"npi": "2", "year": 2020, "entity_type": "1",
                  "service_class": "lab"}])
    X, _ = feature_matrix(df)
    assert X["is_org"].tolist() == [1.0, 0.0]
    assert X["sc_personal_care"].tolist() == [1.0, 0.0]
    assert X["sc_lab"].tolist() == [0.0, 1.0]


# ---------------------------------------------------------------------------
# Rank metrics
# ---------------------------------------------------------------------------

def test_rank_metrics_basic():
    scores = np.array([0.9, 0.8, 0.7, 0.6, 0.5])
    is_pos = np.array([1, 0, 0, 1, 0])
    m = rank_metrics(scores, is_pos, ks=(1, 2, 4))
    assert m["n_pos"] == 2
    assert m["recall@1"] == pytest.approx(0.5)
    assert m["recall@2"] == pytest.approx(0.5)
    assert m["recall@4"] == pytest.approx(1.0)
    # ranks 1 and 4 -> mean 2.5 of 5 -> pctile 1 - 0.5 = 0.5
    assert m["mean_rank_pctile"] == pytest.approx(0.5)


def test_rank_metrics_no_positives_nan():
    m = rank_metrics(np.array([0.4, 0.2]), np.array([0, 0]), ks=(1,))
    assert np.isnan(m["recall@1"]) and np.isnan(m["mean_rank_pctile"])
