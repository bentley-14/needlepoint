"""Open Payments — General Payments scraper.

Wraps the existing :mod:`fraudlib.ingest.providers.openpayments` bulk
downloader in the :class:`BaseScraper` contract so the produced parquet
carries the canonical stamp columns (``source``, ``fetched_at_utc``,
``schema_version``), the canonical NPI columns from
:class:`ProviderRecordSchema` (``npi``, ``provider_name``, ``state``,
``data_year``), and a sibling ``.manifest.json``.

This covers the **General Payments** cut only — payments and transfers
of value from pharmaceutical manufacturers and group purchasing
organizations to physicians and teaching hospitals. The two sibling
cuts — Research Payments and Ownership/Investment — ship as separate
scrapers in later PRs.

Years covered: 2013-2024 (12 program years). PY2013 covers Aug-Dec 2013
only (partial year, the first post-Sunshine-Act release); features that
normalize on payment counts must scale it up — see ``data/docs/open_payments.md``
§6.

URL-catalog shape (deviation from the per-source ``URLS: dict[int, str]``
convention)
==========================================================================
The convention introduced in PR #92 is one URL per year, joined to a
shared ``CMS_FILES_BASE`` prefix via :func:`pipeline.scrapers.download.cms_url`.
**OpenPayments breaks that convention on both axes**:

  1. Different host. ``download.cms.gov/openpayments/`` is not
     ``data.cms.gov/sites/default/files/``; ``cms_url()`` doesn't apply.
  2. Filename rotates with each CMS refresh. The same program year
     re-publishes 1-2x annually under a new ``P<MMDDYY>`` date stamp
     (e.g. PY2023 lives at both ``PGYR23_P063024.zip`` and
     ``PGYR23_P013025.zip``). The June refresh adds dispute resolutions
     to the January cut; both are valid. Older PYs sit behind a
     per-publish sub-directory layout introduced in 2023
     (``PGYR<YY>_P<MMDDYY>/OP_DTL_PGYR<YYYY>_P<MMDDYY>.zip``).

We chose to **keep the list-of-candidates pattern** as ``URLS: dict[int,
list[str]]`` on this module, ordered most-likely-current first. The
fetcher probes them in order and picks the first that returns 200. This
deviates from the per-source convention; the deviation is paid for by
not having to chase CMS URL churn in the URL-resolver layer of the
``audit_url_freshness.py`` registry on every refresh — the registry
checks all candidates and only escalates when *every* one 404s.

The alternative — pin one canonical URL per year and let the audit
script flag staleness when CMS rotates — was rejected because the
rotation cadence (twice yearly) is fast enough that pinning would
require URL maintenance on the order of weeks rather than the months
the audit script's run frequency assumes.
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path
from typing import ClassVar, Optional, Type

import pandas as pd
import pandera.pandas as pa
import requests
from pandera.typing import Series

from pipeline.base import (
    BaseScraper,
    BaseScraperSchema,
    ProviderRecordSchema,
    RunResult,
    format_provider_name,
)
from pipeline.scrapers.download import (
    PANELS_DIR,
    RAW_DIR,
    _download,
)


# ---------------------------------------------------------------------------
# URL catalog
# ---------------------------------------------------------------------------
#
# Ordered most-likely-current first. CMS rotates filenames every refresh
# cycle (~twice a year); the fetcher probes them in order. When all
# candidates 404 for a year, re-resolve from
# https://www.cms.gov/OpenPayments/Data/Dataset-Downloads and prepend
# the new filename to the candidate list.
URLS: dict[int, list[str]] = {
    # PY2024 lands ~June 2025; the Jan 2026 refresh adds dispute fixes.
    2024: [
        "https://download.cms.gov/openpayments/PGYR24_P063025.zip",
        "https://download.cms.gov/openpayments/PGYR24_P013026.zip",
    ],
    2023: [
        "https://download.cms.gov/openpayments/PGYR23_P013025.zip",
        "https://download.cms.gov/openpayments/PGYR23_P063024.zip",
    ],
    2022: [
        "https://download.cms.gov/openpayments/PGYR22_P013024.zip",
        "https://download.cms.gov/openpayments/PGYR22_P063023.zip",
    ],
    2021: [
        "https://download.cms.gov/openpayments/PGYR21_P013023.zip",
        "https://download.cms.gov/openpayments/PGYR21_P063022.zip",
    ],
    2020: [
        "https://download.cms.gov/openpayments/PGYR20_P013022.zip",
        "https://download.cms.gov/openpayments/PGYR20_P063021.zip",
    ],
    2019: [
        "https://download.cms.gov/openpayments/PGYR19_P013021.zip",
        "https://download.cms.gov/openpayments/PGYR19_P063020.zip",
    ],
    2018: [
        "https://download.cms.gov/openpayments/PGYR18_P013020.zip",
        "https://download.cms.gov/openpayments/PGYR18_P063019.zip",
    ],
    # PY2013-PY2017 are archived; in mid-2023 CMS shifted to a per-publish
    # sub-directory layout. Probe newest archive first.
    2017: [
        "https://download.cms.gov/openpayments/PGYR2017_P01172025/OP_DTL_PGYR2017_P01172025.zip",
        "https://download.cms.gov/openpayments/PGYR17_P01172025.zip",
        "https://download.cms.gov/openpayments/PGYR17_P011824/OP_DTL_PGYR2017_P01182024.zip",
        "https://download.cms.gov/openpayments/PGYR17_P063023/OP_DTL_PGYR2017_P06302023.zip",
        "https://download.cms.gov/openpayments/PGYR17_P012019.zip",
    ],
    2016: [
        "https://download.cms.gov/openpayments/PGYR2016_P01172025/OP_DTL_PGYR2016_P01172025.zip",
        "https://download.cms.gov/openpayments/PGYR16_P01172025.zip",
        "https://download.cms.gov/openpayments/PGYR16_P011824/OP_DTL_PGYR2016_P01182024.zip",
        "https://download.cms.gov/openpayments/PGYR16_P063023/OP_DTL_PGYR2016_P06302023.zip",
        "https://download.cms.gov/openpayments/PGYR16_P012018.zip",
    ],
    2015: [
        "https://download.cms.gov/openpayments/PGYR2015_P01172025/OP_DTL_PGYR2015_P01172025.zip",
        "https://download.cms.gov/openpayments/PGYR15_P01172025.zip",
        "https://download.cms.gov/openpayments/PGYR15_P011824/OP_DTL_PGYR2015_P01182024.zip",
        "https://download.cms.gov/openpayments/PGYR15_P063023/OP_DTL_PGYR2015_P06302023.zip",
        "https://download.cms.gov/openpayments/PGYR15_P063016.zip",
    ],
    2014: [
        "https://download.cms.gov/openpayments/PGYR2014_P01172025/OP_DTL_PGYR2014_P01172025.zip",
        "https://download.cms.gov/openpayments/PGYR14_P01172025.zip",
        "https://download.cms.gov/openpayments/PGYR14_P011824/OP_DTL_PGYR2014_P01182024.zip",
        "https://download.cms.gov/openpayments/PGYR14_P063023/OP_DTL_PGYR2014_P06302023.zip",
        "https://download.cms.gov/openpayments/PGYR14_P063015.zip",
    ],
    2013: [
        # PY2013 = Aug-Dec only (partial program year; ~4.4M rows vs.
        # ~10M+ for full years). Features that normalize on payment
        # counts need to scale up — see open_payments.md §6.
        "https://download.cms.gov/openpayments/PGYR2013_P01172025/OP_DTL_PGYR2013_P01172025.zip",
        "https://download.cms.gov/openpayments/PGYR13_P01172025.zip",
        "https://download.cms.gov/openpayments/PGYR13_P011824/OP_DTL_PGYR2013_P01182024.zip",
        "https://download.cms.gov/openpayments/PGYR13_P063023/OP_DTL_PGYR2013_P06302023.zip",
        "https://download.cms.gov/openpayments/PGYR13_P093014.zip",
    ],
}


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
#
# Projected schema: 11 raw CMS columns kept verbatim from the General
# Payments Detail CSV header, plus the 7 canonicals from
# ProviderRecordSchema (3 base stamps + 4 NPI keys). The CMS header is
# stable across all 12 program years for these 11 columns; the 2021
# recipient-type expansion (PA/NP/CRNA/CNS/CNM disclosure) did not
# rename or remove any of them.
#
# CMS publishes ~80 columns per row in the raw zip; we keep the subset
# needed for the per-NPI fraud-feature aggregator. Aggregation lives in
# ``fraudlib/ranker/openpayments_features.py``; this scraper writes the
# thin projection that aggregator reads.


class OpenPaymentsSchema(ProviderRecordSchema):
    """Schema for ``open_payments_general_<year>.parquet`` (multi-row per NPI).

    Unit of observation: **one row per disclosed payment**. A single NPI
    typically appears on hundreds to thousands of rows per program year
    (one row per meal, consulting check, speaking engagement, etc.).

    Three column groups:

    * Record + recipient identifiers (``Record_ID``, ``Covered_Recipient_*``,
      ``Teaching_Hospital_Name``, ``Recipient_State``) — 9 cols
    * Payer + payment (``Applicable_Manufacturer_or_Applicable_GPO_*``,
      ``Total_Amount_of_Payment_USDollars``, ``Date_of_Payment``,
      ``Nature_of_Payment_or_Transfer_of_Value``) — 4 cols
    * Product + program year (``Name_of_Drug_or_Biological_or_Device_or_Medical_Supply_1``,
      ``Program_Year``) — 2 cols

    Dtype contract (the reason this migration exists)
    -------------------------------------------------
    ``Covered_Recipient_NPI`` is the raw CMS NPI. On R2 it drifted to
    ``DOUBLE`` for PY2018–2023 (an exact string match silently missed
    every row). It is declared ``str`` with the same ``^(\\d{10})?$``
    check as the canonical ``npi`` so a float that reaches it coerces to
    ``"1900000075.0"`` and FAILS validation loudly — forcing the parser
    to clean it instead of shipping garbage. The fresh scraper reads the
    CSV with ``dtype=str`` so it produces a clean string already; the
    check guards re-backfills of the old DOUBLE parquets.

    ``Total_Amount_of_Payment_USDollars`` is ``float`` with ``ge=0`` (a
    payment can't be negative). It too drifted (VARCHAR pre-2018, DOUBLE
    after); coercion now normalizes it to ``float`` on every year.
    """

    # --- Record + recipient identifiers (raw CMS) ---
    Record_ID: Series[str] = pa.Field(nullable=True)
    Covered_Recipient_NPI: Series[str] = pa.Field(str_matches=r"^(\d{10})?$", nullable=True)
    Covered_Recipient_Type: Series[str] = pa.Field(nullable=True)
    Covered_Recipient_First_Name: Series[str] = pa.Field(nullable=True)
    Covered_Recipient_Middle_Name: Series[str] = pa.Field(nullable=True)
    Covered_Recipient_Last_Name: Series[str] = pa.Field(nullable=True)
    Teaching_Hospital_Name: Series[str] = pa.Field(nullable=True)
    Covered_Recipient_Specialty_1: Series[str] = pa.Field(nullable=True)
    Recipient_State: Series[str] = pa.Field(nullable=True)

    # --- Payer + payment ---
    Applicable_Manufacturer_or_Applicable_GPO_Making_Payment_Name: Series[str] = pa.Field(nullable=True)
    Total_Amount_of_Payment_USDollars: Series[float] = pa.Field(ge=0, nullable=True)
    Date_of_Payment: Series[str] = pa.Field(nullable=True)
    Nature_of_Payment_or_Transfer_of_Value: Series[str] = pa.Field(nullable=True)

    # --- Product + program year ---
    Name_of_Drug_or_Biological_or_Device_or_Medical_Supply_1: Series[str] = pa.Field(nullable=True)
    Program_Year: Series[str] = pa.Field(nullable=True)


AVAILABLE_YEARS = tuple(sorted(URLS))


# Raw CMS columns kept in the projection. Exposed at module scope so the
# backfill script can import them.
KEEP_COLUMNS = [
    "Record_ID",
    "Covered_Recipient_NPI",
    "Covered_Recipient_Type",
    "Covered_Recipient_First_Name",
    "Covered_Recipient_Middle_Name",
    "Covered_Recipient_Last_Name",
    "Teaching_Hospital_Name",
    "Covered_Recipient_Specialty_1",
    "Recipient_State",
    "Applicable_Manufacturer_or_Applicable_GPO_Making_Payment_Name",
    "Total_Amount_of_Payment_USDollars",
    "Date_of_Payment",
    "Nature_of_Payment_or_Transfer_of_Value",
    "Name_of_Drug_or_Biological_or_Device_or_Medical_Supply_1",
    "Program_Year",
]

# CMS uses ``Covered_Recipient_Type`` values like
# "Covered Recipient Physician" / "Covered Recipient Teaching Hospital" /
# "Covered Recipient Non-Physician Practitioner". Map to the I/O entity
# code that ``format_provider_name`` expects.
_TYPE_TO_ENT_CD = {
    "covered recipient physician": "I",
    "covered recipient non-physician practitioner": "I",
    "non-physician practitioner": "I",
    "physician": "I",
    "covered recipient teaching hospital": "O",
    "teaching hospital": "O",
}


def _recipient_entity_code(raw: Optional[str]) -> Optional[str]:
    """Map ``Covered_Recipient_Type`` to ``"I"`` / ``"O"`` or ``None``."""
    if not isinstance(raw, str):
        return None
    return _TYPE_TO_ENT_CD.get(raw.strip().lower())


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------


class OpenPaymentsScraper(BaseScraper):
    """Scrape one Open Payments General Payments year → parquet + manifest.

    Multi-row source: one row per disclosed payment, NOT one row per NPI.
    Aggregation to per-NPI features lives in
    ``fraudlib/ranker/openpayments_features.py``.
    """

    source: ClassVar[str] = "open_payments_general"
    schema: ClassVar[Type[BaseScraperSchema]] = OpenPaymentsSchema
    output_dir: ClassVar[Path] = PANELS_DIR
    schema_version: ClassVar[str] = "v1.0"

    # Compressed-zip ceiling. General Payments 2023 is ~1.1 GB compressed
    # / ~3 GB raw / ~500 MB projected parquet. 2.0 GB is generous against
    # a pathological refresh.
    max_zip_size_gb: ClassVar[float] = 2.0

    def fetch(self, *, year: int) -> RunResult:
        if year not in AVAILABLE_YEARS:
            raise ValueError(
                f"open_payments year {year} not in catalog "
                f"(have {AVAILABLE_YEARS[0]}-{AVAILABLE_YEARS[-1]})"
            )

        started = self._now()
        zip_url = self._resolve_url(year)
        zip_path = RAW_DIR / "open_payments" / Path(zip_url).name

        _download(zip_url, zip_path)
        raw = self._parse_zip(zip_path)

        df = self._project_and_canonicalize(raw, year=year)
        df = self._stamp_base_columns(df)
        # Future-proof: if a year is missing a declared column (e.g. CMS
        # retires one), NaN-pad rather than fail validation.
        df = self._align_to_schema(df)

        return self._write(
            df,
            name=f"open_payments_general_{year}",
            started_at_utc=started,
            inputs={"url": zip_url, "local_zip": str(zip_path)},
        )

    # ---- helpers ----

    def _resolve_url(self, year: int) -> str:
        """HEAD each candidate URL; return the first 200. Raise if none."""
        candidates = URLS[year]
        last_status: dict[str, str] = {}
        for url in candidates:
            try:
                r = requests.head(url, timeout=15, allow_redirects=True)
                if r.status_code == 200:
                    size_gb = int(r.headers.get("Content-Length", 0)) / (1024 ** 3)
                    if size_gb > self.max_zip_size_gb:
                        raise ValueError(
                            f"open_payments PY{year} at {url} is "
                            f"{size_gb:.2f} GB compressed (> {self.max_zip_size_gb} GB cap)"
                        )
                    return url
                last_status[url] = str(r.status_code)
            except requests.RequestException as exc:
                last_status[url] = type(exc).__name__
        raise RuntimeError(
            f"no open_payments zip reachable for PY{year}; "
            f"probed {len(candidates)} candidate(s): {last_status}. "
            f"Re-resolve from https://www.cms.gov/OpenPayments/Data/Dataset-Downloads "
            f"and prepend the new filename to URLS[{year}]."
        )

    @staticmethod
    def _parse_zip(zip_path: Path) -> pd.DataFrame:
        """Extract the General Payments detail CSV from the zip → projected DataFrame."""
        with zipfile.ZipFile(zip_path) as zf:
            general_csv = next(
                (n for n in zf.namelist()
                 if re.match(r"OP_DTL_GNRL_PGYR\d{4}_P\d{8}.*\.csv$", n, re.IGNORECASE)),
                None,
            )
            if not general_csv:
                raise RuntimeError(
                    f"General Payments CSV not found in {zip_path}; "
                    f"archive contents (first 10): {zf.namelist()[:10]}"
                )

            with zf.open(general_csv) as handle:
                header = pd.read_csv(handle, nrows=0, low_memory=False)
            present = [c for c in KEEP_COLUMNS if c in header.columns]
            missing = [c for c in KEEP_COLUMNS if c not in header.columns]
            if missing:
                # Surface schema drift via stderr; _align_to_schema will
                # NaN-pad these later, but the operator should know.
                print(
                    f"[open_payments] warning: columns absent from CSV "
                    f"header for {general_csv}: {missing}"
                )

            with zf.open(general_csv) as handle:
                chunks: list[pd.DataFrame] = []
                for chunk in pd.read_csv(
                    handle,
                    usecols=present,
                    chunksize=500_000,
                    dtype=str,
                    low_memory=False,
                ):
                    chunks.append(chunk)
                thin = pd.concat(chunks, ignore_index=True)
        return thin

    @staticmethod
    def _project_and_canonicalize(df: pd.DataFrame, *, year: int) -> pd.DataFrame:
        """Drop NPI-less rows, coerce amount, derive ProviderRecordSchema cols.

        NPI-less rows are dropped because the per-NPI ranker can't join
        them; this is mostly teaching-hospital rows. The
        ``Teaching_Hospital_Name`` column is kept in the projection (and
        will be NaN on individual rows) so an upstream consumer that
        wants the org-level view can still recover it.
        """
        out = df.copy()

        out = out[
            out["Covered_Recipient_NPI"].notna()
            & (out["Covered_Recipient_NPI"].astype(str).str.strip() != "")
        ].copy()

        out["Total_Amount_of_Payment_USDollars"] = pd.to_numeric(
            out["Total_Amount_of_Payment_USDollars"], errors="coerce"
        )

        # ProviderRecordSchema canonicals. For individuals (physician /
        # NPP) we use last+first+middle; for the rare NPI-bearing
        # teaching-hospital row the org name falls back to
        # Teaching_Hospital_Name.
        out["npi"] = (
            out["Covered_Recipient_NPI"].astype("int64").astype(str).str.zfill(10)
        )
        ent_codes = [_recipient_entity_code(t) for t in out["Covered_Recipient_Type"]]
        org_names = out["Teaching_Hospital_Name"]
        last_names = out["Covered_Recipient_Last_Name"]
        out["provider_name"] = [
            format_provider_name(
                entity_code=ec,
                last_or_org=org if ec == "O" else last,
                first=first,
                middle=mid,
            )
            for ec, org, last, first, mid in zip(
                ent_codes,
                org_names,
                last_names,
                out["Covered_Recipient_First_Name"],
                out["Covered_Recipient_Middle_Name"],
            )
        ]
        out["state"] = out["Recipient_State"].astype("string")
        out["data_year"] = year
        return out
