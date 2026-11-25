"""Tests for fraudlib.eval.evalkit.npi_symmetric_holdout."""
from __future__ import annotations

import pandas as pd
import pytest

from fraudlib.eval.evalkit import (
    NpiSymmetricSplit,
    _npi_in_val,
    npi_symmetric_holdout,
)


def _mk_train(npis: list[str], years: list[int]) -> pd.DataFrame:
    rows = [{"npi": n, "puf_year": y, "x": 1.0} for n in npis for y in years]
    return pd.DataFrame(rows)


def test_three_npi_sets_are_disjoint():
    train = _mk_train([f"{i:010d}" for i in range(200)], [2014, 2015, 2016])
    test = pd.DataFrame({"npi": [f"{i:010d}" for i in range(180, 220)], "x": 0.0})
    test_npis = set(test["npi"])

    split = npi_symmetric_holdout(train, test, test_npis=test_npis, val_hash_mod=5)

    train_npis = set(split.train["npi"])
    val_npis = set(split.val["npi"])
    test_seen = set(split.test["npi"])

    assert train_npis & val_npis == set()
    assert train_npis & test_seen == set()
    assert val_npis & test_seen == set()
    assert split.summary["train_val_overlap"] == 0
    assert split.summary["train_test_overlap"] == 0


def test_test_npis_dropped_from_train_under_all_years():
    npis = [f"{i:010d}" for i in range(50)]
    train = _mk_train(npis, [2014, 2015, 2016])
    test_npis = {npis[0], npis[1], npis[2]}
    test = pd.DataFrame({"npi": list(test_npis), "x": 0.0})

    split = npi_symmetric_holdout(train, test, test_npis=test_npis)
    combined = set(split.train["npi"]) | set(split.val["npi"])
    assert combined.isdisjoint(test_npis)
    assert split.summary["n_dropped_test_holdout"] == 3 * 3  # 3 NPIs × 3 years


def test_split_is_deterministic():
    train = _mk_train([f"{i:010d}" for i in range(100)], [2018])
    test = pd.DataFrame({"npi": ["9999999999"], "x": 0.0})
    a = npi_symmetric_holdout(train, test, test_npis=set())
    b = npi_symmetric_holdout(train, test, test_npis=set())
    assert set(a.train["npi"]) == set(b.train["npi"])
    assert set(a.val["npi"]) == set(b.val["npi"])


def test_npi_grouped_split_does_not_split_a_single_npi():
    """An NPI's rows for different years must all land in train OR all in val."""
    train = _mk_train([f"{i:010d}" for i in range(80)], [2014, 2015, 2016, 2017])
    test = pd.DataFrame({"npi": ["9999999999"], "x": 0.0})
    split = npi_symmetric_holdout(train, test, test_npis=set())
    train_npis = set(split.train["npi"])
    val_npis = set(split.val["npi"])
    assert train_npis & val_npis == set()


def test_val_share_close_to_one_over_mod():
    """With mod=5, ~20% of distinct NPIs go to val (loose tolerance for 200 NPIs)."""
    train = _mk_train([f"{i:010d}" for i in range(200)], [2018])
    test = pd.DataFrame({"npi": ["9999999999"], "x": 0.0})
    split = npi_symmetric_holdout(train, test, test_npis=set(), val_hash_mod=5)
    n_train = split.summary["n_train_npis"]
    n_val = split.summary["n_val_npis"]
    val_share = n_val / (n_train + n_val)
    assert 0.10 <= val_share <= 0.30, f"val share {val_share:.3f} outside expected band"


def test_rejects_val_hash_mod_below_two():
    train = _mk_train(["0000000001"], [2018])
    test = pd.DataFrame({"npi": ["0000000002"], "x": 0.0})
    with pytest.raises(ValueError):
        npi_symmetric_holdout(train, test, test_npis=set(), val_hash_mod=1)


def test_rejects_missing_npi_column():
    train = pd.DataFrame({"provider_id": ["a"], "x": 1.0})
    test = pd.DataFrame({"npi": ["b"], "x": 0.0})
    with pytest.raises(KeyError):
        npi_symmetric_holdout(train, test, test_npis=set())


def test_returns_namedtuple_like_dataclass():
    train = _mk_train(["0000000001"], [2018])
    test = pd.DataFrame({"npi": ["0000000002"], "x": 0.0})
    split = npi_symmetric_holdout(train, test, test_npis={"0000000002"})
    assert isinstance(split, NpiSymmetricSplit)
    assert hasattr(split, "summary")


def test_npi_in_val_helper_is_stable():
    """SHA-1 hash → mod is deterministic across calls."""
    npi = "1234567890"
    assert _npi_in_val(npi, 5) == _npi_in_val(npi, 5)
