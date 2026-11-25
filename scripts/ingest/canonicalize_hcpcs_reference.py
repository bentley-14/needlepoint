#!/usr/bin/env python3
"""Bake the CANONICAL per-year CPT descriptor (CMS MPFS RVU) into the HCPCS
reference, so consumers get the authoritative descriptor instead of relying only
on the Part B PUF's denormalized (and sometimes wrong) HCPCS_Desc.

Adds two columns to data/ground_truth/{hcpcs_codes_by_year,hcpcs_codes}.parquet
(keeps the readable PUF long descriptor for prose; adds provenance + the
authoritative short form):
  - canonical_short_descriptor : CMS MPFS RVU short descriptor for that code(+year),
                                 or NULL where no canonical match exists.
  - descriptor_source          : 'cms_mpfs_rvu' (matched) | 'cms_partb_puf' (fallback).

2020/2021: CMS removed those RVU files from the live site AND Wayback (verified
2026-06-11), so those rows fall back to descriptor_source='cms_partb_puf' (explicit,
not silent). 2020 PUF descriptors are error-free; 2021's known errors are corrected
at read time by the hcpcs_lookup authoritative-override layer.

Idempotent (re-derives the two columns). Backs up originals; atomic write; asserts
row counts are unchanged.
  python3 scripts/ingest/canonicalize_hcpcs_reference.py
"""
from __future__ import annotations

import shutil, sys
from datetime import datetime, timezone
from pathlib import Path

import duckdb

REPO = Path(__file__).resolve().parents[2]
GT = REPO / "data" / "ground_truth"
BY_YEAR = GT / "hcpcs_codes_by_year.parquet"
MOST_RECENT = GT / "hcpcs_codes.parquet"
RVU = GT / "cpt_descriptors_by_year.parquet"
_DERIVED = ("canonical_short_descriptor", "descriptor_source")


def _base_cols(con, p: Path) -> str:
    cols = [c[0] for c in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{p}')").fetchall()
            if c[0] not in _DERIVED]
    return ", ".join(f'h."{c}"' for c in cols)


def _backup(p: Path) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    b = p.with_suffix(f".parquet.pre_canonical.{ts}.bak")
    shutil.copy2(p, b)
    return b.name


def _write(df, p: Path, n_before: int) -> None:
    assert len(df) == n_before, f"row count changed for {p.name}: {n_before} -> {len(df)}"
    tmp = p.with_suffix(".parquet.tmp")
    df.to_parquet(tmp, index=False)
    tmp.replace(p)


def main() -> int:
    for p in (BY_YEAR, MOST_RECENT, RVU):
        if not p.exists():
            print(f"missing {p}", file=sys.stderr); return 1
    con = duckdb.connect()

    # by-year: authoritative descriptor for the exact code+year
    n = con.execute(f"SELECT count(*) FROM read_parquet('{BY_YEAR}')").fetchone()[0]
    print(f"by_year: {n:,} rows (backup {_backup(BY_YEAR)})")
    df = con.execute(f"""
        SELECT {_base_cols(con, BY_YEAR)},
               r.short_descriptor AS canonical_short_descriptor,
               CASE WHEN r.short_descriptor IS NOT NULL THEN 'cms_mpfs_rvu'
                    ELSE 'cms_partb_puf' END AS descriptor_source
        FROM read_parquet('{BY_YEAR}') h
        LEFT JOIN read_parquet('{RVU}') r ON upper(h.code)=r.code AND h.year=r.year
    """).df()
    _write(df, BY_YEAR, n)
    cov = dict(con.execute(f"SELECT descriptor_source, count(*) FROM read_parquet('{BY_YEAR}') GROUP BY 1").fetchall())
    print(f"  coverage: {cov}")

    # most-recent: each code's latest available RVU short descriptor
    n2 = con.execute(f"SELECT count(*) FROM read_parquet('{MOST_RECENT}')").fetchone()[0]
    print(f"most_recent: {n2:,} rows (backup {_backup(MOST_RECENT)})")
    df2 = con.execute(f"""
        WITH latest AS (
          SELECT code, short_descriptor,
                 row_number() OVER (PARTITION BY code ORDER BY year DESC) AS rn
          FROM read_parquet('{RVU}')
        )
        SELECT {_base_cols(con, MOST_RECENT)},
               l.short_descriptor AS canonical_short_descriptor,
               CASE WHEN l.short_descriptor IS NOT NULL THEN 'cms_mpfs_rvu'
                    ELSE 'cms_partb_puf' END AS descriptor_source
        FROM read_parquet('{MOST_RECENT}') h
        LEFT JOIN latest l ON upper(h.code)=l.code AND l.rn=1
    """).df()
    _write(df2, MOST_RECENT, n2)
    cov2 = dict(con.execute(f"SELECT descriptor_source, count(*) FROM read_parquet('{MOST_RECENT}') GROUP BY 1").fetchall())
    print(f"  coverage: {cov2}")
    print("done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
