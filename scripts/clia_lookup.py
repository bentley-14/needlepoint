"""CLIA disciplinary-registry lookup.

Queries the CMS CLIA Annual Laboratory Registry — 2,512 entries 2013-2024,
labs with CLIA certificates suspended / limited / revoked / cancelled,
plus a (usually empty) fraud-conviction section. Sourced from the 12
annual CMS PDFs (data/raw/clia_registry/) and parsed via
``scripts/ingest/parse_clia_registry.py``.

Queryable by: CLIA ID, lab name, address, city, state, ZIP, year,
sanction-type keyword, reason keyword.

For "is there an active CLIA-certified lab at this address?" (NOT the
disciplinary subset — the WHOLE certified population), use the affirmative
registry (CMS Provider of Services — Clinical Laboratories):

    python scripts/clia_lookup.py --certified --zip <ZIP> --state <ST>

This is the DETERMINISTIC ground truth for a "billed lab tests without a CLIA
certificate" count — confirm the absence (or presence) instead of inferring it
from NPPES. Query by ZIP+state (POS files key on the CLIA number + facility
address, not NPI; street-substring filters are brittle re: "N" vs "NORTH").

Examples:
    # Has a specific CLIA cert ever been sanctioned?
    python scripts/clia_lookup.py --clia-id 05D0913319

    # Has any sanctioned lab been at this address?
    python scripts/clia_lookup.py --address "2100 SOLAR DR" --state CA

    # All sanctioned labs in a ZIP across all years?
    python scripts/clia_lookup.py --zip 93036

    # Fuzzy lab name search
    python scripts/clia_lookup.py --lab-name "Prime Lab"

    # Search reasons for a keyword (e.g., 'fraud', 'kickback', 'referral')
    python scripts/clia_lookup.py --reason-contains fraud
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # repo root, so fraudlib imports

import argparse
import json
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
    r2 = _LocalR2()

REPO_ROOT = Path(__file__).resolve().parent.parent
DISCIPLINARY_PATH = REPO_ROOT / "data" / "processed" / "clia_disciplinary.parquet"
# Affirmative certification registry — the FULL population of CLIA-certified
# labs (CMS Provider of Services File, Clinical Laboratories), so a count that
# turns on "billed lab tests with NO CLIA certificate" can be CONFIRMED against
# the registry instead of inferred from NPPES. Keyed on facility/address (POS
# files carry the CLIA number, not NPI), so query by address/ZIP/name.
CERTIFIED_PATH = REPO_ROOT / "data" / "processed" / "clia_certified.parquet"


def _q(s: str) -> str:
    """SQL-escape a single-quoted literal."""
    return s.replace("'", "''")


def query_disciplinary(
    *,
    clia_id: str | None = None,
    lab_name: str | None = None,
    address: str | None = None,
    city: str | None = None,
    state: str | None = None,
    zipc: str | None = None,
    year: int | None = None,
    sanction_contains: str | None = None,
    reason_contains: str | None = None,
    limit: int = 50,
) -> list[dict]:
    _src = r2.parquet_source(DISCIPLINARY_PATH, f"{r2.R2_PANELS}/{DISCIPLINARY_PATH.name}")
    where = []
    if clia_id:
        where.append(f"clia_id = '{_q(clia_id.upper())}'")
    if lab_name:
        where.append(f"lab_name ILIKE '%{_q(lab_name)}%'")
    if address:
        where.append(f"street ILIKE '%{_q(address)}%'")
    if city:
        where.append(f"city ILIKE '%{_q(city)}%'")
    if state:
        where.append(f"state = '{_q(state.upper())}'")
    if zipc:
        where.append(f"zip LIKE '{_q(zipc)}%'")
    if year:
        where.append(f"year = {int(year)}")
    if sanction_contains:
        where.append(f"sanction ILIKE '%{_q(sanction_contains)}%'")
    if reason_contains:
        where.append(f"reason ILIKE '%{_q(reason_contains)}%'")
    where_sql = " AND ".join(where) if where else "1=1"
    sql = f"""
        SELECT year, clia_id, lab_name, director, street, city, state, zip,
               sanction, effective_date, reason, status, section, source_pdf
        FROM read_parquet('{_src}')
        WHERE {where_sql}
        ORDER BY year DESC, lab_name
        LIMIT {int(limit)}
    """
    return r2.duckdb_r2().execute(sql).df().to_dict("records")


def _format_disciplinary(rows: list[dict]) -> str:
    if not rows:
        return "  ✓ No disciplinary record found matching the criteria."
    out = [f"  {len(rows)} sanctioned-lab match(es):\n"]
    for r in rows:
        out.append(f"  • {r['lab_name']} ({r['clia_id']})  [{r['year']}]")
        out.append(f"      Director: {r['director']}")
        out.append(f"      Address:  {r['street']}, {r['city']}, {r['state']} {r['zip']}")
        out.append(f"      Sanction: {r['sanction']}")
        if r.get("effective_date"):
            out.append(f"      Effective: {r['effective_date']}")
        if r.get("reason"):
            out.append(f"      Reason:   {r['reason']}")
        if r.get("status"):
            out.append(f"      Status:   {r['status']}")
        out.append("")
    return "\n".join(out)


def query_certified(
    *,
    clia_num: str | None = None,
    fac_name: str | None = None,
    address: str | None = None,
    city: str | None = None,
    state: str | None = None,
    zipc: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Affirmative CLIA-certification lookup against the full registry.

    Answers "is there a CLIA-certified lab matching this facility/address?" —
    the deterministic ground truth for a "billed lab tests without a CLIA
    certificate" count. ``termination_cd = '00'`` generally means active /
    not-terminated.
    """
    _src = r2.parquet_source(CERTIFIED_PATH, f"{r2.R2_PANELS}/{CERTIFIED_PATH.name}")
    where = []
    if clia_num:
        where.append(f"clia_num = '{_q(clia_num.upper())}'")
    if fac_name:
        where.append(f"fac_name ILIKE '%{_q(fac_name)}%'")
    if address:
        where.append(f"street ILIKE '%{_q(address)}%'")
    if city:
        where.append(f"city ILIKE '%{_q(city)}%'")
    if state:
        where.append(f"state = '{_q(state.upper())}'")
    if zipc:
        where.append(f"zip LIKE '{_q(zipc)}%'")
    where_sql = " AND ".join(where) if where else "1=1"
    sql = f"""
        SELECT clia_num, fac_name, street, city, state, zip,
               certification_date, cert_action_cd, termination_cd,
               termination_date, compliance_status_cd
        FROM read_parquet('{_src}')
        WHERE {where_sql}
        ORDER BY (termination_cd = '00') DESC, fac_name
        LIMIT {int(limit)}
    """
    return r2.duckdb_r2().execute(sql).df().to_dict("records")


def _format_certified(rows: list[dict]) -> str:
    if not rows:
        return ("  ✓ NO CLIA-certified lab found matching the criteria — supports a "
                "'billed lab tests without a CLIA certificate' theory (verify the "
                "address/name variants before relying on the absence).")
    active = [r for r in rows if str(r.get("termination_cd")) == "00"]
    out = [f"  {len(rows)} CLIA-certified facility match(es) "
           f"({len(active)} active [termination_cd=00]):\n"]
    for r in rows:
        flag = "ACTIVE" if str(r.get("termination_cd")) == "00" else f"term_cd={r.get('termination_cd')}"
        out.append(f"  • {r['fac_name']} (CLIA {r['clia_num']}) [{flag}]")
        out.append(f"      Address: {r['street']}, {r['city']}, {r['state']} {r['zip']}")
        if r.get("certification_date"):
            out.append(f"      Certified: {r['certification_date']}  ·  action_cd={r.get('cert_action_cd')}")
        out.append("")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--certified", action="store_true",
                    help="Query the AFFIRMATIVE CLIA-certification registry (full "
                         "population of certified labs) instead of the disciplinary "
                         "subset. Use the same --clia-id/--lab-name/--address/--city/"
                         "--state/--zip filters; ideal for 'is there ANY CLIA cert at "
                         "this provider's billing address?'")

    # Phase 1 — disciplinary registry queries
    ap.add_argument("--clia-id", type=str, help="Exact CLIA Identification Number (e.g., 05D0913319)")
    ap.add_argument("--lab-name", type=str, help="Fuzzy substring match on lab name")
    ap.add_argument("--address", type=str, help="Substring match on street address (sanctioned labs)")
    ap.add_argument("--city", type=str)
    ap.add_argument("--state", type=str, help="2-letter state code")
    ap.add_argument("--zip", dest="zipc", type=str, help="ZIP code (prefix match supported, e.g., 930)")
    ap.add_argument("--year", type=int, help="Filter sanctioned labs to a single year (2013-2024)")
    ap.add_argument("--sanction-contains", type=str, help="Search sanction text (e.g., 'revocation', 'civil money')")
    ap.add_argument("--reason-contains", type=str, help="Search reason text (e.g., 'fraud', 'kickback', 'referral')")

    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--json", action="store_true", help="Emit raw JSON instead of pretty text")

    args = ap.parse_args()

    any_filter = any([
        args.clia_id, args.lab_name, args.address, args.city, args.state, args.zipc,
        args.year, args.sanction_contains, args.reason_contains,
    ])
    if not any_filter:
        ap.print_help()
        return 1

    # ── Affirmative certification mode (full registry) ──
    if args.certified:
        crows = query_certified(
            clia_num=args.clia_id, fac_name=args.lab_name, address=args.address,
            city=args.city, state=args.state, zipc=args.zipc, limit=args.limit,
        )
        if args.json:
            def _safe(v):
                return v.isoformat() if hasattr(v, "isoformat") else v
            crows = [{k: _safe(v) for k, v in r.items()} for r in crows]
            print(json.dumps({"certified": crows}, indent=2, default=str))
            return 0
        print("=== CLIA Affirmative Certification Lookup ===\n")
        print(f"  Source: {CERTIFIED_PATH.name} (CMS Provider of Services — Clinical "
              f"Laboratories; full CLIA-certified population)")
        print()
        print(_format_certified(crows))
        return 0

    rows = query_disciplinary(
        clia_id=args.clia_id,
        lab_name=args.lab_name,
        address=args.address,
        city=args.city,
        state=args.state,
        zipc=args.zipc,
        year=args.year,
        sanction_contains=args.sanction_contains,
        reason_contains=args.reason_contains,
        limit=args.limit,
    )

    if args.json:
        def _safe(v):
            if hasattr(v, "isoformat"):
                return v.isoformat()
            return v
        rows = [{k: _safe(v) for k, v in r.items()} for r in rows]
        print(json.dumps({"disciplinary": rows}, indent=2, default=str))
        return 0

    print("=== CLIA Disciplinary Registry Lookup ===\n")
    print(f"  Source: {DISCIPLINARY_PATH.name} (2,512 sanctioned-lab entries 2013-2024)")
    print(f"  Note:   For comprehensive 'lab at address' queries (not just sanctioned ones),")
    print(f"          use: python scripts/nppes_lookup.py --postal-code <ZIP> --state <ST>")
    print(f"          and filter results to taxonomy code 291U* (Clinical Medical Laboratory)")
    print()
    print(_format_disciplinary(rows))

    print("\n  Reproducible citation:")
    cmd = "python scripts/clia_lookup.py " + " ".join(
        f"--{k.replace('_','-')} {repr(v)}"
        for k, v in vars(args).items()
        if v is not None and v is not False and k != "json"
    )
    print(f"  {cmd}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
