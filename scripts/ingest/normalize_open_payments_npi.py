"""One-shot repair: add a canonical zero-padded ``npi`` to Open Payments parquets.

The local ``data/processed/open_payments_general_<year>.parquet`` files predate
``OpenPaymentsScraper``'s canonical-``npi`` emission. Their raw
``Covered_Recipient_NPI`` dtype drifts across years — VARCHAR (e.g. "1900000075")
in 2013-2017/2024, DOUBLE (e.g. 1900000075.0) in 2018-2023 — so an exact-string
join like ``CAST(Covered_Recipient_NPI AS VARCHAR)='1900000075'`` silently
returns ZERO rows for the DOUBLE years even when the provider has payments,
producing false negatives.

The durable fix lives in the producer (``pipeline/scrapers/open_payments.py``,
which already derives a zero-padded ``npi``); this script brings the *existing
local parquets* up to that contract without re-downloading ~12x~1GB from CMS.
Each file is rewritten with:

  * ``Covered_Recipient_NPI`` normalized to a clean integer VARCHAR (no ".0"),
  * a new canonical ``npi`` column — zero-padded 10-char VARCHAR, NULL where the
    recipient has no NPI (teaching-hospital rows; these are preserved, not
    dropped, so row counts are unchanged).

Writes each output to a ``.tmp.parquet`` sibling, validates the row count, then
atomically renames over the original — the original is never left half-written.
Idempotent: re-running re-normalizes already-clean files to the same result.

Run from the repo root (where ``data/processed/`` lives):

    python3 scripts/ingest/normalize_open_payments_npi.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import duckdb

DATA_DIR = Path("data/processed")
YEARS = range(2013, 2025)

# Verification: a known provider (Provider CedarI 1900000075) must resolve via
# the canonical column in every year of the span the dtype bug spanned.
CHECK_NPI = "1900000075"
CHECK_YEARS = range(2016, 2024)  # 2016-2023 inclusive

# Integer NPI recovered from any input dtype. The inner TRY_CAST(... AS DOUBLE)
# absorbs both VARCHAR ("1900000075") and DOUBLE (1900000075.0); 10-digit NPIs
# are exactly representable as DOUBLE. TRY_CAST maps empty/garbage to NULL
# instead of raising. NPIs are always 10 digits, so lpad is a no-op for real
# NPIs and a guard against any malformed short value.
_NPI_BIGINT = "TRY_CAST(TRY_CAST(Covered_Recipient_NPI AS DOUBLE) AS BIGINT)"


def _rederive(path: Path) -> int:
    """Rewrite one parquet with a clean ``Covered_Recipient_NPI`` + canonical
    ``npi``. Returns the row count. Atomic via .tmp + os.replace."""
    tmp = path.parent / f"{path.stem}.tmp.parquet"
    n_in = duckdb.sql(f"SELECT COUNT(*) FROM read_parquet('{path}')").fetchone()[0]
    duckdb.sql(
        f"""
        COPY (
            SELECT * REPLACE (CAST({_NPI_BIGINT} AS VARCHAR) AS Covered_Recipient_NPI),
                   lpad(CAST({_NPI_BIGINT} AS VARCHAR), 10, '0') AS npi
            FROM read_parquet('{path}')
        ) TO '{tmp}' (FORMAT PARQUET)
        """
    )
    n_out = duckdb.sql(f"SELECT COUNT(*) FROM read_parquet('{tmp}')").fetchone()[0]
    if n_out != n_in:
        tmp.unlink(missing_ok=True)
        raise SystemExit(
            f"row count changed for {path.name}: {n_in:,} -> {n_out:,}; aborting "
            f"(original left untouched)"
        )
    os.replace(tmp, path)  # atomic on the same filesystem
    return n_out


def _check() -> bool:
    """Assert CHECK_NPI resolves via the canonical ``npi`` column every year."""
    print(
        f"\ncheck: npi = {CHECK_NPI} across "
        f"{CHECK_YEARS.start}-{CHECK_YEARS.stop - 1} via canonical `npi`"
    )
    ok = True
    for year in CHECK_YEARS:
        path = DATA_DIR / f"open_payments_general_{year}.parquet"
        if not path.exists():
            print(f"  {year}: MISSING FILE")
            ok = False
            continue
        n = duckdb.sql(
            f"SELECT COUNT(*) FROM read_parquet('{path}') WHERE npi = '{CHECK_NPI}'"
        ).fetchone()[0]
        if n == 0:
            ok = False
        print(f"  {year}: {n:>4} rows  {'ok' if n else 'FAIL (0 rows)'}")
    return ok


def main() -> int:
    files = [(y, DATA_DIR / f"open_payments_general_{y}.parquet") for y in YEARS]
    missing = [y for y, p in files if not p.exists()]
    if missing:
        print(f"  (skipping absent years: {missing})", file=sys.stderr)

    for year, path in files:
        if not path.exists():
            continue
        n = _rederive(path)
        print(f"  {year}: rewrote {n:,} rows (+ canonical npi)", flush=True)

    if not _check():
        print("CHECK FAILED — canonical npi join is still broken", file=sys.stderr)
        return 1
    print("CHECK PASSED: target NPI resolves in every year via canonical `npi`.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
