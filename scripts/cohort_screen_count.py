#!/usr/bin/env python3
"""National cohort screen count — the selection-effect / multiple-comparisons answer.

Counts how many providers NATIONALLY pass an exact screen ("only 3 of 500,000 match"),
converting a percentile extremity claim into a selectivity fact. This is the honest
multiple-comparisons correction for the *Integra Med Analytics v. Baylor Scott & White*
(5th Cir. 2020) data-dredging defense: a naive "99.8th percentile" ignores that the
provider was SELECTED from ~500K NPIs; the national match count of the exact screen
does not. A tiny count is the strongest pleading fact; a large one is a warning the
extremity claim is weak.

Data: data/cache/feature_frames/features_full_<year>.parquet if available, else
features_<year>.parquet, else data/processed/cms_partb_<year>.parquet. Column
availability varies — run with a bogus column to get the near-miss list, or DESCRIBE.

Usage:
    python scripts/cohort_screen_count.py --year 2019 \
        --where "hcpcs_total_payment >= 1000000" --where "services_per_bene >= 300"
    python scripts/cohort_screen_count.py --year 2019 \
        --where "Tot_Srvcs >= 50000" --specialty "Internal Medicine" --format json --list
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # repo root, so fraudlib imports

import argparse
import difflib
import json
import re
import sys
from pathlib import Path

import duckdb

try:
    from fraudlib import r2
except Exception:  # calibration jail / standalone copy: no fraudlib → local-only, never R2
    from pathlib import Path as _RP
    class _LocalR2:
        R2_PANELS = ""
        @staticmethod
        def parquet_source(local, key): return str(local)
        @staticmethod
        def fetch_local(local, key): return _RP(local)
        @staticmethod
        def attach_r2(con): pass
        @staticmethod
        def duckdb_r2():
            import duckdb; return duckdb.connect()
    r2 = _LocalR2()

REPO = Path(__file__).resolve().parents[1]
CACHE = REPO / "data/cache/feature_frames"
PROC = REPO / "data/processed"
_R2_CACHE = "r2:needlepoint-data/cache/v1/feature_frames"

OPS = (">=", "<=", "==", "!=", ">", "<")
_PRED_RE = re.compile(r"^\s*([A-Za-z0-9_]+)\s*(>=|<=|==|!=|>|<)\s*(.+?)\s*$")
SPECIALTY_COLS = ("provider_type", "partb_provider_type", "Rndrng_Prvdr_Type")
NPI_COLS = ("npi", "Rndrng_NPI")
LIST_THRESHOLD = 25


class Predicate:
    """One validated `column OP value` screen predicate (value is bound as a SQL
    parameter, never interpolated — the column name is regex- AND schema-validated)."""

    def __init__(self, column: str, op: str, value: float | str, raw: str):
        self.column, self.op, self.value, self.raw = column, op, value, raw

    def __repr__(self):
        return f"Predicate({self.raw!r})"


def parse_predicate(text: str) -> Predicate:
    """Parse one --where string. Strict: column must be ^[A-Za-z0-9_]+$, OP one of
    >= <= > < == !=, value a float or a quoted string. Anything else raises ValueError
    (this is the injection gate — raw user text NEVER reaches SQL)."""
    m = _PRED_RE.match(text)
    if not m:
        raise ValueError(
            f"malformed predicate {text!r} — expected: column OP value "
            f"(column = letters/digits/underscore, OP in {' '.join(OPS)})"
        )
    col, op, raw_val = m.group(1), m.group(2), m.group(3)
    if len(raw_val) >= 2 and raw_val[0] == raw_val[-1] and raw_val[0] in ("'", '"'):
        value: float | str = raw_val[1:-1]
        if op not in ("==", "!="):
            raise ValueError(f"predicate {text!r}: string values only support == / !=")
    else:
        try:
            value = float(raw_val)
        except ValueError:
            raise ValueError(
                f"predicate {text!r}: value {raw_val!r} must be a number or a quoted string"
            ) from None
    return Predicate(col, op, value, text.strip())


def _columns(con, src: str) -> list[str]:
    return [row[0] for row in con.execute(
        "DESCRIBE SELECT * FROM read_parquet(?)", [src]).fetchall()]


def _require_column(col: str, columns: list[str]) -> None:
    if col in columns:
        return
    near = difflib.get_close_matches(col, columns, n=5, cutoff=0.5)
    near += [c for c in columns if col.lower() in c.lower() and c not in near][:5]
    hint = f" — near misses: {', '.join(near[:5])}" if near else ""
    raise ValueError(f"column {col!r} not in parquet ({len(columns)} columns){hint}")


def _sql_pred(p: Predicate) -> str:
    op = "=" if p.op == "==" else ("<>" if p.op == "!=" else p.op)
    return f'"{p.column}" {op} ?'


def run_screen(parquet_path: str | Path, predicates: list[Predicate],
               specialty: str | None = None, con=None,
               want_npis: bool = False) -> dict:
    """Core screen: AND all predicates (+ optional specialty cohort filter) over one
    parquet; return total/cohort/match counts, per-predicate marginals (within the
    cohort), and the matching NPIs when ≤ LIST_THRESHOLD or want_npis."""
    con = con or duckdb.connect()
    src = str(parquet_path)
    columns = _columns(con, src)
    for p in predicates:
        _require_column(p.column, columns)

    spec_col = next((c for c in SPECIALTY_COLS if c in columns), None)
    if specialty is not None and spec_col is None:
        raise ValueError(
            f"--specialty given but no specialty column found "
            f"(looked for {', '.join(SPECIALTY_COLS)})")
    cohort_sql = f'"{spec_col}" = ?' if specialty is not None else "TRUE"
    cohort_params: list = [specialty] if specialty is not None else []

    screen_sql = " AND ".join(_sql_pred(p) for p in predicates) or "TRUE"
    screen_params = [p.value for p in predicates]

    select = [
        "COUNT(*) AS total",
        f"COUNT(*) FILTER (WHERE {cohort_sql}) AS cohort",
        f"COUNT(*) FILTER (WHERE {cohort_sql} AND {screen_sql}) AS matched",
    ]
    params = list(cohort_params) + list(cohort_params) + list(screen_params)
    for p in predicates:  # marginal: each predicate alone, within the cohort
        select.append(f"COUNT(*) FILTER (WHERE {cohort_sql} AND {_sql_pred(p)})")
        params += cohort_params + [p.value]
    row = con.execute(
        f"SELECT {', '.join(select)} FROM read_parquet(?)", params + [src]).fetchone()
    total, cohort, matched = row[0], row[1], row[2]
    marginals = [{"predicate": p.raw, "count": row[3 + i]}
                 for i, p in enumerate(predicates)]

    npi_col = next((c for c in NPI_COLS if c in columns), None)
    npis = None
    if npi_col and (want_npis or matched <= LIST_THRESHOLD):
        npis = [str(r[0]) for r in con.execute(
            f'SELECT DISTINCT CAST("{npi_col}" AS VARCHAR) FROM read_parquet(?) '
            f"WHERE {cohort_sql} AND {screen_sql} ORDER BY 1 LIMIT 1000",
            [src] + cohort_params + screen_params).fetchall()]

    return {
        "source": src,
        "total_rows": total,
        "cohort_rows": cohort,
        "specialty": specialty,
        "specialty_column": spec_col if specialty is not None else None,
        "screen_count": matched,
        "match_rate": (matched / cohort) if cohort else 0.0,
        "marginals": marginals,
        "npis": npis,
    }


def _resolve_source(year: int, con) -> str:
    """Pick the richest available panel for the year: features_full → features →
    cms_partb. Probe each (local-or-R2 via parquet_source) with DESCRIBE; on an
    R2-first miss, fall back to the local copy before moving on."""
    candidates = [
        (CACHE / f"features_full_{year}.parquet", f"{_R2_CACHE}/features_full_{year}.parquet"),
        (CACHE / f"features_{year}.parquet", f"{_R2_CACHE}/features_{year}.parquet"),
        (PROC / f"cms_partb_{year}.parquet", f"{r2.R2_PANELS}/cms_partb_{year}.parquet"),
    ]
    for local, key in candidates:
        src = r2.parquet_source(local, key)
        for attempt in dict.fromkeys([src, str(local)] if local.exists() else [src]):
            try:
                con.execute("DESCRIBE SELECT * FROM read_parquet(?)", [attempt])
                return attempt
            except duckdb.Error:
                continue
    raise FileNotFoundError(
        f"no data source for year {year} — looked for "
        + ", ".join(str(local) for local, _ in candidates))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--year", type=int, required=True, help="panel year, e.g. 2019")
    ap.add_argument("--where", action="append", default=[], metavar='"COL OP VALUE"',
                    help=f"repeatable screen predicate, OP in {' '.join(OPS)}; "
                         "e.g. --where \"services_per_bene >= 300\"")
    ap.add_argument("--specialty", help="restrict the cohort to one specialty "
                                        "(provider_type / partb_provider_type / Rndrng_Prvdr_Type)")
    ap.add_argument("--list", action="store_true", help="print matching NPIs even when > 25")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    args = ap.parse_args(argv)

    if not args.where:
        print("ERROR: at least one --where predicate is required", file=sys.stderr)
        return 2
    try:
        preds = [parse_predicate(w) for w in args.where]
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    con = r2.duckdb_r2()
    try:
        src = _resolve_source(args.year, con)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    try:
        res = run_screen(src, preds, specialty=args.specialty, con=con,
                         want_npis=args.list)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    res["year"] = args.year
    if args.format == "json":
        print(json.dumps(res, indent=2, default=str))
        return 0

    print(f"=== Cohort screen count — year {args.year} ===")
    print(f"  source: {res['source']}")
    print(f"  total rows scanned: {res['total_rows']:,}")
    if args.specialty is not None:
        print(f"  cohort ({res['specialty_column']} = {args.specialty!r}): "
              f"{res['cohort_rows']:,} rows")
    print("  per-predicate marginal counts (each predicate alone, within cohort):")
    for m in res["marginals"]:
        print(f"    {m['predicate']:<50} {m['count']:>10,}")
    denom = res["cohort_rows"]
    print(f"  SCREEN COUNT (all predicates AND-combined): "
          f"{res['screen_count']:,} of {denom:,}  "
          f"(match rate {res['match_rate']:.6%})")
    if res["npis"] is not None:
        if res["npis"]:
            print(f"  matching NPIs ({len(res['npis'])}):")
            for n in res["npis"]:
                print(f"    {n}")
        else:
            print("  matching NPIs: none")
    elif res["screen_count"] > LIST_THRESHOLD:
        print(f"  (matches > {LIST_THRESHOLD}; pass --list to print NPIs)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
