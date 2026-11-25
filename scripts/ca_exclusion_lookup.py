"""California CHHS lookup — S&I exclusion list + Medi-Cal FFS Provider Profile.

Two California state-level data sources useful for the investigator:

  1. Provider Suspended and Ineligible List (S&I List) — California's Medi-Cal
     exclusion list (Welfare & Institutions Code §§ 14043.6 + 14123). Direct
     parallel to the federal OIG-LEIE for CA defendants. If a provider is on
     S&I but still billing Medicare or another state's Medicaid, that's
     slam-dunk scienter under SuperValu.

  2. Medi-Cal FFS Provider Profile — current snapshot of providers actively
     enrolled in California Medicaid FFS. Per-NPI, includes specialty, taxonomy,
     address, license, enrollment status.

CAVEATS:
  * Only ~13% of S&I rows have a 10-digit NPI extractable from the Provider
    Number field (older pre-NPI-era exclusions); the rest use license/name
    matching. Use --name or --license for those.
  * Both data sources are state of CA only. For non-CA defendants, the S&I
    list still includes out-of-state providers who were excluded from CA
    Medi-Cal (we sampled FL, AZ, IN physicians on the list).
  * The Medi-Cal FFS Provider Profile is "currently enrolled" only — does NOT
    include historical enrollment or providers who left the program.
  * The dataset excludes Managed Care plan providers (FFS-only).

Usage:
    python scripts/ca_exclusion_lookup.py --npi 1900000045
    python scripts/ca_exclusion_lookup.py --name "Juniper" --first "Nasim"
    python scripts/ca_exclusion_lookup.py --license "A 97083"
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
    r2 = _LocalR2()

REPO_ROOT = Path(__file__).resolve().parents[1]
SI_PATH = REPO_ROOT / "data/panels/ca_si_list.parquet"
FFS_PATH = REPO_ROOT / "data/panels/ca_medi_cal_providers.parquet"


def _si_src() -> str:
    return r2.parquet_source(SI_PATH, f"{r2.R2_PANELS}/{SI_PATH.name}")


def _ffs_src() -> str:
    return r2.parquet_source(FFS_PATH, f"{r2.R2_PANELS}/{FFS_PATH.name}")


def _check_si_by_npi(con, npi: str) -> list[dict]:
    """Return any S&I row whose `npis` list contains the given NPI."""
    rows = con.execute(f"""
        SELECT last_name, first_name, middle_name, aka_dba, address,
               provider_type, license_number, date_of_suspension, active_period,
               primary_npi, npis
        FROM read_parquet('{_si_src()}')
        WHERE list_contains(npis, '{npi}')
    """).fetchall()
    cols = ['last_name','first_name','middle_name','aka_dba','address',
            'provider_type','license_number','date_of_suspension','active_period',
            'primary_npi','npis']
    return [dict(zip(cols, r)) for r in rows]


def _check_si_by_name(con, last: str, first: str | None) -> list[dict]:
    """Return S&I rows matching name (case-insensitive)."""
    where = f"last_name_norm = UPPER('{last}')"
    if first:
        where += f" AND (first_name_norm = UPPER('{first}') OR first_name_norm LIKE UPPER('{first}%'))"
    rows = con.execute(f"""
        SELECT last_name, first_name, middle_name, aka_dba, address,
               provider_type, license_number, date_of_suspension, active_period,
               primary_npi
        FROM read_parquet('{_si_src()}')
        WHERE {where}
        LIMIT 25
    """).fetchall()
    cols = ['last_name','first_name','middle_name','aka_dba','address',
            'provider_type','license_number','date_of_suspension','active_period',
            'primary_npi']
    return [dict(zip(cols, r)) for r in rows]


def _check_si_by_license(con, license_num: str) -> list[dict]:
    """Return S&I rows where license_number contains the search term."""
    safe = license_num.replace("'", "''")
    rows = con.execute(f"""
        SELECT last_name, first_name, middle_name, address,
               provider_type, license_number, date_of_suspension, active_period,
               primary_npi
        FROM read_parquet('{_si_src()}')
        WHERE license_number = '{safe}' OR provider_number_raw LIKE '%{safe}%'
        LIMIT 25
    """).fetchall()
    cols = ['last_name','first_name','middle_name','address',
            'provider_type','license_number','date_of_suspension','active_period',
            'primary_npi']
    return [dict(zip(cols, r)) for r in rows]


def _check_ffs_by_npi(con, npi: str) -> list[dict]:
    """Return Medi-Cal FFS Provider Profile rows for this NPI."""
    rows = con.execute(f"""
        SELECT npi, legal_name, taxonomy, fi_provider_type, fi_provider_specialty,
               license, enroll_status_eff_dt, address1, city, state, zip5,
               out_of_state_indicator, provider_source
        FROM read_parquet('{_ffs_src()}')
        WHERE npi = '{npi}'
    """).fetchall()
    cols = ['npi','legal_name','taxonomy','fi_provider_type','fi_provider_specialty',
            'license','enroll_status_eff_dt','address1','city','state','zip5',
            'out_of_state_indicator','provider_source']
    return [dict(zip(cols, r)) for r in rows]


def _print_si_hit(hit: dict, idx: int) -> None:
    name = " ".join(filter(None, [hit.get('first_name'), hit.get('middle_name'), hit.get('last_name')]))
    print(f"  [{idx}] {name}")
    print(f"      Provider Type:    {hit.get('provider_type')}")
    if hit.get('aka_dba') and hit.get('aka_dba') not in ('N/A', None):
        print(f"      A/K/A — D/B/A:    {hit.get('aka_dba')}")
    if hit.get('address'):
        print(f"      Address:          {str(hit.get('address'))[:120]}")
    print(f"      License:          {hit.get('license_number')}")
    print(f"      Date suspended:   {hit.get('date_of_suspension')}")
    print(f"      Active period:    {hit.get('active_period')}")
    if hit.get('primary_npi'):
        print(f"      NPI:              {hit.get('primary_npi')}")
    print()


def _print_ffs_hit(hit: dict, idx: int) -> None:
    print(f"  [{idx}] {hit.get('legal_name')} (NPI {hit.get('npi')})")
    print(f"      Taxonomy:         {hit.get('taxonomy')}")
    print(f"      FI Provider Type: {hit.get('fi_provider_type')} / Specialty: {hit.get('fi_provider_specialty')}")
    print(f"      License:          {hit.get('license')}")
    print(f"      Enrollment date:  {hit.get('enroll_status_eff_dt')}")
    addr = " ".join(filter(None, [hit.get('address1'), hit.get('city'), hit.get('state'), hit.get('zip5')]))
    print(f"      Address:          {addr}")
    print(f"      Out-of-state?     {hit.get('out_of_state_indicator')}")
    print()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--npi", default=None, help="10-digit NPI to look up (preferred)")
    ap.add_argument("--name", default=None, help="Last name for S&I name-based fallback (use when no NPI)")
    ap.add_argument("--first", default=None, help="First name (refines --name)")
    ap.add_argument("--license", default=None, help="License number (alternative when no NPI)")
    args = ap.parse_args(argv)

    if not (args.npi or args.name or args.license):
        ap.error("Provide at least one of --npi / --name / --license")
    if args.npi and not (len(args.npi) == 10 and args.npi.isdigit() and args.npi[0] in "12"):
        print(f"WARNING: --npi {args.npi!r} doesn't look like a 10-digit NPI starting with 1 or 2",
              file=sys.stderr)

    con = r2.duckdb_r2()

    # ── 1. S&I exclusion lookup ────────────────────────────────────────────
    print("=== California S&I (Suspended & Ineligible) List ===")
    print(f"  Source: Welfare & Institutions Code §§ 14043.6 + 14123 — CA Medi-Cal exclusion list")
    print(f"  Coverage: 23,045 entries; ~13% have extractable NPIs")
    print()

    si_hits: list[dict] = []
    if args.npi:
        si_hits = _check_si_by_npi(con, args.npi)
        method = f"NPI {args.npi}"
    elif args.name:
        si_hits = _check_si_by_name(con, args.name, args.first)
        method = f"name '{args.name}'" + (f", first '{args.first}'" if args.first else "")
    elif args.license:
        si_hits = _check_si_by_license(con, args.license)
        method = f"license '{args.license}'"

    if si_hits:
        print(f"  ⚠️  MATCH FOUND ({len(si_hits)} entries by {method}):")
        print()
        for i, hit in enumerate(si_hits[:25], 1):
            _print_si_hit(hit, i)
        print(f"  IMPLICATION: This provider has been suspended from CA Medi-Cal. If they")
        print(f"  continued billing Medicare or another state's Medicaid after the suspension")
        print(f"  date, that's direct evidence of knowing falsity (SuperValu-grade scienter).")
    else:
        print(f"  ✓ No match found by {method}")
        if args.npi and args.npi.startswith("1"):
            # If no NPI hit, suggest a name-based follow-up
            print(f"    Note: only 13% of S&I rows have NPIs. Consider --name / --license")
            print(f"          fallback if defendant's NPI was issued after their exclusion.")
    print()

    # ── 2. Medi-Cal FFS Provider Profile ────────────────────────────────────
    if args.npi:
        print("=== Medi-Cal FFS Provider Profile (current enrollment) ===")
        ffs_hits = _check_ffs_by_npi(con, args.npi)
        if ffs_hits:
            print(f"  ✓ Currently enrolled in CA Medi-Cal FFS ({len(ffs_hits)} location(s)):")
            print()
            for i, hit in enumerate(ffs_hits[:5], 1):
                _print_ffs_hit(hit, i)
            if len(ffs_hits) > 5:
                print(f"  ... ({len(ffs_hits)-5} more locations)")
        else:
            print(f"  ✗ Not currently enrolled in CA Medi-Cal FFS")
            print(f"    Note: this does NOT mean the defendant has no Medicaid history —")
            print(f"          they may bill Medi-Cal Managed Care (not in this dataset),")
            print(f"          or have been unenrolled, or never enrolled. Cross-check the")
            print(f"          T-MSIS Provider Spending data via medicaid_lookup.py.")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
