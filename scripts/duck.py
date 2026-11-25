"""Clean DuckDB query runner — agents call this instead of embedding python -c duckdb in bash.

Replaces the zsh-mangling `python -c "import duckdb; con.execute(f\"\"\"...\"\"\")..."` pattern
that caused ~17 failures in production runs (bad substitution + wrong-column errors).

Convenience table aliases for --year YYYY:
  partb       → data/processed/cms_partb_<year>.parquet
  partb_svc   → data/processed/cms_partb_svc_<year>.parquet
  partd       → data/processed/cms_partd_prescriber_drug_<year>.parquet
  dmepos      → data/processed/cms_dmepos_<year>.parquet

Usage:
    python scripts/duck.py --year 2022 "SELECT Rndrng_NPI, Tot_Srvcs FROM partb LIMIT 5"
    python scripts/duck.py --describe partb_svc --year 2022
    python scripts/duck.py --years 2020-2024 \\
        "SELECT HCPCS_Cd, SUM(Tot_Srvcs) s FROM partb_svc \\
         WHERE Rndrng_NPI='1900000016' GROUP BY 1"
    python scripts/duck.py --year 2022 --format csv "SELECT ..."
    python scripts/duck.py --year 2022 --limit 10 "SELECT ..."
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root, for fraudlib
try:
    from fraudlib import r2 as _r2
except Exception:
    _r2 = None  # calibration jail / standalone: local files only

REPO = Path(__file__).resolve().parents[1]
PROC = REPO / "data" / "processed"

# Canonical table-name → (filename_stem_prefix, default NPI column)
_TABLE_MAP: dict[str, str] = {
    "partb": "cms_partb",
    "partb_svc": "cms_partb_svc",
    "partd": "cms_partd_prescriber_drug",
    "dmepos": "cms_dmepos",
}

# Words that signal a write operation — rejected in read-only mode
_WRITE_WORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|CREATE|DROP|ATTACH|COPY|INSTALL|LOAD|PRAGMA)\b",
    re.IGNORECASE,
)


def _parquet_path(table: str, year: int) -> Path:
    stem = _TABLE_MAP[table]
    return PROC / f"{stem}_{year}.parquet"


def _bucket_enabled() -> bool:
    return bool(_r2 is not None and not _r2._r2_disabled() and _r2._r2_configured())


def _source(table: str, year: int) -> str | None:
    """Local parquet if present; else the configured bucket's copy (processed/v1); else None."""
    path = _parquet_path(table, year)
    if path.exists():
        return str(path)
    if _bucket_enabled() and path.name in _r2._v1_listing():
        return _r2._s3_url(f"{_r2.R2_PANELS}/{path.name}")
    return None


def _connect():
    """DuckDB connection; bucket credentials attached only when a bucket is configured."""
    if _bucket_enabled():
        return _r2.duckdb_r2()
    return duckdb.connect()


def _resolve_tables(sql: str, year: int | None) -> tuple[str, list[str]]:
    """Replace bare table names (partb, partb_svc, partd, dmepos) with read_parquet(...).

    Returns (rewritten_sql, list_of_missing_files).  If year is None and the SQL
    contains a bare alias, returns an error message list so the caller can bail out.
    """
    missing: list[str] = []

    def _replace(m: re.Match) -> str:
        alias = m.group(0)
        if alias not in _TABLE_MAP:
            return alias
        if year is None:
            missing.append(f"table '{alias}' requires --year YYYY")
            return alias
        path = _source(alias, year)
        if path is None:
            missing.append(
                f"table {alias} unavailable for {year} (not found locally at "
                f"{_parquet_path(alias, year)} or in a configured bucket; "
                f"run scripts/fetch_public_data.py --years {year})"
            )
            return alias
        return f"read_parquet('{path}')"

    pattern = re.compile(r"\b(" + "|".join(_TABLE_MAP.keys()) + r")\b")
    rewritten = pattern.sub(_replace, sql)
    return rewritten, missing


def _reject_writes(sql: str) -> str | None:
    """Return an error message if SQL contains write operations, else None."""
    m = _WRITE_WORDS.search(sql)
    if m:
        return (
            f"Error: write operation '{m.group(0)}' rejected — duck.py is read-only. "
            "Only SELECT/WITH/DESCRIBE/SUMMARIZE are allowed."
        )
    return None


def _fmt_value(v: object) -> str:
    """Human-readable cell value: large numbers with commas, else str."""
    if isinstance(v, float):
        if v == int(v) and abs(v) < 1e15:
            return f"{int(v):,}"
        return f"{v:,.2f}"
    if isinstance(v, int):
        return f"{v:,}"
    return "" if v is None else str(v)


def _print_table(rows: list[tuple], cols: list[str], limit: int, total: int) -> None:
    """Print aligned text table, cap at `limit` rows."""
    display = rows[:limit]
    str_rows = [[_fmt_value(c) for c in row] for row in display]
    widths = [len(c) for c in cols]
    for row in str_rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    sep = "+-" + "-+-".join("-" * w for w in widths) + "-+"
    header = "| " + " | ".join(c.ljust(widths[i]) for i, c in enumerate(cols)) + " |"
    print(sep)
    print(header)
    print(sep)
    for row in str_rows:
        print("| " + " | ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)) + " |")
    print(sep)
    print(f"{min(limit, total)} row(s) shown", end="")
    if total > limit:
        print(f"  ...{total - limit} more rows (use --limit to see more)")
    else:
        print()


def _print_csv(rows: list[tuple], cols: list[str]) -> None:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    w.writerows(rows)
    print(buf.getvalue(), end="")


def _print_json(rows: list[tuple], cols: list[str]) -> None:
    out = [dict(zip(cols, row)) for row in rows]
    print(json.dumps(out, default=str, indent=2))


def _run_query(sql: str, year: int | None, limit: int, fmt: str) -> int:
    """Execute SQL, print results. Returns exit code."""
    err = _reject_writes(sql)
    if err:
        print(err, file=sys.stderr)
        return 1

    rewritten, missing = _resolve_tables(sql, year)
    if missing:
        for msg in missing:
            print(f"Error: {msg}", file=sys.stderr)
        return 1

    try:
        con = _connect()
        result = con.execute(rewritten)
        cols = [d[0] for d in result.description]
        rows = result.fetchall()
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    total = len(rows)
    if fmt == "csv":
        _print_csv(rows, cols)
    elif fmt == "json":
        _print_json(rows, cols)
    else:
        _print_table(rows, cols, limit, total)
    return 0


def _parse_years(spec: str) -> list[int]:
    """Parse --years: '2019-2024', '2019,2020', or '2019 2020'."""
    spec = spec.strip()
    if re.match(r"^\d{4}-\d{4}$", spec):
        lo, hi = int(spec[:4]), int(spec[5:])
        return list(range(lo, hi + 1))
    # comma or space separated
    parts = re.split(r"[,\s]+", spec)
    return [int(p) for p in parts if p]


def _run_multiyear(sql: str, years: list[int], limit: int, fmt: str) -> int:
    err = _reject_writes(sql)
    if err:
        print(err, file=sys.stderr)
        return 1

    all_rows: list[tuple] = []
    cols: list[str] | None = None

    for yr in years:
        yr_sql = sql.replace("{year}", str(yr))
        rewritten, missing = _resolve_tables(yr_sql, yr)
        if missing:
            # table not available for this year — skip with notice, don't abort
            for msg in missing:
                print(f"Note: skipping {yr}: {msg}", file=sys.stderr)
            continue
        try:
            con = _connect()
            result = con.execute(rewritten)
            if cols is None:
                cols = ["year"] + [d[0] for d in result.description]
            rows = result.fetchall()
            all_rows.extend((yr,) + row for row in rows)
        except Exception as exc:
            print(f"Note: skipping {yr}: {exc}", file=sys.stderr)

    if not all_rows:
        print("No results across any year.", file=sys.stderr)
        return 1

    cols = cols or ["year"]
    total = len(all_rows)
    if fmt == "csv":
        _print_csv(all_rows, cols)
    elif fmt == "json":
        _print_json(all_rows, cols)
    else:
        _print_table(all_rows, cols, limit, total)
    return 0


def _run_describe(table: str, year: int | None) -> int:
    if table not in _TABLE_MAP:
        # Might be a raw parquet path
        if table.endswith(".parquet"):
            path = Path(table)
        else:
            print(
                f"Error: unknown table '{table}'. Known: {', '.join(_TABLE_MAP)}",
                file=sys.stderr,
            )
            return 1
    else:
        if year is None:
            print("Error: --describe requires --year YYYY", file=sys.stderr)
            return 1
        path = _source(table, year)
        if path is None:
            print(
                f"Error: table {table} unavailable for {year} (not found locally at "
                f"{_parquet_path(table, year)} or in a configured bucket)",
                file=sys.stderr,
            )
            return 1

    try:
        con = _connect()
        rows = con.execute(f"DESCRIBE SELECT * FROM read_parquet('{path}') LIMIT 1").fetchall()
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"{'Column':<50}  {'Type'}")
    print("-" * 70)
    for r in rows:
        print(f"{r[0]:<50}  {r[1]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Read-only DuckDB runner with CMS panel table aliases.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("sql", nargs="?", help="SQL query (SELECT/WITH/DESCRIBE/SUMMARIZE only)")
    p.add_argument("--year", type=int, metavar="YYYY",
                   help="Year for table aliases (partb, partb_svc, partd, dmepos)")
    p.add_argument("--years", metavar="SPEC",
                   help="Multi-year: '2019-2024', '2019,2020', or '2019 2020'. "
                        "Use {year} placeholder in SQL.")
    p.add_argument("--describe", metavar="TABLE",
                   help="Print column names+types for TABLE (requires --year)")
    p.add_argument("--limit", type=int, default=50,
                   help="Max rows for text output (default 50)")
    p.add_argument("--format", dest="fmt", choices=["table", "csv", "json"],
                   default="table", help="Output format (default: table)")
    args = p.parse_args(argv)

    if args.describe:
        return _run_describe(args.describe, args.year)

    if not args.sql:
        p.print_help()
        return 1

    if args.years:
        years = _parse_years(args.years)
        return _run_multiyear(args.sql, years, args.limit, args.fmt)

    return _run_query(args.sql, args.year, args.limit, args.fmt)


if __name__ == "__main__":
    sys.exit(main())
