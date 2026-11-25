"""DMEPOS by Referring Provider scraper.

Wraps the existing ``download.py`` fetcher in the :class:`BaseScraper`
contract so the produced parquet carries the canonical stamp columns
(``source``, ``fetched_at_utc``, ``schema_version``), the canonical NPI
columns from :class:`ProviderRecordSchema` (``npi``, ``provider_name``,
``state``, ``data_year``), and a sibling ``.manifest.json``.

The raw CSV download and URL catalog still live in
``pipeline.scrapers.download`` — that file is the catalog of record. This
class delegates the bytes-on-the-wire dance to ``download._download`` and
focuses on validation + manifest.

Years covered: 2014–2023. DY13 isn't published in the RY25 release.
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
#
# DY13 is not published in the RY25 V20 release (PUF started 2013 for
# supplier-level, but the referring-provider cut first appears at DY14).
URLS: dict[int, str] = {
    2023: "2025-06/d63c2c87-b33e-4210-8823-46f8deb34a6a/mup_dme_ry25_p05_v10_dy23_rfrr.csv",
    2022: "2025-11/88636d2d-df54-41a5-881c-4c4263cf4dc7/mup_dme_ry25_p05_v20_dy22_rfrr.csv",
    2021: "2025-11/5f2bdd2f-7c1c-4cb9-8156-d2073c3b98b7/mup_dme_ry25_p05_v20_dy21_rfrr.csv",
    2020: "2025-11/c547346d-e98a-4972-b44a-c643df3d039e/mup_dme_ry25_p05_v20_dy20_rfrr.csv",
    2019: "2025-11/68c19bf4-e02b-4ff2-8594-7673f6597e64/mup_dme_ry25_p05_v20_dy19_rfrr.csv",
    2018: "2025-11/89638825-f423-4df6-806f-ce4506e7aa8e/mup_dme_ry25_p05_v20_dy18_rfrr.csv",
    2017: "2025-11/8131ba23-2763-417e-8997-5d0304432057/mup_dme_ry25_p05_v20_dy17_rfrr.csv",
    2016: "2025-11/a4eab5a7-6a24-4e66-b6b6-8e83e1194010/mup_dme_ry25_p05_v20_dy16_rfrr.csv",
    2015: "2025-11/4bb465f4-f59b-4e61-b301-e4a3f0ced51d/mup_dme_ry25_p05_v20_dy15_rfrr.csv",
    2014: "2025-11/3a6116dc-9d28-4394-8dbc-911631414ade/mup_dme_ry25_p05_v20_dy14_rfrr.csv",
}


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
#
# The 97 raw CMS column names below are preserved exactly as published in
# the RY25 release. Strict validation requires the produced parquet's
# columns to match this set exactly — if CMS renames a column upstream,
# the parser must rename back to the canonical name (declared here) so
# downstream consumers don't break.


class DMEPOSSchema(ProviderRecordSchema):
    """Schema for ``cms_dmepos_<year>.parquet`` (one row per referring NPI).

    Columns fall in seven groups:

    * Provider identifiers (``Rfrg_*``) — 17 cols
    * Total supplier aggregates (``Tot_*``, ``Suplr_*``) — 9 cols
    * DME-only subset (``DME_*``) — 10 cols
    * POS (prosthetics/orthotics) subset (``POS_*``) — 10 cols
    * Drug-supply subset (``Drug_*``) — 10 cols
    * Beneficiary demographics (``Bene_Avg_*``, ``Bene_*_Cnt``) — 14 cols
    * Chronic condition prevalences (``Bene_CC_*``) — 28 cols (V1/V2
      indicates which CMS codebook version was used to compute the flag)
    """

    # --- Provider identifiers (raw CMS) ---
    Rfrg_NPI: Series[str] = pa.Field(str_matches=r"^(\d{10})?$", nullable=True)
    Rfrg_Prvdr_Last_Name_Org: Series[str] = pa.Field(nullable=True)
    Rfrg_Prvdr_First_Name: Series[str] = pa.Field(nullable=True)
    Rfrg_Prvdr_MI: Series[str] = pa.Field(nullable=True)
    Rfrg_Prvdr_Crdntls: Series[str] = pa.Field(nullable=True)
    Rfrg_Prvdr_Ent_Cd: Series[str] = pa.Field(nullable=True)
    Rfrg_Prvdr_St1: Series[str] = pa.Field(nullable=True)
    Rfrg_Prvdr_St2: Series[str] = pa.Field(nullable=True)
    Rfrg_Prvdr_City: Series[str] = pa.Field(nullable=True)
    Rfrg_Prvdr_State_Abrvtn: Series[str] = pa.Field(nullable=True)
    Rfrg_Prvdr_State_FIPS: Series[str] = pa.Field(nullable=True)
    Rfrg_Prvdr_Zip5: Series[str] = pa.Field(nullable=True)
    Rfrg_Prvdr_RUCA: Series[float] = pa.Field(nullable=True)
    Rfrg_Prvdr_RUCA_Desc: Series[str] = pa.Field(nullable=True)
    Rfrg_Prvdr_Cntry: Series[str] = pa.Field(nullable=True)
    Rfrg_Prvdr_Spclty_Desc: Series[str] = pa.Field(nullable=True)
    Rfrg_Prvdr_Spclty_Srce: Series[str] = pa.Field(nullable=True)

    # --- Total supplier aggregates ---
    Tot_Suplrs: Series[int] = pa.Field(ge=0, nullable=True)
    Tot_Suplr_HCPCS_Cds: Series[int] = pa.Field(ge=0, nullable=True)
    Tot_Suplr_Benes: Series[float] = pa.Field(ge=0, nullable=True)
    Tot_Suplr_Clms: Series[int] = pa.Field(ge=0, nullable=True)
    Tot_Suplr_Srvcs: Series[int] = pa.Field(ge=0, nullable=True)
    Suplr_Sbmtd_Chrgs: Series[float] = pa.Field(ge=0, nullable=True)
    Suplr_Mdcr_Alowd_Amt: Series[float] = pa.Field(ge=0, nullable=True)
    Suplr_Mdcr_Pymt_Amt: Series[float] = pa.Field(ge=0, nullable=True)
    Suplr_Mdcr_Stdzd_Pymt_Amt: Series[float] = pa.Field(ge=0, nullable=True)

    # --- DME-only subset ---
    DME_Sprsn_Ind: Series[str] = pa.Field(nullable=True)
    DME_Tot_Suplrs: Series[float] = pa.Field(ge=0, nullable=True)
    DME_Tot_Suplr_HCPCS_Cds: Series[float] = pa.Field(ge=0, nullable=True)
    DME_Tot_Suplr_Benes: Series[float] = pa.Field(ge=0, nullable=True)
    DME_Tot_Suplr_Clms: Series[float] = pa.Field(ge=0, nullable=True)
    DME_Tot_Suplr_Srvcs: Series[float] = pa.Field(ge=0, nullable=True)
    DME_Suplr_Sbmtd_Chrgs: Series[float] = pa.Field(ge=0, nullable=True)
    DME_Suplr_Mdcr_Alowd_Amt: Series[float] = pa.Field(ge=0, nullable=True)
    DME_Suplr_Mdcr_Pymt_Amt: Series[float] = pa.Field(ge=0, nullable=True)
    DME_Suplr_Mdcr_Stdzd_Pymt_Amt: Series[float] = pa.Field(ge=0, nullable=True)

    # --- POS (prosthetics/orthotics) subset ---
    POS_Sprsn_Ind: Series[str] = pa.Field(nullable=True)
    POS_Tot_Suplrs: Series[float] = pa.Field(ge=0, nullable=True)
    POS_Tot_Suplr_HCPCS_Cds: Series[float] = pa.Field(ge=0, nullable=True)
    POS_Tot_Suplr_Benes: Series[float] = pa.Field(ge=0, nullable=True)
    POS_Tot_Suplr_Clms: Series[float] = pa.Field(ge=0, nullable=True)
    POS_Tot_Suplr_Srvcs: Series[float] = pa.Field(ge=0, nullable=True)
    POS_Suplr_Sbmtd_Chrgs: Series[float] = pa.Field(ge=0, nullable=True)
    POS_Suplr_Mdcr_Alowd_Amt: Series[float] = pa.Field(ge=0, nullable=True)
    POS_Suplr_Mdcr_Pymt_Amt: Series[float] = pa.Field(ge=0, nullable=True)
    POS_Suplr_Mdcr_Stdzd_Pymt_Amt: Series[float] = pa.Field(ge=0, nullable=True)

    # --- Drug-supply subset ---
    Drug_Sprsn_Ind: Series[str] = pa.Field(nullable=True)
    Drug_Tot_Suplrs: Series[float] = pa.Field(ge=0, nullable=True)
    Drug_Tot_Suplr_HCPCS_Cds: Series[float] = pa.Field(ge=0, nullable=True)
    Drug_Tot_Suplr_Benes: Series[float] = pa.Field(ge=0, nullable=True)
    Drug_Tot_Suplr_Clms: Series[float] = pa.Field(ge=0, nullable=True)
    Drug_Tot_Suplr_Srvcs: Series[float] = pa.Field(ge=0, nullable=True)
    Drug_Suplr_Sbmtd_Chrgs: Series[float] = pa.Field(ge=0, nullable=True)
    Drug_Suplr_Mdcr_Alowd_Amt: Series[float] = pa.Field(ge=0, nullable=True)
    Drug_Suplr_Mdcr_Pymt_Amt: Series[float] = pa.Field(ge=0, nullable=True)
    Drug_Suplr_Mdcr_Stdzd_Pymt_Amt: Series[float] = pa.Field(ge=0, nullable=True)

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
    Bene_Ndual_Cnt: Series[float] = pa.Field(ge=0, nullable=True)
    Bene_Dual_Cnt: Series[float] = pa.Field(ge=0, nullable=True)

    # --- Chronic condition prevalences ---
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


class DMEPOSScraper(BaseScraper):
    """Scrape one DMEPOS PUF year (by Referring Provider) → parquet + manifest."""

    source: ClassVar[str] = "cms_dmepos"
    schema: ClassVar[Type[BaseScraperSchema]] = DMEPOSSchema
    output_dir: ClassVar[Path] = PANELS_DIR
    schema_version: ClassVar[str] = "v1.0"

    def fetch(self, *, year: int) -> RunResult:
        if year not in AVAILABLE_YEARS:
            raise ValueError(
                f"cms_dmepos year {year} not in catalog "
                f"(have {AVAILABLE_YEARS[0]}–{AVAILABLE_YEARS[-1]})"
            )

        started = self._now()
        url = cms_url(URLS[year])
        csv_path = RAW_DIR / "cms_dmepos" / f"cms_dmepos_{year}.csv"

        _download(url, csv_path)
        # Read every schema-declared string column as text (FIPS "06"/"9C",
        # ZIP "02134", …) so pandas' type inference can't numericize a
        # zero-padded / alphanumeric code and destroy it before validation —
        # a loss no later cast can undo. See BaseScraper.string_read_dtypes
        # and data/docs/cms_dmepos.md §6a.
        df = pd.read_csv(csv_path, low_memory=False, dtype=self.string_read_dtypes())
        df = self._add_canonical_columns(df, year=year)
        df = self._stamp_base_columns(df)
        # Pre-2017 DMEPOS lacks the 25 Bene_CC_* columns. Align to the
        # canonical schema so older years materialize with NaN-filled
        # CC columns and pass strict validation uniformly.
        df = self._align_to_schema(df)

        return self._write(
            df,
            name=f"cms_dmepos_{year}",
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
        out["npi"] = out["Rfrg_NPI"].astype("int64").astype(str).str.zfill(10)
        out["provider_name"] = [
            format_provider_name(
                entity_code=ec,
                last_or_org=last,
                first=first,
                middle=mi,
            )
            for ec, last, first, mi in zip(
                out["Rfrg_Prvdr_Ent_Cd"],
                out["Rfrg_Prvdr_Last_Name_Org"],
                out["Rfrg_Prvdr_First_Name"],
                out["Rfrg_Prvdr_MI"],
            )
        ]
        out["state"] = out["Rfrg_Prvdr_State_Abrvtn"].astype("string")
        out["data_year"] = year
        return out
