"""Per-NPI opioid prescribing-intensity metrics from the Part D prescriber-drug PUF.

Surfaces the opioid-diversion signals investigators repeatedly rebuild by hand: per-year
opioid claims / day-supply / cost, opioid CLAIM SHARE (opioid vs all Part D claims),
day-supply per claim, long-acting share, and the top opioid drugs. Pairs with the
specialty OPIOID_NORMS in prompts/triage.md to judge whether a share is anomalous.

LIMITATION (be honest): the Part D PUF has NO drug strength / units, so a true MME/day
(morphine-equivalent) cannot be computed here — that needs NDC-level strength data. These
are prescribing-INTENSITY proxies (claims, day-supply, share), not MME/day.

Data: data/processed/cms_partd_prescriber_drug_<year>.parquet.
Usage:
    python scripts/opioid_metrics.py --npi 1900000075
    python scripts/opioid_metrics.py --npi 1900000075 --year 2022
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
        @staticmethod
        def list_v1_years(family): return []  # jail: years come from the local workspace glob
    r2 = _LocalR2()

REPO = Path(__file__).resolve().parents[1]
PROC = REPO / "data/processed"

# Mirrors DRUG_CLASS_PATTERNS["opioid"] in partd_drug_lookup.py (keep in sync).
OPIOID = ["Oxycodone", "Hydrocodone", "Morphine", "Fentanyl", "Hydromorphone", "Oxymorphone",
          "Methadone", "Tapentadol", "Tramadol", "Codeine", "Buprenorphine", "Meperidine", "Opana"]
# Long-acting / extended-release opioids (fuzzy keyword approximation).
LA = ["Fentanyl", "Methadone", "Oxycontin", "Extended", " ER", "Tapentadol", "Hysingla", "Embeda"]


def _ilike(col_kws, cols=("Gnrc_Name", "Brnd_Name")) -> str:
    return "(" + " OR ".join(f"{c} ILIKE '%{kw}%'" for kw in col_kws for c in cols) + ")"


def _years() -> list[int]:
    # Local glob UNION R2's years (the source of truth) — full canonical span even
    # with no local mirror; the calibration jail's shim returns [] (local workspace).
    local = {int(p.stem.rsplit("_", 1)[-1]) for p in PROC.glob("cms_partd_prescriber_drug_*.parquet")
             if p.stem.rsplit("_", 1)[-1].isdigit()}
    return sorted(local | set(r2.list_v1_years("cms_partd_prescriber_drug")))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--npi", required=True, help="10-digit NPI")
    ap.add_argument("--year", type=int, help="single year (default: all years)")
    args = ap.parse_args(argv)
    npi = args.npi.strip()
    if not (len(npi) == 10 and npi.isdigit()):
        print(f"ERROR: --npi must be a 10-digit NPI; got {npi!r}", file=sys.stderr)
        return 2

    con = r2.duckdb_r2()
    op = _ilike(OPIOID)
    la = _ilike(LA)
    years = [args.year] if args.year else _years()
    print(f"=== Opioid prescribing intensity (Part D) — NPI {npi} ===")
    print("  opioid_share = opioid claims / ALL Part D claims. benes are per-drug (NOT unique across drugs).")
    print("  MME/day not computable from this PUF (no strength). See prompts/triage.md OPIOID_NORMS for specialty thresholds.\n")
    hdr = f"{'year':>4} | {'op_clms':>8} {'op_daysup':>9} {'op_cost':>10} {'op_share':>8} {'LA_share':>8} {'daysup/clm':>10} {'n_drugs':>7}"
    print(hdr); print("-" * len(hdr))
    any_row = False

    # Batch ALL years into ONE read_parquet scan (replaces 3 × N per-year queries).
    # Use conditional aggregation to compute allc, opioid aggs, and LA claims in a
    # single pass; duckdb still pushes the NPI predicate into each file.
    # Build source list from this family's own existing years only (avoids missing-
    # file errors — a year absent from the glob then yields .get(y) is None below).
    family = "cms_partd_prescriber_drug"
    srcs = [r2.parquet_source(PROC / f"{family}_{y}.parquet",
                              f"{r2.R2_PANELS}/{family}_{y}.parquet") for y in years]
    src_list = "[" + ",".join(f"'{s}'" for s in srcs) + "]"
    yr_expr = f"CAST(regexp_extract(filename, '{family}_(\\d+)\\.parquet', 1) AS INT)"
    try:
        batch_rows = con.execute(
            f"SELECT {yr_expr} AS _yr, "
            f"SUM(Tot_Clms) AS allc, "
            f"SUM(CASE WHEN {op} THEN Tot_Clms ELSE 0 END) AS op_clms, "
            f"SUM(CASE WHEN {op} THEN Tot_Day_Suply ELSE 0 END) AS op_days, "
            f"SUM(CASE WHEN {op} THEN Tot_Drug_Cst ELSE 0 END) AS op_cost, "
            f"COUNT(DISTINCT CASE WHEN {op} THEN Gnrc_Name END) AS op_drugs, "
            f"SUM(CASE WHEN {la} THEN Tot_Clms ELSE 0 END) AS la_clms "
            f"FROM read_parquet({src_list}, filename=true) "
            f"WHERE CAST(Prscrbr_NPI AS VARCHAR) = '{npi}' "
            f"GROUP BY 1"
        ).fetchall()
        yr_map = {r[0]: r[1:] for r in batch_rows if r[0] is not None}
    except duckdb.Error:
        yr_map = {}

    for y in years:
        batch = yr_map.get(y)
        if batch is None:
            continue
        allc, clms, days, cost, ndr, la_clms = batch
        if clms is None or clms == 0:
            continue
        any_row = True
        share = (clms / allc * 100) if allc else None
        la_clms = la_clms or 0
        lash = (la_clms / clms * 100) if clms else None
        dpc = (days / clms) if clms else None
        print(f"{y:>4} | {clms:>8,.0f} {days:>9,.0f} {f'${cost:,.0f}':>10} "
              f"{(f'{share:.1f}%' if share is not None else '—'):>8} {(f'{lash:.0f}%' if lash is not None else '—'):>8} "
              f"{(f'{dpc:.0f}' if dpc is not None else '—'):>10} {ndr:>7}")
    if not any_row:
        print(f"  No opioid Part D claims for NPI {npi} ({years[0]}-{years[-1]}).")
        return 0
    # Top opioid drugs across the requested window
    yfilter = f"AND year = {args.year}" if args.year else ""
    top_years = [args.year] if args.year else years
    top_srcs = [r2.parquet_source(
        PROC / f"cms_partd_prescriber_drug_{y}.parquet",
        f"{r2.R2_PANELS}/cms_partd_prescriber_drug_{y}.parquet",
    ) for y in top_years]
    srcs_sql = ", ".join(f"'{s}'" for s in top_srcs)
    top = con.execute(
        f"SELECT Gnrc_Name, SUM(Tot_Clms) AS clms, SUM(Tot_Day_Suply) AS daysup, SUM(Tot_Drug_Cst) AS cost "
        f"FROM read_parquet([{srcs_sql}], union_by_name=true) WHERE CAST(Prscrbr_NPI AS VARCHAR)='{npi}' AND {op} "
        f"GROUP BY Gnrc_Name ORDER BY clms DESC LIMIT 10").fetchall()
    print(f"\n  Top opioid drugs ({'year '+str(args.year) if args.year else 'all years'}):")
    for gn, clms, days, cost in top:
        print(f"    {gn[:42]:<42} clms={clms:>7,.0f}  daysup={days:>8,.0f}  cost=${cost:,.0f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
