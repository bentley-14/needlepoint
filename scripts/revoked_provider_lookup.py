"""Revoked Medicare Providers & Suppliers lookup.

Queries the CMS quarterly Revocation Extract (~7,500 revoked providers per
release, updated quarterly). Source: CMS Center for Program Integrity
(downloadable as `Revoked Medicare Providers and Suppliers.zip` from
cms.gov/data-research/statistics-trends-and-reports/medicare-program-integrity).

Each row = one Medicare enrollment revocation per 42 CFR § 424.535, with:
  - NPI (10-digit)
  - Provider name (individual or organization)
  - State
  - Provider type / specialty
  - Revocation reason (one or more 42 CFR § 424.535(a)(N) subsections)
  - Effective date
  - Reenrollment bar expiration

Common revocation reasons:
  (a)(1)  Noncompliance with enrollment requirements
  (a)(2)  Provider/supplier conduct (= OIG exclusion)
  (a)(3)  Felonies
  (a)(5)  On-site review failure
  (a)(8)(ii) Abuse of billing privileges: pattern or practice
  (a)(9)  Failure to report adverse legal actions / address changes
  (a)(12) Other program termination (e.g., Medicaid)
  (a)(13) Prescribing authority revoked
  (a)(19) Affiliation that poses an undue risk

Examples:
    # Is NPI 1900000045 (Juniper) revoked?
    python scripts/revoked_provider_lookup.py --npi 1900000045

    # Has any clinical lab in California been revoked for billing abuse?
    python scripts/revoked_provider_lookup.py --state CA \
        --provider-type "CLINICAL LABORATORY" \
        --reason-contains "Abuse of Billing"

    # All revocations for felony reasons since 2024
    python scripts/revoked_provider_lookup.py --reason-contains "Felonies" \
        --since 2024-01-01

    # Show only currently-barred providers (reenrollment bar not yet expired)
    python scripts/revoked_provider_lookup.py --npi 1900000045 --bar-active
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # repo root, so fraudlib imports

import argparse
import json
import sys
from datetime import datetime
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

REPO_ROOT = Path(__file__).resolve().parent.parent
REVOKED_PATH = REPO_ROOT / "data" / "processed" / "revoked_medicare_providers.parquet"
ADDITIONAL_PATH = REPO_ROOT / "data" / "processed" / "revoked_medicare_additional_npis.parquet"


def _q(s: str) -> str:
    return s.replace("'", "''")


def query_revoked(
    *,
    npi: str | None = None,
    last_name: str | None = None,
    first_name: str | None = None,
    org_name: str | None = None,
    state: str | None = None,
    provider_type: str | None = None,
    reason_contains: str | None = None,
    since: str | None = None,
    bar_active: bool = False,
    limit: int = 50,
) -> list[dict]:
    _src = r2.parquet_source(REVOKED_PATH, f"{r2.R2_PANELS}/{REVOKED_PATH.name}")
    where = []
    if npi:
        where.append(f"NPI = '{_q(npi.zfill(10))}'")
    if last_name:
        where.append(f"LAST_NAME ILIKE '%{_q(last_name)}%'")
    if first_name:
        where.append(f"FIRST_NAME ILIKE '%{_q(first_name)}%'")
    if org_name:
        where.append(f"ORG_NAME ILIKE '%{_q(org_name)}%'")
    if state:
        where.append(f"STATE_CD = '{_q(state.upper())}'")
    if provider_type:
        where.append(f"PROVIDER_TYPE_DESC ILIKE '%{_q(provider_type)}%'")
    if reason_contains:
        where.append(f"REVOCATION_RSN ILIKE '%{_q(reason_contains)}%'")
    if since:
        # CSV stores dates as MM/DD/YYYY → cast for comparison
        where.append(
            f"strptime(REVOCATION_EFCTV_DT, '%m/%d/%Y') >= strptime('{_q(since)}', '%Y-%m-%d')"
        )
    if bar_active:
        # REENROLLMENT_BAR_EXPRTN_DT > today (or NULL → still barred)
        today = datetime.now().strftime("%Y-%m-%d")
        where.append(
            f"(REENROLLMENT_BAR_EXPRTN_DT IS NULL OR "
            f"strptime(REENROLLMENT_BAR_EXPRTN_DT, '%m/%d/%Y') > strptime('{today}', '%Y-%m-%d'))"
        )
    where_sql = " AND ".join(where) if where else "1=1"
    sql = f"""
        SELECT
            NPI, ENRLMT_ID, FIRST_NAME, MDL_NAME, LAST_NAME, ORG_NAME,
            STATE_CD, PROVIDER_TYPE_DESC, REVOCATION_RSN,
            REVOCATION_EFCTV_DT, REENROLLMENT_BAR_EXPRTN_DT,
            MULTIPLE_NPI_FLAG, snapshot_date, release_quarter
        FROM read_parquet('{_src}')
        WHERE {where_sql}
        ORDER BY strptime(REVOCATION_EFCTV_DT, '%m/%d/%Y') DESC NULLS LAST
        LIMIT {int(limit)}
    """
    return r2.duckdb_r2().execute(sql).df().to_dict("records")


def _format(rows: list[dict]) -> str:
    if not rows:
        return "  ✓ No revocation record found matching the criteria."
    out = [f"  {len(rows)} revocation match(es):\n"]
    for r in rows:
        # Compose provider display name
        if r.get("ORG_NAME"):
            who = r["ORG_NAME"]
        else:
            parts = [r.get(k, "") for k in ("FIRST_NAME", "MDL_NAME", "LAST_NAME")]
            who = " ".join(p for p in parts if p)
        out.append(f"  • {who}  (NPI {r['NPI']})  [{r.get('STATE_CD','?')}]")
        out.append(f"      Type:        {r.get('PROVIDER_TYPE_DESC')}")
        out.append(f"      Reason:      {r.get('REVOCATION_RSN')}")
        out.append(f"      Effective:   {r.get('REVOCATION_EFCTV_DT')}")
        if r.get("REENROLLMENT_BAR_EXPRTN_DT"):
            out.append(f"      Bar expires: {r['REENROLLMENT_BAR_EXPRTN_DT']}")
        if r.get("MULTIPLE_NPI_FLAG") == "Y":
            out.append(f"      Multiple NPIs: YES (see Rvo_Additional_NPIs file)")
        out.append(f"      Enrollment ID: {r.get('ENRLMT_ID')}")
        out.append("")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--npi", type=str, help="10-digit NPI")
    ap.add_argument("--last-name", type=str)
    ap.add_argument("--first-name", type=str)
    ap.add_argument("--org-name", type=str, help="Organization name (fuzzy)")
    ap.add_argument("--state", type=str, help="2-letter state code")
    ap.add_argument("--provider-type", type=str,
                    help="Specialty/type substring (e.g., 'CLINICAL LABORATORY', 'PODIATRY')")
    ap.add_argument("--reason-contains", type=str,
                    help="Search revocation reasons (e.g., 'Felonies', 'Abuse of Billing', '424.535(A)(2)')")
    ap.add_argument("--since", type=str, help="Only revocations on/after YYYY-MM-DD")
    ap.add_argument("--bar-active", action="store_true",
                    help="Only show providers whose reenrollment bar has NOT yet expired")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--json", action="store_true")

    args = ap.parse_args()

    has_filter = any([
        args.npi, args.last_name, args.first_name, args.org_name,
        args.state, args.provider_type, args.reason_contains,
        args.since, args.bar_active,
    ])
    if not has_filter:
        ap.print_help()
        return 1

    rows = query_revoked(
        npi=args.npi,
        last_name=args.last_name,
        first_name=args.first_name,
        org_name=args.org_name,
        state=args.state,
        provider_type=args.provider_type,
        reason_contains=args.reason_contains,
        since=args.since,
        bar_active=args.bar_active,
        limit=args.limit,
    )

    if args.json:
        def _safe(v):
            if hasattr(v, "isoformat"):
                return v.isoformat()
            return v
        rows = [{k: _safe(v) for k, v in r.items()} for r in rows]
        print(json.dumps({"revoked": rows}, indent=2, default=str))
        return 0

    print("=== Revoked Medicare Providers & Suppliers Lookup ===\n")
    print(f"  Source: {REVOKED_PATH.name} (Q1 2026 release, snapshot 2026-02-14)")
    print(f"  Coverage: 7,465 revocations across 6,375 unique NPIs")
    print(f"  Citation: CMS Center for Program Integrity, 42 CFR § 424.535")
    print()
    print(_format(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
