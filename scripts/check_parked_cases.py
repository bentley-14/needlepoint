"""PARKED-case re-trigger scanner (M3) — wake slot-3-only abandons.

A case abandoned ONLY because convergence-floor slot 3 was empty (audit
verdict PASS_NO_SLOT3 — no falsity anchor AND no regulatory corroboration)
is parked by the orchestrator as a sidecar record under
``<state_root>/parked/<case_id>.json`` with the NPIs worth watching (target +
co-billers). This scanner checks every watch NPI against the regulatory
sources whose refresh can fill slot 3:

    data/processed/revoked_medicare_providers.parquet
    data/processed/leie.parquet
    data/processed/ca_si_list.parquet
    data/extracted/gold_set.jsonl   (npi field)

NPI columns are detected from each parquet's DESCRIBE output; a source whose
file is absent is skipped cleanly. Exit code is always 0.

Usage:
    python scripts/check_parked_cases.py
    python scripts/check_parked_cases.py --state-root fraud-investigator-v2/state --quiet
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # repo root, so fraudlib imports

import argparse
import json
import sys
from pathlib import Path

import duckdb

try:
    from fraudlib import r2
except Exception:  # calibration jail / standalone copy: no fraudlib → local-only, never R2
    class _LocalR2:
        @staticmethod
        def duckdb_r2():
            import duckdb; return duckdb.connect()
    r2 = _LocalR2()

REPO = Path(__file__).resolve().parents[1]
STATE_ROOT_DEFAULT = REPO / "fraud-investigator-v2" / "state"
DATA_ROOT_DEFAULT = REPO / "data"

PARQUET_SOURCES = (
    "processed/revoked_medicare_providers.parquet",
    "processed/leie.parquet",
    "processed/ca_si_list.parquet",
)
GOLD_SET_REL = "extracted/gold_set.jsonl"
MAX_ROWS_PER_SOURCE = 5


def _npi_columns(con, src: str) -> list[tuple[str, bool]]:
    """DESCRIBE-driven NPI-column detection → [(column, is_list)].

    A column qualifies when its name contains 'npi' and its type can hold NPI
    values (VARCHAR / integer scalar, or VARCHAR[]); flag/count columns
    (has_npi BOOLEAN, n_npis BIGINT-by-name) are excluded.
    """
    cols = con.execute(f"DESCRIBE SELECT * FROM read_parquet('{src}')").fetchall()
    out = []
    for name, ctype, *_ in cols:
        low = name.lower()
        if "npi" not in low or low.startswith(("has_", "n_")):
            continue
        if ctype == "VARCHAR[]":
            out.append((name, True))
        elif ctype.startswith(("VARCHAR", "BIGINT", "INTEGER", "UBIGINT", "HUGEINT")):
            out.append((name, False))
    return out


def _row_summary(colnames: list[str], row: tuple, limit: int = 8) -> dict:
    """First few non-null fields of a matched row — enough to identify it."""
    out = {}
    for name, val in zip(colnames, row):
        if val is None or val == "":
            continue
        out[name] = val if isinstance(val, (int, float, bool, list)) else str(val)
        if len(out) >= limit:
            break
    return out


def _scan_parquet(con, path: Path, watch: list[str]) -> list[dict]:
    """Hits for any watch NPI in one parquet source (absent file → [])."""
    if not path.exists():
        return []
    src = str(path).replace("'", "''")
    try:
        npi_cols = _npi_columns(con, src)
    except duckdb.Error:
        return []
    hits = []
    for col, is_list in npi_cols:
        if is_list:
            cond = f'list_has_any(CAST("{col}" AS VARCHAR[]), ?)'
            params: list = [watch]
        else:
            placeholders = ",".join("?" for _ in watch)
            cond = f'CAST("{col}" AS VARCHAR) IN ({placeholders})'
            params = list(watch)
        try:
            cur = con.execute(
                f"SELECT * FROM read_parquet('{src}') WHERE {cond} "
                f"LIMIT {MAX_ROWS_PER_SOURCE}", params)
            colnames = [d[0] for d in cur.description]
            rows = cur.fetchall()
        except duckdb.Error:
            continue
        for row in rows:
            val = row[colnames.index(col)]
            vals = [str(v) for v in val] if isinstance(val, list) else [str(val)]
            matched = sorted(set(vals) & set(watch))
            hits.append({
                "source": path.name,
                "npi": matched[0] if matched else vals[0],
                "matched_column": col,
                "row": _row_summary(colnames, row),
            })
    return hits


def _scan_gold_set(path: Path, watch: list[str]) -> list[dict]:
    if not path.exists():
        return []
    watch_set = set(watch)
    hits = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        npi = str(rec.get("npi") or "")
        if npi in watch_set:
            summary = {k: rec[k] for k in ("npi", "case_id", "case_name", "title", "date")
                       if k in rec}
            hits.append({"source": path.name, "npi": npi,
                         "matched_column": "npi", "row": summary or {"npi": npi}})
            if len(hits) >= MAX_ROWS_PER_SOURCE:
                break
    return hits


def scan(state_root: Path, data_root: Path) -> list[dict]:
    parked_dir = state_root / "parked"
    results = []
    con = r2.duckdb_r2()
    for f in sorted(parked_dir.glob("*.json")) if parked_dir.is_dir() else []:
        try:
            rec = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        watch = [str(n) for n in (rec.get("watch_npis") or []) if n]
        hits = []
        for rel in PARQUET_SOURCES:
            hits.extend(_scan_parquet(con, data_root / rel, watch))
        hits.extend(_scan_gold_set(data_root / GOLD_SET_REL, watch))
        results.append({
            "case_id": rec.get("case_id") or f.stem,
            "subject": rec.get("subject"),
            "parked_at": rec.get("parked_at"),
            "missing": rec.get("missing"),
            "watch_npis": watch,
            "hits": hits,
            "resume_cmd": rec.get("resume_cmd"),
        })
    return results


def print_report(results: list[dict], quiet: bool) -> None:
    shown = [r for r in results if r["hits"]] if quiet else results
    if not results:
        print("No parked cases found.")
        return
    n_hits = sum(1 for r in results if r["hits"])
    print(f"=== Parked-case re-trigger scan — {len(results)} parked, {n_hits} with hits ===")
    for r in shown:
        flag = "RETRIGGER" if r["hits"] else "no change"
        print(f"\n[{flag}] {r['case_id']} (parked {r['parked_at']}) — "
              f"watch NPIs: {', '.join(r['watch_npis']) or '—'}")
        print(f"    missing: {r['missing']}")
        for h in r["hits"]:
            print(f"    HIT {h['source']}  npi={h['npi']}  ({h['matched_column']})  {h['row']}")
        if r["hits"] and r.get("resume_cmd"):
            print(f"    resume: {r['resume_cmd']}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--state-root", default=str(STATE_ROOT_DEFAULT),
                    help="Orchestrator state root holding parked/ (default: fraud-investigator-v2/state)")
    ap.add_argument("--data-root", default=str(DATA_ROOT_DEFAULT),
                    help="Data root holding processed/ + extracted/ (default: data/)")
    ap.add_argument("--format", choices=["table", "json"], default="table")
    ap.add_argument("--quiet", action="store_true", help="Print only cases with hits")
    args = ap.parse_args(argv)

    results = scan(Path(args.state_root), Path(args.data_root))
    if args.format == "json":
        out = [r for r in results if r["hits"]] if args.quiet else results
        print(json.dumps(out, indent=2, default=str))
    else:
        print_report(results, args.quiet)
    return 0


if __name__ == "__main__":
    sys.exit(main())
