"""Part B by Provider scraper.

Wraps the existing ``download.py`` fetcher in the :class:`BaseScraper`
contract so the produced parquet carries the canonical stamp columns
(``source``, ``fetched_at_utc``, ``schema_version``), the canonical NPI
columns from :class:`ProviderRecordSchema` (``npi``, ``provider_name``,
``state``, ``data_year``), and a sibling ``.manifest.json``.

This covers the "by Provider" cut (~1 row per NPI per year, the project
spine). The "by Provider & Service" cut (multi-row per NPI, 5–10 GB
each) is a separate source — ``cms_partb_svc`` — handled by its own
scraper.

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
    2024: "2026-05/7323ba02-52e7-4a86-b2ce-ad210c25d9aa/MUP_PHY_R26_P05_V10_D24_Prov.csv",
    2023: "2025-04/22edfd1e-d17a-4478-ad6b-92cac2a5a3c4/MUP_PHY_R25_P05_V20_D23_Prov.csv",
    2022: "2025-11/adcd20c5-4534-43cd-8dfa-881ebe7bacfd/MUP_PHY_R25_P07_V20_D22_Prov.csv",
    2021: "2025-11/fc6ea9aa-12f0-4c2f-9909-6c8e06c961cf/MUP_PHY_R25_P07_V20_D21_Prov.csv",
    2020: "2025-11/056e8c6b-7e39-4945-b9a4-52d0a1cbbb9a/MUP_PHY_R25_P07_V20_D20_Prov.csv",
    2019: "2025-11/ac110c46-3429-4f3c-9348-56f0a5312cb8/MUP_PHY_R25_P07_V20_D19_Prov.csv",
    2018: "2025-11/57ea60f2-ef4b-46f2-8778-c7a50fab1737/MUP_PHY_R25_P07_V20_D18_Prov.csv",
    2017: "2025-11/b7e195ce-710d-4584-8293-787ffc38b017/MUP_PHY_R25_P07_V20_D17_Prov.csv",
    2016: "2025-11/e4cd28e1-2d05-4f33-b232-f32b5bebc153/MUP_PHY_R25_P05_V20_D16_Prov.csv",
    2015: "2025-11/efb7e2f9-b903-4f54-b57f-742f436c316f/MUP_PHY_R25_P05_V20_D15_Prov.csv",
    2014: "2025-11/da4b3b2b-1ee2-4e25-b2fe-012b880afd37/MUP_PHY_R25_P05_V20_D14_Prov.csv",
    2013: "2025-11/4fe919a0-94d4-4681-b076-4106f3766eef/MUP_PHY_R25_P05_V20_D13_Prov.csv",
}


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
#
# The 81 raw CMS column names below are preserved exactly as published in
# the RY25 release. Strict validation requires the produced parquet's
# columns to match this set exactly — if CMS renames a column upstream,
# the parser must rename back to the canonical name (declared here) so
# downstream consumers don't break.


class PartBSchema(ProviderRecordSchema):
    """Schema for ``cms_partb_<year>.parquet`` (one row per rendering NPI).

    Columns fall in six groups:

    * Provider identifiers (``Rndrng_*``) — 17 cols
    * Total billing aggregates (``Tot_*``) — 7 cols
    * Drug-administered subset (``Drug_*``) — 8 cols
    * Medical-services subset (``Med_*``) — 8 cols
    * Beneficiary demographics (``Bene_Avg_*``, ``Bene_*_Cnt``) — 15 cols
    * Chronic condition prevalences (``Bene_CC_*``) — 25 cols (V1/V2
      indicates which CMS codebook version was used to compute the flag)
    * Average risk score — 1 col
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

    # --- Total billing aggregates ---
    Tot_HCPCS_Cds: Series[int] = pa.Field(ge=0, nullable=True)
    Tot_Benes: Series[int] = pa.Field(ge=0, nullable=True)
    Tot_Srvcs: Series[float] = pa.Field(ge=0, nullable=True)
    Tot_Sbmtd_Chrg: Series[float] = pa.Field(ge=0, nullable=True)
    Tot_Mdcr_Alowd_Amt: Series[float] = pa.Field(ge=0, nullable=True)
    Tot_Mdcr_Pymt_Amt: Series[float] = pa.Field(ge=0, nullable=True)
    Tot_Mdcr_Stdzd_Amt: Series[float] = pa.Field(ge=0, nullable=True)

    # --- Drug-administered subset (Part B drugs — J-codes etc.) ---
    Drug_Sprsn_Ind: Series[str] = pa.Field(nullable=True)
    Drug_Tot_HCPCS_Cds: Series[float] = pa.Field(ge=0, nullable=True)
    Drug_Tot_Benes: Series[float] = pa.Field(ge=0, nullable=True)
    Drug_Tot_Srvcs: Series[float] = pa.Field(ge=0, nullable=True)
    Drug_Sbmtd_Chrg: Series[float] = pa.Field(ge=0, nullable=True)
    Drug_Mdcr_Alowd_Amt: Series[float] = pa.Field(ge=0, nullable=True)
    Drug_Mdcr_Pymt_Amt: Series[float] = pa.Field(ge=0, nullable=True)
    Drug_Mdcr_Stdzd_Amt: Series[float] = pa.Field(ge=0, nullable=True)

    # --- Medical-services subset (everything that isn't a Part B drug) ---
    Med_Sprsn_Ind: Series[str] = pa.Field(nullable=True)
    Med_Tot_HCPCS_Cds: Series[float] = pa.Field(ge=0, nullable=True)
    Med_Tot_Benes: Series[float] = pa.Field(ge=0, nullable=True)
    Med_Tot_Srvcs: Series[float] = pa.Field(ge=0, nullable=True)
    Med_Sbmtd_Chrg: Series[float] = pa.Field(ge=0, nullable=True)
    Med_Mdcr_Alowd_Amt: Series[float] = pa.Field(ge=0, nullable=True)
    Med_Mdcr_Pymt_Amt: Series[float] = pa.Field(ge=0, nullable=True)
    Med_Mdcr_Stdzd_Amt: Series[float] = pa.Field(ge=0, nullable=True)

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
    Bene_Race_API_Cnt: Series[float] = pa.Field(ge=0, nullable=True)
    Bene_Race_Hspnc_Cnt: Series[float] = pa.Field(ge=0, nullable=True)
    Bene_Race_NatInd_Cnt: Series[float] = pa.Field(ge=0, nullable=True)
    Bene_Race_Othr_Cnt: Series[float] = pa.Field(ge=0, nullable=True)
    Bene_Dual_Cnt: Series[float] = pa.Field(ge=0, nullable=True)
    Bene_Ndual_Cnt: Series[float] = pa.Field(ge=0, nullable=True)

    # --- Chronic condition prevalences (DY17+ only; pre-2017 NaN-padded) ---
    Bene_CC_BH_ADHD_OthCD_V1_Pct: Series[float] = pa.Field(ge=0, nullable=True)
    Bene_CC_BH_Alcohol_Drug_V1_Pct: Series[float] = pa.Field(ge=0, nullable=True)
    Bene_CC_BH_Tobacco_V1_Pct: Series[float] = pa.Field(ge=0, nullable=True)
    Bene_CC_BH_Alz_NonAlzdem_V2_Pct: Series[float] = pa.Field(ge=0, nullable=True)
    Bene_CC_BH_Anxiety_V1_Pct: Series[float] = pa.Field(ge=0, nullable=True)
    Bene_CC_BH_Bipolar_V1_Pct: Series[float] = pa.Field(ge=0, nullable=True)
    Bene_CC_BH_Mood_V2_Pct: Series[float] = pa.Field(ge=0, nullable=True)
    Bene_CC_BH_Depress_V1_Pct: Series[float] = pa.Field(ge=0, nullable=True)
    Bene_CC_BH_PD_V1_Pct: Series[float] = pa.Field(ge=0, nullable=True)
    Bene_CC_BH_PTSD_V1_Pct: Series[float] = pa.Field(ge=0, nullable=True)
    Bene_CC_BH_Schizo_OthPsy_V1_Pct: Series[float] = pa.Field(ge=0, nullable=True)
    Bene_CC_PH_Asthma_V2_Pct: Series[float] = pa.Field(ge=0, nullable=True)
    Bene_CC_PH_Afib_V2_Pct: Series[float] = pa.Field(ge=0, nullable=True)
    Bene_CC_PH_Cancer6_V2_Pct: Series[float] = pa.Field(ge=0, nullable=True)
    Bene_CC_PH_CKD_V2_Pct: Series[float] = pa.Field(ge=0, nullable=True)
    Bene_CC_PH_COPD_V2_Pct: Series[float] = pa.Field(ge=0, nullable=True)
    Bene_CC_PH_Diabetes_V2_Pct: Series[float] = pa.Field(ge=0, nullable=True)
    Bene_CC_PH_HF_NonIHD_V2_Pct: Series[float] = pa.Field(ge=0, nullable=True)
    Bene_CC_PH_Hyperlipidemia_V2_Pct: Series[float] = pa.Field(ge=0, nullable=True)
    Bene_CC_PH_Hypertension_V2_Pct: Series[float] = pa.Field(ge=0, nullable=True)
    Bene_CC_PH_IschemicHeart_V2_Pct: Series[float] = pa.Field(ge=0, nullable=True)
    Bene_CC_PH_Osteoporosis_V2_Pct: Series[float] = pa.Field(ge=0, nullable=True)
    Bene_CC_PH_Parkinson_V2_Pct: Series[float] = pa.Field(ge=0, nullable=True)
    Bene_CC_PH_Arthritis_V2_Pct: Series[float] = pa.Field(ge=0, nullable=True)
    Bene_CC_PH_Stroke_TIA_V2_Pct: Series[float] = pa.Field(ge=0, nullable=True)
    Bene_Avg_Risk_Scre: Series[float] = pa.Field(ge=0, nullable=True)


AVAILABLE_YEARS = tuple(sorted(URLS))


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------


class PartBScraper(BaseScraper):
    """Scrape one Part B PUF year (by Provider) → parquet + manifest."""

    source: ClassVar[str] = "cms_partb"
    schema: ClassVar[Type[BaseScraperSchema]] = PartBSchema
    output_dir: ClassVar[Path] = PANELS_DIR
    schema_version: ClassVar[str] = "v1.0"

    def fetch(self, *, year: int) -> RunResult:
        if year not in AVAILABLE_YEARS:
            raise ValueError(
                f"cms_partb year {year} not in catalog "
                f"(have {AVAILABLE_YEARS[0]}–{AVAILABLE_YEARS[-1]})"
            )

        started = self._now()
        url = cms_url(URLS[year])
        csv_path = RAW_DIR / "cms_partb" / f"cms_partb_{year}.csv"

        _download(url, csv_path)
        # Read every schema-declared string column as text (FIPS "06"/"9C",
        # ZIP "02134", …) so pandas' type inference can't numericize a
        # zero-padded / alphanumeric code and destroy it before validation —
        # a loss no later cast can undo. See BaseScraper.string_read_dtypes
        # and data/docs/cms_partb.md §6a.
        df = pd.read_csv(csv_path, low_memory=False, dtype=self.string_read_dtypes())
        df = self._add_canonical_columns(df, year=year)
        df = self._stamp_base_columns(df)
        # Pre-2017 Part B lacks the 25 Bene_CC_* columns. Align to the
        # canonical schema so older years materialize with NaN-filled
        # CC columns and pass strict validation uniformly.
        df = self._align_to_schema(df)

        return self._write(
            df,
            name=f"cms_partb_{year}",
            started_at_utc=started,
            inputs={"url": url, "local_csv": str(csv_path)},
        )

    @staticmethod
    def _add_canonical_columns(df: pd.DataFrame, *, year: int) -> pd.DataFrame:
        """Populate the ``ProviderRecordSchema`` canonical NPI columns.

        Kept separate from the raw CMS columns so a future ``Tot_Mdcr_*``
        column rename upstream doesn't entangle the canonical-name path.
        """
        out = df.copy()
        out["npi"] = out["Rndrng_NPI"].astype("int64").astype(str).str.zfill(10)
        out["provider_name"] = [
            format_provider_name(
                entity_code=ec,
                last_or_org=last,
                first=first,
                middle=mi,
            )
            for ec, last, first, mi in zip(
                out["Rndrng_Prvdr_Ent_Cd"],
                out["Rndrng_Prvdr_Last_Org_Name"],
                out["Rndrng_Prvdr_First_Name"],
                out["Rndrng_Prvdr_MI"],
            )
        ]
        out["state"] = out["Rndrng_Prvdr_State_Abrvtn"].astype("string")
        out["data_year"] = year
        return out
