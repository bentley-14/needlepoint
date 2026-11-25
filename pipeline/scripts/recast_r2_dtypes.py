"""Schema-driven in-place dtype backfill for R2 panels via DuckDB.

Motivation
----------
Historical panel parquets on R2 carry per-year dtype drift (the Open
Payments ``Covered_Recipient_NPI`` stored as ``DOUBLE`` in 2018-2023, the
Part D 2024 release flipping suppression flags to ``INTEGER``, ``Bene_CC_*``
prevalences ``INTEGER``->``DOUBLE``, …). The producer now enforces dtypes at
write-time (Pandera, ``pipeline/base.py``), so *new* writes are clean — this
tool repairs the *existing* artifacts to match the same schema, without the
pull -> pandas -> push cycle.

It reads each parquet straight from R2 via DuckDB httpfs, casts every column
to the dtype its scraper's Pandera schema declares, writes the result to a
staging key, asserts the staged dtypes match the schema, then (only with
``--execute``) swaps it over the original and refreshes the ``.manifest.json``.
Bytes transit this machine over httpfs but never hit local disk.

The ``.0`` trap
---------------
A column whose schema target is a string but whose stored dtype is a float
must go through ``BIGINT`` first: ``CAST(1900000075.0 AS VARCHAR)`` yields
``'1900000075.0'`` (12 chars, breaks exact-match), whereas
``CAST(CAST(... AS BIGINT) AS VARCHAR)`` yields ``'1900000075'``. The cast
generator applies the int-hop automatically for float-source/string-target
columns; every other cast is a plain ``CAST``.

Idempotent: a year already matching its schema produces no casts and is
skipped.

Usage
-----
    # dry-run (default): print the recast SQL per year, touch nothing
    python -m pipeline.scripts.recast_r2_dtypes --source open_payments_general
    python -m pipeline.scripts.recast_r2_dtypes --source cms_partd --year 2024

    # stage + validate on R2 but do NOT swap (inspect the .recast.parquet):
    python -m pipeline.scripts.recast_r2_dtypes --source open_payments_general --stage

    # actually swap + refresh manifest (outward-facing, irreversible):
    python -m pipeline.scripts.recast_r2_dtypes --source open_payments_general --execute
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from typing import Optional, Type

import duckdb

from pipeline.base import BaseScraper
from pipeline.scrapers.cms_dmepos import DMEPOSScraper
from pipeline.scrapers.cms_partb import PartBScraper
from pipeline.scrapers.cms_partb_svc import PartBSvcScraper
from pipeline.scrapers.cms_partd import PartDScraper
from pipeline.scrapers.nppes import NppesScraper
from pipeline.scrapers.open_payments import OpenPaymentsScraper

# Only NPI-keyed parquet panels are recast here. DOJ press releases and the
# gold_set JSONL are not dtype-drifting parquet panels.
SCRAPERS: dict[str, Type[BaseScraper]] = {
    c.source: c for c in [
        OpenPaymentsScraper, PartBScraper, PartBSvcScraper,
        PartDScraper, DMEPOSScraper, NppesScraper,
    ]
}

ENDPOINT_HOST = "<ACCOUNT_ID>.r2.cloudflarestorage.com"
BUCKET = "needlepoint-data"
PREFIX = "processed/v1"

# Floating-point parquet types that hit the .0 trap when cast straight to text.
_FLOAT_DUCK_TYPES = ("DOUBLE", "FLOAT", "REAL", "DECIMAL")

# Zero-padded / potentially-alphanumeric code columns that a cast CANNOT
# faithfully repair: once stored as a float, FIPS '01' -> 1.0 and ZIP
# '02134' -> 2134.0 have already lost the leading zero, and the int-hop
# yields '1'/'2134', not the canonical padded code. These need a producer
# re-ingest from source (read as VARCHAR), not a cast. The recast leaves
# them untouched and reports them as re-ingest-only.
REINGEST_ONLY = {
    "Rndrng_Prvdr_State_FIPS", "Prscrbr_State_FIPS",
    "Rndrng_Prvdr_Zip5", "Prscrbr_zip5",
}


# ---------------------------------------------------------------------------
# Pure, testable cast-generation core
# ---------------------------------------------------------------------------

def duck_target(pandera_dtype: str) -> str:
    """Map a Pandera/pandas dtype string to the DuckDB CAST target type."""
    d = str(pandera_dtype).lower()
    if d in ("str", "string", "object") or d.startswith("string"):
        return "VARCHAR"
    if "datetime" in d or "timestamp" in d:
        return "TIMESTAMP WITH TIME ZONE" if "tz" in d or "utc" in d else "TIMESTAMP"
    if "bool" in d:
        return "BOOLEAN"
    if "float" in d or d == "double":
        return "DOUBLE"
    if "int" in d:               # Int64, int64, int32, …
        return "BIGINT"
    return "VARCHAR"


def _is_float_type(duck_type: str) -> bool:
    return any(duck_type.upper().startswith(f) for f in _FLOAT_DUCK_TYPES)


def cast_expr(col: str, actual_duck_type: str, target: str) -> Optional[str]:
    """Return a SELECT projection that casts ``col`` to ``target``, or None if
    it already matches (no-op).

    Applies the BIGINT int-hop for float-source / string-target columns to
    avoid the ``.0`` trap.
    """
    q = f'"{col}"'
    if actual_duck_type.upper() == target.upper():
        return None
    if target == "VARCHAR" and _is_float_type(actual_duck_type):
        return f"CAST(CAST({q} AS BIGINT) AS VARCHAR) AS {q}"
    return f"CAST({q} AS {target}) AS {q}"


def build_recast_select(
    table_expr: str,
    actual_types: dict[str, str],
    schema_targets: dict[str, str],
) -> Optional[str]:
    """Build a ``SELECT * REPLACE (...) FROM <table_expr>`` that re-casts only
    the columns whose stored dtype differs from the schema target.

    Returns None if every shared column already matches (nothing to do).
    """
    replacements: list[str] = []
    for col, actual in actual_types.items():
        if col not in schema_targets:
            continue  # not declared (e.g. raw col we chose not to model) — leave as-is
        expr = cast_expr(col, actual, schema_targets[col])
        if expr is not None:
            replacements.append(expr)
    if not replacements:
        return None
    body = ",\n  ".join(replacements)
    return f"SELECT * REPLACE (\n  {body}\n) FROM {table_expr}"


# ---------------------------------------------------------------------------
# R2 I/O
# ---------------------------------------------------------------------------

def _rclone(*args: str) -> str:
    return subprocess.run(["rclone", *args], capture_output=True, text=True,
                          check=True).stdout.strip()


def _r2_creds() -> tuple[str, str]:
    """Read R2 key id + secret from the rclone `r2` remote, in-memory only."""
    show = _rclone("config", "show", "r2")
    akid = sec = ""
    for line in show.splitlines():
        s = line.strip()
        if s.startswith("access_key_id"):
            akid = s.split("=", 1)[1].strip()
        elif s.startswith("secret_access_key"):
            sec = s.split("=", 1)[1].strip()
    return akid, sec


def _connect() -> duckdb.DuckDBPyConnection:
    akid, sec = _r2_creds()
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute(
        "CREATE SECRET r2 (TYPE s3, PROVIDER config, KEY_ID ?, SECRET ?, "
        "REGION 'auto', ENDPOINT ?, URL_STYLE 'path');",
        [akid, sec, ENDPOINT_HOST],
    )
    return con


def _list_years(source: str) -> list[str]:
    out = _rclone("lsf", f"r2:{BUCKET}/{PREFIX}/")
    years = []
    pat = re.compile(rf"^{re.escape(source)}_(\d{{4}})\.parquet$")
    for line in out.splitlines():
        m = pat.match(line.strip())
        if m:
            years.append(m.group(1))
    return sorted(years)


def _actual_types(con, s3path: str) -> dict[str, str]:
    rows = con.execute(
        "SELECT column_name, column_type FROM "
        "(DESCRIBE SELECT * FROM read_parquet(?))", [s3path]
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def recast_one(con, source: str, scraper: Type[BaseScraper], year: str, *,
               mode: str) -> dict:
    """Plan (and optionally stage/execute) the recast for one year.

    mode: 'dry' (print SQL only) | 'stage' (write+verify .recast, no swap) |
          'execute' (write, verify, swap, refresh manifest).
    """
    fname = f"{source}_{year}.parquet"
    src_s3 = f"s3://{BUCKET}/{PREFIX}/{fname}"
    stage_s3 = f"s3://{BUCKET}/{PREFIX}/{fname}.recast.parquet"

    schema_targets = {name: duck_target(col.dtype)
                      for name, col in scraper.schema.to_schema().columns.items()}
    actual = _actual_types(con, src_s3)

    # Zero-padded code columns can't be faithfully cast — exclude them from
    # the recast (leave the stored bytes untouched) and surface them as
    # needing a producer re-ingest instead.
    reingest = sorted(c for c in schema_targets
                      if c in REINGEST_ONLY and c in actual
                      and actual[c].upper() != schema_targets[c].upper())
    castable = {k: v for k, v in schema_targets.items() if k not in REINGEST_ONLY}
    select_sql = build_recast_select(f"read_parquet('{src_s3}')", actual, castable)

    if select_sql is None:
        status = "clean (no-op)" if not reingest else "needs re-ingest only (no cast-fixable drift)"
        res = {"source": source, "year": year, "status": status}
        if reingest:
            res["reingest_only"] = reingest
        return res

    drifted = [c for c in actual
               if c in castable and cast_expr(c, actual[c], castable[c])]
    if mode == "dry":
        res = {"source": source, "year": year, "status": "would recast",
               "drifted_cols": drifted, "sql": select_sql}
        if reingest:
            res["reingest_only"] = reingest
        return res

    # Safety: the int-hop (float -> BIGINT -> VARCHAR) TRUNCATES any
    # fractional part. It's correct for integer-valued codes (NPI, FIPS,
    # zip) stored as floats, but would silently corrupt a genuine decimal
    # (a RUCA code, a rate). Refuse loudly if any value being int-hopped
    # actually carries a fraction.
    for c in drifted:
        expr = cast_expr(c, actual[c], schema_targets[c]) or ""
        if "AS BIGINT) AS VARCHAR" not in expr:
            continue
        n_frac = con.execute(
            f"SELECT count(*) FROM read_parquet('{src_s3}') "
            f'WHERE "{c}" IS NOT NULL AND "{c}" != floor("{c}")'
        ).fetchone()[0]
        if n_frac:
            return {"source": source, "year": year,
                    "status": "ABORT — int-hop would truncate fractional values",
                    "column": c, "fractional_rows": int(n_frac),
                    "hint": f"declare {c} as float (it has decimals) or fix the source"}

    # stage: COPY the recast result to a sibling key on R2
    con.execute(f"COPY ({select_sql}) TO '{stage_s3}' (FORMAT PARQUET);")
    # verify: every staged castable column's dtype must now equal its target.
    # (REINGEST_ONLY columns are intentionally left untouched, so they're not
    # checked here — they're reported as needing a re-ingest instead.)
    staged = _actual_types(con, stage_s3)
    mismatches = {c: (staged[c], t) for c, t in castable.items()
                  if c in staged and staged[c].upper() != t.upper()}
    if mismatches:
        return {"source": source, "year": year, "status": "STAGED-FAILED-VERIFY",
                "drifted_cols": drifted, "mismatches": mismatches, "staged_key": stage_s3}

    if mode == "stage":
        res = {"source": source, "year": year, "status": "staged (not swapped)",
               "drifted_cols": drifted, "staged_key": stage_s3}
        if reingest:
            res["reingest_only"] = reingest
        return res

    # execute: swap staged over original, then refresh the manifest
    _rclone("moveto", "--s3-no-check-bucket",
            f"r2:{BUCKET}/{PREFIX}/{fname}.recast.parquet",
            f"r2:{BUCKET}/{PREFIX}/{fname}")
    _refresh_manifest(con, scraper, source, year, src_s3, fname)
    res = {"source": source, "year": year, "status": "recast+swapped",
           "drifted_cols": drifted}
    if reingest:
        res["reingest_only"] = reingest
    return res


def _refresh_manifest(con, scraper, source: str, year: str, src_s3: str, fname: str) -> None:
    """Rewrite the sibling .manifest.json with new rows/bytes/columns."""
    mfname = f"{source}_{year}.manifest.json"
    try:
        old = json.loads(_rclone("cat", f"r2:{BUCKET}/{PREFIX}/{mfname}"))
    except subprocess.CalledProcessError:
        old = {}
    rows = con.execute(f"SELECT count(*) FROM read_parquet('{src_s3}')").fetchone()[0]
    size = int(_rclone("size", "--json", f"r2:{BUCKET}/{PREFIX}/{fname}")
               .strip() and json.loads(_rclone("size", "--json",
               f"r2:{BUCKET}/{PREFIX}/{fname}")).get("bytes", 0))
    old.update({
        "source": source, "rows": rows, "bytes": size,
        "schema_class": scraper.schema.__name__,
        "schema_version": scraper.schema_version,
        "schema_columns": sorted(scraper.expected_columns()),
    })
    old.setdefault("inputs", {})["dtype_recast"] = "schema-driven duckdb recast"
    tmp = f"/tmp/{mfname}"
    with open(tmp, "w") as f:
        json.dump(old, f, indent=2)
    _rclone("moveto", "--s3-no-check-bucket", tmp, f"r2:{BUCKET}/{PREFIX}/{mfname}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", required=True, choices=sorted(SCRAPERS),
                    help="panel source (maps to its scraper's Pandera schema)")
    ap.add_argument("--year", help="single year; default = all years on R2")
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument("--stage", action="store_true",
                     help="write + verify the .recast.parquet on R2 but do NOT swap")
    grp.add_argument("--execute", action="store_true",
                     help="swap recast over original + refresh manifest (irreversible)")
    args = ap.parse_args()

    mode = "execute" if args.execute else "stage" if args.stage else "dry"
    scraper = SCRAPERS[args.source]
    con = _connect()
    years = [args.year] if args.year else _list_years(args.source)
    if not years:
        raise SystemExit(f"no {args.source}_<year>.parquet found on r2:{BUCKET}/{PREFIX}/")

    for y in years:
        res = recast_one(con, args.source, scraper, y, mode=mode)
        sql = res.pop("sql", None)
        print(json.dumps(res))
        if sql and mode == "dry":
            print(sql + "\n")


if __name__ == "__main__":
    main()
