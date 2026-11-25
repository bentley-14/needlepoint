#!/usr/bin/env python3
"""IMPOSSIBILITY hunt — absolute limits, not percentiles.

Every percentile-based target this project produced died the same death: "p97
vs peers" invites "my patients are sicker," and that defense wins because
percentiles are RELATIVE. An impossible number is ABSOLUTE — it does not care
what peers do, and there is no case-mix story that buys back a 30-hour day.

Three absolute tests, all computable from the Part B by-service PUF (which
publishes Tot_Bene_Day_Srvcs = distinct beneficiary-days, the field that turns
units into TIME):

  A. PROVIDER-DAY TIME  — total time-denominated minutes billed in the year.
     A solo human cannot exceed 24h x 365 = 8,760 h. Flag >4,380 h (12h EVERY
     day incl. weekends/holidays) and hard-flag >8,760 h (physically impossible).
  B. PATIENT-DAY TIME   — units per beneficiary-day x unit-minutes. One patient
     cannot receive >24 h of a service in one calendar day.
  C. UNITS PER PATIENT-DAY — units/bene-day far above any clinical construction
     (e.g. 40 units of a "per 15 min" code = 10 h on ONE patient in ONE day).

Unit minutes are PARSED from the authoritative HCPCS descriptor at runtime
(never hardcoded — the 99489 lesson). Codes whose descriptor is not
time-denominated are excluded from A/B entirely.

CAVEAT the output states plainly: incident-to billing means a group's minutes
may be furnished by employed staff, so provider-day time is only dispositive
for a SOLO practitioner (cross-check PECOS group size via
scripts/foreclosure_proxy.py staff). Patient-day impossibility (B) has no such
escape — one patient cannot be in two places, whoever furnished the service.

Usage:
    python scripts/hunt_impossible.py --year 2023
    python scripts/hunt_impossible.py --year 2023 --min-hours 4380 --out results/impossible_2023.jsonl
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import duckdb

REPO_ROOT = Path(__file__).resolve().parent.parent
PANELS = REPO_ROOT / "data" / "panels"
HCPCS_L2 = REPO_ROOT / "data" / "ground_truth" / "hcpcs_level2_full.parquet"
HCPCS_CPT = REPO_ROOT / "data" / "ground_truth" / "hcpcs_codes.parquet"

MIN_IN_DAY = 24 * 60


def unit_minutes(desc: str | None) -> float | None:
    """Minutes per unit parsed from an authoritative LONG descriptor, else None.

    Deliberately conservative — a first pass over 2023 flagged Provider Banyan at
    "276,342 billed hours" purely because the SHORT descriptor for 99490 reads
    "Chron care mgmt srvc 20 min" while the real code is *20 minutes of clinical
    staff time per calendar month*. Two failure modes to refuse:

      * per-month / per-diem codes — units don't accumulate within a day, so
        multiplying them by a per-unit time is meaningless;
      * "clinical staff time" codes — CMS assigns CCM (99487/99489/99490/99439)
        GENERAL supervision (MLN909188 p.4: "the billing practitioner doesn't
        personally provide the service"), so those minutes are not the billing
        provider's and cannot ground a personal-time impossibility;
      * office/hospital E&M, where a trailing "40 minutes" is the *typical* time
        for the visit level, not a billable unit.

    Only an explicit per-unit construction ("each additional 15 minutes",
    "per 30 minutes") counts. A bare "N minutes" is rejected.
    """
    if not desc:
        return None
    d = str(desc).lower()
    if re.search(r"per\s+diem|per\s+day|each\s+day|per\s+month|calendar\s+month"
                 r"|per\s+visit|per\s+trip|per\s+session", d):
        return None                      # not a time UNIT we can multiply
    if "clinical staff" in d or "staff time" in d:
        return None                      # general supervision — not the biller's own minutes
    m = re.search(r"(?:each|per)\s+(?:additional\s+)?(\d+)\s*min", d)
    if m:
        return float(m.group(1))
    if re.search(r"\bper\s+hour|/\s*hour|\bper\s+hr\b", d):
        return 60.0
    return None


def load_minutes_map(con, svc: Path | None = None) -> dict[str, float]:
    """Prefer the PUF's own HCPCS_Desc — it is the LONG descriptor, and only the
    long form carries the "per calendar month" / "clinical staff time"
    qualifiers that unit_minutes() must see in order to reject a code."""
    out: dict[str, float] = {}
    if svc is not None and svc.exists():
        df = con.execute(
            f"SELECT DISTINCT HCPCS_Cd AS code, HCPCS_Desc AS d FROM read_parquet('{svc}')"
        ).fetchdf()
        for _, r in df.iterrows():
            mm = unit_minutes(r["d"])
            if mm:
                out.setdefault(str(r["code"]).strip().upper(), mm)
    for path, col in ((HCPCS_L2, "short_descriptor"), (HCPCS_CPT, None)):
        if not path.exists():
            continue
        cols = con.execute(f"SELECT * FROM read_parquet('{path}') LIMIT 0").df().columns.tolist()
        dcol = col if col in cols else next((c for c in cols if "desc" in c.lower()), None)
        if not dcol:
            continue
        df = con.execute(f'SELECT code, "{dcol}" AS d FROM read_parquet(\'{path}\')').fetchdf()
        for _, r in df.iterrows():
            mm = unit_minutes(r["d"])
            if mm:
                out.setdefault(str(r["code"]).strip().upper(), mm)
    return out


def hunt(con, year: int, min_hours: float) -> list[dict]:
    svc = PANELS / f"cms_partb_svc_{year}.parquet"
    if not svc.exists():
        raise FileNotFoundError(svc)
    mins = load_minutes_map(con, svc)
    if not mins:
        raise RuntimeError("no HCPCS reference tables — cannot derive unit minutes")
    vals = ",".join(f"('{c}',{m})" for c, m in mins.items())
    con.execute("DROP TABLE IF EXISTS um")
    con.execute(f"CREATE TEMP TABLE um AS SELECT * FROM (VALUES {vals}) t(code,mins)")

    rows = con.execute(f"""
        WITH s AS (
          SELECT p.Rndrng_NPI npi,
                 ANY_VALUE(p.Rndrng_Prvdr_Last_Org_Name||', '||p.Rndrng_Prvdr_First_Name) nm,
                 ANY_VALUE(p.Rndrng_Prvdr_Type) spec, ANY_VALUE(p.Rndrng_Prvdr_State_Abrvtn) st,
                 ANY_VALUE(p.Rndrng_Prvdr_Ent_Cd) ent,
                 SUM(p.Tot_Srvcs * um.mins) tot_min,
                 SUM(p.Avg_Mdcr_Pymt_Amt * p.Tot_Srvcs) time_paid,
                 MAX(p.Tot_Srvcs / NULLIF(p.Tot_Bene_Day_Srvcs,0) * um.mins) worst_patient_day_min,
                 arg_max(p.HCPCS_Cd, p.Tot_Srvcs / NULLIF(p.Tot_Bene_Day_Srvcs,0) * um.mins) worst_code,
                 arg_max(p.Tot_Srvcs / NULLIF(p.Tot_Bene_Day_Srvcs,0), p.Tot_Srvcs / NULLIF(p.Tot_Bene_Day_Srvcs,0) * um.mins) worst_units_per_day
          FROM read_parquet('{svc}') p JOIN um ON um.code = p.HCPCS_Cd
          GROUP BY 1),
        t AS (SELECT Rndrng_NPI npi, SUM(Avg_Mdcr_Pymt_Amt*Tot_Srvcs) all_paid
              FROM read_parquet('{svc}') GROUP BY 1)
        SELECT s.*, t.all_paid, s.tot_min/60.0 tot_hours
        FROM s JOIN t ON t.npi=s.npi
        WHERE s.tot_min/60.0 >= {min_hours} OR s.worst_patient_day_min > {MIN_IN_DAY}
        ORDER BY s.tot_min DESC""").fetchdf()

    out = []
    for _, r in rows.iterrows():
        out.append({
            "npi": str(r["npi"]), "name": r["nm"], "specialty": r["spec"], "state": r["st"],
            "entity_code": r["ent"],
            "billed_hours_year": round(float(r["tot_hours"]), 1),
            "hours_per_calendar_day": round(float(r["tot_hours"]) / 365.0, 1),
            "exceeds_24h_every_day": bool(float(r["tot_hours"]) > 8760),
            "worst_patient_day_minutes": round(float(r["worst_patient_day_min"] or 0), 1),
            "worst_patient_day_code": r["worst_code"],
            "worst_units_per_patient_day": round(float(r["worst_units_per_day"] or 0), 1),
            "patient_day_impossible": bool(float(r["worst_patient_day_min"] or 0) > MIN_IN_DAY),
            "time_code_paid": round(float(r["time_paid"] or 0), 0),
            "total_medicare_paid": round(float(r["all_paid"] or 0), 0),
        })
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--year", type=int, default=2023)
    ap.add_argument("--min-hours", type=float, default=4380.0,
                    help="flag threshold: annual billed hours (default 4380 = 12h EVERY day)")
    ap.add_argument("--out", type=Path)
    args = ap.parse_args(argv)
    con = duckdb.connect()
    rows = hunt(con, args.year, args.min_hours)
    print(f"{len(rows)} provider(s) at/over {args.min_hours:,.0f} billed hours "
          f"({args.min_hours/365:.1f} h EVERY calendar day) or with an impossible patient-day, {args.year}\n")
    print(f"{'NPI':11s}{'hrs/yr':>9s}{'h/day':>7s}{'>24h/d':>7s}{'worst pt-day':>13s} {'code':7s}{'total $':>12s}  {'st':3s}{'ent':4s} name")
    for r in rows[:40]:
        flag = "YES" if r["exceeds_24h_every_day"] else ""
        print(f"{r['npi']:11s}{r['billed_hours_year']:>9,.0f}{r['hours_per_calendar_day']:>7.1f}{flag:>7s}"
              f"{r['worst_patient_day_minutes']:>11,.0f}m {r['worst_patient_day_code']:7s}"
              f"${r['total_medicare_paid']:>11,.0f}  {str(r['state']):3s}{str(r['entity_code']):4s} {str(r['name'])[:26]}")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("w") as f:
            for r in rows:
                f.write(json.dumps(r, default=str) + "\n")
        print(f"\n-> {args.out}")
    print("\nCAVEAT: annual-hours is dispositive only for a SOLO biller (incident-to staff can")
    print("furnish a group's minutes) — cross-check PECOS group size. Patient-day impossibility")
    print("has no such escape: one patient cannot receive >24h of service in one day.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
