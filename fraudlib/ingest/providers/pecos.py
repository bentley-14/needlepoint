"""PECOS Ordering & Referring file ingest + feature-table builder.

CMS publishes the Medicare Provider and Supplier Enrollment (PECOS)
Ordering and Referring file quarterly. It's a small, flat CSV (~50 MB,
~1.5M rows) listing every NPI currently authorized to *order* or
*refer* Medicare items — durable medical equipment, home health,
imaging, lab, Part B drugs, Part B services.

Why we care: a sudden activation of ordering/referring privilege — or
an NPI that appears to order aggressively within the first year of
enrollment — is a classic straw-NPI signal. The PECOS file is the
canonical source for those timestamps.

Landing page:
    https://data.cms.gov/provider-characteristics/medicare-provider-supplier-enrollment/order-and-referring

The direct CSV URL rotates each quarter; we HEAD-probe a curated
candidate list, then fall back to whatever the user passes via
``local_csv=``.

Downstream: :func:`load_pecos_features` returns an NPI-indexed frame
LEFT-JOINable onto the Part B panel. ``query_tools.open_partb`` calls
it when ``with_pecos=True``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import pandas as pd
import requests

PROCESSED = Path("data/registries")
RAW = Path("data/raw") / "pecos"

# Curated candidate URLs for the current quarterly file. CMS reposts on
# the same URL family each quarter but the specific dataset-id hash
# changes; we probe in order and use the first HEAD that returns 200.
# Update by visiting the landing page above — the "Download CSV" link
# target on that page is always the current file.
_BULK_URL_CANDIDATES = [
    "https://data.cms.gov/sites/default/files/2026-04/Order_and_Referring.csv",
    "https://data.cms.gov/sites/default/files/2026-01/Order_and_Referring.csv",
    "https://data.cms.gov/sites/default/files/2025-10/Order_and_Referring.csv",
    "https://data.cms.gov/sites/default/files/2025-07/Order_and_Referring.csv",
]

# The CSV ships with these columns (plus a couple of optional legacy
# fields that come and go between quarterly releases — we ignore them).
_CORE_COLS = [
    "NPI",
    "LAST NAME",
    "FIRST NAME",
    "PARTB",
    "DME",
    "HHA",
    "PMD",
    "HOSPICE",
]

# Canonical snake_case output columns.
_RENAME = {
    "NPI": "NPI",
    "LAST NAME": "pecos_last_name",
    "FIRST NAME": "pecos_first_name",
    "PARTB": "pecos_partb_eligible",
    "DME": "pecos_dme_eligible",
    "HHA": "pecos_hha_eligible",
    "PMD": "pecos_pmd_eligible",
    "HOSPICE": "pecos_hospice_eligible",
}


def _probe_candidate(url: str) -> bool:
    try:
        r = requests.head(url, timeout=15, allow_redirects=True)
        return r.status_code == 200
    except Exception:
        return False


def _parse_yn(series: pd.Series) -> pd.Series:
    """PECOS uses 'Y'/'N' (occasionally blank) for privilege flags."""
    return series.astype(str).str.strip().str.upper().eq("Y")


def download_pecos_ordering_referring(
    out_path: Optional[Path] = None,
    url: Optional[str] = None,
) -> Path:
    """Fetch the current PECOS Ordering & Referring CSV and write a
    projected parquet to ``data/registries/pecos_ordering_referring.parquet``.

    Passing ``url=`` overrides the candidate probe. Passing ``out_path=``
    overrides the default destination (useful for tests).
    """
    out_path = out_path or (PROCESSED / "pecos_ordering_referring.parquet")
    if out_path.exists():
        return out_path

    if url is None:
        for candidate in _BULK_URL_CANDIDATES:
            if _probe_candidate(candidate):
                url = candidate
                break
        else:
            raise RuntimeError(
                "No PECOS URL candidate responded to HEAD. Visit "
                "https://data.cms.gov/provider-characteristics/medicare-"
                "provider-supplier-enrollment/order-and-referring and "
                "pass the current CSV URL via url= (or download it "
                "manually and pass local_csv= to fetch_pecos_ordering_referring)."
            )

    RAW.mkdir(parents=True, exist_ok=True)
    csv_path = RAW / "order_and_referring.csv"
    with requests.get(url, stream=True, timeout=600) as r:
        r.raise_for_status()
        with csv_path.open("wb") as f:
            for chunk in r.iter_content(1 << 20):
                if chunk:
                    f.write(chunk)

    return _csv_to_parquet(csv_path, out_path)


def _csv_to_parquet(csv_path: Path, out_path: Path) -> Path:
    """Project the raw CSV to the curated schema and write parquet."""
    df = pd.read_csv(csv_path, dtype=str, low_memory=False).fillna("")
    # Some PECOS quarters capitalize differently — normalize to the
    # canonical names we expect.
    df.columns = [c.strip().upper() for c in df.columns]
    keep = [c for c in _CORE_COLS if c in df.columns]
    df = df[keep].rename(columns=_RENAME)

    # Privilege flags → bool.
    for col in (
        "pecos_partb_eligible",
        "pecos_dme_eligible",
        "pecos_hha_eligible",
        "pecos_pmd_eligible",
        "pecos_hospice_eligible",
    ):
        if col in df.columns:
            df[col] = _parse_yn(df[col])

    # NPI zero-pad invariant (10 chars).
    df["NPI"] = df["NPI"].astype(str).str.strip().str.zfill(10)
    df = df[df["NPI"].str.len() == 10]
    df = df.drop_duplicates(subset=["NPI"], keep="first")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out_path, index=False)
    return out_path


def load_pecos_features(
    parquet_path: Optional[Path] = None,
) -> pd.DataFrame:
    """Return NPI-indexed PECOS features ready to LEFT-JOIN on Part B.

    Output columns:
      * ``NPI`` — 10-char zero-padded string.
      * ``pecos_has_ordering_privilege`` — bool. True if the NPI is
        eligible to order at least one of DME / HHA / PMD / hospice.
      * ``pecos_has_referring_privilege`` — bool. True if PARTB
        referral privilege is set. (CMS folds ordering and referring
        into the same file but splits by item type; PartB represents
        the referral surface.)
      * ``pecos_n_privileges`` — int 0–5. Sum of the individual flags.
      * ``pecos_partb_eligible`` / ``pecos_dme_eligible`` /
        ``pecos_hha_eligible`` / ``pecos_pmd_eligible`` /
        ``pecos_hospice_eligible`` — booleans passed through.
    """
    parquet_path = parquet_path or (PROCESSED / "pecos_ordering_referring.parquet")
    if not parquet_path.exists():
        raise FileNotFoundError(
            f"{parquet_path} — run download_pecos_ordering_referring() first"
        )

    df = pd.read_parquet(parquet_path)

    flag_cols = [
        "pecos_partb_eligible",
        "pecos_dme_eligible",
        "pecos_hha_eligible",
        "pecos_pmd_eligible",
        "pecos_hospice_eligible",
    ]
    present = [c for c in flag_cols if c in df.columns]
    for c in flag_cols:
        if c not in df.columns:
            df[c] = False

    order_cols = [c for c in ("pecos_dme_eligible", "pecos_hha_eligible",
                              "pecos_pmd_eligible", "pecos_hospice_eligible")
                  if c in present or c in df.columns]
    df["pecos_has_ordering_privilege"] = df[order_cols].any(axis=1) if order_cols else False
    df["pecos_has_referring_privilege"] = df["pecos_partb_eligible"].fillna(False)
    df["pecos_n_privileges"] = df[flag_cols].sum(axis=1).astype(int)

    return df[[
        "NPI",
        "pecos_has_ordering_privilege",
        "pecos_has_referring_privilege",
        "pecos_n_privileges",
        "pecos_partb_eligible",
        "pecos_dme_eligible",
        "pecos_hha_eligible",
        "pecos_pmd_eligible",
        "pecos_hospice_eligible",
    ]]
