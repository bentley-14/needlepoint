"""Per-NPI DMEPOS referral line-item breakdown — WHICH HCPCS codes a referrer ordered.

The aggregated `cms_dmepos` PUF tells you THAT a provider is a high-volume DMEPOS
referrer; this surfaces WHICH codes — turning a high-volume flag into a specific,
chargeable scheme hypothesis. Source: `cms_dmepos_detailed_<year>.parquet`, one row
per (Referring NPI × HCPCS_CD × Suplr_Rentl_Ind) line item, 2014-2023.

Full domain primer (acronyms, schemes, joins, suppression): run
`python scripts/source_primer.py --source cms_dmepos_detailed`.

Dollar columns are PER-SERVICE AVERAGES, not totals — this tool derives
`tot_paid = Avg_Suplr_Mdcr_Pymt_Amt * Tot_Suplr_Srvcs`. CMS suppresses any element
with <11 claims → those counts/amounts arrive null (a suppressed line is invisible).

Derived fraud signals per year (see primer §4):
  - L-code paid share          → brace-mill (orthotic) concentration  (4a)
  - K-code paid share          → power-wheelchair scheme               (4b)
  - rental months/bene (E)     → rental-cycle abuse on capped items    (4c)
  - HCPCS diversity + top-1/3 concentration → mill fingerprint
  - referrer specialty         → specialty/credential mismatch         (4d)

Usage:
    python scripts/dmepos_detailed_lookup.py --npi 1900000075
    python scripts/dmepos_detailed_lookup.py --npi 1900000075 --year 2022 --top 25
    python scripts/dmepos_detailed_lookup.py --npi 1900000075 --format json
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
        @staticmethod
        def list_v1_years(family): return []  # jail: years come from the local workspace glob
    r2 = _LocalR2()

REPO = Path(__file__).resolve().parents[1]
FAMILY = "cms_dmepos_detailed"
# Detailed DMEPOS ships under data/panels/ (see primer §8); fall back to processed/.
LOCAL_DIRS = (REPO / "data/panels", REPO / "data/processed")

# Columns we want, in display order. We DESCRIBE the file first and keep only the
# ones present (the primer warns names can drift), so a dropped column degrades
# gracefully instead of crashing the query.
_WANT = [
    "HCPCS_CD", "HCPCS_Desc", "Suplr_Rentl_Ind",
    "Tot_Suplrs", "Tot_Suplr_Benes", "Tot_Suplr_Clms", "Tot_Suplr_Srvcs",
    "Avg_Suplr_Mdcr_Pymt_Amt", "Avg_Suplr_Sbmtd_Chrg",
]
_SPECIALTY_ALIASES = ("Rfrg_Prvdr_Spclty_Desc", "Rfrg_Prvdr_Type", "Spclty")


def _local(year: int) -> Path:
    for d in LOCAL_DIRS:
        p = d / f"{FAMILY}_{year}.parquet"
        if p.exists():
            return p
    return LOCAL_DIRS[0] / f"{FAMILY}_{year}.parquet"  # canonical path for the R2 fallback


def _years() -> list[int]:
    local = set()
    for d in LOCAL_DIRS:
        for p in d.glob(f"{FAMILY}_*.parquet"):
            tail = p.stem.rsplit("_", 1)[-1]
            if tail.isdigit():
                local.add(int(tail))
    return sorted(local | set(r2.list_v1_years(FAMILY)))


def _f(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _src(year: int) -> str:
    return r2.parquet_source(_local(year), f"{r2.R2_PANELS}/{FAMILY}_{year}.parquet")


def _present(con, src: str, names) -> list[str]:
    cols = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{src}')").fetchall()}
    return [n for n in names if n in cols], cols


def _rows_for_year(con, src: str, npi: str) -> list[dict]:
    keep, cols = _present(con, src, _WANT)
    spec = next((c for c in _SPECIALTY_ALIASES if c in cols), None)
    select = ", ".join(keep + ([spec] if spec else []))
    try:
        res = con.execute(
            f"SELECT {select} FROM read_parquet('{src}') WHERE CAST(npi AS VARCHAR) = '{npi}'"
        ).fetchall()
    except duckdb.Error as e:
        print(f"  query failed on {src}: {e}", file=sys.stderr)
        return []
    field = keep + (["_specialty"] if spec else [])
    out = []
    for r in res:
        d = dict(zip(field, r))
        srvcs, avg = _f(d.get("Tot_Suplr_Srvcs")), _f(d.get("Avg_Suplr_Mdcr_Pymt_Amt"))
        d["tot_paid"] = (avg * srvcs) if (avg is not None and srvcs is not None) else None
        out.append(d)
    return out


def _signals(rows: list[dict]) -> dict:
    paid = [r for r in rows if r.get("tot_paid") is not None]
    total = sum(r["tot_paid"] for r in paid) or 0.0
    by_code = sorted((r["tot_paid"] for r in paid), reverse=True)

    def _prefix_share(pfx: str) -> float | None:
        if not total:
            return None
        s = sum(r["tot_paid"] for r in paid if str(r.get("HCPCS_CD", "")).upper().startswith(pfx))
        return round(s / total, 3)

    # Rental-cycle: max months-per-bene among rented lines (srvcs/benes on Rentl_Ind=Y).
    rent_mpb = None
    for r in rows:
        if str(r.get("Suplr_Rentl_Ind", "")).upper() == "Y":
            s, b = _f(r.get("Tot_Suplr_Srvcs")), _f(r.get("Tot_Suplr_Benes"))
            if s and b:
                rent_mpb = max(rent_mpb or 0, s / b)
    spec = next((r.get("_specialty") for r in rows if r.get("_specialty")), None)
    return {
        "total_paid": round(total, 2),
        "n_codes": len({r.get("HCPCS_CD") for r in rows if r.get("HCPCS_CD")}),
        "top1_share": round(by_code[0] / total, 3) if (by_code and total) else None,
        "top3_share": round(sum(by_code[:3]) / total, 3) if (by_code and total) else None,
        "l_code_share": _prefix_share("L"),
        "k_code_share": _prefix_share("K"),
        "max_rental_months_per_bene": round(rent_mpb, 1) if rent_mpb else None,
        "referrer_specialty": spec,
    }


def _print_year(year: int, rows: list[dict], top: int) -> None:
    sig = _signals(rows)
    print(f"\n=== {year} — DMEPOS referrals (NPI line items) ===")
    print(f"  specialty: {sig['referrer_specialty'] or '—'}  ·  total paid ${sig['total_paid']:,.0f}  ·  "
          f"{sig['n_codes']} codes  ·  top-1 {sig['top1_share']}  top-3 {sig['top3_share']}")
    print(f"  L-code share {sig['l_code_share']} (brace)  ·  K-code share {sig['k_code_share']} (wheelchair)  ·  "
          f"max rental months/bene {sig['max_rental_months_per_bene']}")
    ranked = sorted(rows, key=lambda r: (r.get("tot_paid") or -1), reverse=True)[:top]
    hdr = f"  {'HCPCS':>6} {'R':>1} {'srvcs':>8} {'benes':>7} {'supl':>4} {'avg_pymt':>9} {'tot_paid':>11}  desc"
    print(hdr); print("  " + "-" * (len(hdr) - 2))
    for r in ranked:
        tp = r.get("tot_paid")
        print(f"  {str(r.get('HCPCS_CD','—')):>6} {str(r.get('Suplr_Rentl_Ind','')or'-'):>1} "
              f"{(_f(r.get('Tot_Suplr_Srvcs')) or 0):>8,.0f} {(_f(r.get('Tot_Suplr_Benes')) or 0):>7,.0f} "
              f"{(_f(r.get('Tot_Suplrs')) or 0):>4,.0f} "
              f"{('$'+format(_f(r.get('Avg_Suplr_Mdcr_Pymt_Amt')) or 0,',.0f')):>9} "
              f"{('$'+format(tp,',.0f')) if tp is not None else '—':>11}  "
              f"{str(r.get('HCPCS_Desc','') or '')[:48]}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--npi", required=True, help="10-digit referring-provider NPI")
    ap.add_argument("--year", type=int, default=None, help="restrict to one year (default: all available)")
    ap.add_argument("--top", type=int, default=15, help="top-N codes by total paid per year (default 15)")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    args = ap.parse_args(argv)

    npi = args.npi.strip()
    if not (len(npi) == 10 and npi.isdigit()):
        print(f"ERROR: --npi must be a 10-digit NPI; got {npi!r}", file=sys.stderr)
        return 2

    years = [args.year] if args.year else _years()
    if not years:
        print(f"No {FAMILY} parquet found locally ({', '.join(str(d) for d in LOCAL_DIRS)}) "
              f"or on R2 — run `make pull-data`.", file=sys.stderr)
        return 1

    con = r2.duckdb_r2()
    result = {}
    for y in years:
        rows = _rows_for_year(con, _src(y), npi)
        if rows:
            result[y] = rows

    if not result:
        print(f"No DMEPOS referral rows for NPI {npi} in {years[0]}-{years[-1]} "
              f"(not a DMEPOS referrer, or all lines suppressed).")
        return 0

    if args.format == "json":
        print(json.dumps({"npi": npi,
                          "years": {str(y): {"signals": _signals(r), "lines": r}
                                    for y, r in result.items()}}, indent=2, default=str))
    else:
        print(f"=== DMEPOS referral breakdown — NPI {npi} ===")
        for y in sorted(result):
            _print_year(y, result[y], args.top)
    return 0


if __name__ == "__main__":
    sys.exit(main())
