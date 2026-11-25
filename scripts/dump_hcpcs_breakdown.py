"""Dump full per-(NPI, HCPCS) breakdown for an investigator agent.

Reads the CMS Provider-and-Service PUF (``cms_partb_svc_<year>.parquet``)
and prints, for the requested NPI + year(s):

  1. Top-15 HCPCS codes by service volume + payment
  2. Modality-level service / payment / beneficiary totals
  3. Predicate-evaluation presence check — for each modality where
     evaluation/intake codes are required before treatment can be billed
     (E&M new vs established; PT, OT, speech, mental-health, chiro
     treatment vs evaluation), reports the ratio and flags when no
     predicate codes are present at all (Mackby-grade incident-to
     compliance failure under 42 CFR 410.26 / 410.32 / 410.61).
  4. Full E&M code-level detail (which 99xxx codes, services, beneficiaries)

This is a DATA-DUMP tool — it surfaces structural facts. It does NOT
classify the provider as fraudulent. Interpretation is the investigator's
job (e.g., a chiropractor billing exclusively manipulation codes is
expected; a GP billing them is not).

Usage:
    python scripts/dump_hcpcs_breakdown.py --npi <NPI> --year 2023
    python scripts/dump_hcpcs_breakdown.py --npi <NPI> --years 2019,2020,2021,2022,2023
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd
import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
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

# Cleanroom + main worktree both standardize on data/panels (cleanroom
# symlinks legacy data/processed → data/panels).
_CANDIDATE_DIRS = [Path("data/panels")]

# ─────────────────────────────────────────────────────────────────────────────
# Treatment-vs-predicate-evaluation modality registry
# ─────────────────────────────────────────────────────────────────────────────
# Generic principle: for any therapy/treatment modality with required
# initial-evaluation codes, a provider billing treatment codes at volume
# while billing zero (or near-zero) evaluation codes is structurally
# non-compliant. The same logic applies whether the modality is PT, OT,
# speech, mental health, or established-patient E&M.
#
# Each modality has:
#   - eval_codes:  the initial evaluation / intake / new-patient code set
#   - treat_codes: the treatment / established-patient / procedural set
#   - description: human-readable label
#
# The script reports services / payment in both groups + the eval-to-treat
# ratio. NO interpretation is encoded beyond the structural ratio; whether
# the provider's specialty makes the modality legitimate is the
# investigator's call.
MODALITIES = {
    "em_office": {
        "eval_codes":  {"99201", "99202", "99203", "99204", "99205"},
        "treat_codes": {"99211", "99212", "99213", "99214", "99215"},
        "description": "E&M office visit (new patient vs established patient)",
        "predicate_kind": "new-patient intake",
        "rule_citation": "CPT § 99201-99215 distinction",
    },
    # Eye-care providers (optometry / ophthalmology) legitimately bill the
    # 92002/92004/92012/92014 ophthalmological exam family INSTEAD OF 99xxx
    # E&M — not in addition to it. Without this modality, an optometrist
    # billing 92012/92014 at normal volume with zero 99xxx trips no `em_office`
    # hit (both its eval_codes and treat_codes are 99xxx-only, so eval_svcs
    # == treat_svcs == 0 and the modality is silently skipped) — but that
    # silence was previously misread as "no predicate-evaluation codes
    # present at all," a false eval-presence-absence flag. See the
    # family-satisfied summary emitted after this modality pair below.
    "ophthalmology_office": {
        "eval_codes":  {"92002", "92004"},
        "treat_codes": {"92012", "92014"},
        "description": "Ophthalmological exam (new patient vs established patient)",
        "predicate_kind": "new-patient intake",
        "rule_citation": "CPT § 92002/92004/92012/92014 distinction",
    },
    "physical_therapy": {
        "eval_codes":  {"97161", "97162", "97163", "97164"},
        "treat_codes": {"97110", "97112", "97116", "97140", "97150", "97530",
                        "97535", "97035", "97032", "97014", "G0283"},
        "description": "Physical therapy (initial evaluation vs treatment)",
        "predicate_kind": "PT initial evaluation",
        "rule_citation": "42 CFR 410.61; CMS Pub 100-04 Ch. 15 § 220; U.S. v. Mackby, 261 F.3d 821 (9th Cir. 2001)",
    },
    "occupational_therapy": {
        "eval_codes":  {"97165", "97166", "97167", "97168"},
        "treat_codes": {"97530", "97535", "97110", "97112"},
        "description": "Occupational therapy (initial evaluation vs treatment)",
        "predicate_kind": "OT initial evaluation",
        "rule_citation": "42 CFR 410.59; CMS Pub 100-04 Ch. 15 § 220",
    },
    "speech_therapy": {
        "eval_codes":  {"92521", "92522", "92523", "92524", "92610"},
        "treat_codes": {"92507", "92508"},
        "description": "Speech-language pathology (evaluation vs therapy)",
        "predicate_kind": "speech evaluation",
        "rule_citation": "42 CFR 410.62",
    },
    "mental_health_psych": {
        "eval_codes":  {"90791", "90792"},
        "treat_codes": {"90832", "90833", "90834", "90836", "90837", "90838",
                        "90846", "90847", "90849", "90853"},
        "description": "Psychotherapy (diagnostic evaluation vs therapy session)",
        "predicate_kind": "psychiatric diagnostic evaluation",
        "rule_citation": "CMS Pub 100-04 Ch. 12 § 210",
    },
    "chiropractic": {
        # Chiropractors do NOT typically bill 99201-99215 E&M; predicate is the
        # initial visit established by an exam component within their service set.
        # The flag below is generated only when manipulation codes are billed
        # with no E&M codes at all from the new-patient set.
        "eval_codes":  {"99201", "99202", "99203", "99204", "99205"},
        "treat_codes": {"98940", "98941", "98942", "98943"},
        "description": "Chiropractic manipulation (new-patient exam vs manipulation)",
        "predicate_kind": "new-patient exam",
        "rule_citation": "42 CFR 410.21; CMS Pub 100-04 Ch. 15 § 240",
    },
}

# E&M complexity grouping (a separate axis from new-vs-established)
EM_HIGH = {"99204", "99205", "99214", "99215"}
EM_LOW = {"99211", "99212", "99213", "99202", "99203"}


def _find_svc_parquet(year: int) -> str | None:
    """Return the parquet source (local path string or R2 s3:// URL) for the given year."""
    basename = f"cms_partb_svc_{year}.parquet"
    for d in _CANDIDATE_DIRS:
        p = d / basename
        if p.exists():
            return str(p)
    # Fall back to R2
    return r2.parquet_source(
        _CANDIDATE_DIRS[0] / basename,
        f"{r2.R2_PANELS}/{basename}",
    )


def _years_svc() -> list[int]:
    """Years for which a cms_partb_svc_<year> panel exists — local glob UNION R2's
    years (the source of truth), so it spans the full canonical history even with no
    local mirror; the calibration jail's shim returns [] (local workspace only)."""
    seen: set[int] = set()
    for d in [REPO / "data/panels", REPO / "data/processed"]:
        for p in d.glob("cms_partb_svc_*.parquet"):
            tail = p.stem.rsplit("_", 1)[-1]
            if tail.isdigit():
                seen.add(int(tail))
    return sorted(seen | set(r2.list_v1_years("cms_partb_svc")))


def _svc_source(year: int) -> str:
    """Parquet source for a single svc year, using the same resolution as
    _find_svc_parquet (same relative _CANDIDATE_DIRS lookup, same R2 fallback)."""
    return _find_svc_parquet(year)


def _dump_year_sub(npi: str, year: int, sub: "pd.DataFrame", src: str) -> None:
    """Run all per-year analysis and printing on a pre-loaded NPI slice DataFrame.

    Called both from dump_year (single-year, per-year duckdb read) and from the
    batched multi-year path in main() (one read_parquet scan for all years)."""
    if sub.empty:
        print(f"\n[year {year}] NPI {npi} not in this year's PUF")
        return

    # Derived per-row dollar totals
    if "Avg_Mdcr_Pymt_Amt" in sub.columns and "Tot_Srvcs" in sub.columns:
        sub = sub.assign(_tot_paid=sub["Avg_Mdcr_Pymt_Amt"] * sub["Tot_Srvcs"])

    total_svcs = float(sub["Tot_Srvcs"].sum())
    total_paid = float(sub["_tot_paid"].sum()) if "_tot_paid" in sub.columns else 0.0

    print(f"\n{'=' * 78}")
    print(f"YEAR {year} — NPI {npi} — HCPCS BREAKDOWN")
    print(f"{'=' * 78}")
    print(f"  source: {src}")
    print(f"  HCPCS codes billed: {len(sub)}")
    print(f"  total services: {int(total_svcs):,}")
    if total_paid > 0:
        print(f"  total Medicare paid: ${total_paid:,.0f}")

    # ── Predicate-evaluation presence check (generic across modalities) ──
    print(f"\n  PREDICATE-EVALUATION PRESENCE CHECK:")
    print(f"  Generic principle: any modality with required initial-evaluation codes")
    print(f"  is structurally non-compliant when treatment is billed at volume but")
    print(f"  predicate evaluation codes are absent (or near-absent).")
    print(f"  See 42 CFR 410.26 et seq.; U.S. v. Mackby, 261 F.3d 821 (9th Cir. 2001).")
    print()
    any_modality_hit = False
    em_family_svcs: dict[str, float] = {}  # "em_office"/"ophthalmology_office" -> total svcs billed
    for mname, mod in MODALITIES.items():
        eval_rows = sub[sub["HCPCS_Cd"].isin(mod["eval_codes"])]
        treat_rows = sub[sub["HCPCS_Cd"].isin(mod["treat_codes"])]
        eval_svcs = float(eval_rows["Tot_Srvcs"].sum())
        treat_svcs = float(treat_rows["Tot_Srvcs"].sum())
        if mname in ("em_office", "ophthalmology_office"):
            em_family_svcs[mname] = eval_svcs + treat_svcs
        # Only report modalities the provider actually bills
        if eval_svcs == 0 and treat_svcs == 0:
            continue
        any_modality_hit = True
        treat_share = treat_svcs / total_svcs if total_svcs > 0 else 0
        if treat_svcs > 0:
            ratio = eval_svcs / treat_svcs
        else:
            ratio = float("inf")  # treatment absent; not interesting
        # Flag thresholds:
        #   treat-only (zero eval despite ≥100 treat svcs) → ★★★ structural absence
        #   treat-dominant (eval/treat < 0.01)             → ★★  near-absent predicate
        #   asymmetric (eval/treat < 0.05)                 → ★   asymmetric ratio
        flag = ""
        if treat_svcs >= 100 and eval_svcs == 0:
            flag = "★★★ ZERO-EVALUATION — structural predicate-code absence"
        elif treat_svcs > 0 and ratio < 0.01:
            flag = "★★  NEAR-ZERO-EVALUATION — predicate codes near-absent"
        elif treat_svcs > 0 and ratio < 0.05:
            flag = "★   ASYMMETRIC RATIO — eval codes underrepresented"
        ratio_str = f"{ratio:.4f}" if ratio != float("inf") else "n/a (no treat)"
        print(f"    [{mname}] {mod['description']}")
        print(f"      eval codes  ({sorted(mod['eval_codes'])}): {int(eval_svcs):>6,} svcs")
        print(f"      treat codes ({len(mod['treat_codes'])} codes incl. {sorted(mod['treat_codes'])[:4]}...): {int(treat_svcs):>6,} svcs")
        print(f"      treatment share of total billing: {treat_share:.1%}")
        print(f"      eval/treat ratio: {ratio_str}  {flag}")
        if flag.startswith("★★★"):
            print(f"      RULE: {mod['rule_citation']}")
            print(f"      Predicate-kind missing: {mod['predicate_kind']}")
        print()
    if not any_modality_hit:
        print(f"    (no codes from any tracked modality found — check top-15 below)")

    # ── E&M-vs-ophthalmology family-satisfied statement ──
    # Both `em_office` (99xxx) and `ophthalmology_office` (92002/92004/92012/
    # 92014) are alternative billing families for the SAME predicate-eval
    # concept (new-vs-established patient exam) — a provider legitimately
    # uses one or the other, not both. State explicitly which family (if
    # any) satisfied the check so "no 99xxx" is never misread as "no
    # predicate-evaluation codes present at all" for an eye-care provider.
    em_hit = em_family_svcs.get("em_office", 0.0) > 0
    oph_hit = em_family_svcs.get("ophthalmology_office", 0.0) > 0
    if em_hit and oph_hit:
        family_stmt = "BOTH families billed — E&M (99xxx) AND ophthalmology (92002/92004/92012/92014)."
    elif em_hit:
        family_stmt = "E&M family (99xxx) satisfied the check; ophthalmology family (92002/92004/92012/92014) is absent."
    elif oph_hit:
        family_stmt = "Ophthalmology family (92002/92004/92012/92014) satisfied the check; E&M family (99xxx) is absent — expected for eye-care specialties, not a predicate-evaluation absence."
    else:
        family_stmt = "BOTH families absent — no 99xxx and no 92002/92004/92012/92014 billed."
    print(f"  E&M/OPHTHALMOLOGY FAMILY CHECK: {family_stmt}")

    # ── E&M complexity (separate axis) ──
    high_svcs = float(sub[sub["HCPCS_Cd"].isin(EM_HIGH)]["Tot_Srvcs"].sum())
    low_svcs = float(sub[sub["HCPCS_Cd"].isin(EM_LOW)]["Tot_Srvcs"].sum())
    total_em = high_svcs + low_svcs
    if total_em > 0:
        share = high_svcs / total_em
        flag = " ★ ELEVATED — high-complexity E&M dominates" if share > 0.50 else ""
        print(f"  E&M COMPLEXITY:")
        print(f"    high-level share (99204/99205/99214/99215): {share:.1%}{flag}")

    # ── J-code concentration ──
    j_rows = sub[sub["HCPCS_Cd"].astype(str).str.startswith("J")]
    if not j_rows.empty:
        j_svcs = float(j_rows["Tot_Srvcs"].sum())
        j_paid = float(j_rows["_tot_paid"].sum()) if "_tot_paid" in j_rows.columns else 0
        top_j = j_rows.nlargest(1, "Tot_Srvcs").iloc[0]
        j_share = j_svcs / total_svcs if total_svcs > 0 else 0
        top1_conc = float(top_j["Tot_Srvcs"]) / j_svcs if j_svcs > 0 else 0
        print(f"\n  J-CODE (drug) CONCENTRATION:")
        print(f"    J-code services: {int(j_svcs):,} ({j_share:.1%} of total)")
        print(f"    J-code payment: ${j_paid:,.0f}")
        print(f"    top-1 J-code: {top_j['HCPCS_Cd']} = {top1_conc:.0%} of all J-code services")
        if top1_conc > 0.90:
            print(f"      ★ EXTREME single-drug concentration — fraud schemes commonly converge on one J-code")

    # ── Top-15 by service volume ──
    print(f"\n  TOP-15 BY SERVICE VOLUME:")
    print(f"    {'HCPCS':<8} {'Tot_Srvcs':>10} {'Tot_Benes':>10} {'Avg_Pmt':>9} {'_tot_paid':>11}")
    top = sub.nlargest(15, "Tot_Srvcs")
    for _, r in top.iterrows():
        code = r["HCPCS_Cd"]
        svcs = int(r.get("Tot_Srvcs", 0))
        benes = int(r.get("Tot_Benes", 0))
        avg = float(r.get("Avg_Mdcr_Pymt_Amt", 0))
        paid = float(r.get("_tot_paid", 0))
        print(f"    {code:<8} {svcs:>10,} {benes:>10,} ${avg:>7.2f} ${paid:>10,.0f}")


def dump_year(npi: str, year: int) -> None:
    src = _find_svc_parquet(year)

    # Graceful skip: if the panel file doesn't exist locally (remote sources are
    # URLs such as s3:// or hf://, so check only local paths).
    if src and "://" not in src:
        from pathlib import Path as _P
        if not _P(src).exists():
            print(f"(year {year}: no data)")
            return

    if not src:
        print(f"(year {year}: no data)")
        return

    # Stream-filter via duckdb instead of pd.read_parquet(p) (which loaded the
    # entire ~400MB Part B service PUF into memory — several parallel dump calls
    # OOM'd the machine). duckdb pushes the NPI predicate into the parquet scan and
    # returns only this provider's rows, so peak memory is a few MB regardless of
    # how many run concurrently. NPI column dtype varies across years (int64 vs
    # string), so cast to VARCHAR in the predicate.
    try:
        con = r2.duckdb_r2()
        sub = con.execute(
            f"SELECT * FROM read_parquet('{src}') "
            f"WHERE CAST(Rndrng_NPI AS VARCHAR) = '{npi}'"
        ).df()
    except Exception as exc:  # noqa: BLE001
        print(f"(year {year}: no data — {exc})")
        return
    _dump_year_sub(npi, year, sub, src)


def _parse_years(spec: str | list[str]) -> list[int]:
    """Normalize any year specification to a sorted list of ints.

    Accepts:
      - A single string: ``'2019'``, ``'2019,2020'``, ``'2019-2024'``, mix
      - A list of strings (nargs='+'): ``['2022', '2023']`` or ``['2019-2024']``
      - Space-separated tokens within a single string: ``'2022 2023'``

    All forms end up in one flat token stream before parsing.
    """
    # Normalise to a flat list of comma/dash/space-delimited tokens
    if isinstance(spec, list):
        raw = ",".join(spec)
    else:
        raw = spec
    # Replace spaces and semicolons with commas so one splitter handles all
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--npi", required=True, type=str)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--year", type=int)
    g.add_argument("--years", nargs="+",
                   help="Year(s): range '2019-2024', comma '2019,2020', or space-separated '2022 2023'")
    args = ap.parse_args(argv)

    npi = args.npi
    print(f"NPI: {npi}")

    if args.year:
        # Single-year path unchanged.
        years = [args.year]
        print(f"Years: {years}")
        dump_year(npi, args.year)
        return 0

    # Multi-year path: batch ALL requested years into ONE read_parquet scan.
    years = _parse_years(args.years)
    print(f"Years: {years}")

    # Intersect requested years with locally-existing years to avoid referencing
    # missing files (e.g. a year not yet published).  A missing year then yields
    # an empty slice below — matching the original per-year read's "not in PUF" path.
    existing = set(_years_svc())
    batch_years = [y for y in years if y in existing]
    missing_years = [y for y in years if y not in existing]

    if batch_years:
        # Build ONE source list — one entry per (existing) year, so read_parquet
        # reads all years in a single scan and exposes `filename` for year extraction.
        srcs = [_svc_source(y) for y in batch_years]
        src_list = "[" + ",".join(f"'{s}'" for s in srcs) + "]"
        family = "cms_partb_svc"
        yr_expr = f"CAST(regexp_extract(filename, '{family}_(\\d+)\\.parquet', 1) AS INT)"
        con = r2.duckdb_r2()
        all_df = con.execute(
            f"SELECT {yr_expr} AS _yr, * FROM read_parquet({src_list}, filename=true) "
            f"WHERE CAST(Rndrng_NPI AS VARCHAR) = '{npi}'"
        ).df()
        # Build a dict year → (slice_df, src_string) for the analysis loop below.
        src_by_year = {y: _svc_source(y) for y in batch_years}
        slices: dict[int, "pd.DataFrame"] = {}
        for y in batch_years:
            slices[y] = all_df[all_df["_yr"] == y].drop(columns=["_yr"]).reset_index(drop=True)
    else:
        slices = {}
        src_by_year = {}

    for y in years:
        if y in slices:
            _dump_year_sub(npi, y, slices[y], src_by_year[y])
        else:
            # Year was in the requested list but not locally available — fall through
            # to the original per-year path which will resolve via R2.
            dump_year(npi, y)

    return 0


if __name__ == "__main__":
    sys.exit(main())
