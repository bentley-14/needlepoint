"""Dump full per-(NPI, HCPCS) DMEPOS-referral breakdown for an investigator agent.

Reads the CMS DMEPOS "by Referring Provider & Service" PUF
(``cms_dmepos_detailed_<year>.parquet``) and prints, for a referring NPI + year(s):

  1. Header — referrer name/specialty, total DME-referral $, services, #codes, #supplier links
  2. DME family mix — by RBCS category (wheelchairs, oxygen, orthotics, …) and by
     HCPCS prefix (A supplies / E durable equipment / K DMERC-wheelchairs / L orthotic-prosthetic)
  3. Code concentration — top-1 / top-5 code share of $ and the Herfindahl index
     (a brace-mill / wheelchair-mill is a distribution collapsed onto a few codes)
  4. Rental vs purchase $ split
  5. Supplier captivity — codes billed through a single DME supplier (kickback signal)
  6. High-salience DME-fraud code watchlist (back braces, power wheelchairs, etc.) presence
  7. Top-15 codes by estimated Medicare $

This is a DATA-DUMP tool — it surfaces structural facts, it does NOT classify the
provider as fraudulent. A DME supplier or orthotist billing these codes is
expected; a hospitalist referring 800 back braces is not. Interpretation is the
investigator's job.

Usage::
    python scripts/dump_dme_breakdown.py --npi <NPI> --year 2023
    python scripts/dump_dme_breakdown.py --npi <NPI> --years 2019-2023
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    from fraudlib import r2
except Exception:  # calibration jail / standalone copy: local-only, never R2
    class _LocalR2:
        R2_PANELS = ""
        @staticmethod
        def parquet_source(local, key): return str(local)
        @staticmethod
        def duckdb_r2():
            import duckdb; return duckdb.connect()
        @staticmethod
        def list_v1_years(family): return []
    r2 = _LocalR2()

REPO = Path(__file__).resolve().parents[1]
_PANELS = REPO / "data/panels"

# High-salience DMEPOS-fraud code watchlist — codes that recur in DOJ brace-mill /
# wheelchair-mill / oxygen / CGM telemarketing schemes. Presence is NOT proof; it
# tells the investigator which scheme-typical codes this referrer touched.
_WATCHLIST = {
    "L0648": "Lumbar-sacral orthosis, off-the-shelf (back-brace telemarketing)",
    "L0650": "Lumbar-sacral orthosis, off-the-shelf (back-brace telemarketing)",
    "L1833": "Knee orthosis, off-the-shelf (brace-mill)",
    "L1851": "Knee orthosis, off-the-shelf (brace-mill)",
    "L0631": "Lumbar-sacral orthosis (brace-mill)",
    "K0823": "Power wheelchair, Group 2 (wheelchair-mill)",
    "E1390": "Oxygen concentrator (oxygen scheme)",
    "E0601": "CPAP device (sleep/DME scheme)",
    "A4253": "Blood-glucose test strips (CGM/diabetic-supply telemarketing)",
    "K0001": "Standard wheelchair",
    "E0260": "Hospital bed, semi-electric",
}


def _src(year: int) -> str:
    basename = f"cms_dmepos_detailed_{year}.parquet"
    p = _PANELS / basename
    if p.exists():
        return str(p)
    return r2.parquet_source(p, f"{r2.R2_PANELS}/{basename}")


def _years() -> list[int]:
    seen: set[int] = set()
    for p in _PANELS.glob("cms_dmepos_detailed_*.parquet"):
        tail = p.stem.rsplit("_", 1)[-1]
        if tail.isdigit():
            seen.add(int(tail))
    return sorted(seen | set(r2.list_v1_years("cms_dmepos_detailed")))


def _load(npi: str, year: int) -> "pd.DataFrame | None":
    src = _src(year)
    if src and "://" not in src and not Path(src).exists():
        return None
    try:
        con = r2.duckdb_r2()
        return con.execute(
            f"SELECT * FROM read_parquet('{src}') "
            f"WHERE CAST(npi AS VARCHAR) = '{npi}' OR CAST(Rfrg_NPI AS VARCHAR) = '{npi}'"
        ).df()
    except Exception as exc:  # noqa: BLE001
        print(f"(year {year}: no data — {exc})")
        return None


def _dump_year(npi: str, year: int, sub: "pd.DataFrame", src: str) -> None:
    if sub is None or sub.empty:
        print(f"\n[year {year}] NPI {npi} not in this year's DMEPOS-referral PUF")
        return

    sub = sub.assign(
        _paid=pd.to_numeric(sub["Avg_Suplr_Mdcr_Pymt_Amt"], errors="coerce").fillna(0)
        * pd.to_numeric(sub["Tot_Suplr_Srvcs"], errors="coerce").fillna(0),
        _srvcs=pd.to_numeric(sub["Tot_Suplr_Srvcs"], errors="coerce").fillna(0),
        _suplrs=pd.to_numeric(sub["Tot_Suplrs"], errors="coerce").fillna(0),
    )
    total_paid = float(sub["_paid"].sum())
    total_srvcs = float(sub["_srvcs"].sum())
    name = str(sub["provider_name"].iloc[0]) if "provider_name" in sub else "?"
    spec = str(sub.get("Rfrg_Prvdr_Spclty_Desc", pd.Series(["?"])).iloc[0])

    print(f"\n{'=' * 78}")
    print(f"YEAR {year} — NPI {npi} — DMEPOS REFERRAL BREAKDOWN")
    print(f"{'=' * 78}")
    print(f"  referrer: {name}  ·  specialty: {spec}")
    print(f"  source: {src}")
    print(f"  distinct HCPCS codes: {sub['HCPCS_CD'].nunique()}  ·  (code,rental) rows: {len(sub)}")
    print(f"  total services: {int(total_srvcs):,}")
    print(f"  est. total Medicare DME paid: ${total_paid:,.0f}  (Σ avg-pymt × services)")
    if total_paid <= 0:
        return

    # ── DME family mix (RBCS) ──
    fam = sub.groupby("RBCS_Desc")["_paid"].sum().sort_values(ascending=False)
    print(f"\n  DME FAMILY MIX (RBCS category, by $):")
    for cat, paid in fam.head(8).items():
        print(f"    {str(cat)[:46]:<46} ${paid:>11,.0f}  {paid/total_paid:>6.1%}")

    # ── HCPCS prefix mix ──
    pre = sub.assign(_p=sub["HCPCS_CD"].astype(str).str[0].str.upper()).groupby("_p")["_paid"].sum()
    PRE = {"A": "supplies", "E": "durable equip", "K": "DMERC/wheelchair", "L": "orthotic/prosthetic"}
    print(f"\n  HCPCS PREFIX MIX:")
    for p, lbl in PRE.items():
        v = float(pre.get(p, 0))
        if v > 0:
            print(f"    {p} ({lbl:<20}): ${v:>11,.0f}  {v/total_paid:>6.1%}")

    # ── Code concentration ──
    by_code = sub.groupby("HCPCS_CD")["_paid"].sum().sort_values(ascending=False)
    shares = (by_code / total_paid)
    hhi = float((shares ** 2).sum())
    print(f"\n  CODE CONCENTRATION:")
    print(f"    top-1 code share of $: {shares.iloc[0]:.1%} ({by_code.index[0]})")
    print(f"    top-5 code share of $: {shares.head(5).sum():.1%}")
    print(f"    Herfindahl (HHI) over codes: {hhi:.3f}"
          + ("  ★ highly concentrated (mill-like distribution)" if hhi > 0.5 else ""))

    # ── Rental vs purchase ──
    rental = float(sub[sub["Suplr_Rentl_Ind"].astype(str).str.upper() == "Y"]["_paid"].sum())
    print(f"\n  RENTAL vs PURCHASE ($): rental {rental/total_paid:.1%} · purchase {1 - rental/total_paid:.1%}")

    # ── Supplier captivity ──
    one_supplier = sub[sub["_suplrs"] == 1]
    print(f"\n  SUPPLIER CAPTIVITY:")
    print(f"    mean suppliers per (code,rental) row: {sub['_suplrs'].mean():.1f}")
    print(f"    rows billed via a SINGLE supplier: {len(one_supplier)}/{len(sub)}"
          + ("  ★ captive-supplier pattern (kickback-salient)" if len(sub) and len(one_supplier) / len(sub) > 0.5 else ""))

    # ── Watchlist presence ──
    hits = sub[sub["HCPCS_CD"].isin(_WATCHLIST)]
    if not hits.empty:
        print(f"\n  HIGH-SALIENCE DME-FRAUD CODE PRESENCE (watchlist — presence, not proof):")
        for code, grp in hits.groupby("HCPCS_CD"):
            paid = float(grp["_paid"].sum())
            print(f"    {code}  ${paid:>10,.0f}  {paid/total_paid:>5.1%}  — {_WATCHLIST[code]}")

    # ── Top-15 by $ ──
    print(f"\n  TOP-15 CODES BY EST. $:")
    print(f"    {'HCPCS':<8} {'rent':<4} {'srvcs':>9} {'suplrs':>7} {'est_paid':>12}  desc")
    top = sub.sort_values("_paid", ascending=False).head(15)
    for _, r in top.iterrows():
        print(f"    {str(r['HCPCS_CD']):<8} {str(r.get('Suplr_Rentl_Ind','')):<4} "
              f"{int(r['_srvcs']):>9,} {int(r['_suplrs']):>7,} ${float(r['_paid']):>11,.0f}  "
              f"{str(r.get('HCPCS_Desc',''))[:44]}")


def _parse_years(spec) -> list[int]:
    raw = ",".join(spec) if isinstance(spec, list) else spec
    raw = raw.replace(" ", ",").replace(";", ",")
    out: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            out.extend(range(int(lo), int(hi) + 1))
        else:
            out.append(int(part))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--npi", required=True, type=str)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--year", type=int)
    g.add_argument("--years", nargs="+",
                   help="Year(s): range '2019-2023', comma '2019,2020', or space-separated")
    args = ap.parse_args(argv)

    npi = args.npi
    print(f"NPI: {npi}")
    years = [args.year] if args.year else _parse_years(args.years)
    print(f"Years: {years}")
    for y in years:
        _dump_year(npi, y, _load(npi, y), _src(y))
    return 0


if __name__ == "__main__":
    sys.exit(main())
