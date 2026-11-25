"""Tests for dump_hcpcs_breakdown.py year-argument normalization and no-crash behavior.

All tests are hermetic — no real parquet files or network calls.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock
import tempfile

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO))

import dump_hcpcs_breakdown as D  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# _parse_years unit tests
# ─────────────────────────────────────────────────────────────────────────────

def test_parse_range():
    assert D._parse_years("2019-2024") == list(range(2019, 2025))


def test_parse_comma():
    assert D._parse_years("2019,2020") == [2019, 2020]


def test_parse_list_of_strings():
    """nargs='+' gives a list — must handle space-separated tokens from argparse."""
    assert D._parse_years(["2022", "2023"]) == [2022, 2023]


def test_parse_single_year_string():
    assert D._parse_years("2023") == [2023]


def test_parse_single_year_list():
    assert D._parse_years(["2023"]) == [2023]


def test_parse_mixed_range_comma():
    assert D._parse_years("2019-2021,2024") == [2019, 2020, 2021, 2024]


def test_parse_space_separated_string():
    """Space-separated in a single string (e.g. env-injected) also works."""
    assert D._parse_years("2022 2023") == [2022, 2023]


def test_parse_list_with_range():
    """Range token inside a list (e.g. ['2019-2024'])."""
    assert D._parse_years(["2019-2024"]) == list(range(2019, 2025))


# ─────────────────────────────────────────────────────────────────────────────
# dump_year: missing panel file → prints "(year YYYY: no data)", no Traceback
# ─────────────────────────────────────────────────────────────────────────────

def test_dump_year_missing_file_no_crash(capsys, tmp_path):
    """When the panel file doesn't exist, dump_year prints 'no data' and returns."""
    missing = str(tmp_path / "cms_partb_svc_2099.parquet")

    with patch.object(D, "_find_svc_parquet", return_value=missing):
        # Should not raise
        D.dump_year("1234567890", 2099)

    out = capsys.readouterr().out
    assert "2099" in out
    assert "no data" in out


def test_dump_year_none_src_no_crash(capsys):
    """When _find_svc_parquet returns None, dump_year prints 'no data'."""
    with patch.object(D, "_find_svc_parquet", return_value=None):
        D.dump_year("1234567890", 2099)

    out = capsys.readouterr().out
    assert "no data" in out


def test_dump_year_duckdb_error_no_crash(capsys, tmp_path):
    """When duckdb raises (e.g. corrupt file), dump_year catches and prints 'no data'."""
    # Create a real (empty, invalid) file so the path-existence check passes
    fake = tmp_path / "cms_partb_svc_2099.parquet"
    fake.write_bytes(b"NOT A PARQUET FILE")

    with patch.object(D, "_find_svc_parquet", return_value=str(fake)):
        D.dump_year("1234567890", 2099)

    out = capsys.readouterr().out
    assert "no data" in out


# ─────────────────────────────────────────────────────────────────────────────
# main(): --years range and space-separated both parse without argparse error
# ─────────────────────────────────────────────────────────────────────────────

def _main_no_crash(argv: list[str]) -> int:
    """Run main() with all I/O patched to avoid filesystem needs."""
    with patch.object(D, "_years_svc", return_value=[]), \
         patch.object(D, "dump_year", return_value=None), \
         patch.object(D, "_svc_source", return_value=None):
        return D.main(argv)


def test_main_years_range(capsys):
    """--years 2019-2024 must not cause argparse 'unrecognized arguments'."""
    rc = _main_no_crash(["--npi", "1900000075", "--years", "2019-2024"])
    assert rc == 0


def test_main_years_space_separated(capsys):
    """--years 2022 2023 (two tokens) must be accepted without argparse error."""
    rc = _main_no_crash(["--npi", "1900000075", "--years", "2022", "2023"])
    assert rc == 0


def test_main_year_single(capsys):
    """--year 2023 (legacy single-year flag) still works."""
    with patch.object(D, "dump_year", return_value=None):
        rc = D.main(["--npi", "1900000075", "--year", "2023"])
    assert rc == 0


# ─────────────────────────────────────────────────────────────────────────────
# Synthetic parquet: happy-path year yields real output (no crash)
# ─────────────────────────────────────────────────────────────────────────────

def test_dump_year_sub_with_synthetic_data(capsys):
    """_dump_year_sub runs without error on minimal synthetic DataFrame."""
    import pandas as pd

    df = pd.DataFrame([
        {"HCPCS_Cd": "99213", "Tot_Srvcs": 500, "Tot_Benes": 300,
         "Avg_Mdcr_Pymt_Amt": 75.0},
        {"HCPCS_Cd": "99214", "Tot_Srvcs": 200, "Tot_Benes": 150,
         "Avg_Mdcr_Pymt_Amt": 120.0},
    ])

    D._dump_year_sub("9999999999", 2022, df, "synthetic")
    out = capsys.readouterr().out
    assert "YEAR 2022" in out
    assert "99213" in out


# ─────────────────────────────────────────────────────────────────────────────
# Predicate-evaluation presence check: optometry (92002/92004/92012/92014)
# family must not false-flag on zero 99xxx — eye-care providers bill the
# ophthalmological exam family INSTEAD OF E&M, not in addition to it.
# ─────────────────────────────────────────────────────────────────────────────

def test_optometry_normal_billing_no_absence_flag(capsys):
    """Optometrist: normal 92012/92014 (established) + some 92002/92004 (new)
    volume, zero 99xxx. Must NOT report a ZERO-EVALUATION / NEAR-ZERO /
    ASYMMETRIC absence flag — the ophthalmology family satisfies the check."""
    import pandas as pd

    df = pd.DataFrame([
        {"HCPCS_Cd": "92014", "Tot_Srvcs": 595, "Tot_Benes": 420, "Avg_Mdcr_Pymt_Amt": 95.0},
        {"HCPCS_Cd": "92012", "Tot_Srvcs": 494, "Tot_Benes": 350, "Avg_Mdcr_Pymt_Amt": 70.0},
        {"HCPCS_Cd": "92004", "Tot_Srvcs": 60, "Tot_Benes": 58, "Avg_Mdcr_Pymt_Amt": 110.0},
        {"HCPCS_Cd": "92002", "Tot_Srvcs": 40, "Tot_Benes": 39, "Avg_Mdcr_Pymt_Amt": 85.0},
    ])

    D._dump_year_sub("1900000074", 2023, df, "synthetic")
    out = capsys.readouterr().out

    assert "ZERO-EVALUATION" not in out
    assert "NEAR-ZERO-EVALUATION" not in out
    assert "ASYMMETRIC RATIO" not in out
    assert "[ophthalmology_office]" in out
    assert "Ophthalmology family (92002/92004/92012/92014) satisfied the check" in out
    # em_office must be silently skipped (no 99xxx billed at all) — it must
    # not appear as a reported modality row.
    assert "[em_office]" not in out


def test_provider_with_neither_family_flags_both_absent(capsys):
    """A provider billing neither 99xxx nor the ophthalmology family (e.g. a
    pure J-code biller) gets the explicit 'BOTH families absent' statement."""
    import pandas as pd

    df = pd.DataFrame([
        {"HCPCS_Cd": "J0585", "Tot_Srvcs": 300, "Tot_Benes": 40, "Avg_Mdcr_Pymt_Amt": 500.0},
    ])

    D._dump_year_sub("9999999999", 2023, df, "synthetic")
    out = capsys.readouterr().out

    assert "BOTH families absent — no 99xxx and no 92002/92004/92012/92014 billed." in out
    assert "[em_office]" not in out
    assert "[ophthalmology_office]" not in out


def test_gp_em_only_billing_reports_em_family_satisfied(capsys):
    """Sanity check the OTHER side of the pair: a GP billing normal 99xxx
    E&M and zero ophthalmology codes reports the E&M family as satisfying
    the check (pre-existing em_office behavior, unaffected by this change)."""
    import pandas as pd

    df = pd.DataFrame([
        {"HCPCS_Cd": "99213", "Tot_Srvcs": 500, "Tot_Benes": 300, "Avg_Mdcr_Pymt_Amt": 75.0},
        {"HCPCS_Cd": "99203", "Tot_Srvcs": 50, "Tot_Benes": 48, "Avg_Mdcr_Pymt_Amt": 110.0},
    ])

    D._dump_year_sub("1234567890", 2023, df, "synthetic")
    out = capsys.readouterr().out

    assert "E&M family (99xxx) satisfied the check" in out
    assert "[ophthalmology_office]" not in out
