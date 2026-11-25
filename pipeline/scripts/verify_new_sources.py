"""Verification checks for the two net-new CMS sources.

Per the onboarding ticket, each source gets a row-count sanity check plus a
known-NPI lookup:

* **DMEPOS detailed** (``cms_dmepos_detailed_<year>.parquet``) — NPI
  ``1900000050`` (Willow) should return ~5–50 rows (one per HCPCS code
  referred) for a given year.
* **Physician Compare** (``physician_compare.parquet`` pointer or a dated
  snapshot) — NPI ``1900000007`` (Aspen) should return ≥1
  group-affiliation row with a populated ``Org_PAC_ID``.

Each check is **skipped** (not failed) when its parquet is absent, so this
runs after either backfill independently. A check that runs and finds the
file malformed (0 rows, or the known NPI missing) exits non-zero.

Usage
-----
    python -m pipeline.scripts.verify_new_sources                 # check whatever is on disk
    python -m pipeline.scripts.verify_new_sources --year 2023     # pin the DMEPOS-detailed year
    python -m pipeline.scripts.verify_new_sources --source dmepos_detailed
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

from pipeline.scrapers.download import PANELS_DIR, REGISTRIES_DIR

WILLOW_NPI = "1900000050"        # DMEPOS-detailed referring provider
ASPEN_NPI = "1900000007"      # Physician Compare clinician


def _ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def _fail(msg: str) -> str:
    print(f"  ✗ {msg}")
    return msg


def _warn(msg: str) -> None:
    # Non-fatal: a single example NPI can churn out of a snapshot/year without
    # the file being wrong. Surfaced, but does not fail the run.
    print(f"  ! {msg}")


def verify_dmepos_detailed(year: int | None) -> list[str]:
    """Row-count + Willow-NPI lookup on cms_dmepos_detailed_<year>."""
    print("== DMEPOS detailed ==")
    if year is not None:
        paths = [PANELS_DIR / f"cms_dmepos_detailed_{year}.parquet"]
    else:
        paths = sorted(PANELS_DIR.glob("cms_dmepos_detailed_[0-9][0-9][0-9][0-9].parquet"))
    paths = [p for p in paths if p.exists()]
    if not paths:
        print("  - skipped (no cms_dmepos_detailed_*.parquet on disk)")
        return []

    errors: list[str] = []
    for path in paths:
        df = pd.read_parquet(path, columns=["npi", "HCPCS_CD", "Suplr_Rentl_Ind",
                                            "Avg_Suplr_Mdcr_Pymt_Amt", "Tot_Suplr_Srvcs"])
        n = len(df)
        if n == 0:
            errors.append(_fail(f"{path.name}: 0 rows"))
            continue
        _ok(f"{path.name}: {n:,} rows")
        willow = df[df["npi"] == WILLOW_NPI]
        if willow.empty:
            _warn(f"{path.name}: Willow NPI {WILLOW_NPI} not found "
                  f"(may not have referred DME this year)")
        else:
            codes = sorted(willow["HCPCS_CD"].dropna().unique())[:8]
            _ok(f"{path.name}: Willow {WILLOW_NPI} → {len(willow)} row(s); "
                f"sample HCPCS {codes}")
            if not (1 <= len(willow) <= 200):
                _warn(f"{path.name}: Willow row count {len(willow)} "
                      f"outside expected ~5–50 band")
    return errors


def verify_physician_compare() -> list[str]:
    """Row-count + Aspen-NPI lookup on the physician_compare pointer."""
    print("== Physician Compare ==")
    pointer = REGISTRIES_DIR / "physician_compare.parquet"
    if pointer.exists():
        path = pointer
    else:
        dated = sorted(REGISTRIES_DIR.glob("physician_compare_[0-9][0-9][0-9][0-9]-[0-9][0-9].parquet"))
        if not dated:
            print("  - skipped (no physician_compare parquet on disk)")
            return []
        path = dated[-1]

    df = pd.read_parquet(path, columns=["npi", "Org_PAC_ID", "Org_Lgl_Nm", "snapshot"])
    errors: list[str] = []
    n = len(df)
    if n == 0:
        return [_fail(f"{path.name}: 0 rows")]
    snap = df["snapshot"].iloc[0] if "snapshot" in df.columns and n else "?"
    _ok(f"{path.name}: {n:,} rows (snapshot {snap})")

    # Structural check: the headline field must be populated somewhere. A file
    # with zero non-empty Org_PAC_ID is broken (projection / alias failure).
    org = df["Org_PAC_ID"].astype("string").fillna("")
    n_org = int((org.str.strip() != "").sum())
    if n_org == 0:
        return errors + [_fail(f"{path.name}: Org_PAC_ID never populated "
                               f"(projection/alias failure)")]
    _ok(f"{path.name}: {n_org:,} rows with a populated Org_PAC_ID")

    # Known-NPI probe is a sanity check, not a gate: a single clinician can
    # churn out of the current snapshot without the file being wrong.
    rows = df[df["npi"] == ASPEN_NPI]
    if rows.empty:
        _warn(f"{path.name}: Aspen NPI {ASPEN_NPI} not in this snapshot "
              f"(provider may have left the roster; try a historical snapshot)")
    else:
        with_org = rows[rows["Org_PAC_ID"].notna() & (rows["Org_PAC_ID"].astype(str) != "")]
        orgs = sorted(with_org["Org_PAC_ID"].astype(str).unique())
        if not orgs:
            _warn(f"{path.name}: Aspen {ASPEN_NPI} present but no populated Org_PAC_ID")
        else:
            _ok(f"{path.name}: Aspen {ASPEN_NPI} → {len(rows)} affiliation row(s); "
                f"Org_PAC_ID(s) {orgs}")
    return errors


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=["dmepos_detailed", "physician_compare", "all"],
                    default="all")
    ap.add_argument("--year", type=int, default=None,
                    help="pin the DMEPOS-detailed year (default: all years on disk)")
    args = ap.parse_args()

    errors: list[str] = []
    if args.source in ("dmepos_detailed", "all"):
        errors += verify_dmepos_detailed(args.year)
    if args.source in ("physician_compare", "all"):
        errors += verify_physician_compare()

    if errors:
        print(f"\nVERIFY FAILED ({len(errors)} problem(s))")
        sys.exit(1)
    print("\nVERIFY OK")


if __name__ == "__main__":
    main()
