"""Tests for the schema-driven dtype recast cast-generation core.

These cover the pure SQL-generation logic (no R2 / DuckDB I/O), especially
the .0-trap int-hop for float-source / string-target columns.
"""
from __future__ import annotations

from pipeline.scripts.recast_r2_dtypes import (
    REINGEST_ONLY, build_recast_select, cast_expr, duck_target,
)


def test_zero_padded_code_columns_are_reingest_only() -> None:
    # FIPS / ZIP must NOT be cast-repaired (the int-hop drops leading zeros:
    # '01' -> 1.0 -> '1'). They're flagged for producer re-ingest instead.
    assert "Prscrbr_State_FIPS" in REINGEST_ONLY
    assert "Rndrng_Prvdr_State_FIPS" in REINGEST_ONLY
    assert "Prscrbr_zip5" in REINGEST_ONLY


def test_duck_target_mapping() -> None:
    assert duck_target("str") == "VARCHAR"
    assert duck_target("object") == "VARCHAR"
    assert duck_target("float64") == "DOUBLE"
    assert duck_target("Int64") == "BIGINT"
    assert duck_target("int64") == "BIGINT"
    assert duck_target("bool") == "BOOLEAN"
    assert "TIMESTAMP" in duck_target("datetime64[ns, UTC]")


def test_cast_expr_noop_when_already_matching() -> None:
    assert cast_expr("npi", "VARCHAR", "VARCHAR") is None
    assert cast_expr("amt", "DOUBLE", "DOUBLE") is None


def test_cast_expr_float_to_string_uses_int_hop() -> None:
    # The .0 trap: a DOUBLE NPI cast straight to text keeps '...0'.
    expr = cast_expr("Covered_Recipient_NPI", "DOUBLE", "VARCHAR")
    assert expr == 'CAST(CAST("Covered_Recipient_NPI" AS BIGINT) AS VARCHAR) AS "Covered_Recipient_NPI"'


def test_cast_expr_string_to_float_is_plain() -> None:
    # Total_Amount: VARCHAR -> float is safe, no int-hop.
    expr = cast_expr("Total_Amount_of_Payment_USDollars", "VARCHAR", "DOUBLE")
    assert expr == 'CAST("Total_Amount_of_Payment_USDollars" AS DOUBLE) AS "Total_Amount_of_Payment_USDollars"'


def test_cast_expr_int_to_string_is_plain_no_hop() -> None:
    # INTEGER -> str (e.g. partd 2024 suppression flags) has no .0 risk.
    expr = cast_expr("Brnd_Sprsn_Flag", "INTEGER", "VARCHAR")
    assert expr == 'CAST("Brnd_Sprsn_Flag" AS VARCHAR) AS "Brnd_Sprsn_Flag"'


def test_build_recast_select_picks_only_drifted_columns() -> None:
    actual = {
        "npi": "VARCHAR",                       # matches -> skip
        "Covered_Recipient_NPI": "DOUBLE",      # float->str -> int-hop
        "Total_Amount_of_Payment_USDollars": "VARCHAR",  # str->float -> plain
    }
    targets = {
        "npi": "VARCHAR",
        "Covered_Recipient_NPI": "VARCHAR",
        "Total_Amount_of_Payment_USDollars": "DOUBLE",
    }
    sql = build_recast_select("read_parquet('x')", actual, targets)
    assert sql is not None
    assert "CAST(CAST(\"Covered_Recipient_NPI\" AS BIGINT) AS VARCHAR)" in sql
    assert 'CAST("Total_Amount_of_Payment_USDollars" AS DOUBLE)' in sql
    assert '"npi"' not in sql  # untouched column not in REPLACE list
    assert sql.startswith("SELECT * REPLACE (")


def test_build_recast_select_noop_returns_none() -> None:
    actual = {"npi": "VARCHAR", "amt": "DOUBLE"}
    targets = {"npi": "VARCHAR", "amt": "DOUBLE"}
    assert build_recast_select("read_parquet('x')", actual, targets) is None


def test_build_recast_select_ignores_undeclared_columns() -> None:
    # A stored column not in the schema is left as-is (not in REPLACE).
    actual = {"npi": "DOUBLE", "legacy_extra": "INTEGER"}
    targets = {"npi": "VARCHAR"}
    sql = build_recast_select("read_parquet('x')", actual, targets)
    assert sql is not None
    assert "legacy_extra" not in sql
