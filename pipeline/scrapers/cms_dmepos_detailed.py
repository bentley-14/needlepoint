"""DMEPOS by Referring Provider & Service scraper ("Detailed" PUF).

Wraps the CMS "Medicare Durable Medical Equipment, Devices & Supplies —
by Referring Provider and Service" PUF in the :class:`BaseScraper`
contract so the produced parquet carries the canonical stamp columns
(``source``, ``fetched_at_utc``, ``schema_version``), the canonical NPI
columns from :class:`ProviderRecordSchema` (``npi``, ``provider_name``,
``state``, ``data_year``), and a sibling ``.manifest.json``.

This is the **per-(referring NPI × HCPCS × supplier-rental-indicator)
cut** of the DMEPOS data. The aggregated cut we already ship
(``cms_dmepos`` — one row per referring NPI per year, with DME / POS /
Drug category rollups) tells you a provider ordered, say, $612k of DME
but not *what*. This detailed cut blows that out to one row per HCPCS
code, surfacing the specific items a referring provider ordered —
A-codes (medical supplies), E-codes (durable equipment: wheelchairs,
hospital beds), K-codes (DMERC temporary), L-codes (orthotics). That is
the grain at which a brace-mill / wheelchair-mill scheme is legible
(e.g. one family-practice MD referring 10,000 K0001 standard
wheelchairs).

⚠️ Each row is one (Rfrg_NPI × HCPCS_CD × Suplr_Rentl_Ind) tuple, NOT one
row per NPI. The referring NPI repeats once per distinct HCPCS code (and
rental indicator) the provider ordered. ``ProviderRecordSchema`` declares
``npi: str`` but does not assert uniqueness — multi-row-per-NPI parquets
are legal under it. Downstream ranker consumers that treat ``npi`` as a
row key must aggregate to one-row-per-NPI first (the same contract that
``cms_partb_svc`` lives under). See ``data/docs/cms_dmepos_detailed.md`` §2.

Dollar columns are ``Avg_*`` (per-service averages), NOT totals — to get
total Medicare paid for a (NPI, HCPCS) row, multiply
``Avg_Suplr_Mdcr_Pymt_Amt * Tot_Suplr_Srvcs``. Same gotcha as
``cms_partb_svc``; downstream feature code derives the total rather than
reading it directly.

CMS replaced the old BETOS taxonomy with the **Restructured BETOS
Classification System (RBCS)**; this PUF ships ``RBCS_Lvl`` / ``RBCS_Id``
/ ``RBCS_Desc`` rather than a ``BETOS_Cd`` column. The HCPCS column is
published as ``HCPCS_CD`` (upper-case ``CD``), unlike ``cms_partb_svc``'s
``HCPCS_Cd`` — both casings are preserved exactly as published.

Years covered: 2014–2023 (matches our ``cms_dmepos`` aggregated panel
coverage; CMS's DMEPOS referring-provider series starts at DY2014).
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
# Direct-CSV permalinks resolved from CMS's data.json catalog (probe
# 2026-06-10). Stored as relative paths under
# ``pipeline.scrapers.download.CMS_FILES_BASE``; :func:`cms_url` does the
# join. URLs rotate per RY release; update by re-scraping the data.json
# catalog (dataset id 86b4807a-d63a-44be-bfdf-ffd398d5e623) and pasting
# either the relative tail (canonical) or the full URL (helper accepts
# both). All ten years were published under the RY25 release, so the
# column set is homogeneous across the series (2023 shipped at V10, the
# rest at V20; identical 33-column header).
URLS: dict[int, str] = {
    2023: "2025-06/959bcd27-4371-416d-bf8a-9d0537359a41/mup_dme_ry25_p05_v10_dy23_rfrhpr.csv",
    2022: "2025-11/966e2f4d-a7e0-4347-8b15-eb04f54d0a7f/mup_dme_ry25_p05_v20_dy22_rfrhpr.csv",
    2021: "2025-11/51c0118b-2d23-4289-a9cf-4f0b5338290f/mup_dme_ry25_p05_v20_dy21_rfrhpr.csv",
    2020: "2025-11/2b688191-01aa-47b3-913c-f41528db9985/mup_dme_ry25_p05_v20_dy20_rfrhpr.csv",
    2019: "2025-11/cc1e7411-049a-4431-ae0e-05b849971e14/mup_dme_ry25_p05_v20_dy19_rfrhpr.csv",
    2018: "2025-11/28b8d437-63b7-41db-81df-27888404b3ae/mup_dme_ry25_p05_v20_dy18_rfrhpr.csv",
    2017: "2025-11/e5524438-dd1c-4e8c-b4d6-6da01adb4def/mup_dme_ry25_p05_v20_dy17_rfrhpr.csv",
    2016: "2025-11/89f17392-4ff7-4952-ab34-cede8ec1ef45/mup_dme_ry25_p05_v20_dy16_rfrhpr.csv",
    2015: "2025-11/c1b632a4-5b7c-4edf-acf1-319c76bfccb1/mup_dme_ry25_p05_v20_dy15_rfrhpr.csv",
    2014: "2025-11/d95bdfff-d966-453f-9532-29aaa1055cba/mup_dme_ry25_p05_v20_dy14_rfrhpr.csv",
}


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
#
# The 33 raw CMS column names below are preserved exactly as published in
# the RY25 release. Strict validation (``Config.strict``) requires the
# produced parquet's columns to match this set exactly — if CMS renames a
# column upstream, the parser must rename back to the canonical name
# (declared here) so downstream consumers don't break.
#
# ``Suplr_Rentl_Ind`` ("Y"/"N") splits each (NPI, HCPCS) into rented vs
# purchased rows — it is part of the row grain, which is why a single
# HCPCS code can appear twice for one referring provider.


class DMEPOSDetailedSchema(ProviderRecordSchema):
    """Schema for ``cms_dmepos_detailed_<year>.parquet`` (multi-row per NPI).

    Columns fall in four groups:

    * Referring-provider identifiers (``Rfrg_*``) — 19 cols, repeated per
      row of a given NPI (denormalized into every HCPCS row)
    * RBCS taxonomy — 3 cols (replaces the legacy BETOS classification)
    * HCPCS / rental identifiers — 3 cols
    * Per-(NPI, HCPCS, rental) aggregates — 8 cols (4 counts + 4 dollar
      averages)

    Dtype contract
    --------------
    ``Rfrg_NPI`` is declared ``str`` with the same ``^(\\d{10})?$`` check
    as the canonical ``npi``: a float that reaches it coerces to
    ``"1900000075.0"`` and FAILS validation loudly rather than shipping
    garbage. The parser reads NPI as int upstream (int→str coerces clean).

    Dollar averages (``Avg_*``) and count columns (``Tot_*``) carry
    ``ge=0`` and ``nullable=True`` — CMS redacts (blanks) any element
    representing fewer than 11 claims to protect beneficiary privacy, so a
    suppressed count/amount arrives null rather than zero.
    """

    # --- Referring-provider identifiers (raw CMS) ---
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
    Rfrg_Prvdr_RUCA_Cat: Series[str] = pa.Field(nullable=True)
    Rfrg_Prvdr_RUCA: Series[str] = pa.Field(nullable=True)
    Rfrg_Prvdr_RUCA_Desc: Series[str] = pa.Field(nullable=True)
    Rfrg_Prvdr_Cntry: Series[str] = pa.Field(nullable=True)
    Rfrg_Prvdr_Spclty_Cd: Series[str] = pa.Field(nullable=True)
    Rfrg_Prvdr_Spclty_Desc: Series[str] = pa.Field(nullable=True)
    Rfrg_Prvdr_Spclty_Srce: Series[str] = pa.Field(nullable=True)

    # --- RBCS taxonomy (Restructured BETOS Classification System) ---
    RBCS_Lvl: Series[str] = pa.Field(nullable=True)
    RBCS_Id: Series[str] = pa.Field(nullable=True)
    RBCS_Desc: Series[str] = pa.Field(nullable=True)

    # --- HCPCS / rental identifiers ---
    HCPCS_CD: Series[str] = pa.Field(nullable=True)          # note: upper-case CD, unlike cms_partb_svc's HCPCS_Cd
    HCPCS_Desc: Series[str] = pa.Field(nullable=True)
    Suplr_Rentl_Ind: Series[str] = pa.Field(nullable=True)   # "Y" rented / "N" purchased — part of the row grain

    # --- Per-(NPI, HCPCS, rental) aggregates ---
    Tot_Suplrs: Series[int] = pa.Field(ge=0, nullable=True)
    Tot_Suplr_Benes: Series[int] = pa.Field(ge=0, nullable=True)
    Tot_Suplr_Clms: Series[float] = pa.Field(ge=0, nullable=True)
    Tot_Suplr_Srvcs: Series[float] = pa.Field(ge=0, nullable=True)
    Avg_Suplr_Sbmtd_Chrg: Series[float] = pa.Field(ge=0, nullable=True)
    Avg_Suplr_Mdcr_Alowd_Amt: Series[float] = pa.Field(ge=0, nullable=True)
    Avg_Suplr_Mdcr_Pymt_Amt: Series[float] = pa.Field(ge=0, nullable=True)
    Avg_Suplr_Mdcr_Stdzd_Amt: Series[float] = pa.Field(ge=0, nullable=True)


AVAILABLE_YEARS = tuple(sorted(URLS))


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------


class DMEPOSDetailedScraper(BaseScraper):
    """Scrape one DMEPOS detailed PUF year (by Referring Provider &
    Service) → parquet + manifest.

    Multi-row per NPI. Smaller than ``cms_partb_svc`` (DME referrals are a
    narrower universe than all physician services) but still multi-GB CSV
    in / hundreds-of-MB parquet out. ``BaseScraper._write()`` does a single
    in-memory ``df.to_parquet`` call, so peak RAM is on the order of the
    CSV size — run the backfill on a CI runner, not the dev box. See the
    dev-machine note in the data-pipeline docs.
    """

    source: ClassVar[str] = "cms_dmepos_detailed"
    schema: ClassVar[Type[BaseScraperSchema]] = DMEPOSDetailedSchema
    output_dir: ClassVar[Path] = PANELS_DIR
    schema_version: ClassVar[str] = "v1.0"

    def fetch(self, *, year: int) -> RunResult:
        if year not in AVAILABLE_YEARS:
            raise ValueError(
                f"cms_dmepos_detailed year {year} not in catalog "
                f"(have {AVAILABLE_YEARS[0]}–{AVAILABLE_YEARS[-1]})"
            )

        started = self._now()
        url = cms_url(URLS[year])
        csv_path = RAW_DIR / "cms_dmepos_detailed" / f"cms_dmepos_detailed_{year}.csv"

        _download(url, csv_path)
        # Read every schema-declared string column as text (FIPS "06",
        # ZIP "02134", RBCS ids, …) so pandas' type inference can't
        # numericize a zero-padded / alphanumeric code and destroy it
        # before validation. See BaseScraper.string_read_dtypes.
        df = pd.read_csv(csv_path, low_memory=False, dtype=self.string_read_dtypes())
        df = self._add_canonical_columns(df, year=year)
        df = self._stamp_base_columns(df)
        # All ten years ship under the RY25 release with a homogeneous
        # 33-column header, but pad defensively in case an older year drops
        # a column — strict _validate still surfaces any *unknown* column.
        df = self._align_to_schema(df)

        return self._write(
            df,
            name=f"cms_dmepos_detailed_{year}",
            started_at_utc=started,
            inputs={"url": url, "local_csv": str(csv_path)},
        )

    @staticmethod
    def _add_canonical_columns(df: pd.DataFrame, *, year: int) -> pd.DataFrame:
        """Populate the ``ProviderRecordSchema`` canonical NPI columns.

        At millions of rows per year, the naive per-row
        ``format_provider_name`` call is too slow. We build the per-NPI
        name lookup once on the unique-NPI frame, then map it back onto the
        full multi-row frame — the same approach ``cms_partb_svc`` uses.
        """
        out = df.copy()
        out["npi"] = out["Rfrg_NPI"].astype("int64").astype(str).str.zfill(10)

        name_cols = [
            "Rfrg_NPI",
            "Rfrg_Prvdr_Ent_Cd",
            "Rfrg_Prvdr_Last_Name_Org",
            "Rfrg_Prvdr_First_Name",
            "Rfrg_Prvdr_MI",
        ]
        unique_providers = (
            out[name_cols].drop_duplicates(subset=["Rfrg_NPI"]).set_index("Rfrg_NPI")
        )
        name_map = {
            npi: format_provider_name(
                entity_code=row["Rfrg_Prvdr_Ent_Cd"],
                last_or_org=row["Rfrg_Prvdr_Last_Name_Org"],
                first=row["Rfrg_Prvdr_First_Name"],
                middle=row["Rfrg_Prvdr_MI"],
            )
            for npi, row in unique_providers.iterrows()
        }
        out["provider_name"] = out["Rfrg_NPI"].map(name_map)
        out["state"] = out["Rfrg_Prvdr_State_Abrvtn"].astype("string")
        out["data_year"] = year
        return out
