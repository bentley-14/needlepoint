"""Build the affirmative CLIA-certification parquet from the CMS Provider of
Services File — Clinical Laboratories.

This is the FULL population of CLIA-certified labs (CLIA number, facility name,
address, certification date, termination status), so a count that turns on
"billed lab tests with NO CLIA certificate" can be CONFIRMED against the
registry instead of inferred from NPPES (see scripts/clia_lookup.py --certified).

Source: CMS Provider of Services File - Clinical Laboratories (data.cms.gov),
updated ~quarterly. POS files key on the CLIA number + facility address, NOT NPI.

Usage:
    python scripts/ingest/parse_clia_pos.py [--csv <path>] [--url <download-url>]

Then (for teammates / the calibration jail) upload to R2:
    rclone copyto data/processed/clia_certified.parquet \\
        r2:needlepoint-data/processed/v1/clia_certified.parquet
"""
from __future__ import annotations

import argparse
import sys
import tempfile
import urllib.request
from pathlib import Path

import duckdb

REPO_ROOT = Path(__file__).resolve().parents[2]
OUT_PATH = REPO_ROOT / "data" / "processed" / "clia_certified.parquet"
# Latest known CMS POS Clinical Laboratories CSV (update the year path when CMS
# publishes a newer quarter; the dataset landing page links the current file).
DEFAULT_URL = ("https://data.cms.gov/sites/default/files/2024-01/"
               "4650a277-adeb-439e-b4c9-f0f4f6c0f5e9/clia_data_yr23.csv")


def build(csv_path: str) -> int:
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute(f"""
        COPY (
          SELECT
            PRVDR_NUM            AS clia_num,
            FAC_NAME             AS fac_name,
            ST_ADR               AS street,
            CITY_NAME            AS city,
            STATE_CD             AS state,
            CAST(ZIP_CD AS VARCHAR) AS zip,
            PRVDR_CTGRY_SBTYP_CD AS cert_subtype_cd,
            CRTFCTN_DT           AS certification_date,
            CRTFCTN_ACTN_TYPE_CD AS cert_action_cd,
            PGM_TRMNTN_CD        AS termination_cd,
            TRMNTN_EXPRTN_DT     AS termination_date,
            CMPLNC_STUS_CD       AS compliance_status_cd
          FROM read_csv_auto('{csv_path}', header=true, all_varchar=true)
        ) TO '{OUT_PATH}' (FORMAT parquet);
    """)
    n = con.execute(f"SELECT count(*) FROM read_parquet('{OUT_PATH}')").fetchone()[0]
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--csv", help="Local POS Clinical Labs CSV (skips download)")
    ap.add_argument("--url", default=DEFAULT_URL, help="CMS CSV download URL")
    args = ap.parse_args()

    csv_path = args.csv
    if not csv_path:
        tmp = Path(tempfile.gettempdir()) / "clia_pos.csv"
        print(f"downloading {args.url} → {tmp} …")
        urllib.request.urlretrieve(args.url, tmp)
        csv_path = str(tmp)

    n = build(csv_path)
    print(f"✓ wrote {OUT_PATH} ({n} rows, {OUT_PATH.stat().st_size // 1024} KB)")
    print("  next: rclone copyto data/processed/clia_certified.parquet "
          "r2:needlepoint-data/processed/v1/clia_certified.parquet")
    return 0


if __name__ == "__main__":
    sys.exit(main())
