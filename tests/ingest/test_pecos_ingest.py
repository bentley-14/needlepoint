"""Tests for fraudlib.ingest.providers.pecos.

Network-free: the quarterly file download is exercised separately. Here
we fabricate a raw PECOS CSV that matches the ingest schema, run
:func:`_csv_to_parquet` + :func:`load_pecos_features`, and assert the
derived flags match expectations.

Run from the repo root:

    /opt/anaconda3/bin/python -m pytest tests/test_pecos_ingest.py -v
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from fraudlib.ingest.providers import pecos as pecos
from fraudlib.ingest.providers.pecos import load_pecos_features


def _write_fake_csv(path: Path) -> None:
    rows = [
        # NPI 1 — full privileges (orders DME + HHA + PMD + hospice, refers Part B).
        {"NPI": "1", "LAST NAME": "DOE", "FIRST NAME": "JANE",
         "PARTB": "Y", "DME": "Y", "HHA": "Y", "PMD": "Y", "HOSPICE": "Y"},
        # NPI 2 — referring only (Part B) but no ordering privileges.
        {"NPI": "2", "LAST NAME": "SMITH", "FIRST NAME": "JOHN",
         "PARTB": "Y", "DME": "N", "HHA": "N", "PMD": "N", "HOSPICE": "N"},
        # NPI 3 — DME-only ordering.
        {"NPI": "3", "LAST NAME": "NG", "FIRST NAME": "ANN",
         "PARTB": "N", "DME": "Y", "HHA": "N", "PMD": "N", "HOSPICE": "N"},
        # Short NPI — filtered out after zfill? (It still becomes 10 chars
        # so it's kept — the filter only drops rows that are <10 post-zfill,
        # which is impossible. Including to confirm behavior.)
        {"NPI": "12345", "LAST NAME": "Z", "FIRST NAME": "Q",
         "PARTB": "Y", "DME": "", "HHA": "", "PMD": "", "HOSPICE": ""},
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(path, index=False)


@pytest.fixture
def fake_parquet(tmp_path: Path) -> Path:
    csv_path = tmp_path / "order_and_referring.csv"
    _write_fake_csv(csv_path)
    out = tmp_path / "pecos_ordering_referring.parquet"
    pecos._csv_to_parquet(csv_path, out)
    return out


def test_csv_to_parquet_projects_and_pads(fake_parquet: Path) -> None:
    df = pd.read_parquet(fake_parquet)
    # 10-char zero-padded NPI invariant.
    assert all(len(n) == 10 for n in df["NPI"])
    assert "0000000001" in set(df["NPI"])
    assert "0000012345" in set(df["NPI"])

    # Boolean flag coercion.
    assert df.set_index("NPI").loc["0000000001", "pecos_dme_eligible"] is True or \
           bool(df.set_index("NPI").loc["0000000001", "pecos_dme_eligible"]) is True
    assert bool(df.set_index("NPI").loc["0000000002", "pecos_dme_eligible"]) is False


def test_load_features_derives_flags(fake_parquet: Path) -> None:
    feats = load_pecos_features(parquet_path=fake_parquet).set_index("NPI")

    # NPI 1 — all privileges.
    r1 = feats.loc["0000000001"]
    assert bool(r1["pecos_has_ordering_privilege"]) is True
    assert bool(r1["pecos_has_referring_privilege"]) is True
    assert int(r1["pecos_n_privileges"]) == 5

    # NPI 2 — Part B referring only, no ordering.
    r2 = feats.loc["0000000002"]
    assert bool(r2["pecos_has_ordering_privilege"]) is False
    assert bool(r2["pecos_has_referring_privilege"]) is True
    assert int(r2["pecos_n_privileges"]) == 1

    # NPI 3 — DME ordering only, no Part B referring.
    r3 = feats.loc["0000000003"]
    assert bool(r3["pecos_has_ordering_privilege"]) is True
    assert bool(r3["pecos_has_referring_privilege"]) is False
    assert int(r3["pecos_n_privileges"]) == 1


def test_missing_parquet_raises(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist.parquet"
    with pytest.raises(FileNotFoundError) as ei:
        load_pecos_features(parquet_path=missing)
    assert "download_pecos_ordering_referring" in str(ei.value)
