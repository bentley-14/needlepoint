"""Per-NPI Medicaid spending lookup for investigator agents.

Reads the HHS T-MSIS "Medicaid Provider Spending by HCPCS" dataset
(2018-2024, ~238M rows) via DuckDB and surfaces per-NPI billing patterns.
Mirrors ``scripts/dump_hcpcs_breakdown.py`` but for the Medicaid side.

DATA QUALITY CAVEATS (from KFF + HHS T-MSIS analysis):
  * 6 states have unusable T-MSIS data in 2024; 16 more with high concerns.
    Absence of Medicaid records for a defendant does NOT prove absence of
    Medicaid billing — it may indicate state-level data quality issues.
  * ~3.3% of rows have NULL billing NPI (state/local agency aggregates).
    This CLI filters those out automatically via NPI regex match.
  * No diagnosis codes, no modifiers, no member-level data, no Rx.
  * Coverage is FFS Medicaid + Managed Care + CHIP, outpatient/professional
    only. Excludes ~37% of total Medicaid spending (institutional + Rx).
  * Some "providers" are state/local government agencies — filtered via
    10-digit NPI regex ('^[12][0-9]{9}$').

Cross-program audit (--compare-medicare):
  Pulls the same HCPCS codes from the Medicare PUF (cms_partb_svc_<year>)
  side-by-side. Useful for detecting selective-payer schemes (e.g., a
  UDS-overuse pattern that applies to Medicare but not Medicaid implies
  knowing payer-specific reimbursement gaming).

Usage:
    python scripts/medicaid_lookup.py --npi 1900000016
    python scripts/medicaid_lookup.py --npi 1900000016 --year 2023
    python scripts/medicaid_lookup.py --npi 1900000016 --compare-medicare
    python scripts/medicaid_lookup.py --npi 1900000016 --hcpcs G0480,G0482,80307
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb

REPO_ROOT = Path(__file__).resolve().parents[1]
DUCKDB_PATH = REPO_ROOT / "data/raw/medicaid/provider_spending.duckdb"
PARQUET_PATH = REPO_ROOT / "data/raw/medicaid/provider_spending_full.parquet"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
from fraudlib import r2  # noqa: E402  (local file, else private bucket / public mirror)
from fraudlib.medicaid_cutoff import claim_clause  # noqa: E402  leak-safe gate
MEDICARE_PUF_GLOB = "data/panels/cms_partb_svc_{year}.parquet"

# Shown when the Medicaid T-MSIS dataset isn't present locally — degrade to the Medicare-side
# tools instead of crashing, so a missing download still leaves the investigator a usable path.
MEDICARE_FALLBACK_MSG = (
    "\nMedicare-data FALLBACK (use while Medicaid T-MSIS is unavailable):\n"
    "  - Part B by HCPCS:        python scripts/dump_hcpcs_breakdown.py --npi <NPI> --years 2018,2019,2020,2021,2022,2023\n"
    "  - Part D prescriber/drug: python scripts/partd_drug_lookup.py --npi <NPI> [--year YYYY] [--drug-class opioid]\n"
    "To enable the Medicaid side here: `make pull-medicaid` (pulls the ~3 GB T-MSIS parquet from R2)."
)

# Years available in the Medicaid dataset
DEFAULT_YEARS = [2018, 2019, 2020, 2021, 2022, 2023, 2024]

# Filter to real provider NPIs (10-digit starting with 1 or 2). Excludes
# the ~3.3% NULL billing NPIs (state/local agency rows) and the rare
# sentinel/non-NPI IDs.
NPI_FILTER = "BILLING_PROVIDER_NPI_NUM ~ '^[12][0-9]{9}$'"


def _connect() -> tuple[duckdb.DuckDBPyConnection, str]:
    """Open the indexed DuckDB file when available, else fall back to the parquet.

    Returns ``(connection, table_expr)`` where ``table_expr`` is the SQL
    fragment to use in FROM clauses (e.g., ``dataset`` or
    ``read_parquet('path/to/file.parquet')``).
    """
    if DUCKDB_PATH.exists():
        con = duckdb.connect(str(DUCKDB_PATH), read_only=True)
        return con, "dataset"
    src = r2.locate(PARQUET_PATH)
    if src:
        con = r2.duckdb_r2()
        return con, f"read_parquet('{src}')"
    raise FileNotFoundError(
        f"Neither {DUCKDB_PATH} nor {PARQUET_PATH} found. "
        f"Did you run the download step?"
    )


def _print_annual_rollup(con, table: str, npi: str) -> None:
    """Print year x metric rollup across all 7 years of coverage."""
    rows = con.execute(f"""
        SELECT SUBSTR(CLAIM_FROM_MONTH, 1, 4) AS year,
               SUM(TOTAL_CLAIM_LINES) AS claims,
               SUM(TOTAL_PATIENTS)    AS pts,
               ROUND(SUM(TOTAL_PAID), 0) AS paid_usd,
               COUNT(DISTINCT HCPCS_CODE) AS unique_hcpcs
        FROM {table}
        WHERE {NPI_FILTER}
          AND BILLING_PROVIDER_NPI_NUM = '{npi}'{claim_clause("CLAIM_FROM_MONTH")}
        GROUP BY year
        ORDER BY year
    """).fetchall()
    if not rows:
        print(f"  No Medicaid billing records for NPI {npi} (2018-2024).")
        print(f"  Note: 6 states have unusable T-MSIS data + 16 more with high concerns;")
        print(f"        absence here does NOT prove absence of Medicaid billing.")
        return
    print(f"  {'year':<6} {'claims':>10} {'patients':>10} {'paid_usd':>14} {'#hcpcs':>8}")
    print(f"  {'-'*6} {'-'*10} {'-'*10} {'-'*14} {'-'*8}")
    for year, claims, pts, paid, n_hcpcs in rows:
        print(f"  {year:<6} {claims:>10,} {pts:>10,} ${paid:>13,.0f} {n_hcpcs:>8}")


def _print_top_hcpcs(con, table: str, npi: str, year: int | None) -> None:
    """Print top-15 HCPCS codes by paid amount (optionally year-filtered)."""
    where = f"BILLING_PROVIDER_NPI_NUM = '{npi}'"
    if year is not None:
        where += f" AND SUBSTR(CLAIM_FROM_MONTH, 1, 4) = '{year}'"
    where += claim_clause("CLAIM_FROM_MONTH")
    rows = con.execute(f"""
        SELECT HCPCS_CODE,
               SUM(TOTAL_CLAIM_LINES) AS claims,
               SUM(TOTAL_PATIENTS)    AS pts,
               ROUND(SUM(TOTAL_PAID), 0) AS paid_usd
        FROM {table}
        WHERE {NPI_FILTER}
          AND {where}
        GROUP BY HCPCS_CODE
        ORDER BY paid_usd DESC
        LIMIT 15
    """).fetchall()
    if not rows:
        return
    label = f"Top-15 HCPCS (year {year})" if year else "Top-15 HCPCS (all years)"
    print(f"\n  === {label} ===")
    print(f"  {'hcpcs':<8} {'claims':>10} {'patients':>10} {'paid_usd':>14}")
    print(f"  {'-'*8} {'-'*10} {'-'*10} {'-'*14}")
    for code, claims, pts, paid in rows:
        print(f"  {code:<8} {claims:>10,} {pts:>10,} ${paid:>13,.0f}")


def _print_monthly_trajectory(con, table: str, npi: str, year: int) -> None:
    """Print month-by-month billing for a single year."""
    rows = con.execute(f"""
        SELECT CLAIM_FROM_MONTH,
               COUNT(DISTINCT HCPCS_CODE) AS n_hcpcs,
               SUM(TOTAL_CLAIM_LINES) AS claims,
               SUM(TOTAL_PATIENTS)    AS pts,
               ROUND(SUM(TOTAL_PAID), 0) AS paid_usd
        FROM {table}
        WHERE {NPI_FILTER}
          AND BILLING_PROVIDER_NPI_NUM = '{npi}'
          AND SUBSTR(CLAIM_FROM_MONTH, 1, 4) = '{year}'{claim_clause("CLAIM_FROM_MONTH")}
        GROUP BY CLAIM_FROM_MONTH
        ORDER BY CLAIM_FROM_MONTH
    """).fetchall()
    if not rows:
        return
    print(f"\n  === Monthly trajectory (year {year}) ===")
    print(f"  {'month':<8} {'#hcpcs':>7} {'claims':>10} {'patients':>10} {'paid_usd':>14}")
    print(f"  {'-'*8} {'-'*7} {'-'*10} {'-'*10} {'-'*14}")
    for month, n_hcpcs, claims, pts, paid in rows:
        print(f"  {month:<8} {n_hcpcs:>7} {claims:>10,} {pts:>10,} ${paid:>13,.0f}")


def _print_hcpcs_filter(con, table: str, npi: str, hcpcs_list: list[str]) -> None:
    """Print only the requested HCPCS codes."""
    codes_sql = ",".join(f"'{c}'" for c in hcpcs_list)
    rows = con.execute(f"""
        SELECT HCPCS_CODE,
               SUBSTR(CLAIM_FROM_MONTH, 1, 4) AS year,
               SUM(TOTAL_CLAIM_LINES) AS claims,
               SUM(TOTAL_PATIENTS)    AS pts,
               ROUND(SUM(TOTAL_PAID), 0) AS paid_usd
        FROM {table}
        WHERE {NPI_FILTER}
          AND BILLING_PROVIDER_NPI_NUM = '{npi}'
          AND HCPCS_CODE IN ({codes_sql}){claim_clause("CLAIM_FROM_MONTH")}
        GROUP BY HCPCS_CODE, year
        ORDER BY HCPCS_CODE, year
    """).fetchall()
    if not rows:
        print(f"\n  === HCPCS filter {hcpcs_list} ===")
        print(f"  (none of those codes billed to Medicaid by NPI {npi})")
        return
    print(f"\n  === HCPCS filter {hcpcs_list} ===")
    print(f"  {'hcpcs':<8} {'year':<6} {'claims':>10} {'patients':>10} {'paid_usd':>14}")
    print(f"  {'-'*8} {'-'*6} {'-'*10} {'-'*10} {'-'*14}")
    for code, year, claims, pts, paid in rows:
        print(f"  {code:<8} {year:<6} {claims:>10,} {pts:>10,} ${paid:>13,.0f}")


def _compare_medicare(con, table: str, npi: str) -> None:
    """Cross-program audit — for each HCPCS the NPI billed to Medicaid,
    pull the same HCPCS from Medicare PUF + display side-by-side.

    Three patterns to surface:
      1. MATCH — billed to both → systemic pattern (scienter strengthened)
      2. MEDICAID-ONLY — billed only to Medicaid → may indicate dual-eligible focus
      3. MEDICARE-ONLY — billed only to Medicare → selective-payer scheme (also scienter-relevant)
    """
    print(f"\n  === Cross-program audit (Medicare PUF vs Medicaid) ===")
    print(f"  Caveat: Medicare PUF coverage years may differ from Medicaid (2018-2024).")
    print(f"          Cross-program comparison restricted to overlapping years.")
    print()

    # Aggregate Medicaid by (hcpcs, year)
    medicaid_rows = con.execute(f"""
        SELECT HCPCS_CODE, SUBSTR(CLAIM_FROM_MONTH, 1, 4) AS year,
               SUM(TOTAL_CLAIM_LINES) AS mcd_claims,
               ROUND(SUM(TOTAL_PAID), 0) AS mcd_paid
        FROM {table}
        WHERE {NPI_FILTER} AND BILLING_PROVIDER_NPI_NUM = '{npi}'
        GROUP BY HCPCS_CODE, year
    """).fetchall()

    if not medicaid_rows:
        print(f"  No Medicaid billing for NPI {npi}. Cannot perform cross-program audit.")
        return

    # Build dict (hcpcs, year) → (mcd_claims, mcd_paid)
    mcd = {(c, y): (cl, p) for c, y, cl, p in medicaid_rows}

    # Try to pull Medicare PUF for each year
    medicare = {}
    pq_paths = []
    for yr in sorted({y for _, y in mcd.keys()}):
        p = r2.DataPath(REPO_ROOT / f"data/panels/cms_partb_svc_{yr}.parquet")
        if not p.exists():
            continue
        pq_paths.append((yr, p))

    if not pq_paths:
        print(f"  Medicare PUF files not found under data/panels/cms_partb_svc_*.parquet")
        print(f"  Skipping cross-program comparison.")
        return

    # Medicare PUF: NPI is BIGINT, HCPCS is HCPCS_Cd, services is Tot_Srvcs,
    # paid is Tot_Bene_Day_Srvcs × Avg_Mdcr_Pymt_Amt (no direct total paid col)
    npi_int = int(npi)
    for yr, p in pq_paths:
        try:
            rows = r2.duckdb_r2().execute(f"""
                SELECT HCPCS_Cd,
                       SUM(Tot_Srvcs) AS mc_claims,
                       ROUND(SUM(Tot_Bene_Day_Srvcs * Avg_Mdcr_Pymt_Amt), 0) AS mc_paid
                FROM read_parquet('{p}')
                WHERE Rndrng_NPI = {npi_int}
                GROUP BY HCPCS_Cd
            """).fetchall()
            for code, claims, paid in rows:
                medicare[(code, str(yr))] = (claims or 0, paid or 0)
        except Exception as e:
            print(f"  Medicare PUF read failed for {yr}: {e}")

    # Merge + display
    all_keys = sorted(set(mcd.keys()) | set(medicare.keys()))
    if not all_keys:
        print(f"  Nothing to compare.")
        return

    print(f"  {'hcpcs':<8} {'year':<6} {'medicare_claims':>16} {'medicare_paid':>14} "
          f"{'medicaid_claims':>16} {'medicaid_paid':>14} {'pattern':<14}")
    print(f"  {'-'*8} {'-'*6} {'-'*16} {'-'*14} {'-'*16} {'-'*14} {'-'*14}")
    for code, year in all_keys:
        mc_cl, mc_pd = medicare.get((code, year), (0, 0))
        md_cl, md_pd = mcd.get((code, year), (0, 0))
        if mc_cl and md_cl:
            pattern = "BOTH"
        elif mc_cl and not md_cl:
            pattern = "MEDICARE-ONLY"
        elif md_cl and not mc_cl:
            pattern = "MEDICAID-ONLY"
        else:
            continue
        print(f"  {code:<8} {year:<6} {mc_cl:>16,} ${mc_pd:>13,.0f} "
              f"{md_cl:>16,} ${md_pd:>13,.0f} {pattern:<14}")

    print()
    print(f"  Interpretation:")
    print(f"    BOTH           → scheme is systemic across payers (strong scienter)")
    print(f"    MEDICARE-ONLY  → selective-payer scheme (knowing payer-specific gaming)")
    print(f"    MEDICAID-ONLY  → dual-eligible or Medicaid-niche pattern")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--npi", required=True, help="Billing NPI to look up")
    ap.add_argument("--year", type=int, default=None,
                    help="Single year to drill into (else: all-year rollup)")
    ap.add_argument("--hcpcs", default=None,
                    help="Comma-separated HCPCS codes to filter to (else: top-15)")
    ap.add_argument("--compare-medicare", action="store_true",
                    help="Pull Medicare PUF side-by-side for cross-program audit")
    args = ap.parse_args(argv)

    npi = args.npi.strip()
    if not (len(npi) == 10 and npi.isdigit() and npi[0] in "12"):
        print(f"ERROR: --npi must be a 10-digit NPI starting with 1 or 2; got {npi!r}",
              file=sys.stderr)
        return 2

    try:
        con, table = _connect()
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        print(MEDICARE_FALLBACK_MSG, file=sys.stderr)
        return 2
    print(f"=== Medicaid Provider Spending — NPI {npi} ===")
    print(f"  Source: {DUCKDB_PATH if DUCKDB_PATH.exists() else PARQUET_PATH}")
    print(f"  Coverage: 2018-2024 FFS + Managed Care + CHIP, outpatient/professional only")
    print()
    print("  === Annual rollup ===")
    _print_annual_rollup(con, table, npi)

    if args.hcpcs:
        codes = [c.strip().upper() for c in args.hcpcs.split(",") if c.strip()]
        _print_hcpcs_filter(con, table, npi, codes)
    else:
        _print_top_hcpcs(con, table, npi, args.year)

    if args.year is not None:
        _print_monthly_trajectory(con, table, npi, args.year)

    if args.compare_medicare:
        _compare_medicare(con, table, npi)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
