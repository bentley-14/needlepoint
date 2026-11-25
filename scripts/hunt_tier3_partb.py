#!/usr/bin/env python3
"""Tier-3 fileable hunt on Medicare Part B — the Rowan archetype.

Finds the ONE fileable band the Lab Ebony A/B identified: integrated
order-and-render providers billing an anchored DEFINITIVE code themselves with
blanket max-tier uniformity — the shape that reached FILE for Rowan / North
Georgia Pain, NOT the pass-through reference labs (tier-2, referral-only).

Per candidate (from cms_partb_svc_{year} = provider x HCPCS line items):
  udt_max_tier_share  G0483 $ / all-UDT(G0480-3) $   (>= THRESH = blanket)
  udt_pay             total definitive-UDT payments
  em_share            office-E&M(99202-99215) $ / provider total  (>0 = sees
                      patients = ORDERS-AND-RENDERS = tier 3, not a lab)
  novel               NOT in gold_set and NOT LEIE-excluded (qui-tam value)
Ranked by (orders_and_renders, udt_max_tier_share, udt_pay). Prints the tier-3
shortlist + the anchor (LCD L36707) counsel would plead. Read-only; no launch.

Usage:
    python scripts/hunt_tier3_partb.py --year 2023 --min-udt-pay 300000
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import duckdb

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
from fraudlib.anchors import anchor_for_code  # noqa: E402

PANELS = REPO_ROOT / "data" / "panels"
GOLD = REPO_ROOT / "data" / "extracted" / "gold_set.jsonl"
LEIE = REPO_ROOT / "data" / "registries" / "leie.parquet"
UDT = ("G0480", "G0481", "G0482", "G0483")
# Per-analyte definitive-testing vehicles: how a lab bills drug CLASSES that have
# no dedicated G-code (fentanyl, buprenorphine, tramadol, zolpidem, cannabinoids…).
# A max-tier share computed over the NAMED G-codes alone OVERSTATES blanket-tier
# behavior whenever these are also billed — there, 27.7% of determinations sat in
# these codes and the "2016 pivot to G0483" was largely a CODING transition.
# We therefore report the corrected denominator too. (Verified 2026-07-26: all 10
# current tier-3 candidates bill ZERO of these, so their shares are unaffected —
# the confound is a reference-lab phenomenon, not a clinician one.)
PER_ANALYTE = ("82542", "82543", "83789", "G6058", "G6056", "G6041", "G6042")
EM = tuple(f"992{n:02d}" for n in list(range(2, 6)) + list(range(11, 16)))  # 99202-05,11-15


def gold_npis() -> set[str]:
    if not GOLD.exists():
        return set()
    out = set()
    for l in GOLD.open():
        if l.strip():
            try:
                out.add(str(json.loads(l).get("npi")))
            except Exception:
                pass
    return out


def hunt(year: int, min_udt_pay: float, max_tier_thresh: float) -> list[dict]:
    svc = PANELS / f"cms_partb_svc_{year}.parquet"
    if not svc.exists():
        raise FileNotFoundError(f"{svc} — run: rclone copy r2:needlepoint-data/processed/v1/{svc.name} data/panels/")
    con = duckdb.connect()
    ids = ",".join(f"'{c}'" for c in UDT)
    emids = ",".join(f"'{c}'" for c in EM)
    paids = ",".join(f"'{c}'" for c in PER_ANALYTE)
    # per-provider UDT profile + max-tier share + total & E&M pay
    rows = con.execute(f"""
        WITH udt AS (
          SELECT Rndrng_NPI npi, HCPCS_Cd c,
                 SUM(Avg_Mdcr_Pymt_Amt * Tot_Srvcs) paid, SUM(Tot_Benes) benes
          FROM read_parquet('{svc}') WHERE HCPCS_Cd IN ({ids}) GROUP BY 1,2),
        prof AS (
          SELECT npi, SUM(paid) udt_pay,
                 SUM(CASE WHEN c='G0483' THEN paid ELSE 0 END) g0483_pay,
                 SUM(CASE WHEN c='G0483' THEN benes ELSE 0 END) g0483_benes
          FROM udt GROUP BY 1 HAVING SUM(paid) >= {min_udt_pay}),
        tot AS (
          SELECT Rndrng_NPI npi, SUM(Avg_Mdcr_Pymt_Amt * Tot_Srvcs) total_pay,
                 SUM(CASE WHEN HCPCS_Cd IN ({emids}) THEN Avg_Mdcr_Pymt_Amt*Tot_Srvcs ELSE 0 END) em_pay,
                 SUM(CASE WHEN HCPCS_Cd IN ({paids}) THEN Avg_Mdcr_Pymt_Amt*Tot_Srvcs ELSE 0 END) per_analyte_pay
          FROM read_parquet('{svc}') GROUP BY 1)
        SELECT p.npi, p.udt_pay, p.g0483_pay/NULLIF(p.udt_pay,0) max_tier_share,
               p.g0483_pay/NULLIF(p.udt_pay + t.per_analyte_pay,0) max_tier_share_corrected,
               t.per_analyte_pay,
               p.g0483_benes, t.total_pay, t.em_pay/NULLIF(t.total_pay,0) em_share,
               n.provider_name, n.Rndrng_Prvdr_Type prov_type, n.Rndrng_Prvdr_State_Abrvtn st
        FROM prof p JOIN tot t ON t.npi=p.npi
        LEFT JOIN (SELECT DISTINCT Rndrng_NPI, Rndrng_Prvdr_Last_Org_Name || ', ' || Rndrng_Prvdr_First_Name provider_name,
                          Rndrng_Prvdr_Type, Rndrng_Prvdr_State_Abrvtn FROM read_parquet('{svc}')) n
               ON n.Rndrng_NPI=p.npi
        ORDER BY max_tier_share DESC, p.udt_pay DESC""").fetchdf()
    gold = gold_npis()
    leie = set()
    if LEIE.exists():
        try:
            leie = set(con.execute(f"SELECT DISTINCT CAST(NPI AS VARCHAR) FROM read_parquet('{LEIE}') WHERE NPI IS NOT NULL").fetchdf().iloc[:, 0])
        except Exception:
            pass
    out = []
    for _, r in rows.iterrows():
        npi = str(r["npi"])
        # Gate on the CORRECTED share (per-analyte codes in the denominator) so a
        # lab whose "blanket max tier" is really a coding convention can't qualify.
        corrected = float(r["max_tier_share_corrected"] or 0)
        tier = 3 if (corrected >= max_tier_thresh and (r["em_share"] or 0) >= 0.02) else 2
        out.append({
            "npi": npi, "name": r["provider_name"], "type": r["prov_type"], "state": r["st"],
            "udt_pay": float(r["udt_pay"]), "max_tier_share": float(r["max_tier_share"] or 0),
            "max_tier_share_corrected": corrected,
            "per_analyte_pay": float(r["per_analyte_pay"] or 0),
            "em_share": float(r["em_share"] or 0), "g0483_benes": int(r["g0483_benes"] or 0),
            "fileability_tier": tier,
            "novel": npi not in gold and npi not in leie,
            "anchor": ",".join(anchor_for_code("G0483")["anchor_ids"]),
        })
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--year", type=int, default=2023)
    ap.add_argument("--min-udt-pay", type=float, default=300_000)
    ap.add_argument("--max-tier-thresh", type=float, default=0.70)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)
    rows = hunt(args.year, args.min_udt_pay, args.max_tier_thresh)
    t3 = [r for r in rows if r["fileability_tier"] == 3 and r["novel"]]
    print(f"{len(rows)} UDT providers >=${args.min_udt_pay:,.0f}; "
          f"{sum(1 for r in rows if r['fileability_tier']==3)} tier-3 (blanket max-tier + orders-and-renders); "
          f"{len(t3)} of those NOVEL (not gold_set/LEIE):\n")
    print(f"{'NPI':11s} {'max-tier':9s}{'E&M':6s}{'UDT $':>11s}  {'st':3s} {'type':22s} name")
    for r in sorted(t3, key=lambda x: (-x["max_tier_share"], -x["udt_pay"]))[:20]:
        print(f"{r['npi']:11s} {r['max_tier_share']:8.0%} {r['em_share']:5.0%} "
              f"${r['udt_pay']:>10,.0f}  {str(r['state'])[:2]:3s} {str(r['type'])[:21]:22s} {str(r['name'])[:30]}")
    if args.out:
        with args.out.open("w") as f:
            for r in rows:
                f.write(json.dumps(r, default=str) + "\n")
        print(f"\nfull -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
