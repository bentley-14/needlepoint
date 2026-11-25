#!/usr/bin/env python3
"""claims_lookup — query synthetic CLAIM-LEVEL Medicare data (rendering vs billed-under).

Calibration tool. Reads ONLY data/synth_claims.parquet (built by
scripts/calibration/build_synth_claims.py). Answer-key-free: the parquet carries the
claim MECHANISM (who rendered vs who it was billed under, code, date, beneficiary, amounts)
but NO fraud/verdict label — the caller must draw conclusions.

This is the claim-level layer the public PUF tools (dump_hcpcs_breakdown, address_sweep) lack.
Use it to test whether NP/PA-rendered services were billed under another provider's NPI
(incident-to), to size that pattern, and to pull representative claims for Rule 9(b) pleading.

Modes:
  --billed-under NPI [--year Y]   Wbirch actually rendered the services billed under this NPI?
  --rendered-by  NPI [--year Y]   Which billing NPI were this provider's services billed under?
  --scan [--year Y] [--min N]     Cross-provider: rank billing NPIs by share of services
                                  rendered by SOMEONE ELSE (the discovery / anomaly mode).
  --exemplars NPI [--n 5]         Representative individual claims (bene+date+code+renderer)
                                  where rendering != billed-under — Rule 9(b) exemplars.
"""
from __future__ import annotations
import argparse, sys
from pathlib import Path

import pandas as pd


def _find_claims(override: str | None) -> Path:
    if override:
        return Path(override)
    for c in (Path("data/synth_claims.parquet"), Path("synth_claims.parquet")):
        if c.exists():
            return c
    print("ERROR: data/synth_claims.parquet not found (run from the export dir).", file=sys.stderr)
    sys.exit(2)


def _load(p: Path) -> pd.DataFrame:
    df = pd.read_parquet(p)
    df["rendered_elsewhere"] = df["rendering_npi"] != df["billed_under_npi"]
    return df


NEW = {"99201", "99202", "99203", "99204", "99205"}


def mode_billed_under(df, npi, year):
    sub = df[df["billed_under_npi"] == npi]
    if year:
        sub = sub[sub["year"] == year]
    if sub.empty:
        print(f"No claims billed under NPI {npi}" + (f" in {year}" if year else "")); return
    print(f"=== Claims BILLED UNDER NPI {npi}" + (f" — {year}" if year else " — all years") + " ===")
    print(f"Total claims: {len(sub):,}   distinct beneficiaries: {sub['bene_id'].nunique():,}")
    by_r = sub.groupby("rendering_npi").agg(
        claims=("claim_id", "size"), benes=("bene_id", "nunique"),
        new_pt=("new_patient", "sum"), paid=("paid_amt", "sum")).reset_index()
    by_r["pct"] = (by_r["claims"] / len(sub) * 100).round(1)
    by_r = by_r.sort_values("claims", ascending=False)
    print("\nRendered by (who actually performed the service):")
    print(f"  {'rendering_npi':>14} {'claims':>7} {'%':>6} {'benes':>6} {'new_pt':>7} {'paid$':>12}")
    for _, r in by_r.iterrows():
        tag = "  <- billed-under provider (self)" if r["rendering_npi"] == npi else "  <- OTHER provider"
        print(f"  {r['rendering_npi']:>14} {int(r['claims']):>7} {r['pct']:>5}% "
              f"{int(r['benes']):>6} {int(r['new_pt']):>7} {r['paid']:>12,.0f}{tag}")
    other = sub[sub["rendered_elsewhere"]]
    if len(other):
        newpt_other = int(other["new_patient"].sum())
        print(f"\n{len(other):,} of {len(sub):,} services ({len(other)/len(sub)*100:.0f}%) billed "
              f"under {npi} were rendered by a DIFFERENT provider.")
        print(f"  Of those, {newpt_other} were NEW-patient visits "
              f"(incident-to is categorically not allowed for new patients).")


def mode_rendered_by(df, npi, year):
    sub = df[df["rendering_npi"] == npi]
    if year:
        sub = sub[sub["year"] == year]
    if sub.empty:
        print(f"No claims rendered by NPI {npi}"); return
    print(f"=== Services RENDERED BY NPI {npi}" + (f" — {year}" if year else "") + " ===")
    by_b = sub.groupby("billed_under_npi").agg(
        claims=("claim_id", "size"), new_pt=("new_patient", "sum"),
        paid=("paid_amt", "sum")).reset_index().sort_values("claims", ascending=False)
    print(f"  {'billed_under_npi':>16} {'claims':>7} {'new_pt':>7} {'paid$':>12}")
    for _, r in by_b.iterrows():
        tag = "  <- billed under SELF" if r["billed_under_npi"] == npi else "  <- billed under ANOTHER NPI"
        print(f"  {r['billed_under_npi']:>16} {int(r['claims']):>7} {int(r['new_pt']):>7} "
              f"{r['paid']:>12,.0f}{tag}")


def mode_scan(df, year, min_n):
    sub = df[df["year"] == year] if year else df
    g = sub.groupby("billed_under_npi")
    rows = []
    for npi, s in g:
        if len(s) < min_n:
            continue
        other = s[s["rendered_elsewhere"]]
        rows.append((npi, len(s), len(other), len(other) / len(s),
                     int(other["new_patient"].sum())))
    rows.sort(key=lambda x: (-x[4], -x[3]))  # new-patient-incident-to first, then share
    print(f"=== Cross-provider scan: services rendered by SOMEONE OTHER than the billing NPI"
          + (f" — {year}" if year else "") + " ===")
    print("(rendering != billed-under is LEGAL for compliant incident-to on ESTABLISHED patients;")
    print(" it is NOT allowed for NEW patients — so new_pt_elsewhere is the compliance red flag.)")
    print(f"\n  {'billed_under_npi':>16} {'total':>7} {'elsewhere':>9} {'share':>6} {'new_pt_elsewhere':>16}")
    for npi, tot, oth, share, newpt in rows[:25]:
        flag = "  ***" if newpt > 0 else ""
        print(f"  {npi:>16} {tot:>7} {oth:>9} {share*100:>5.0f}% {newpt:>16}{flag}")
    print("\n  *** = bills NEW-patient visits rendered by another provider (non-compliant incident-to).")


def mode_exemplars(df, npi, n):
    sub = df[(df["billed_under_npi"] == npi) & (df["rendered_elsewhere"])].copy()
    if sub.empty:
        print(f"No incident-to (rendering!=billing) claims under NPI {npi}."); return
    # prioritise new-patient exemplars (the categorical violations)
    sub["prio"] = sub["new_patient"].astype(int)
    sub = sub.sort_values(["prio", "service_date"], ascending=[False, True])
    print(f"=== Representative claims billed under {npi} but rendered by another provider "
          f"(Rule 9(b) exemplars) ===")
    cols = ["claim_id", "bene_id", "service_date", "hcpcs", "new_patient",
            "rendering_npi", "billed_under_npi", "supervising_npi", "allowed_amt", "paid_amt"]
    for _, r in sub.head(n).iterrows():
        kind = "NEW-PATIENT (incident-to prohibited)" if r["new_patient"] else "established"
        print(f"\n  claim {r['claim_id']} | bene {r['bene_id']} | {r['service_date']} | {r['hcpcs']} ({kind})")
        print(f"    rendered_by={r['rendering_npi']}  billed_under={r['billed_under_npi']}  "
              f"supervising={r['supervising_npi'] or '(none)'}")
        print(f"    allowed=${r['allowed_amt']:.2f}  medicare_paid=${r['paid_amt']:.2f} (full physician rate)")


def main():
    ap = argparse.ArgumentParser(description="Query synthetic claim-level data (rendering vs billed-under).")
    ap.add_argument("--billed-under", metavar="NPI")
    ap.add_argument("--rendered-by", metavar="NPI")
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--exemplars", metavar="NPI")
    ap.add_argument("--year", type=int)
    ap.add_argument("--min", type=int, default=50, help="--scan: min claims to include a provider")
    ap.add_argument("--n", type=int, default=5, help="--exemplars: number to show")
    ap.add_argument("--claims", help="override path to synth_claims.parquet")
    a = ap.parse_args()
    df = _load(_find_claims(a.claims))
    if a.billed_under:
        mode_billed_under(df, a.billed_under, a.year)
    elif a.rendered_by:
        mode_rendered_by(df, a.rendered_by, a.year)
    elif a.scan:
        mode_scan(df, a.year, a.min)
    elif a.exemplars:
        mode_exemplars(df, a.exemplars, a.n)
    else:
        ap.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
