#!/usr/bin/env python3
"""Scheme-MO signature scorer — target by conviction MECHANICS, not math extremity.

For each org NPI it computes deterministic,
aggregate-observable CORRELATES of the modus operandi configurations that dominate
past org convictions, then reports which known-MO each candidate best matches.

Doctrine baked in:
* Percentile position is a DETECTION heuristic, never innocence. We score MO
  CONFIGURATION (a bundle of weak correlates), not single-axis extremity.
* p99 single-axis outliers are what state PI units already screen -> low qui-tam
  novelty; the valuable band is the camouflaged middle with a recognizable MO.
* Kickback/recruitment (38% of convictions) INVERTS the naive read: fast patient-
  count growth WITH low per-patient intensity is the volume-buying signature, NOT
  evidence of innocence (the mistake made on Edgewater/MDCareNow).

Signals (all local T-MSIS + NPPES; honors FRAUD_INV_MEDICAID_CUTOFF for calibration):
  recruitment_shape   growth(multi-yr) x (1 - per-patient-intensity pctile)   [MO-1]
  blanket_top_share   patient-encounter share of the single dominant code     [MO-2]
  max_tier_share      within UDT (G0480-3) / E&M (99211-5): max-tier share     [MO-2/tier]
  metronome           1 - CV of monthly pay over the window (phantom smoothness)[MO-3]
  scope_mismatch      NPPES taxonomy family vs dominant billed-code family     [MO-5]
  new_entrant         fast $ ramp within N years of NPI enumeration           [MO-1]
  is_public_entity    govt/university/county biller (EXCLUDE: not a defendant)

Output per NPI: the raw signals + an `mo_match` block naming the best-fit MO and the
count of its correlates present. Rank targets by (mo_correlates_present, then $) —
never by a lone percentile.

Usage:
    python scripts/mo_signatures.py --npi 1900000079 1900000089 --year 2022
    python scripts/mo_signatures.py --from-shortlist results/entity_head/target_shortlist_2026-07-16.jsonl
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import duckdb
import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
from fraudlib.medicaid_cutoff import claim_clause, cutoff_year  # noqa: E402

TMSIS = REPO_ROOT / "data/raw/medicaid/provider_spending_full.parquet"
NPPES = REPO_ROOT / "data/registries/nppes_bulk.parquet"
HCPCS = REPO_ROOT / "data/ground_truth/hcpcs_level2_full.parquet"

UDT_TIER = {"G0480": 1, "G0481": 2, "G0482": 3, "G0483": 4}
EM_TIER = {"99211": 1, "99212": 2, "99213": 3, "99214": 4, "99215": 5}
PUBLIC_RE = re.compile(
    r"\b(STATE OF|COUNTY OF|CITY OF|COMMONWEALTH|DEPARTMENT OF|DEPT OF|"
    r"UNIVERSITY|COLLEGE|SCHOOL DISTRICT|BOARD OF|PUBLIC HEALTH|"
    r"REGIONAL CENTER|MUNICIPAL|VETERANS AFFAIRS)\b", re.I)
# taxonomy prefix -> coarse clinical family, for scope_mismatch
TAX_FAMILY = {
    "101": "behavioral", "103": "behavioral", "106": "behavioral", "104": "behavioral",
    "251": "agency", "253": "home_health", "3747": "personal_care",
    "291": "lab", "293": "lab", "261Q": "clinic", "335": "pharmacy",
}


def _window(year: int) -> tuple[str, str]:
    """Analysis window: 3 years ending at `year`, clamped to the cutoff."""
    cy = cutoff_year()
    hi = min(year, cy - 1) if cy else year
    return f"{hi - 2}-01", f"{hi}-12"


def code_family(code: str) -> str:
    c = (code or "").upper()
    if c in UDT_TIER:
        return "lab"
    if c in EM_TIER:
        return "em"
    if c and c[0] == "H" and c[1:].isdigit():
        return "behavioral"
    if c.startswith(("T1000", "T1002", "T1003")):
        return "nursing"
    if c.startswith("T1019") or c.startswith("S512"):
        return "personal_care"
    if c.startswith("T20") or c.startswith("A0"):
        return "transport"
    if c and (c[0] == "8" or c[0] == "P"):
        return "lab"
    return "other"


def score_one(con, npi: str, year: int) -> dict:
    w0, w1 = _window(year)
    cc = claim_clause("CLAIM_FROM_MONTH")
    out: dict = {"npi": npi, "window": [w0, w1]}

    # identity + public-entity screen + taxonomy family
    ident = con.execute(f"""SELECT COALESCE(Provider_Organization_Name, provider_name) AS nm,
            state AS st, Healthcare_Provider_Taxonomy_Code_1 AS tax,
            Enumeration_Date AS enrolled
        FROM read_parquet('{NPPES}') WHERE CAST(npi AS VARCHAR)='{npi}'""").fetchdf()
    name = ident.at[0, "nm"] if len(ident) else None
    out["name"] = name
    out["state"] = ident.at[0, "st"] if len(ident) else None
    out["is_public_entity"] = bool(name and PUBLIC_RE.search(str(name)))
    tax = str(ident.at[0, "tax"]) if len(ident) else ""
    tax_fam = next((f for p, f in TAX_FAMILY.items() if tax.startswith(p)), "unknown")

    # per-code rollup over the window
    codes = con.execute(f"""SELECT HCPCS_CODE c, SUM(TOTAL_PAID) p, SUM(TOTAL_PATIENTS) pm,
            SUM(TOTAL_CLAIM_LINES) ln
        FROM read_parquet('{TMSIS}')
        WHERE BILLING_PROVIDER_NPI_NUM='{npi}' AND CLAIM_FROM_MONTH BETWEEN '{w0}' AND '{w1}'{cc}
        GROUP BY 1""").fetchdf()
    if codes.empty:
        out["error"] = "no window billing"
        return out
    tot_pm = float(codes["pm"].sum()) or 1.0
    top = codes.loc[codes["p"].idxmax()]
    out["dominant_code"] = top["c"]
    out["window_pay"] = float(codes["p"].sum())
    out["dominant_code_family"] = code_family(top["c"])

    # MO-2 cross-code blanket: patient-encounter share of the single dominant code
    out["blanket_top_share"] = float(top["pm"]) / tot_pm

    # MO-2 tier blanket: within UDT / E&M family, max-tier's share of family $
    def tier_share(tiers: dict) -> float | None:
        fam = codes[codes["c"].isin(tiers)]
        if fam["p"].sum() < 50_000:
            return None
        mx = max(tiers.values())
        return float(fam[fam["c"].map(lambda x: tiers.get(x, 0)) == mx]["p"].sum() / fam["p"].sum())
    out["udt_max_tier_share"] = tier_share(UDT_TIER)
    out["em_max_tier_share"] = tier_share(EM_TIER)

    # FILEABILITY TIER (the Lab Ebony A/B lesson): a target that bills office-visit
    # E&M ALONGSIDE its anchored definitive code SEES the patients it tests -> it
    # CONTROLS the individualization decision (integrated order-and-render, the
    # Rowan-fileable tier 3). A pass-through reference lab bills only the lab
    # code, so the order belongs to referring physicians -> foreclosure needs
    # their charts (tier 2, strong referral only). em_share is the discriminator.
    em_pay = float(codes[codes["c"].isin(EM_TIER)]["p"].sum())
    out["em_share"] = em_pay / out["window_pay"] if out["window_pay"] else 0.0
    out["orders_and_renders"] = bool(out["em_share"] >= 0.05)

    # MO-3 metronome: 1 - CV of MONTHLY pay over the window (phantom smoothness)
    mon = con.execute(f"""SELECT CLAIM_FROM_MONTH m, SUM(TOTAL_PAID) p FROM read_parquet('{TMSIS}')
        WHERE BILLING_PROVIDER_NPI_NUM='{npi}' AND CLAIM_FROM_MONTH BETWEEN '{w0}' AND '{w1}'{cc}
        GROUP BY 1 ORDER BY 1""").fetchdf()
    if len(mon) >= 9 and mon["p"].mean() > 0:
        out["metronome"] = float(1 - mon["p"].std() / mon["p"].mean())
        out["months_active"] = int(len(mon))
    else:
        out["metronome"] = None

    # MO-1 recruitment shape: growth x low per-patient intensity
    hi_yr = int(w1[:4])
    prev = con.execute(f"""SELECT SUM(TOTAL_PAID) FROM read_parquet('{TMSIS}')
        WHERE BILLING_PROVIDER_NPI_NUM='{npi}' AND SUBSTR(CLAIM_FROM_MONTH,1,4)='{hi_yr - 2}'{cc}
        """).fetchone()[0] or 0.0
    cur = con.execute(f"""SELECT SUM(TOTAL_PAID) FROM read_parquet('{TMSIS}')
        WHERE BILLING_PROVIDER_NPI_NUM='{npi}' AND SUBSTR(CLAIM_FROM_MONTH,1,4)='{hi_yr}'{cc}
        """).fetchone()[0] or 0.0
    growth = cur / max(prev, 1.0)
    out["growth_2y"] = float(min(growth, 99))
    # per-patient intensity vs same-state same-dominant-code peers (pre-cutoff safe)
    ppm = out["window_pay"] / tot_pm
    peers = con.execute(f"""SELECT SUM(t.TOTAL_PAID)/NULLIF(SUM(t.TOTAL_PATIENTS),0) ppm
        FROM read_parquet('{TMSIS}') t JOIN read_parquet('{NPPES}') n
          ON CAST(n.npi AS VARCHAR)=t.BILLING_PROVIDER_NPI_NUM
        WHERE t.HCPCS_CODE='{top["c"]}' AND n.state='{out["state"]}'
          AND t.CLAIM_FROM_MONTH BETWEEN '{w0}' AND '{w1}'{claim_clause("t.CLAIM_FROM_MONTH")}
        GROUP BY t.BILLING_PROVIDER_NPI_NUM HAVING SUM(t.TOTAL_PATIENTS) >= 50""").fetchdf()
    ppm_pctile = float((peers["ppm"] <= ppm).mean()) if len(peers) else 0.5
    out["paid_per_patient_month"] = float(ppm)
    out["intensity_pctile"] = ppm_pctile
    out["recruitment_shape"] = float(min(growth, 20) / 20 * (1 - ppm_pctile))

    # MO-5 scope mismatch: NPPES clinical family vs dominant billed-code family
    out["scope_mismatch"] = bool(
        tax_fam not in ("unknown", "agency")
        and out["dominant_code_family"] not in ("other", tax_fam))

    # MO-1 new entrant: significant $ within 3 yrs of enumeration
    en = str(ident.at[0, "enrolled"]) if len(ident) else ""
    m = re.search(r"(20\d{2})", en)
    out["new_entrant"] = bool(m and hi_yr - int(m.group(1)) <= 3 and out["window_pay"] > 1_000_000)

    out["mo_match"] = classify_mo(out)
    return out


def classify_mo(s: dict) -> dict:
    """Count present correlates per known MO; report the best-fit configuration.

    Dict order matters: on a tie, the FIRST key wins, so the SPECIFIC MOs are
    listed before the catch-all kickback_recruitment (which over-fires — a
    lab's normally-low per-patient intensity must NOT read as 'recruitment').
    Recruitment is gated on recruitment_shape (which already REQUIRES growth AND
    low intensity together), never on low-intensity alone.
    """
    def hi(v, t):
        return v is not None and v >= t
    mo = {
        "blanket_unnecessary": sum([
            hi(s.get("blanket_top_share"), 0.6), hi(s.get("udt_max_tier_share"), 0.7),
            hi(s.get("em_max_tier_share"), 0.85)]),
        "phantom_not_rendered": sum([
            hi(s.get("metronome"), 0.85), hi(s.get("blanket_top_share"), 0.5)]),
        "unqualified_scope": sum([bool(s.get("scope_mismatch"))]),
        "kickback_recruitment": sum([
            hi(s.get("recruitment_shape"), 0.15), bool(s.get("new_entrant"))]),
    }
    best = max(mo, key=mo.get)
    return {"scores": mo, "best_mo": best if mo[best] else None,
            "best_correlates": mo[best], "total_correlates": sum(mo.values())}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--npi", nargs="+")
    ap.add_argument("--from-shortlist", type=Path)
    ap.add_argument("--year", type=int, default=2024)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args(argv)
    npis = list(args.npi or [])
    if args.from_shortlist:
        npis += [str(json.loads(l)["npi"]) for l in args.from_shortlist.open() if l.strip()]
    con = duckdb.connect()
    rows = [score_one(con, str(n), args.year) for n in dict.fromkeys(npis)]
    rows.sort(key=lambda r: (-(r.get("mo_match", {}).get("total_correlates", 0)),
                             -(r.get("window_pay", 0))))
    for r in rows:
        mm = r.get("mo_match", {})
        excl = "  [PUBLIC-EXCLUDE]" if r.get("is_public_entity") else ""
        print(f"{str(r.get('name'))[:30]:31s} {r['npi']} ${r.get('window_pay',0)/1e6:6.1f}M "
              f"MO={str(mm.get('best_mo')):22s} corr={mm.get('total_correlates',0)} "
              f"| blanket={r.get('blanket_top_share',0):.2f} recruit={r.get('recruitment_shape',0):.2f} "
              f"metro={r.get('metronome') if r.get('metronome') is None else round(r['metronome'],2)}{excl}")
    if args.out:
        with args.out.open("w") as f:
            for r in rows:
                f.write(json.dumps(r, default=str) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
