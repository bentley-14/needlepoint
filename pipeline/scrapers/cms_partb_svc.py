"""Part B by Provider & Service scraper.

Wraps the existing ``download.py`` fetcher in the :class:`BaseScraper`
contract so the produced parquet carries the canonical stamp columns
(``source``, ``fetched_at_utc``, ``schema_version``), the canonical NPI
columns from :class:`ProviderRecordSchema` (``npi``, ``provider_name``,
``state``, ``data_year``), and a sibling ``.manifest.json``.

This is the **per-(provider, HCPCS, place-of-service) cut** of the Part
B PUF (multi-row per NPI; ~5–10 GB CSV per year, ~10M+ rows). It is the
file that surfaces upcoding code-mix, J-code drug substitution at the
line-item level, telehealth POS-02/POS-10 abuse, and code-family
concentration patterns invisible from the aggregated by-Provider cut
(``cms_partb``).

⚠️ Each row is one (NPI × HCPCS × Place_Of_Srvc) tuple, NOT one row per
NPI. NPI repeats hundreds of times per provider. ``ProviderRecordSchema``
declares ``npi: str`` but does not assert uniqueness — multi-row-per-NPI
parquets are legal under it. Downstream consumers that treat ``npi`` as a
row key must aggregate first. See ``data/docs/cms_partb_svc.md`` §2.

Years covered: 2013–2024.
"""
from __future__ import annotations

from pathlib import Path
from typing import ClassVar, Type

import pandas as pd
import pandera.pandas as pa
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
    cms_url,
)


# ---------------------------------------------------------------------------
# URL catalog
# ---------------------------------------------------------------------------
#
# Direct-CSV permalinks resolved from CMS data.json catalog (April 2026).
# Stored as relative paths under ``pipeline.scrapers.download.CMS_FILES_BASE``;
# :func:`cms_url` does the join. URLs rotate per RY release; update by
# re-scraping the data.json catalog and pasting either the relative tail
# (canonical) or the full URL (helper accepts both).
URLS: dict[int, str] = {
    # 2024 (RY26) re-posted 2026-05 after the February release was retracted.
    # Filename dropped the MUP_ prefix and the V10 release reused the column
    # set unchanged (probe 2026-05-30 confirmed identical 28-col header).
    2024: "2026-05/b5ebab5a-f490-418a-9bce-4b9f31419356/PHY_R26_P05_V10_D24_Prov_Svc.csv",
    2023: "2025-04/e3f823f8-db5b-4cc7-ba04-e7ae92b99757/MUP_PHY_R25_P05_V20_D23_Prov_Svc.csv",
    2022: "2025-11/53fb2bae-4913-48dc-a6d4-d8c025906567/MUP_PHY_R25_P05_V20_D22_Prov_Svc.csv",
    2021: "2025-11/bffaf97a-c2ab-4fd7-8718-be90742e3485/MUP_PHY_R25_P05_V20_D21_Prov_Svc.csv",
    2020: "2025-11/d22b18cd-7726-4bf5-8e9c-3e4587c589a1/MUP_PHY_R25_P05_V20_D20_Prov_Svc.csv",
    2019: "2025-11/7befba27-752e-47a8-a76c-6c6d4f74f2e3/MUP_PHY_R25_P04_V20_D19_Prov_Svc.csv",
    2018: "2025-11/5669eafb-f0b3-4dc5-be6d-abc09b480c2e/MUP_PHY_R25_P04_V20_D18_Prov_Svc.csv",
    # D17 Prov_Svc only published at the V10 initial release (2025-03); the
    # RY25 V20 republish under 2025-11/ hasn't surfaced for this year.
    2017: "2025-03/78bbcbc8-e39a-4def-a18e-b663cf3ac49f/MUP_PHY_R25_P04_V10_D17_Prov_Svc.csv",
    2016: "2025-11/426bf97a-4cb8-47ca-9727-a535d9e8c298/MUP_PHY_R25_P04_V20_D16_Prov_Svc.csv",
    2015: "2025-11/14954ce3-4c43-43df-97e9-2c0437d7b43c/MUP_PHY_R25_P04_V20_D15_Prov_Svc.csv",
    2014: "2025-11/6700f86d-d2e5-4f2d-9dcb-8c30412768ff/MUP_PHY_R25_P04_V20_D14_Prov_Svc.csv",
    2013: "2025-11/bf4231f9-ec7f-4189-afc3-ba5d53b8bd12/MUP_PHY_R25_P04_V20_D13_Prov_Svc.csv",
}


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
#
# The 28 raw CMS column names below are preserved exactly as published in
# the RY25 V20 (and RY26 V10) releases. Strict validation requires the
# produced parquet's columns to match this set exactly — if CMS renames a
# column upstream, the parser must rename back to the canonical name
# (declared here) so downstream consumers don't break.
#
# A schema-drift probe across all 12 years (2013–2024) on 2026-05-30
# confirmed the column set is stable: every year ships the same 28
# columns, identical casing. The 2024 RY26 release dropped the ``MUP_``
# filename prefix but did NOT change the column headers.
#
# Pre-aggregate ``Tot_*`` columns (Tot_Benes, Tot_Srvcs, Tot_Bene_Day_Srvcs)
# are integer counts. The dollar columns are ``Avg_*`` (per-service
# averages, NOT totals) — to compute total Medicare paid for a given
# (NPI, HCPCS, POS) row, multiply ``Avg_Mdcr_Pymt_Amt * Tot_Srvcs``. This
# is why downstream feature code (``scripts/compute_hcpcs_rich_features``,
# ``fraudlib/ingest/cms/hcpcs_stats``) always derives ``_tot_paid`` rather
# than reading it directly.


class PartBSvcSchema(ProviderRecordSchema):
    """Schema for ``cms_partb_svc_<year>.parquet`` (multi-row per NPI).

    Columns fall in three groups:

    * Provider identifiers (``Rndrng_*``) — 17 cols, repeated per row of
      a given NPI (denormalized into every line-item row)
    * HCPCS / place-of-service identifiers — 4 cols
    * Per-(NPI, HCPCS, POS) aggregates — 7 cols (3 counts + 4 dollar
      averages)

    Dtype contract
    --------------
    ``Rndrng_NPI`` is declared ``str`` with the same ``^(\\d{10})?$``
    check as the canonical ``npi``: a float that reaches it coerces to
    ``"1900000075.0"`` and FAILS validation loudly rather than shipping
    garbage. The parser reads NPI as int upstream (int→str coerces clean).

    Dollar averages (``Avg_*``) and count columns (``Tot_*``) carry
    ``ge=0`` — a per-service average payment or a beneficiary count can't
    be negative.
    """

    # --- Provider identifiers (raw CMS) ---
    Rndrng_NPI: Series[str] = pa.Field(str_matches=r"^(\d{10})?$", nullable=True)
    Rndrng_Prvdr_Last_Org_Name: Series[str] = pa.Field(nullable=True)
    Rndrng_Prvdr_First_Name: Series[str] = pa.Field(nullable=True)
    Rndrng_Prvdr_MI: Series[str] = pa.Field(nullable=True)
    Rndrng_Prvdr_Crdntls: Series[str] = pa.Field(nullable=True)
    Rndrng_Prvdr_Ent_Cd: Series[str] = pa.Field(nullable=True)
    Rndrng_Prvdr_St1: Series[str] = pa.Field(nullable=True)
    Rndrng_Prvdr_St2: Series[str] = pa.Field(nullable=True)
    Rndrng_Prvdr_City: Series[str] = pa.Field(nullable=True)
    Rndrng_Prvdr_State_Abrvtn: Series[str] = pa.Field(nullable=True)
    Rndrng_Prvdr_State_FIPS: Series[str] = pa.Field(nullable=True)
    Rndrng_Prvdr_Zip5: Series[str] = pa.Field(nullable=True)
    Rndrng_Prvdr_RUCA: Series[float] = pa.Field(nullable=True)
    Rndrng_Prvdr_RUCA_Desc: Series[str] = pa.Field(nullable=True)
    Rndrng_Prvdr_Cntry: Series[str] = pa.Field(nullable=True)
    Rndrng_Prvdr_Type: Series[str] = pa.Field(nullable=True)
    Rndrng_Prvdr_Mdcr_Prtcptg_Ind: Series[str] = pa.Field(nullable=True)

    # --- HCPCS / place-of-service ---
    HCPCS_Cd: Series[str] = pa.Field(nullable=True)
    HCPCS_Desc: Series[str] = pa.Field(nullable=True)
    HCPCS_Drug_Ind: Series[str] = pa.Field(nullable=True)   # "Y" for Part B drug HCPCS (J-codes etc.)
    Place_Of_Srvc: Series[str] = pa.Field(nullable=True)    # "F" facility, "O" non-facility; POS-02/POS-10 telehealth lives here

    # --- Per-(NPI, HCPCS, POS) aggregates ---
    Tot_Benes: Series[int] = pa.Field(ge=0, nullable=True)
    Tot_Srvcs: Series[float] = pa.Field(ge=0, nullable=True)
    Tot_Bene_Day_Srvcs: Series[float] = pa.Field(ge=0, nullable=True)
    Avg_Sbmtd_Chrg: Series[float] = pa.Field(ge=0, nullable=True)
    Avg_Mdcr_Alowd_Amt: Series[float] = pa.Field(ge=0, nullable=True)
    Avg_Mdcr_Pymt_Amt: Series[float] = pa.Field(ge=0, nullable=True)
    Avg_Mdcr_Stdzd_Amt: Series[float] = pa.Field(ge=0, nullable=True)


AVAILABLE_YEARS = tuple(sorted(URLS))


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------


class PartBSvcScraper(BaseScraper):
    """Scrape one Part B PUF year (by Provider & Service) → parquet + manifest.

    Multi-row per NPI. Expect 5–10 GB CSV in, ~1–3 GB parquet out, and
    ~10M+ rows per year. Disk peak during ``to_parquet`` is ~2× the
    output file size — verify free disk before running on multiple years
    in sequence. ``BaseScraper._write()`` does a single in-memory
    ``df.to_parquet`` call, so peak RAM is on the order of the CSV size;
    a 32 GB host handles a single year comfortably.
    """

    source: ClassVar[str] = "cms_partb_svc"
    schema: ClassVar[Type[BaseScraperSchema]] = PartBSvcSchema
    output_dir: ClassVar[Path] = PANELS_DIR
    schema_version: ClassVar[str] = "v1.0"

    def fetch(self, *, year: int) -> RunResult:
        if year not in AVAILABLE_YEARS:
            raise ValueError(
                f"cms_partb_svc year {year} not in catalog "
                f"(have {AVAILABLE_YEARS[0]}–{AVAILABLE_YEARS[-1]})"
            )

        started = self._now()
        url = cms_url(URLS[year])
        csv_path = RAW_DIR / "cms_partb_svc" / f"cms_partb_svc_{year}.csv"

        _download(url, csv_path)
        # Read every schema-declared string column as text (FIPS "06"/"9C",
        # ZIP "02134", …) so pandas' type inference can't numericize a
        # zero-padded / alphanumeric code and destroy it before validation —
        # a loss no later cast can undo. See BaseScraper.string_read_dtypes
        # and data/docs/cms_partb_svc.md §6a.
        df = pd.read_csv(csv_path, low_memory=False, dtype=self.string_read_dtypes())
        df = self._add_canonical_columns(df, year=year)
        df = self._stamp_base_columns(df)
        # The schema-drift probe (2026-05-30) confirmed every year ships
        # the same 28 columns; no _align_to_schema call needed today. The
        # base ABC's strict _validate() will surface any future drift.

        return self._write(
            df,
            name=f"cms_partb_svc_{year}",
            started_at_utc=started,
            inputs={"url": url, "local_csv": str(csv_path)},
        )

    @staticmethod
    def _add_canonical_columns(df: pd.DataFrame, *, year: int) -> pd.DataFrame:
        """Populate the ``ProviderRecordSchema`` canonical NPI columns.

        At ~10M rows per year, the naive per-row ``format_provider_name``
        call used by Part B / Part D is too slow. We build the
        per-NPI name lookup once on the unique-NPI frame, then map it
        back onto the full multi-row frame.
        """
        out = df.copy()
        out["npi"] = out["Rndrng_NPI"].astype("int64").astype(str).str.zfill(10)

        name_cols = [
            "Rndrng_NPI",
            "Rndrng_Prvdr_Ent_Cd",
            "Rndrng_Prvdr_Last_Org_Name",
            "Rndrng_Prvdr_First_Name",
            "Rndrng_Prvdr_MI",
        ]
        unique_providers = (
            out[name_cols].drop_duplicates(subset=["Rndrng_NPI"]).set_index("Rndrng_NPI")
        )
        name_map = {
            npi: format_provider_name(
                entity_code=row["Rndrng_Prvdr_Ent_Cd"],
                last_or_org=row["Rndrng_Prvdr_Last_Org_Name"],
                first=row["Rndrng_Prvdr_First_Name"],
                middle=row["Rndrng_Prvdr_MI"],
            )
            for npi, row in unique_providers.iterrows()
        }
        out["provider_name"] = out["Rndrng_NPI"].map(name_map)
        out["state"] = out["Rndrng_Prvdr_State_Abrvtn"].astype("string")
        out["data_year"] = year
        return out
