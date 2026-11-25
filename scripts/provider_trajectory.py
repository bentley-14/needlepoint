"""Per-NPI cross-program Medicare billing trajectory (Part B + Part D + DMEPOS) by year.

One row per year showing the provider's footprint across all three programs — built for
investigator agents to spot ramp-ups, sudden collapses (e.g., a 2023 volume cliff that
signals a DEA limitation / payer audit), and program-mix shifts, WITHOUT hand-writing
duckdb scans each time.

Data: data/processed/cms_partb_<year>.parquet (provider-level Part B PUF),
cms_partd_prescriber_drug_<year>.parquet (Part D), cms_dmepos_<year>.parquet (DMEPOS
referrals). NPI matched as VARCHAR so int- or string-typed NPI columns both work.

Usage:
    python scripts/provider_trajectory.py --npi 1900000075
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


def _years(family: str) -> list[int]:
    # Local glob UNION R2's years (the source of truth) — so the full canonical span
    # is used even with no local mirror; in the calibration jail the shim returns [].
    local = {int(p.stem.rsplit("_", 1)[-1]) for p in PROC.glob(f"{family}_*.parquet")
             if p.stem.rsplit("_", 1)[-1].isdigit()}
    return sorted(local | set(r2.list_v1_years(family)))


def _batched(con, family: str, npi_col: str, npi: str, select: str,
             years: list[int], group: bool = False) -> dict:
    """Fetch `select` for `npi` across ALL `years` of one panel family in a SINGLE
    query (vs one httpfs round-trip per year — collapses ~N reads to 1, far faster
    and network-jitter-robust over R2). Returns {year: row_tuple}.

    read_parquet([...], filename=true) reads every year's file in one scan and
    exposes the source path, so the year is recovered with regexp_extract; duckdb
    still pushes the NPI predicate into each file. group=True for aggregate selects
    (Part D SUM/COUNT → one row per year via GROUP BY)."""
    srcs = [r2.parquet_source(PROC / f"{family}_{y}.parquet",
                              f"{r2.R2_PANELS}/{family}_{y}.parquet") for y in years]
    if not srcs:
        return {}
    src_list = "[" + ",".join(f"'{s}'" for s in srcs) + "]"
    yr = f"CAST(regexp_extract(filename, '{family}_(\\d+)\\.parquet', 1) AS INT)"
    grp = " GROUP BY 1" if group else ""
    try:
        rows = con.execute(
            f"SELECT {yr} AS _yr, {select} FROM read_parquet({src_list}, filename=true) "
            f"WHERE CAST({npi_col} AS VARCHAR) = '{npi}'{grp}"
        ).fetchall()
    except duckdb.Error:
        return {}
    return {r[0]: r[1:] for r in rows if r[0] is not None}


def _f(v, d=0):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--npi", required=True, help="10-digit NPI")
    args = ap.parse_args(argv)
    npi = args.npi.strip()
    if not (len(npi) == 10 and npi.isdigit()):
        print(f"ERROR: --npi must be a 10-digit NPI; got {npi!r}", file=sys.stderr)
        return 2

    con = r2.duckdb_r2()
    pb_years = _years("cms_partb")
    pd_years = _years("cms_partd_prescriber_drug")
    dm_years = _years("cms_dmepos")
    years = sorted(set(pb_years) | set(pd_years) | set(dm_years))
    # Batched fetch: ONE query per family across THAT family's own years (not one
    # query per year). Each family uses its own year set so read_parquet() never
    # references a missing file (e.g. DMEPOS has no 2013); the print loop's .get(y)
    # then yields None for a year a family lacks — matching the original per-year read.
    b_map = _batched(con, "cms_partb", "Rndrng_NPI", npi,
                     "Tot_Mdcr_Pymt_Amt, Tot_Benes, Tot_Srvcs, Bene_Dual_Cnt, Tot_HCPCS_Cds", pb_years)
    d_map = _batched(con, "cms_partd_prescriber_drug", "Prscrbr_NPI", npi,
                     "SUM(Tot_Clms), SUM(Tot_Day_Suply), SUM(Tot_Drug_Cst), COUNT(DISTINCT Gnrc_Name)",
                     pd_years, group=True)
    m_map = _batched(con, "cms_dmepos", "Rfrg_NPI", npi,
                     "Suplr_Mdcr_Pymt_Amt, Tot_Suplr_Clms, Tot_Suplrs", dm_years)
    print(f"=== Cross-program Medicare trajectory — NPI {npi} ===")
    print(f"  Part B (provider PUF) · Part D (prescriber-drug) · DMEPOS (referrals). '—' = no rows that year.\n")
    hdr = (f"{'year':>4} | {'B_paid':>11} {'B_benes':>7} {'B_srvcs':>9} {'B_$/bene':>9} {'B_dual%':>7} "
           f"| {'D_clms':>8} {'D_daysup':>9} {'D_cost':>11} {'D_drugs':>7} | {'DME_paid':>10} {'DME_clms':>8} {'DME_supl':>8}")
    print(hdr); print("-" * len(hdr))
    any_row = False
    for y in years:
        b = b_map.get(y)
        d = d_map.get(y)
        m = m_map.get(y)
        d = d if (d and d[0] is not None) else None
        if not (b or d or m):
            continue
        any_row = True
        b_paid, b_benes, b_srvcs, b_dual, _ = (b or (None,)*5)
        b_ppb = (_f(b_paid) / _f(b_benes)) if (b and _f(b_benes) and _f(b_paid) is not None) else None
        b_dualpct = (_f(b_dual) / _f(b_benes) * 100) if (b and _f(b_benes) and _f(b_dual) is not None) else None
        d_clms, d_days, d_cost, d_drugs = (d or (None,)*4)
        m_paid, m_clms, m_supl = (m or (None,)*3)

        def c(v, kind):
            if v is None: return "—"
            v = float(v)
            return f"${v:,.0f}" if kind == "usd" else (f"{v:,.0f}" if kind == "int" else f"{v:.1f}")
        print(f"{y:>4} | {c(b_paid,'usd'):>11} {c(b_benes,'int'):>7} {c(b_srvcs,'int'):>9} "
              f"{c(b_ppb,'usd'):>9} {(f'{b_dualpct:.0f}%' if b_dualpct is not None else '—'):>7} "
              f"| {c(d_clms,'int'):>8} {c(d_days,'int'):>9} {c(d_cost,'usd'):>11} {c(d_drugs,'int'):>7} "
              f"| {c(m_paid,'usd'):>10} {c(m_clms,'int'):>8} {c(m_supl,'int'):>8}")
    if not any_row:
        print(f"  No Part B / Part D / DMEPOS rows for NPI {npi} in any year ({years[0]}-{years[-1]}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
