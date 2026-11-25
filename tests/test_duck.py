"""Tests for scripts/duck.py — clean DuckDB query runner.

All tests are CI-safe: synthetic parquet in tmp_path, no real data.
Deps: pytest, pandas, pyarrow, duckdb.

Run: pytest tests/test_duck.py -v
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parents[1]

# Load duck.py as a module
_spec = importlib.util.spec_from_file_location("duck", REPO / "scripts" / "duck.py")
duck = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(duck)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def synth_parquet(tmp_path) -> Path:
    """Write a small synthetic partb_svc-style parquet in tmp_path."""
    df = pd.DataFrame({
        "Rndrng_NPI": ["1000000001", "1000000001", "1000000002"],
        "HCPCS_Cd": ["G0482", "80307", "G0482"],
        "Tot_Srvcs": [10.0, 5.0, 20.0],
        "Tot_Bene_Day_Srvcs": [10.0, 5.0, 20.0],
        "Avg_Mdcr_Pymt_Amt": [100.0, 50.0, 80.0],
    })
    path = tmp_path / "cms_partb_svc_2022.parquet"
    df.to_parquet(path, index=False)
    return path


@pytest.fixture()
def patched_proc(tmp_path, monkeypatch) -> Path:
    """Monkeypatch duck.PROC to point at tmp_path so table aliases resolve there."""
    monkeypatch.setattr(duck, "PROC", tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# Helper to call main() and capture stdout/stderr
# ---------------------------------------------------------------------------

def run(args: list[str], capsys) -> tuple[str, str, int]:
    rc = duck.main(args)
    cap = capsys.readouterr()
    return cap.out, cap.err, rc


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBasicSelect:
    def test_raw_parquet_select(self, synth_parquet, capsys):
        sql = f"SELECT Rndrng_NPI, Tot_Srvcs FROM read_parquet('{synth_parquet}') ORDER BY 1, 2"
        out, err, rc = run([sql], capsys)
        assert rc == 0
        assert "1000000001" in out
        assert "1000000002" in out

    def test_alias_resolves(self, synth_parquet, patched_proc, capsys):
        sql = "SELECT Rndrng_NPI, SUM(Tot_Srvcs) s FROM partb_svc GROUP BY 1 ORDER BY 1"
        out, err, rc = run(["--year", "2022", sql], capsys)
        assert rc == 0
        assert "1000000001" in out
        assert "1000000002" in out

    def test_with_clause(self, synth_parquet, capsys):
        sql = f"WITH t AS (SELECT * FROM read_parquet('{synth_parquet}')) SELECT COUNT(*) n FROM t"
        out, err, rc = run([sql], capsys)
        assert rc == 0
        assert "3" in out

    def test_limit_truncation_footer(self, synth_parquet, patched_proc, capsys):
        sql = "SELECT * FROM partb_svc"
        out, err, rc = run(["--year", "2022", "--limit", "2", sql], capsys)
        assert rc == 0
        assert "more rows" in out  # footer appears


class TestDescribe:
    def test_describe_known_table(self, synth_parquet, patched_proc, capsys):
        out, err, rc = run(["--describe", "partb_svc", "--year", "2022"], capsys)
        assert rc == 0
        assert "Rndrng_NPI" in out
        assert "HCPCS_Cd" in out
        assert "Tot_Srvcs" in out

    def test_describe_requires_year(self, capsys):
        out, err, rc = run(["--describe", "partb_svc"], capsys)
        assert rc != 0
        assert "--year" in err

    def test_describe_unknown_table(self, capsys):
        out, err, rc = run(["--describe", "unknown_table", "--year", "2022"], capsys)
        assert rc != 0
        assert "unknown" in err.lower() or "known" in err.lower()


class TestWriteRejection:
    @pytest.mark.parametrize("bad_sql", [
        "INSERT INTO foo VALUES (1)",
        "UPDATE foo SET x=1",
        "DELETE FROM foo",
        "CREATE TABLE foo AS SELECT 1",
        "DROP TABLE foo",
        "ATTACH ':memory:' AS m",
        "COPY foo TO '/tmp/out.csv'",
        "INSTALL httpfs",
        "LOAD httpfs",
        "PRAGMA database_list",
    ])
    def test_write_rejected(self, bad_sql, capsys):
        out, err, rc = run([bad_sql], capsys)
        assert rc != 0
        assert "rejected" in err.lower() or "read-only" in err.lower()


class TestMultiYear:
    def test_range_syntax(self, synth_parquet, patched_proc, capsys):
        # 2022 exists (synth_parquet), 2021 doesn't → should skip 2021 gracefully
        sql = "SELECT Rndrng_NPI, SUM(Tot_Srvcs) s FROM partb_svc GROUP BY 1"
        out, err, rc = run(["--years", "2021-2022", sql], capsys)
        # 2021 missing → Note in stderr; 2022 present → results in stdout
        assert rc == 0
        assert "1000000001" in out
        # year column: integer 2022 formatted with commas → "2,022"
        assert "2,022" in out

    def test_comma_syntax(self, synth_parquet, patched_proc, capsys):
        sql = "SELECT Rndrng_NPI, SUM(Tot_Srvcs) s FROM partb_svc GROUP BY 1"
        out, err, rc = run(["--years", "2022,2023", sql], capsys)
        assert rc == 0
        assert "1000000001" in out

    def test_space_syntax(self, synth_parquet, patched_proc, tmp_path, capsys):
        # Create a second year file
        df = pd.DataFrame({
            "Rndrng_NPI": ["1000000003"],
            "HCPCS_Cd": ["G0482"],
            "Tot_Srvcs": [99.0],
            "Tot_Bene_Day_Srvcs": [99.0],
            "Avg_Mdcr_Pymt_Amt": [200.0],
        })
        df.to_parquet(tmp_path / "cms_partb_svc_2023.parquet", index=False)
        sql = "SELECT Rndrng_NPI, SUM(Tot_Srvcs) s FROM partb_svc GROUP BY 1"
        out, err, rc = run(["--years", "2022 2023", sql], capsys)
        assert rc == 0
        # years formatted with commas: 2,022 and 2,023
        assert "2,022" in out
        assert "2,023" in out

    def test_year_placeholder(self, synth_parquet, patched_proc, capsys):
        sql = "SELECT {year} AS y, COUNT(*) n FROM partb_svc"
        out, err, rc = run(["--years", "2022", sql], capsys)
        assert rc == 0
        # year column formatted with commas: 2,022
        assert "2,022" in out

    def test_all_years_missing(self, patched_proc, capsys):
        sql = "SELECT * FROM partb_svc"
        out, err, rc = run(["--years", "1999-2000", sql], capsys)
        assert rc != 0  # no results → error
        assert "No results" in err or "unavailable" in err


class TestMissingTableFile:
    def test_missing_gives_clean_message(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(duck, "PROC", tmp_path)
        sql = "SELECT * FROM partb WHERE Rndrng_NPI='1'"
        out, err, rc = run(["--year", "2099", sql], capsys)
        assert rc != 0
        assert "unavailable" in err or "not found" in err
        # Must not be a Python traceback
        assert "Traceback" not in err

    def test_missing_no_traceback(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(duck, "PROC", tmp_path)
        sql = "SELECT * FROM dmepos"
        out, err, rc = run(["--year", "2099", sql], capsys)
        assert "Traceback" not in err


class TestOutputFormats:
    def test_csv_format(self, synth_parquet, capsys):
        sql = f"SELECT Rndrng_NPI, Tot_Srvcs FROM read_parquet('{synth_parquet}') LIMIT 1"
        out, err, rc = run(["--format", "csv", sql], capsys)
        assert rc == 0
        assert "Rndrng_NPI" in out  # header row
        assert "," in out  # CSV delimited

    def test_json_format(self, synth_parquet, capsys):
        import json as _json
        sql = f"SELECT Rndrng_NPI, Tot_Srvcs FROM read_parquet('{synth_parquet}') LIMIT 1"
        out, err, rc = run(["--format", "json", sql], capsys)
        assert rc == 0
        parsed = _json.loads(out)
        assert isinstance(parsed, list)
        assert "Rndrng_NPI" in parsed[0]


class TestParseYears:
    def test_range(self):
        assert duck._parse_years("2019-2024") == list(range(2019, 2025))

    def test_comma(self):
        assert duck._parse_years("2019,2020") == [2019, 2020]

    def test_space(self):
        assert duck._parse_years("2019 2020") == [2019, 2020]
