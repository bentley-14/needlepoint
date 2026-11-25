"""Per-specialty triage norms COMPUTED from the feature panels (not hand-asserted).

For a given --year, computes per-specialty median + p95 of `partd_opioid_clm_share`
and median dual-eligible share (`partb_dual_pct`) from
data/cache/feature_frames/features_full_{year}.parquet (fallback: features_{year}.parquet),
keeping only specialties (`partb_provider_type`) with >= 200 providers (stability floor).
Opioid stats are computed over Part D-active providers (has_partd = 1) when that column
exists — the panel imputes opioid share to 0 for non-prescribers, which would deflate
the norm.

Output: data/cache/triage_norms/norms_{year}.json — rendered into the Stage-3 triage
prompt via the `{computed_norms}` substitution (see agents/triage.py), superseding the
hand-curated OPIOID_NORMS / DUAL_SHARE_NORMS reference tables when present. Rationale:
hand-asserted constants are exactly the failure mode of the 99489 episode
(docs/CANONICAL_LOOP_GUARDRAILS.md) — lookup-able numbers must be computed or verified.

Usage:
    python scripts/compute_triage_norms.py --year 2023
    python scripts/compute_triage_norms.py --year 2023 --format json   # also print to stdout
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # repo root, so fraudlib imports

import argparse
import datetime
import json
import sys
from pathlib import Path

import duckdb

try:
    from fraudlib import r2
except Exception:  # calibration jail / standalone copy: no fraudlib → local-only, never R2
    from pathlib import Path as _RP
    class _LocalR2:
        @staticmethod
        def parquet_source(local, key): return str(local)
        @staticmethod
        def duckdb_r2():
            import duckdb; return duckdb.connect()
    r2 = _LocalR2()

REPO = Path(__file__).resolve().parents[1]
FRAMES = REPO / "data/cache/feature_frames"
OUT_DIR = REPO / "data/cache/triage_norms"
R2_FRAMES = "r2:needlepoint-data/cache/v1/feature_frames"

MIN_PROVIDERS = 200
SPECIALTY_COL = "partb_provider_type"
OPIOID_COL = "partd_opioid_clm_share"
DUAL_COL = "partb_dual_pct"


def _features_source(year: int) -> str:
    """duckdb source for the year's feature panel: local features_full first,
    then local features (lean), then R2 features_full."""
    for fam in ("features_full", "features"):
        local = FRAMES / f"{fam}_{year}.parquet"
        if local.exists():
            return str(local)
    return r2.parquet_source(FRAMES / f"features_full_{year}.parquet",
                             f"{R2_FRAMES}/features_full_{year}.parquet")


def compute_norms(source: str, year: int, min_providers: int = MIN_PROVIDERS,
                  con=None) -> dict:
    """Compute the per-specialty norms dict from one feature parquet.

    Pure data → dict (no I/O besides the parquet read) so tests can drive it
    with a tiny synthetic panel. `source` is any duckdb-readable parquet path.
    """
    con = con or r2.duckdb_r2()
    cols = {row[0] for row in con.execute(
        f"DESCRIBE SELECT * FROM read_parquet('{source}')").fetchall()}
    for required in (SPECIALTY_COL, OPIOID_COL, DUAL_COL):
        if required not in cols:
            raise SystemExit(f"ERROR: {source} lacks required column {required!r}")
    # Non-prescribers carry an imputed opioid share of 0 — norm over prescribers only.
    op_filter = " FILTER (WHERE has_partd = 1)" if "has_partd" in cols else ""
    rows = con.execute(f"""
        SELECT {SPECIALTY_COL} AS specialty,
               COUNT(*) AS n_providers,
               COUNT({OPIOID_COL}){op_filter} AS n_opioid,
               median({OPIOID_COL}){op_filter} AS opioid_med,
               quantile_cont({OPIOID_COL}, 0.95){op_filter} AS opioid_p95,
               median({DUAL_COL}) AS dual_med
        FROM read_parquet('{source}')
        WHERE {SPECIALTY_COL} IS NOT NULL
          AND regexp_matches({SPECIALTY_COL}, '[A-Za-z]')   -- drop numeric fill values ('0.0')
        GROUP BY 1
        HAVING COUNT(*) >= ?
        ORDER BY n_providers DESC
    """, [min_providers]).fetchall()

    def _r(v):
        return round(float(v), 4) if v is not None else None

    return {
        "_meta": {
            "year": year,
            "source": str(source),
            "generated": datetime.date.today().isoformat(),
            "min_providers": min_providers,
            "opioid_basis": ("Part D-active providers (has_partd = 1)" if op_filter
                             else "all providers in specialty"),
            "columns": {"specialty": SPECIALTY_COL, "opioid_share": OPIOID_COL,
                        "dual_share": DUAL_COL},
        },
        "specialties": {
            spec: {
                "n_providers": int(n),
                "n_opioid": int(n_op),
                "opioid_share_median": _r(op_med),
                "opioid_share_p95": _r(op_p95),
                "dual_share_median": _r(dual_med),
            }
            for spec, n, n_op, op_med, op_p95, dual_med in rows
        },
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--year", type=int, required=True, help="panel year, e.g. 2023")
    ap.add_argument("--min-providers", type=int, default=MIN_PROVIDERS,
                    help=f"specialty stability floor (default {MIN_PROVIDERS})")
    ap.add_argument("--format", choices=["json"], default=None,
                    help="'json' → also print the norms JSON to stdout")
    args = ap.parse_args(argv)

    source = _features_source(args.year)
    norms = compute_norms(source, args.year, min_providers=args.min_providers)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"norms_{args.year}.json"
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(norms, indent=2) + "\n")
    tmp.replace(out)  # atomic — never leave a half-written norms file

    if args.format == "json":
        print(json.dumps(norms, indent=2))
    else:
        print(f"wrote {out} — {len(norms['specialties'])} specialties "
              f"(>= {args.min_providers} providers) from {source}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
