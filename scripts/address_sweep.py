"""Find providers co-located at the same Medicare practice address.

Answers the investigator's analysis-unit question — "who else bills Medicare from this
address?" — to decide single_npi vs office-suite vs network. Uses the practice address
(Rndrng_Prvdr_St1 + Zip5) from the provider-level Part B PUF.

Match: UPPER(TRIM(St1)) + first-5 ZIP, in the latest year the subject NPI appears (or a
chosen --year). Exact-suite-string match, so a large building with distinct suite strings
can split — widen with --zip-only to sweep the whole ZIP's street.

Data: data/processed/cms_partb_<year>.parquet.
Usage:
    python scripts/address_sweep.py --npi 1900000075
    python scripts/address_sweep.py --st1 "7233 W JEFFERSON BLVD" --zip 46804
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # repo root, so fraudlib imports

import argparse
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
PROC = REPO / "data/processed"


def _years() -> list[int]:
    local = {int(p.stem.rsplit("_", 1)[-1]) for p in PROC.glob("cms_partb_*.parquet")
             if p.stem.rsplit("_", 1)[-1].isdigit()}
    remote = getattr(r2, "list_v1_years", lambda family: [])("cms_partb")
    return sorted(local | set(remote))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--npi", help="resolve the address from this NPI's Part B record")
    ap.add_argument("--st1", help="practice street address line 1 (with --zip)")
    ap.add_argument("--zip", help="5-digit ZIP (with --st1)")
    ap.add_argument("--year", type=int, help="sweep this year (default: latest with data)")
    ap.add_argument("--zip-only", action="store_true", help="sweep the whole ZIP (ignore St1) — wider net")
    args = ap.parse_args(argv)
    if not (args.npi or (args.st1 and args.zip)):
        print("ERROR: pass --npi, or both --st1 and --zip", file=sys.stderr)
        return 2

    con = r2.duckdb_r2()
    years = _years()
    st1 = (args.st1 or "").upper().strip()
    zip5 = (args.zip or "")[:5]
    subj_year = args.year

    if args.npi:
        npi = args.npi.strip()
        # find the latest year the NPI appears + its address
        for y in (([args.year] if args.year else []) or years[::-1]):
            local = PROC / f"cms_partb_{y}.parquet"
            p = r2.parquet_source(local, f"{r2.R2_PANELS}/{local.name}")
            r = con.execute(
                f"SELECT Rndrng_Prvdr_St1, Rndrng_Prvdr_City, Rndrng_Prvdr_State_Abrvtn, "
                f"CAST(Rndrng_Prvdr_Zip5 AS VARCHAR) FROM read_parquet('{p}') "
                f"WHERE CAST(Rndrng_NPI AS VARCHAR) = '{npi}'").fetchone()
            if r:
                st1, city, state, zfull = (r[0] or "").upper().strip(), r[1], r[2], (r[3] or "")
                zip5 = zfull[:5]
                subj_year = y
                print(f"=== Address sweep — subject NPI {npi} ({y}) ===")
                print(f"  Practice address: {r[0]}, {city}, {state} {zip5}\n")
                break
        else:
            print(f"  NPI {npi} not found in any Part B year ({years[0]}-{years[-1]}).")
            return 0
    else:
        subj_year = args.year or years[-1]
        print(f"=== Address sweep — {st1}, ZIP {zip5} ({subj_year}) ===\n")

    _local = PROC / f"cms_partb_{subj_year}.parquet"
    p = r2.parquet_source(_local, f"{r2.R2_PANELS}/{_local.name}")
    where = (f"CAST(Rndrng_Prvdr_Zip5 AS VARCHAR) LIKE '{zip5}%'"
             if args.zip_only else
             f"UPPER(TRIM(Rndrng_Prvdr_St1)) = '{st1.replace(chr(39), chr(39)*2)}' "
             f"AND CAST(Rndrng_Prvdr_Zip5 AS VARCHAR) LIKE '{zip5}%'")
    rows = con.execute(
        f"SELECT CAST(Rndrng_NPI AS VARCHAR), Rndrng_Prvdr_First_Name, Rndrng_Prvdr_Last_Org_Name, "
        f"Rndrng_Prvdr_Type, Rndrng_Prvdr_St1, Tot_Mdcr_Pymt_Amt "
        f"FROM read_parquet('{p}') WHERE {where} ORDER BY Tot_Mdcr_Pymt_Amt DESC NULLS LAST").fetchall()
    print(f"  {len(rows)} provider(s) co-located ({'whole ZIP' if args.zip_only else 'exact St1+ZIP'}):")
    for r in rows:
        npi_, fn, ln, typ, s1, paid = r
        nm = f"{fn or ''} {ln or ''}".strip()
        paid_s = f"${float(paid):,.0f}" if paid is not None else "—"
        suite = "" if args.zip_only else ""
        extra = f"  [{s1}]" if args.zip_only else ""
        print(f"    NPI {npi_}  {nm[:34]:<34} {(typ or '?')[:26]:<26} partb_paid={paid_s}{extra}")
    if len(rows) <= 1 and not args.zip_only:
        print("\n  (≤1 provider at the exact suite string — try --zip-only to sweep the whole ZIP/building.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
