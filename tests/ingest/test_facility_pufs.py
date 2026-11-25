"""Tests for the facility-PUF fetchers.

Network-free: we exercise the ``local_csv=`` path with a synthetic CSV
and confirm the parquet lands at the expected location. The real
bulk-download path is exercised separately.

Run from the repo root:

    /opt/anaconda3/bin/python -m pytest tests/test_facility_pufs.py -v
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from pipeline.scrapers import download


FACILITY_FIXTURE_ROWS = [
    {"CMS Certification Number (CCN)": "010001",
     "Facility Name": "Acme General Hospital",
     "State": "AL", "Hospital Type": "Acute Care Hospitals",
     "Hospital Ownership": "Proprietary",
     "Hospital overall rating": "3"},
    {"CMS Certification Number (CCN)": "010002",
     "Facility Name": "Bayside Medical Center",
     "State": "CA", "Hospital Type": "Acute Care Hospitals",
     "Hospital Ownership": "Voluntary non-profit - Private",
     "Hospital overall rating": "4"},
]


@pytest.fixture
def synthetic_hospital_csv(tmp_path: Path) -> Path:
    csv_path = tmp_path / "hospital_general_information.csv"
    pd.DataFrame(FACILITY_FIXTURE_ROWS).to_csv(csv_path, index=False)
    return csv_path


def test_fetch_cms_facility_hospital_from_local_csv(
    monkeypatch, tmp_path, synthetic_hospital_csv
) -> None:
    # Point the module at a temp processed-dir so we don't clobber
    # anything real.
    monkeypatch.setattr(download, "REGISTRIES_DIR", tmp_path)

    df = download.fetch_cms_facility_hospital(local_csv=synthetic_hospital_csv)
    parquet_path = tmp_path / "cms_facility_hospital.parquet"

    assert parquet_path.exists()
    assert len(df) == 2
    assert "Facility Name" in df.columns
    assert set(df["State"]) == {"AL", "CA"}

    # Second call must be idempotent — reuses the existing parquet
    # rather than re-parsing the CSV (even if we pass a bogus CSV path).
    bogus = tmp_path / "does_not_exist.csv"
    df2 = download.fetch_cms_facility_hospital(local_csv=bogus)
    assert len(df2) == 2


def test_facility_url_candidates_cover_all_four_sources() -> None:
    for key in (
        "cms_facility_hospital",
        "cms_facility_snf",
        "cms_facility_hha",
        "cms_facility_hospice",
    ):
        assert key in download.FACILITY_URL_CANDIDATES
        assert len(download.FACILITY_URL_CANDIDATES[key]) >= 1
