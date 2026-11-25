"""Part D by Provider scraper.

Wraps the existing ``download.py`` fetcher in the :class:`BaseScraper`
contract so the produced parquet carries the canonical stamp columns
(``source``, ``fetched_at_utc``, ``schema_version``), the canonical NPI
columns from :class:`ProviderRecordSchema` (``npi``, ``provider_name``,
``state``, ``data_year``), and a sibling ``.manifest.json``.

This covers the "by Provider" cut (~1 row per prescribing NPI per year).
The "by Provider & Drug" cut (multi-row per NPI, one row per HCPCS drug)
is a separate source not yet scraped.

Years covered: 2013–2023.
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
    2024: "2026-05/e9cd7dfb-9c27-4b3f-8f5d-2454091303ee/MUP_DPR_RY26_P04_V10_DY24_NPI.csv",
    2023: "2025-04/750769a3-bb0f-4f05-81dc-7dcb6e105cb0/MUP_DPR_RY25_P04_V10_DY23_NPI.csv",
    2022: "2025-11/429c776b-63ab-4976-8764-0b7f05db14bd/MUP_DPR_RY25_P04_V20_DY22_NPI.csv",
    2021: "2025-11/0ae3731b-2a46-4233-8aa6-51426a478dbc/MUP_DPR_RY25_P04_V20_DY21_NPI.csv",
    2020: "2025-11/77e12580-bf64-4cae-a0b9-e04468054a2e/MUP_DPR_RY25_P04_V20_DY20_NPI.csv",
    2019: "2025-11/b8653539-dfde-4d76-8303-efdf87207a4c/MUP_DPR_RY25_P04_V20_DY19_NPI.csv",
    2018: "2025-11/0ed18b7d-0441-4177-bb63-b12035e9ac38/MUP_DPR_RY25_P04_V20_DY18_NPI.csv",
    2017: "2025-11/f1aec944-4844-4bef-806b-328e2b8b19ba/MUP_DPR_RY25_P04_V20_DY17_NPI.csv",
    2016: "2025-11/58cbc9cb-06ba-442d-86cd-fcd047dd0da1/MUP_DPR_RY25_P04_V20_DY16_NPI.csv",
    2015: "2025-11/ffc658fc-771e-4f0f-9883-eb3fc26f93d8/MUP_DPR_RY25_P04_V20_DY15_NPI.csv",
    2014: "2025-11/e56060ab-a16a-4474-9759-c2f7d80c6612/MUP_DPR_RY25_P04_V20_DY14_NPI.csv",
    2013: "2025-11/1073b2a0-b1c7-428a-85ab-9e98396b9f80/MUP_DPR_RY25_P04_V20_DY13_NPI.csv",
}


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
#
# The 84 raw CMS column names below are preserved exactly as published in
# the RY25 V20 release. Strict validation requires the produced parquet's
# columns to match this set exactly — if CMS renames a column upstream,
# the parser must rename back to the canonical name (declared here) so
# downstream consumers don't break.
#
# Two intentional deviations from "the literal CSV header":
#
# * ``Antpsyct_GE65_Bene_Suprsn_Flag`` — CMS has a typo here ("Suprsn"
#   instead of "Sprsn"). Preserved because the typo IS the CSV header.
# * ``Prscrbr_NPI`` and ``Prscrbr_Type_Src`` — 2023's V10 release uses
#   ``PRSCRBR_NPI`` (all-caps) and ``Prscrbr_Type_src`` (lower-case
#   ``src``); 2013–2022's V20 release uses the mixed-case forms shown
#   here. We canonicalize on V20 since it's the longer-running form and
#   alias V10 at parse time (see :data:`_RENAMES_FROM_V10`).


class PartDSchema(ProviderRecordSchema):
    """Schema for ``cms_partd_<year>.parquet`` (one row per prescribing NPI).

    Columns fall in eight groups:

    * Prescriber identifiers (``Prscrbr_*``) — 17 cols
    * Total prescribing aggregates (``Tot_*``) — 5 cols
    * Age-65+ subset (``GE65_*``) — 7 cols
    * Brand vs Generic vs Other vs MAPD vs PDP subsets — 18 cols total
      across the five drug-type / plan-type breakdowns
    * Low-Income-Subsidy vs Non-LIS subsets (``LIS_*`` / ``NonLIS_*``) — 6 cols
    * Opioid prescribing block (``Opioid_*``) — 10 cols (includes long-acting
      ``Opioid_LA_*`` subset, the key DEA-scrutinized category)
    * Antibiotic + Antipsychotic-65+ blocks — 8 cols
    * Beneficiary demographics + average risk score — 13 cols
    """

    # --- Prescriber identifiers (raw CMS) ---
    Prscrbr_NPI: Series[str] = pa.Field(str_matches=r"^(\d{10})?$", nullable=True)
    Prscrbr_Last_Org_Name: Series[str] = pa.Field(nullable=True)
    Prscrbr_First_Name: Series[str] = pa.Field(nullable=True)
    Prscrbr_MI: Series[str] = pa.Field(nullable=True)
    Prscrbr_Crdntls: Series[str] = pa.Field(nullable=True)
    Prscrbr_Ent_Cd: Series[str] = pa.Field(nullable=True)
    Prscrbr_St1: Series[str] = pa.Field(nullable=True)
    Prscrbr_St2: Series[str] = pa.Field(nullable=True)
    Prscrbr_City: Series[str] = pa.Field(nullable=True)
    Prscrbr_State_Abrvtn: Series[str] = pa.Field(nullable=True)
    # Zero-padded codes — MUST be str. FIPS '01' and ZIP '02134' lose their
    # leading zero (and any territory/alphanumeric code) if stored numeric;
    # the DOUBLE years on R2 already did. Not faithfully cast-recoverable —
    # re-ingest from source as VARCHAR. See data/docs/cms_partd.md §6a.
    Prscrbr_State_FIPS: Series[str] = pa.Field(nullable=True)
    Prscrbr_zip5: Series[str] = pa.Field(nullable=True)
    Prscrbr_RUCA: Series[float] = pa.Field(nullable=True)
    Prscrbr_RUCA_Desc: Series[str] = pa.Field(nullable=True)
    Prscrbr_Cntry: Series[str] = pa.Field(nullable=True)
    Prscrbr_Type: Series[str] = pa.Field(nullable=True)
    Prscrbr_Type_Src: Series[str] = pa.Field(nullable=True)

    # --- Total prescribing aggregates ---
    Tot_Clms: Series[int] = pa.Field(ge=0, nullable=True)
    Tot_30day_Fills: Series[float] = pa.Field(ge=0, nullable=True)
    Tot_Drug_Cst: Series[float] = pa.Field(ge=0, nullable=True)
    Tot_Day_Suply: Series[int] = pa.Field(ge=0, nullable=True)
    # Integer count — kept Int64 to match cms_partb / cms_partb_svc Tot_Benes
    # (cross-source dtype consistency; these are distinct beneficiary counts
    # that share a CMS column name).
    Tot_Benes: Series[int] = pa.Field(ge=0, nullable=True)

    # --- Age-65+ subset ---
    GE65_Sprsn_Flag: Series[str] = pa.Field(nullable=True)
    GE65_Tot_Clms: Series[float] = pa.Field(ge=0, nullable=True)
    GE65_Tot_30day_Fills: Series[float] = pa.Field(ge=0, nullable=True)
    GE65_Tot_Drug_Cst: Series[float] = pa.Field(ge=0, nullable=True)
    GE65_Tot_Day_Suply: Series[float] = pa.Field(ge=0, nullable=True)
    GE65_Bene_Sprsn_Flag: Series[str] = pa.Field(nullable=True)
    GE65_Tot_Benes: Series[float] = pa.Field(ge=0, nullable=True)

    # --- Brand drugs ---
    Brnd_Sprsn_Flag: Series[str] = pa.Field(nullable=True)
    Brnd_Tot_Clms: Series[float] = pa.Field(ge=0, nullable=True)
    Brnd_Tot_Drug_Cst: Series[float] = pa.Field(ge=0, nullable=True)

    # --- Generic drugs ---
    Gnrc_Sprsn_Flag: Series[str] = pa.Field(nullable=True)
    Gnrc_Tot_Clms: Series[float] = pa.Field(ge=0, nullable=True)
    Gnrc_Tot_Drug_Cst: Series[float] = pa.Field(ge=0, nullable=True)

    # --- Other drugs (vaccines, supplies) ---
    Othr_Sprsn_Flag: Series[str] = pa.Field(nullable=True)
    Othr_Tot_Clms: Series[float] = pa.Field(ge=0, nullable=True)
    Othr_Tot_Drug_Cst: Series[float] = pa.Field(ge=0, nullable=True)

    # --- Medicare Advantage Prescription Drug plan share ---
    MAPD_Sprsn_Flag: Series[str] = pa.Field(nullable=True)
    MAPD_Tot_Clms: Series[float] = pa.Field(ge=0, nullable=True)
    MAPD_Tot_Drug_Cst: Series[float] = pa.Field(ge=0, nullable=True)

    # --- Standalone Prescription Drug Plan share ---
    PDP_Sprsn_Flag: Series[str] = pa.Field(nullable=True)
    PDP_Tot_Clms: Series[float] = pa.Field(ge=0, nullable=True)
    PDP_Tot_Drug_Cst: Series[float] = pa.Field(ge=0, nullable=True)

    # --- Low-Income-Subsidy beneficiaries ---
    LIS_Sprsn_Flag: Series[str] = pa.Field(nullable=True)
    LIS_Tot_Clms: Series[float] = pa.Field(ge=0, nullable=True)
    LIS_Drug_Cst: Series[float] = pa.Field(ge=0, nullable=True)

    # --- Non-LIS beneficiaries ---
    NonLIS_Sprsn_Flag: Series[str] = pa.Field(nullable=True)
    NonLIS_Tot_Clms: Series[float] = pa.Field(ge=0, nullable=True)
    NonLIS_Drug_Cst: Series[float] = pa.Field(ge=0, nullable=True)

    # --- Opioid block (load-bearing for pill-mill detection) ---
    Opioid_Tot_Clms: Series[float] = pa.Field(ge=0, nullable=True)
    Opioid_Tot_Drug_Cst: Series[float] = pa.Field(ge=0, nullable=True)
    Opioid_Tot_Suply: Series[float] = pa.Field(ge=0, nullable=True)
    Opioid_Tot_Benes: Series[float] = pa.Field(ge=0, nullable=True)
    Opioid_Prscrbr_Rate: Series[float] = pa.Field(ge=0, nullable=True)
    Opioid_LA_Tot_Clms: Series[float] = pa.Field(ge=0, nullable=True)
    Opioid_LA_Tot_Drug_Cst: Series[float] = pa.Field(ge=0, nullable=True)
    Opioid_LA_Tot_Suply: Series[float] = pa.Field(ge=0, nullable=True)
    Opioid_LA_Tot_Benes: Series[float] = pa.Field(ge=0, nullable=True)
    Opioid_LA_Prscrbr_Rate: Series[float] = pa.Field(ge=0, nullable=True)

    # --- Antibiotic block ---
    Antbtc_Tot_Clms: Series[float] = pa.Field(ge=0, nullable=True)
    Antbtc_Tot_Drug_Cst: Series[float] = pa.Field(ge=0, nullable=True)
    Antbtc_Tot_Benes: Series[float] = pa.Field(ge=0, nullable=True)

    # --- Antipsychotic 65+ block (nursing-home fraud signal) ---
    Antpsyct_GE65_Sprsn_Flag: Series[str] = pa.Field(nullable=True)
    Antpsyct_GE65_Tot_Clms: Series[float] = pa.Field(ge=0, nullable=True)
    Antpsyct_GE65_Tot_Drug_Cst: Series[float] = pa.Field(ge=0, nullable=True)
    # CMS publishes this with "Suprsn" (their typo); preserved to match the raw header.
    Antpsyct_GE65_Bene_Suprsn_Flag: Series[str] = pa.Field(nullable=True)
    Antpsyct_GE65_Tot_Benes: Series[float] = pa.Field(ge=0, nullable=True)

    # --- Beneficiary demographics ---
    Bene_Avg_Age: Series[float] = pa.Field(ge=0, nullable=True)
    Bene_Age_LT_65_Cnt: Series[float] = pa.Field(ge=0, nullable=True)
    Bene_Age_65_74_Cnt: Series[float] = pa.Field(ge=0, nullable=True)
    Bene_Age_75_84_Cnt: Series[float] = pa.Field(ge=0, nullable=True)
    Bene_Age_GT_84_Cnt: Series[float] = pa.Field(ge=0, nullable=True)
    Bene_Feml_Cnt: Series[float] = pa.Field(ge=0, nullable=True)
    Bene_Male_Cnt: Series[float] = pa.Field(ge=0, nullable=True)
    Bene_Race_Wht_Cnt: Series[float] = pa.Field(ge=0, nullable=True)
    Bene_Race_Black_Cnt: Series[float] = pa.Field(ge=0, nullable=True)
    Bene_Race_Api_Cnt: Series[float] = pa.Field(ge=0, nullable=True)
    Bene_Race_Hspnc_Cnt: Series[float] = pa.Field(ge=0, nullable=True)
    Bene_Race_Natind_Cnt: Series[float] = pa.Field(ge=0, nullable=True)
    Bene_Race_Othr_Cnt: Series[float] = pa.Field(ge=0, nullable=True)
    Bene_Dual_Cnt: Series[float] = pa.Field(ge=0, nullable=True)
    Bene_Ndual_Cnt: Series[float] = pa.Field(ge=0, nullable=True)
    Bene_Avg_Risk_Scre: Series[float] = pa.Field(ge=0, nullable=True)


AVAILABLE_YEARS = tuple(sorted(URLS))


# CMS's V10 D23 release deviates from the V20 column naming used by every
# other year. When V20 D23 publishes, the deviation should disappear; the
# aliases stay here as a safety net.
_RENAMES_FROM_V10: dict[str, str] = {
    "PRSCRBR_NPI": "Prscrbr_NPI",
    "Prscrbr_Type_src": "Prscrbr_Type_Src",
}


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------


class PartDScraper(BaseScraper):
    """Scrape one Part D PUF year (by Provider) → parquet + manifest."""

    source: ClassVar[str] = "cms_partd"
    schema: ClassVar[Type[BaseScraperSchema]] = PartDSchema
    output_dir: ClassVar[Path] = PANELS_DIR
    schema_version: ClassVar[str] = "v1.0"

    def fetch(self, *, year: int) -> RunResult:
        if year not in AVAILABLE_YEARS:
            raise ValueError(
                f"cms_partd year {year} not in catalog "
                f"(have {AVAILABLE_YEARS[0]}–{AVAILABLE_YEARS[-1]})"
            )

        started = self._now()
        url = cms_url(URLS[year])
        csv_path = RAW_DIR / "cms_partd" / f"cms_partd_{year}.csv"

        _download(url, csv_path)
        # Read every schema-declared string column as text (FIPS "06"/"9C",
        # ZIP "02134", …) so pandas' type inference can't numericize a
        # zero-padded / alphanumeric code and destroy it before validation —
        # a loss no later cast can undo. See BaseScraper.string_read_dtypes
        # and data/docs/cms_partd.md §6a.
        df = pd.read_csv(csv_path, low_memory=False, dtype=self.string_read_dtypes())
        df = df.rename(columns=_RENAMES_FROM_V10)
        df = self._add_canonical_columns(df, year=year)
        df = self._stamp_base_columns(df)
        # Older years may lack the opioid / antibiotic / antipsychotic
        # blocks that were added partway through the PUF's history. Align
        # to the canonical schema so every year materializes uniformly.
        df = self._align_to_schema(df)

        return self._write(
            df,
            name=f"cms_partd_{year}",
            started_at_utc=started,
            inputs={"url": url, "local_csv": str(csv_path)},
        )

    @staticmethod
    def _add_canonical_columns(df: pd.DataFrame, *, year: int) -> pd.DataFrame:
        """Populate the ``ProviderRecordSchema`` canonical NPI columns."""
        out = df.copy()
        out["npi"] = out["Prscrbr_NPI"].astype("int64").astype(str).str.zfill(10)
        out["provider_name"] = [
            format_provider_name(
                entity_code=ec,
                last_or_org=last,
                first=first,
                middle=mi,
            )
            for ec, last, first, mi in zip(
                out["Prscrbr_Ent_Cd"],
                out["Prscrbr_Last_Org_Name"],
                out["Prscrbr_First_Name"],
                out["Prscrbr_MI"],
            )
        ]
        out["state"] = out["Prscrbr_State_Abrvtn"].astype("string")
        out["data_year"] = year
        return out
