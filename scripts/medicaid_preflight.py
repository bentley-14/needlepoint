#!/usr/bin/env python3
"""Medicaid org pre-flight — deterministic foreclosure math BEFORE loop spend.

The Medicaid-org analog of innocent_explanation_prescreen.py (which is
Medicare-individual/ring oriented). For each target org NPI it computes, from
the raw T-MSIS aggregates + LEIE + NPPES, the numbers a reviewer needs to
decide whether an innocent explanation ("we're just a big program") survives:

1. LEIE        — org NPI excluded? (reuses scripts/leie_lookup.py)
2. INTENSITY   — paid-per-patient-per-year on the dominant code vs EVERY
                 same-state biller of that code (percentile; >=p99 flagged).
3. HOURS MATH  — for time-denominated codes only: implied hours per patient
                 per month + implied hours per servicing clinician per month.
                 Unit minutes are PARSED FROM THE OFFICIAL HCPCS DESCRIPTOR at
                 runtime ("per hour" / "per 15 minutes" / "per diem"), never
                 hardcoded (the 99489 lesson: constants must be verifiable).
                 NOTE: T-MSIS aggregates carry claim LINES, not units — a line
                 can carry multiple units, so line-derived hours are a LOWER
                 bound and are labeled as such.
4. TRAJECTORY  — monthly dominant-code series: growth shape, max/median month
                 ratio, cliff detection (a collapse often means enforcement or
                 payment suspension already found them).
5. STAFF GRAPH — distinct servicing NPIs under the billing org (T-MSIS
                 billing vs servicing split); hours-per-clinician uses this.
6. RING SCAN   — same-state orgs billing the same dominant code that (a) share
                 a distinctive NPPES name token, or (b) SHARE SERVICING NPIs
                 with the target (staff moonlighting across "independent"
                 orgs = the funneling/ring edge, computable natively).

Output: per-target JSON + a human summary with a conservative track:
  per_se_candidate        — an arithmetic impossibility or exclusion fired
  ambiguous_needs_judgment — extreme but a benign story could survive
  benign_leaning          — nothing separates it from peers
This is a SCREEN, not proof: per-patient math uses aggregate lower bounds.

Usage:
    python scripts/medicaid_preflight.py --npi 1900000087 1900000084 --year 2024
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path

import duckdb
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fraudlib.medicaid_cutoff import (  # noqa: E402  leak-safe calibration gate
    banner as _cutoff_banner, claim_clause, clamp_feature_year)

LOG = logging.getLogger("medicaid_preflight")

from fraudlib import r2  # noqa: E402  (local file, else private bucket / public mirror)
TMSIS = r2.DataPath(REPO_ROOT / "data/raw/medicaid/provider_spending_full.parquet")
FEATURES_DIR = r2.DataPath(REPO_ROOT / "data/processed")
NPPES = r2.DataPath(REPO_ROOT / "data/registries/nppes_bulk.parquet")
HCPCS_REF = r2.DataPath(REPO_ROOT / "data/ground_truth/hcpcs_level2_full.parquet")

INTENSITY_FLAG_PCTILE = 0.99
# 8h/day x ~21 working days — a patient in MORE treatment than a full-time job
# every month has no plausible outpatient story.
IMPOSSIBLE_PATIENT_HOURS_PER_MONTH = 168.0
GENERIC_NAME_TOKENS = {
    "LLC", "INC", "CORP", "CO", "COMPANY", "GROUP", "SERVICES", "SERVICE",
    "HEALTH", "HEALTHCARE", "CARE", "HOME", "OF", "THE", "AND", "&", "PC",
    "PLLC", "LTD", "CENTER", "CENTERS", "MEDICAL", "CLINIC", "RECOVERY",
    "BEHAVIORAL", "COMMUNITY", "FAMILY", "MN", "COLORADO",
}


def unit_minutes_from_descriptor(desc: str | None) -> float | None:
    """Parse the time denomination out of an official HCPCS short descriptor.
    Returns minutes per unit, or None when the code isn't time-denominated
    (per diem / per trip / NOS codes get no hours math)."""
    if not desc:
        return None
    d = desc.lower()
    m = re.search(r"per\s+(\d+)\s*min", d)
    if m:
        return float(m.group(1))
    if re.search(r"per\s+hour|/\s*hour|per\s+hr", d):
        return 60.0
    if re.search(r"(\d+)\s*min(ute)?s?\b", d):
        return float(re.search(r"(\d+)\s*min", d).group(1))
    return None


def name_tokens(name: str | None) -> set[str]:
    if not name:
        return set()
    toks = re.split(r"[^A-Z0-9]+", str(name).upper())
    return {t for t in toks if len(t) >= 4 and t not in GENERIC_NAME_TOKENS}


def pctile_of(value: float, population: pd.Series) -> float:
    pop = pd.to_numeric(population, errors="coerce").dropna()
    if not len(pop):
        return float("nan")
    return float((pop <= value).mean())


def preflight_one(con, npi: str, year: int) -> dict:
    out: dict = {"npi": npi, "year": year, "flags": [], "notes": []}

    # Leak-safe calibration: clamp the analysis year to before the cutoff and
    # inject a CLAIM_FROM_MONTH gate into every all-months T-MSIS query. Inert
    # (year unchanged, clause="") in production. CUT gates unaliased queries;
    # CUTt gates the ring/peer queries that alias the T-MSIS table as ``t``.
    year = clamp_feature_year(year)
    out["year"] = year          # reflect the clamped year, not the requested one
    CUT = claim_clause("CLAIM_FROM_MONTH")
    CUTt = claim_clause("t.CLAIM_FROM_MONTH")
    if _cutoff_banner():
        out["calibration_cutoff"] = _cutoff_banner()

    # ── target profile: dominant code, state, org name ──────────────────────
    feat = con.execute(f"""
        SELECT top_hcpcs, service_class, provider_state, medicaid_total_pay
        FROM read_parquet('{FEATURES_DIR / f"medicaid_features_{year}.parquet"}')
        WHERE CAST(npi AS VARCHAR) = '{npi}'""").fetchdf()
    if feat.empty:
        out["error"] = "no medicaid features row"
        return out
    code = feat.at[0, "top_hcpcs"]
    state = feat.at[0, "provider_state"]
    out.update(dominant_code=code, state=state,
               total_pay=float(feat.at[0, "medicaid_total_pay"]),
               service_class=feat.at[0, "service_class"])
    nm = con.execute(f"""SELECT Provider_Organization_Name FROM read_parquet('{NPPES}')
                     WHERE CAST(npi AS VARCHAR)='{npi}'""").fetchone()
    out["org_name"] = nm[0] if nm else None

    # ── 1. LEIE ──────────────────────────────────────────────────────────────
    try:
        from scripts.leie_lookup import lookup as leie_lookup
        hit = leie_lookup(npi)
        out["leie"] = hit
        if hit and hit.get("excluded"):
            out["flags"].append("LEIE_EXCLUDED")
    except Exception as e:                                    # pragma: no cover
        out["leie"] = {"error": str(e)[:120]}
        out["notes"].append("LEIE check unavailable")

    # ── target's own numbers on the dominant code ───────────────────────────
    tgt = con.execute(f"""
        SELECT SUM(TOTAL_PAID) paid, SUM(TOTAL_CLAIM_LINES) lines,
               MAX(TOTAL_PATIENTS) max_month_patients,
               SUM(TOTAL_PATIENTS) patient_months,
               COUNT(DISTINCT CLAIM_FROM_MONTH) n_months
        FROM read_parquet('{TMSIS}')
        WHERE BILLING_PROVIDER_NPI_NUM='{npi}' AND HCPCS_CODE='{code}'
          AND SUBSTR(CLAIM_FROM_MONTH,1,4)='{year}'""").fetchdf().iloc[0]
    paid, lines = float(tgt["paid"] or 0), float(tgt["lines"] or 0)
    patient_months = float(tgt["patient_months"] or 0)
    out["dominant_code_paid"] = paid
    out["dominant_code_lines"] = lines
    out["patient_months"] = patient_months

    # ── 2. per-patient intensity vs same-state same-code peers ──────────────
    peers = con.execute(f"""
        SELECT t.BILLING_PROVIDER_NPI_NUM npi,
               SUM(t.TOTAL_PAID)/NULLIF(SUM(t.TOTAL_PATIENTS),0) paid_per_patient_month
        FROM read_parquet('{TMSIS}') t
        JOIN read_parquet('{NPPES}') n ON CAST(n.npi AS VARCHAR)=t.BILLING_PROVIDER_NPI_NUM
        WHERE t.HCPCS_CODE='{code}' AND SUBSTR(t.CLAIM_FROM_MONTH,1,4)='{year}'
          AND n.state='{state}'
        GROUP BY 1 HAVING SUM(t.TOTAL_PATIENTS) >= 50""").fetchdf()
    tgt_ppm = paid / patient_months if patient_months else float("nan")
    out["paid_per_patient_month"] = tgt_ppm
    out["intensity_pctile_in_state"] = pctile_of(
        tgt_ppm, peers["paid_per_patient_month"])
    out["n_state_peers_same_code"] = int(len(peers))
    if out["intensity_pctile_in_state"] >= INTENSITY_FLAG_PCTILE:
        out["flags"].append("INTENSITY_P99_VS_STATE_PEERS")

    # ── 3. hours math (only when the descriptor is time-denominated) ────────
    desc = con.execute(f"""SELECT short_descriptor FROM read_parquet('{HCPCS_REF}')
                       WHERE code='{code}'""").fetchone()
    minutes = unit_minutes_from_descriptor(desc[0] if desc else None)
    out["code_descriptor"] = desc[0] if desc else None
    out["unit_minutes"] = minutes
    if minutes and patient_months:
        # LOWER BOUND: T-MSIS lines, not units — real hours can only be higher.
        hours_lb = lines * minutes / 60.0
        out["implied_hours_lower_bound"] = hours_lb
        out["hours_per_patient_month_lb"] = hours_lb / patient_months
        # Servicing-field QUALITY GATE (validated 2026-07-16): many org billers
        # put their OWN NPI (or nothing) in SERVICING_PROVIDER_NPI_NUM — e.g.
        # both CO wraparound targets are servicing==billing on 100% of rows.
        # Clinician-hours math is only sound when the field is a real staff
        # roster: >=5 distinct NON-SELF servicing NPIs on >=50% of rows.
        sv = con.execute(f"""
            SELECT COUNT(*) n_rows,
                   SUM(CASE WHEN SERVICING_PROVIDER_NPI_NUM ~ '^[12][0-9]{{9}}$'
                             AND SERVICING_PROVIDER_NPI_NUM <> BILLING_PROVIDER_NPI_NUM
                        THEN 1 ELSE 0 END) n_other_rows,
                   COUNT(DISTINCT CASE WHEN SERVICING_PROVIDER_NPI_NUM ~ '^[12][0-9]{{9}}$'
                             AND SERVICING_PROVIDER_NPI_NUM <> BILLING_PROVIDER_NPI_NUM
                        THEN SERVICING_PROVIDER_NPI_NUM END) n_clinicians
            FROM read_parquet('{TMSIS}')
            WHERE BILLING_PROVIDER_NPI_NUM='{npi}' AND HCPCS_CODE='{code}'
              AND SUBSTR(CLAIM_FROM_MONTH,1,4)='{year}'""").fetchdf().iloc[0]
        n_clin = int(sv["n_clinicians"] or 0)
        cover = float(sv["n_other_rows"] or 0) / max(float(sv["n_rows"] or 0), 1.0)
        out["n_servicing_clinicians"] = n_clin
        out["servicing_field_coverage"] = cover
        if n_clin >= 5 and cover >= 0.5:
            if float(tgt["n_months"] or 0):
                out["hours_per_clinician_month_lb"] = (
                    hours_lb / n_clin / float(tgt["n_months"]))
            if out.get("hours_per_clinician_month_lb", 0) > 320:   # 2x full-time
                out["flags"].append("CLINICIAN_HOURS_2X_FULL_TIME")
        else:
            out["notes"].append(
                f"servicing field unusable as staff roster (distinct non-self={n_clin}, "
                f"coverage={cover:.0%}) — clinician-hours math skipped; staffing "
                "impossibility needs state licensure/roster data")
        if out["hours_per_patient_month_lb"] > IMPOSSIBLE_PATIENT_HOURS_PER_MONTH:
            out["flags"].append("PATIENT_HOURS_EXCEED_FULL_TIME_JOB")

    # ── 4. monthly trajectory ────────────────────────────────────────────────
    mon = con.execute(f"""
        SELECT CLAIM_FROM_MONTH m, SUM(TOTAL_PAID) p FROM read_parquet('{TMSIS}')
        WHERE BILLING_PROVIDER_NPI_NUM='{npi}' AND HCPCS_CODE='{code}'{CUT}
        GROUP BY 1 ORDER BY 1""").fetchdf()
    if len(mon) >= 6:
        p = mon["p"].astype(float)
        out["months_active"] = int(len(mon))
        out["max_over_median_month"] = float(p.max() / max(p.median(), 1.0))
        recent, early = p.tail(6).mean(), p.head(6).mean()
        out["recent_vs_early_ratio"] = float(recent / max(early, 1.0))
        if recent < 0.25 * p.max():
            out["flags"].append("RECENT_CLIFF_possible_existing_action")
            out["notes"].append("recent billing collapsed vs peak — check for "
                                "existing suspension/enforcement before spending")
        elif out["recent_vs_early_ratio"] >= 5.0:
            # Ambiguity-level only (never per-se): explosive growth is the
            # Evergreen trajectory but has legitimate versions (new contracts,
            # expansion) — it sends a target to judgment, not to file.
            out["flags"].append("EXPLOSIVE_GROWTH_5X")

    # ── 6. ring scan: shared name tokens or shared servicing staff ──────────
    toks = name_tokens(out["org_name"])
    ring: list[dict] = []
    same_code_orgs = con.execute(f"""
        SELECT t.BILLING_PROVIDER_NPI_NUM npi, ANY_VALUE(n.Provider_Organization_Name) nm,
               SUM(t.TOTAL_PAID) paid
        FROM read_parquet('{TMSIS}') t
        JOIN read_parquet('{NPPES}') n ON CAST(n.npi AS VARCHAR)=t.BILLING_PROVIDER_NPI_NUM
        WHERE t.HCPCS_CODE='{code}' AND SUBSTR(t.CLAIM_FROM_MONTH,1,4)='{year}'
          AND n.state='{state}' AND n.Entity_Type_Code='2'
          AND t.BILLING_PROVIDER_NPI_NUM <> '{npi}'
        GROUP BY 1 HAVING SUM(t.TOTAL_PAID) > 1000000""").fetchdf()
    shared_staff = con.execute(f"""
        WITH tgt AS (SELECT DISTINCT SERVICING_PROVIDER_NPI_NUM s FROM read_parquet('{TMSIS}')
                     WHERE BILLING_PROVIDER_NPI_NUM='{npi}'
                       AND SERVICING_PROVIDER_NPI_NUM ~ '^[12][0-9]{{9}}$'{CUT})
        SELECT t.BILLING_PROVIDER_NPI_NUM npi, COUNT(DISTINCT t.SERVICING_PROVIDER_NPI_NUM) shared
        FROM read_parquet('{TMSIS}') t JOIN tgt ON tgt.s = t.SERVICING_PROVIDER_NPI_NUM
        WHERE t.BILLING_PROVIDER_NPI_NUM <> '{npi}'{CUTt}
        GROUP BY 1 HAVING COUNT(DISTINCT t.SERVICING_PROVIDER_NPI_NUM) >= 3
        ORDER BY 2 DESC LIMIT 15""").fetchdf()
    shared_map = dict(zip(shared_staff["npi"], shared_staff["shared"]))
    for _, r in same_code_orgs.iterrows():
        tok_hit = toks & name_tokens(r["nm"])
        staff_hit = int(shared_map.get(r["npi"], 0))
        if tok_hit or staff_hit:
            ring.append({"npi": r["npi"], "name": r["nm"], "paid": float(r["paid"]),
                         "shared_name_tokens": sorted(tok_hit),
                         "shared_servicing_npis": staff_hit})
    # staff sharing with DIFFERENT-code orgs still matters — append any not caught
    for onpi, cnt in shared_map.items():
        if not any(x["npi"] == onpi for x in ring):
            nm2 = con.execute(f"""SELECT Provider_Organization_Name FROM read_parquet('{NPPES}')
                              WHERE CAST(npi AS VARCHAR)='{onpi}'""").fetchone()
            ring.append({"npi": onpi, "name": nm2[0] if nm2 else None, "paid": None,
                         "shared_name_tokens": [], "shared_servicing_npis": int(cnt)})
    out["ring_candidates"] = ring
    if any(r["shared_servicing_npis"] >= 3 for r in ring):
        out["flags"].append("SHARED_SERVICING_STAFF_ACROSS_ORGS")
    if any(r["shared_name_tokens"] for r in ring):
        out["flags"].append("NAME_TOKEN_SIBLING_ORG_SAME_CODE_STATE")

    # ── track ────────────────────────────────────────────────────────────────
    per_se = {"LEIE_EXCLUDED", "PATIENT_HOURS_EXCEED_FULL_TIME_JOB",
              "CLINICIAN_HOURS_2X_FULL_TIME"}
    if per_se & set(out["flags"]):
        out["track"] = "per_se_candidate"
    elif out["flags"]:
        out["track"] = "ambiguous_needs_judgment"
    else:
        out["track"] = "benign_leaning"
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--npi", nargs="+", required=True)
    ap.add_argument("--year", type=int, default=2024)
    ap.add_argument("--out", type=Path, default=None, help="also write JSONL here")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    con = r2.duckdb_r2()
    results = []
    for npi in args.npi:
        LOG.info("pre-flight %s ...", npi)
        r = preflight_one(con, str(npi), args.year)
        results.append(r)
        print(json.dumps(r, indent=2, default=str))
    if args.out:
        with args.out.open("w") as f:
            for r in results:
                f.write(json.dumps(r, default=str) + "\n")
    print("\n=== SUMMARY ===")
    for r in results:
        print(f"{r['npi']} {str(r.get('org_name'))[:34]:35s} -> {r.get('track','?'):26s} "
              f"flags={r.get('flags')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
