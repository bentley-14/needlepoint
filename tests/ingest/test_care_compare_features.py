"""Tests for fraudlib.ingest.providers.care_compare.

Network-free: fabricate a raw Care Compare CSV that matches the
enrollment-row schema, project via :func:`_csv_to_parquet`, then collapse
to per-NPI via :func:`load_care_compare_features` and assert the aggregates.

Run from the repo root:

    /opt/anaconda3/bin/python -m pytest tests/test_care_compare_features.py -v
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from fraudlib.ingest.providers import care_compare as cc
from fraudlib.ingest.providers.care_compare import load_care_compare_features


def _write_fake_csv(path: Path) -> None:
    rows = [
        # NPI 1 — enrolled with two groups; largest is BigClinic (12 members).
        # Primary pri_spec is "Internal Medicine" across both rows.
        # Two secondary specs and two hospital affiliations total.
        {"NPI": "1", "Ind_PAC_ID": "PAC_A1", "Org_PAC_ID": "ORG_SMALL",
         "Org_Lgl_Nm": "Small Group", "Num_Org_Mem": "3",
         "pri_spec": "Internal Medicine", "sec_spec_1": "Geriatric Medicine",
         "sec_spec_2": "", "sec_spec_3": "", "sec_spec_4": "",
         "Hosp_Afl_LBN_1": "Alpha Hospital", "Hosp_Afl_LBN_2": "",
         "Hosp_Afl_LBN_3": "", "Hosp_Afl_LBN_4": "", "Hosp_Afl_LBN_5": ""},
        {"NPI": "1", "Ind_PAC_ID": "PAC_A1", "Org_PAC_ID": "ORG_BIG",
         "Org_Lgl_Nm": "BigClinic", "Num_Org_Mem": "12",
         "pri_spec": "Internal Medicine", "sec_spec_1": "Hospice Medicine",
         "sec_spec_2": "", "sec_spec_3": "", "sec_spec_4": "",
         "Hosp_Afl_LBN_1": "Alpha Hospital", "Hosp_Afl_LBN_2": "Beta Medical Ctr",
         "Hosp_Afl_LBN_3": "", "Hosp_Afl_LBN_4": "", "Hosp_Afl_LBN_5": ""},
        # NPI 2 — solo practitioner, no hospital, no secondaries.
        {"NPI": "2", "Ind_PAC_ID": "PAC_B", "Org_PAC_ID": "ORG_SOLO",
         "Org_Lgl_Nm": "Solo Practice", "Num_Org_Mem": "1",
         "pri_spec": "Pain Management", "sec_spec_1": "", "sec_spec_2": "",
         "sec_spec_3": "", "sec_spec_4": "",
         "Hosp_Afl_LBN_1": "", "Hosp_Afl_LBN_2": "",
         "Hosp_Afl_LBN_3": "", "Hosp_Afl_LBN_4": "", "Hosp_Afl_LBN_5": ""},
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(path, index=False)


@pytest.fixture
def fake_parquet(tmp_path: Path) -> Path:
    csv_path = tmp_path / "DAC_NationalDownloadableFile.csv"
    _write_fake_csv(csv_path)
    out = tmp_path / "care_compare.parquet"
    cc._csv_to_parquet(csv_path, out)
    return out


def test_collapse_to_per_npi(fake_parquet: Path) -> None:
    feats = load_care_compare_features(parquet_path=fake_parquet).set_index("NPI")

    # NPI 1 — largest group is BigClinic (12 members).
    r1 = feats.loc["0000000001"]
    assert r1["cc_group_practice_pac_id"] == "ORG_BIG"
    assert r1["cc_group_practice_name"] == "BigClinic"
    assert int(r1["cc_largest_group_size"]) == 12
    assert int(r1["cc_n_group_affiliations"]) == 2

    assert r1["cc_primary_specialty"] == "Internal Medicine"
    # Secondary specialties: union across rows (order-preserving unique).
    secs = list(r1["cc_secondary_specialties"])
    assert set(secs) == {"Geriatric Medicine", "Hospice Medicine"}
    assert int(r1["cc_n_secondary_specialties"]) == 2

    # Hospital affiliations: deduplicated across rows.
    hosps = list(r1["cc_hospital_affiliations"])
    assert set(hosps) == {"Alpha Hospital", "Beta Medical Ctr"}
    assert int(r1["cc_n_hospital_affiliations"]) == 2

    # NPI 2 — solo, no affiliations.
    r2 = feats.loc["0000000002"]
    assert r2["cc_primary_specialty"] == "Pain Management"
    assert int(r2["cc_largest_group_size"]) == 1
    assert int(r2["cc_n_group_affiliations"]) == 1
    assert int(r2["cc_n_secondary_specialties"]) == 0
    assert int(r2["cc_n_hospital_affiliations"]) == 0


def test_missing_parquet_raises(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist.parquet"
    with pytest.raises(FileNotFoundError) as ei:
        load_care_compare_features(parquet_path=missing)
    assert "fetch_care_compare" in str(ei.value)
