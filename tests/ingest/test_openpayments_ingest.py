"""Tests for fraudlib.ingest.providers.openpayments.

Network-free: the bulk download and Socrata API fallback are exercised
separately via integration scripts. Here we fabricate a raw parquet that
matches the ingest schema, run :func:`load_openpayments_features`, and
assert the aggregate shape is correct.

Run from the repo root:

    /opt/anaconda3/bin/python -m pytest tests/test_openpayments_ingest.py -v
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from fraudlib.ingest.providers import openpayments as opi
from fraudlib.ingest.providers.openpayments import (
    _nature_bucket,
    load_openpayments_features,
)


def _write_fake_raw(path: Path, program_year: int = 2019) -> None:
    """Write a small raw Open Payments parquet with known aggregates."""
    rows = [
        # NPI 0000000001 — oncologist receiving 3 meals, 1 speaker, 1 royalty
        # from two different manufacturers.
        {"record_id": "R1", "NPI": "1", "recipient_type": "Covered Recipient Physician",
         "specialty": "Oncology", "state": "MI", "manufacturer": "AcmePharma",
         "amount_usd": 150.0, "date_of_payment": "2019-03-01",
         "nature_of_payment": "Food and Beverage",
         "product_name": "DrugA", "program_year": str(program_year)},
        {"record_id": "R2", "NPI": "1", "recipient_type": "Covered Recipient Physician",
         "specialty": "Oncology", "state": "MI", "manufacturer": "AcmePharma",
         "amount_usd": 80.0, "date_of_payment": "2019-04-01",
         "nature_of_payment": "Food and Beverage",
         "product_name": "DrugA", "program_year": str(program_year)},
        {"record_id": "R3", "NPI": "1", "recipient_type": "Covered Recipient Physician",
         "specialty": "Oncology", "state": "MI", "manufacturer": "BetaBio",
         "amount_usd": 210.0, "date_of_payment": "2019-05-01",
         "nature_of_payment": "Food and Beverage",
         "product_name": "DrugB", "program_year": str(program_year)},
        {"record_id": "R4", "NPI": "1", "recipient_type": "Covered Recipient Physician",
         "specialty": "Oncology", "state": "MI", "manufacturer": "AcmePharma",
         "amount_usd": 5000.0, "date_of_payment": "2019-06-01",
         "nature_of_payment": "Compensation for services other than consulting, "
                              "including serving as faculty or as a speaker at a "
                              "venue other than a continuing education program",
         "product_name": "DrugA", "program_year": str(program_year)},
        {"record_id": "R5", "NPI": "1", "recipient_type": "Covered Recipient Physician",
         "specialty": "Oncology", "state": "MI", "manufacturer": "AcmePharma",
         "amount_usd": 90000.0, "date_of_payment": "2019-07-01",
         "nature_of_payment": "Royalty or License",
         "product_name": "DrugA", "program_year": str(program_year)},
        # NPI 0000000002 — peer oncologist with tiny payments.
        {"record_id": "R6", "NPI": "2", "recipient_type": "Covered Recipient Physician",
         "specialty": "Oncology", "state": "MI", "manufacturer": "BetaBio",
         "amount_usd": 25.0, "date_of_payment": "2019-03-10",
         "nature_of_payment": "Food and Beverage",
         "product_name": "DrugB", "program_year": str(program_year)},
        # NPI 0000000003 — ophthalmologist, different specialty pool.
        {"record_id": "R7", "NPI": "3", "recipient_type": "Covered Recipient Physician",
         "specialty": "Ophthalmology", "state": "FL", "manufacturer": "GammaCo",
         "amount_usd": 500.0, "date_of_payment": "2019-02-01",
         "nature_of_payment": "Travel and Lodging",
         "product_name": "DrugC", "program_year": str(program_year)},
        # A row with no NPI that should be dropped at ingest (simulated here
        # by being pre-filtered — the bulk loader drops them). We include a
        # row with a short NPI to ensure the 10-char zfill + length-check
        # guards work.
        {"record_id": "R8", "NPI": "12345", "recipient_type": "Covered Recipient Physician",
         "specialty": "Pain Management", "state": "AL", "manufacturer": "DeltaLabs",
         "amount_usd": 100.0, "date_of_payment": "2019-08-01",
         "nature_of_payment": "Consulting Fee",
         "product_name": "DrugD", "program_year": str(program_year)},
    ]
    df = pd.DataFrame(rows)
    df["amount_usd"] = pd.to_numeric(df["amount_usd"])
    df["date_of_payment"] = pd.to_datetime(df["date_of_payment"])
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)


@pytest.fixture
def fake_raw(tmp_path: Path) -> Path:
    out = tmp_path / "open_payments_general_2019.parquet"
    _write_fake_raw(out)
    return out


def test_nature_bucket_covers_common_phrases() -> None:
    assert _nature_bucket("Food and Beverage") == "meals"
    assert _nature_bucket("Travel and Lodging") == "travel"
    assert _nature_bucket("Royalty or License") == "royalty"
    assert _nature_bucket("Consulting Fee") == "consulting"
    assert _nature_bucket(
        "Compensation for services other than consulting, including serving "
        "as faculty or as a speaker at a venue other than a continuing "
        "education program"
    ) == "speaker"
    # Unmapped value falls through to "other".
    assert _nature_bucket("Something Totally Novel") == "other"
    # Non-string input is tolerated.
    assert _nature_bucket(None) == "other"  # type: ignore[arg-type]


def test_load_features_aggregates(fake_raw: Path) -> None:
    feats = load_openpayments_features(2019, parquet_path=fake_raw)

    assert "NPI" in feats.columns
    # NPIs are zero-padded to 10 chars.
    assert all(len(n) == 10 for n in feats["NPI"])

    npi1 = feats[feats["NPI"] == "0000000001"].iloc[0]
    # 150 + 80 + 210 + 5000 + 90000 = 95440
    assert npi1["op_total_received_usd"] == pytest.approx(95440.0)
    assert int(npi1["op_n_payments"]) == 5
    assert int(npi1["op_manufacturers_count"]) == 2  # Acme + Beta
    assert npi1["op_top_manufacturer"] == "AcmePharma"  # $95,230 > Beta's $210
    assert int(npi1["op_n_meals"]) == 3
    assert int(npi1["op_n_speaker"]) == 1
    assert int(npi1["op_n_royalty"]) == 1
    assert bool(npi1["op_has_royalty_income"]) is True

    # NPI 0000000002 peer — 1 meal only, small dollars, no royalty.
    npi2 = feats[feats["NPI"] == "0000000002"].iloc[0]
    assert npi2["op_total_received_usd"] == pytest.approx(25.0)
    assert bool(npi2["op_has_royalty_income"]) is False

    # Specialty percentile sanity: within Oncology (2 NPIs), NPI 1 is the
    # max and must be > NPI 2's percentile.
    pct1 = npi1["op_specialty_pct"]
    pct2 = npi2["op_specialty_pct"]
    assert pct1 > pct2


def test_npi_padding_and_length_filter(fake_raw: Path) -> None:
    feats = load_openpayments_features(2019, parquet_path=fake_raw)
    # A 5-digit pre-pad NPI "12345" becomes "0000012345" and is kept —
    # the length filter drops only rows that are still short of 10 after
    # zfill, which is impossible here but the filter guards against
    # malformed upstream rows (empty strings). Assert we got the padded
    # version back.
    assert "0000012345" in set(feats["NPI"].values)


def test_missing_parquet_raises(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist.parquet"
    with pytest.raises(FileNotFoundError) as ei:
        load_openpayments_features(2019, parquet_path=missing)
    assert "download_openpayments_bulk" in str(ei.value)
