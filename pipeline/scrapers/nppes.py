"""NPPES bulk-registry scraper.

NPPES — National Plan and Provider Enumeration System — is CMS's registry
of every healthcare provider with an NPI. Unlike the annual Part B / Part
D / DMEPOS PUFs, NPPES is an **evergreen monthly snapshot**:

* Not year-keyed. ``data_year`` is always ``None`` on every row.
* Published at a different host (``download.cms.gov/nppes``) — the
  ``CMS_FILES_BASE`` prefix used for the PUFs doesn't apply.
* Output lands in ``data/registries/`` rather than ``data/panels/``.

The raw monthly zip is ~2 GB compressed; the extracted ``npidata_pfile_*.csv``
is ~7 GB (~7M individuals + ~2M organizations × ~330 columns). For the
fraud pipeline we project the file down to 13 columns (~189 MB
parquet) — the ones that turn into ranker features or feed the
cross-source mismatch signals documented in :doc:`/data/docs/nppes`.

Schema-version policy
---------------------
NPPES is the first non-annual source on ``BaseScraper``. Bump
``schema_version``:

* **minor** when CMS adds a column to the monthly file (the canonical
  projection grows but old consumers still work).
* **major** when CMS renames/removes a column in the projection, or when
  we change the semantics of a derived column (e.g. switching ``state``
  from practice-location to mailing).
* Don't bump when the parquet's CMS-side row count drifts month-over-
  month — that's data motion, not schema motion.
"""
from __future__ import annotations

import datetime as _dt
import re
import zipfile
from pathlib import Path
from typing import Any, ClassVar, Optional, Type

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
from pipeline.scrapers.download import RAW_DIR, REGISTRIES_DIR


# ---------------------------------------------------------------------------
# URL catalog
# ---------------------------------------------------------------------------
#
# NPPES publishes one zip per month at a predictable URL. CMS rolls the
# monthly file out around the second week, so a fetch on the 1st of the
# month commonly hits 404 on the current-month URL — we probe the
# current month, then fall back through the prior two.
#
# URL template lives on the class as :func:`NppesScraper._candidate_urls`
# so it can be exercised in tests without spinning up the network. There
# is no year-keyed dict like the PUF scrapers carry; the "catalog" here
# is the single template + the rolling probe.

NPPES_BASE = "https://download.cms.gov/nppes/"
_BULK_URL_FMT = NPPES_BASE + "NPPES_Data_Dissemination_{month}_{year}.zip"


# ---------------------------------------------------------------------------
# Projection: raw column → snake_case schema column
# ---------------------------------------------------------------------------
#
# NPPES ships ~330 columns. We project down to the ones that earn their
# place in either a ranker feature or a cross-source mismatch check.
#
# Schema-expansion log (when to grow the projection):
#
# * v1.0 (2026-05-30) — initial 10 cols from the existing
#   ``nppes_ingest.py`` projection + 3 new cols:
#     - ``Provider_Business_Practice_Location_Address_State`` — needed to
#       populate the canonical ``state`` column. Without this, every
#       NPPES row had ``state=None``, breaking the state-keyed joins the
#       Part B/D scrapers rely on.
#     - ``NPI_Deactivation_Reason_Code`` / ``NPI_Deactivation_Date`` —
#       needed for the "deactivated NPI still billing" fraud signal
#       (cross-checked against the most-recent PUF year; see
#       ``data/docs/nppes.md`` §4).
#
# Adding more columns (replacement NPI, taxonomy 2–15, secondary
# practice-location address, etc.) is a minor schema bump — append to
# ``_KEEP_COLUMNS`` + the schema dataclass in one commit.

_KEEP_COLUMNS: list[str] = [
    "NPI",
    "Entity Type Code",
    "Provider Organization Name (Legal Business Name)",
    "Provider Last Name (Legal Name)",
    "Provider First Name",
    "Provider Credential Text",
    "Healthcare Provider Taxonomy Code_1",
    "Is Sole Proprietor",
    "Provider Enumeration Date",
    "Last Update Date",
    # --- v1.0 expansions on top of the legacy nppes_ingest projection ---
    "Provider Business Practice Location Address State Name",
    "NPI Deactivation Reason Code",
    "NPI Deactivation Date",
]

# Normalized snake_case names used in the parquet. Note that ``NPI`` keeps
# its all-caps form because that's how every consumer of the legacy
# nppes_ingest projection already addresses it; the strict-validation
# schema below declares ``NPI`` (not ``npi``) for the raw column and
# ``npi`` for the canonical zero-padded join key.
_RENAMES: dict[str, str] = {
    "NPI": "NPI",
    "Entity Type Code": "Entity_Type_Code",
    "Provider Organization Name (Legal Business Name)": "Provider_Organization_Name",
    "Provider Last Name (Legal Name)": "Provider_Last_Name_Legal_Name",
    "Provider First Name": "Provider_First_Name",
    "Provider Credential Text": "Provider_Credential_Text",
    "Healthcare Provider Taxonomy Code_1": "Healthcare_Provider_Taxonomy_Code_1",
    "Is Sole Proprietor": "Is_Sole_Proprietor",
    "Provider Enumeration Date": "Enumeration_Date",
    "Last Update Date": "Last_Update_Date",
    "Provider Business Practice Location Address State Name": "Provider_Business_Practice_Location_Address_State",
    "NPI Deactivation Reason Code": "NPI_Deactivation_Reason_Code",
    "NPI Deactivation Date": "NPI_Deactivation_Date",
}


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class NppesSchema(ProviderRecordSchema):
    """Schema for ``data/registries/nppes_bulk.parquet``.

    Total declared shape: **19 columns** = 12 raw NPPES (projected from
    the ~330 columns published in the monthly zip) + 4 canonical NPI
    columns from :class:`ProviderRecordSchema` (``npi``, ``provider_name``,
    ``state``, ``data_year``) + 3 base stamps from :class:`BaseScraperSchema`
    (``source``, ``fetched_at_utc``, ``schema_version``).

    Column groups
    -------------
    * **Identifier:** the canonical zero-padded string ``npi`` is the only
      NPI column. The raw source ``NPI`` is read to derive ``npi`` but is
      **not** emitted (v2.0): it carried no information ``npi`` lacks (NPIs
      never lead with a zero), and keeping both ``NPI`` and ``npi`` collided
      under case-insensitive SQL engines — DuckDB surfaced the canonical
      column as ``npi_1``, so ``SELECT npi`` silently bound to the raw int.
      See :meth:`NppesScraper._add_canonical_columns`.
    * **Entity:** ``Entity_Type_Code`` — ``"1"`` for individual, ``"2"``
      for organization. **Not** ``"I"`` / ``"O"`` like the PUFs — see
      :meth:`NppesScraper._add_canonical_columns` for the mapping.
    * **Identity:** organization name (for entity "2") or last/first
      name + credential text (for entity "1").
    * **Specialty:** ``Healthcare_Provider_Taxonomy_Code_1`` — primary
      NUCC taxonomy. NPPES allows up to 15 taxonomies per NPI; we
      project only the first.
    * **Sole-prop / structural:** ``Is_Sole_Proprietor`` — ``"Y"``/``"N"``/``"X"``.
    * **Dates:** ``Enumeration_Date`` (when NPI was issued — phantom-
      billing signal), ``Last_Update_Date``, plus the deactivation pair
      ``NPI_Deactivation_Date`` + ``NPI_Deactivation_Reason_Code``
      (``DT`` = death, ``FR`` = fraud, ``OT`` = other, ``RR`` = reassigned).
    * **State:** ``Provider_Business_Practice_Location_Address_State`` —
      practice-location state name (the full name, e.g. ``"CALIFORNIA"``);
      the canonical ``state`` column carries the 2-letter abbreviation
      we look up from this.

    Choices made in v1.0 (see PR description + ``data/docs/nppes.md``):

    * **state** is populated from *practice-location*, not mailing. CMS
      requires the practice-location field for individual providers;
      orgs may have it blank, in which case ``state`` stays ``None``.
    * **Taxonomy 2–15** are not projected. Most NPIs only declare one
      taxonomy and the secondary entries are rarely fraud-relevant.
      Expand if a downstream consumer needs cross-taxonomy comparison.
    * **Replacement NPI** column is not projected — covers the rare
      reassign-after-fraud case but adds 1.5 GB to the read.
    """

    # --- Identifier ---
    # The raw source ``NPI`` is intentionally NOT declared here: it is read
    # from the CSV to derive the canonical ``npi`` (a clean zero-padded
    # string, inherited from ProviderRecordSchema) and then dropped before
    # validation. ``Config.strict`` therefore rejects any stray ``NPI``
    # column, keeping ``npi`` the single, unambiguous join key across every
    # engine (no ``NPI``/``npi`` case-collision).

    # --- Entity / identity ---
    Entity_Type_Code: Series[str] = pa.Field(nullable=True)
    Provider_Organization_Name: Series[str] = pa.Field(nullable=True)
    Provider_Last_Name_Legal_Name: Series[str] = pa.Field(nullable=True)
    Provider_First_Name: Series[str] = pa.Field(nullable=True)
    Provider_Credential_Text: Series[str] = pa.Field(nullable=True)

    # --- Specialty / structural ---
    Healthcare_Provider_Taxonomy_Code_1: Series[str] = pa.Field(nullable=True)
    Is_Sole_Proprietor: Series[str] = pa.Field(nullable=True)

    # --- Dates ---
    Enumeration_Date: Series[str] = pa.Field(nullable=True)
    Last_Update_Date: Series[str] = pa.Field(nullable=True)
    NPI_Deactivation_Reason_Code: Series[str] = pa.Field(nullable=True)
    NPI_Deactivation_Date: Series[str] = pa.Field(nullable=True)

    # --- Address ---
    Provider_Business_Practice_Location_Address_State: Series[str] = pa.Field(nullable=True)


# Full state-name → 2-letter abbreviation lookup. NPPES stores the full
# upper-case name in the practice-location-state column (e.g.
# "CALIFORNIA"); the canonical ``state`` field is the 2-letter form.
_STATE_NAME_TO_ABBR: dict[str, str] = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR",
    "CALIFORNIA": "CA", "COLORADO": "CO", "CONNECTICUT": "CT", "DELAWARE": "DE",
    "DISTRICT OF COLUMBIA": "DC", "FLORIDA": "FL", "GEORGIA": "GA", "HAWAII": "HI",
    "IDAHO": "ID", "ILLINOIS": "IL", "INDIANA": "IN", "IOWA": "IA",
    "KANSAS": "KS", "KENTUCKY": "KY", "LOUISIANA": "LA", "MAINE": "ME",
    "MARYLAND": "MD", "MASSACHUSETTS": "MA", "MICHIGAN": "MI", "MINNESOTA": "MN",
    "MISSISSIPPI": "MS", "MISSOURI": "MO", "MONTANA": "MT", "NEBRASKA": "NE",
    "NEVADA": "NV", "NEW HAMPSHIRE": "NH", "NEW JERSEY": "NJ", "NEW MEXICO": "NM",
    "NEW YORK": "NY", "NORTH CAROLINA": "NC", "NORTH DAKOTA": "ND", "OHIO": "OH",
    "OKLAHOMA": "OK", "OREGON": "OR", "PENNSYLVANIA": "PA", "RHODE ISLAND": "RI",
    "SOUTH CAROLINA": "SC", "SOUTH DAKOTA": "SD", "TENNESSEE": "TN", "TEXAS": "TX",
    "UTAH": "UT", "VERMONT": "VT", "VIRGINIA": "VA", "WASHINGTON": "WA",
    "WEST VIRGINIA": "WV", "WISCONSIN": "WI", "WYOMING": "WY",
    # Territories — Medicare-eligible
    "PUERTO RICO": "PR", "VIRGIN ISLANDS": "VI", "GUAM": "GU",
    "AMERICAN SAMOA": "AS", "NORTHERN MARIANA ISLANDS": "MP",
    # Military / federal
    "ARMED FORCES AMERICAS": "AA", "ARMED FORCES EUROPE": "AE",
    "ARMED FORCES PACIFIC": "AP",
}

# Valid 2-letter codes for the pass-through path in `_add_canonical_columns`.
# CMS publishes the abbreviated form in the practice-location-state column
# ~96% of the time despite the header being named "State Name".
_VALID_STATE_ABBRS: frozenset[str] = frozenset(_STATE_NAME_TO_ABBR.values())


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------


class NppesScraper(BaseScraper):
    """Scrape one NPPES monthly snapshot → parquet + manifest.

    Unlike the PUF scrapers, ``fetch()`` takes no required argument:
    the default is "most recent month available", probed via
    :meth:`_candidate_urls`. Pass ``month=`` / ``year=`` to pin a
    specific snapshot (e.g. for regression).
    """

    source: ClassVar[str] = "nppes"
    schema: ClassVar[Type[BaseScraperSchema]] = NppesSchema
    output_dir: ClassVar[Path] = REGISTRIES_DIR
    # v2.0: dropped the raw ``NPI`` column (major bump — column removed).
    # Canonical ``npi`` is now the only NPI column. See NppesSchema docstring.
    schema_version: ClassVar[str] = "v2.0"

    # Cap on the compressed zip size — abort and fall back to per-NPI API
    # enrichment (``fraudlib.labels.nppes_lookup.lookup``) above this.
    max_zip_size_gb: ClassVar[float] = 3.0

    # ---- fetch entrypoint --------------------------------------------------

    def fetch(self, *, month: Optional[str] = None, year: Optional[int] = None) -> RunResult:
        started = self._now()

        if month is not None and year is not None:
            candidates = [_BULK_URL_FMT.format(month=month, year=year)]
        elif month is None and year is None:
            candidates = self._candidate_urls()
        else:
            raise ValueError("pass both month= and year= or neither (got mixed)")

        zip_url, content_length = self._resolve_url(candidates)
        size_gb = content_length / (1024 ** 3) if content_length else 0.0
        if content_length and size_gb > self.max_zip_size_gb:
            raise ValueError(
                f"NPPES bulk at {zip_url} is {size_gb:.2f} GB compressed "
                f"(> {self.max_zip_size_gb} GB cap). Fall back to per-NPI "
                f"enrichment via fraudlib.labels.nppes_lookup.lookup."
            )

        raw_dir = RAW_DIR / "nppes"
        raw_dir.mkdir(parents=True, exist_ok=True)
        zip_path = raw_dir / Path(zip_url).name
        self._download_zip(zip_url, zip_path)

        df = self._extract_and_project(zip_path)
        df = self._add_canonical_columns(df)
        df = self._stamp_base_columns(df)

        return self._write(
            df,
            name="nppes_bulk",
            started_at_utc=started,
            inputs={"url": zip_url, "local_zip": str(zip_path)},
        )

    # ---- URL probing -------------------------------------------------------

    @staticmethod
    def _candidate_urls(today: Optional[_dt.date] = None) -> list[str]:
        """Return candidate monthly URLs — current month first, prior two as fallback.

        CMS publishes ~2nd week of each month, so at month-start the
        current month's file commonly doesn't exist yet. We probe back
        three months total before giving up.
        """
        today = today or _dt.date.today()
        urls = []
        for offset in range(0, 3):
            y, m = today.year, today.month - offset
            while m <= 0:
                m += 12
                y -= 1
            month_name = _dt.date(y, m, 1).strftime("%B")
            # CMS started appending "_V2" to the monthly filename in 2026.
            # Probe the V2 form first, then the legacy bare form, so we
            # cover both the current convention and any future revert.
            urls.append(_BULK_URL_FMT.format(month=month_name, year=y).replace(".zip", "_V2.zip"))
            urls.append(_BULK_URL_FMT.format(month=month_name, year=y))
        return urls

    def _resolve_url(self, candidates: list[str]) -> tuple[str, int]:
        """HEAD-probe each candidate; return the first 200 + its Content-Length.

        Raises ``RuntimeError`` if none of the candidates resolve. Network
        errors on individual candidates are swallowed (the next candidate
        is tried), but a top-level "nothing reachable" outcome is fatal.
        """
        last_err: Optional[Exception] = None
        for url in candidates:
            try:
                head = requests.head(url, timeout=15, allow_redirects=True)
                if head.status_code == 200:
                    content_length = int(head.headers.get("Content-Length", 0))
                    return url, content_length
            except requests.RequestException as exc:
                last_err = exc
                continue
        raise RuntimeError(
            "No NPPES monthly zip reachable at any candidate URL. "
            "Check https://download.cms.gov/nppes/NPI_Files.html for a "
            f"filename drift. Candidates tried: {candidates!r}. "
            f"Last error: {last_err!r}"
        )

    # ---- raw download ------------------------------------------------------

    @staticmethod
    def _download_zip(url: str, dest: Path) -> Path:
        """Streamed download of the ~2 GB NPPES zip. Idempotent on existing files."""
        if dest.exists() and dest.stat().st_size > 0:
            return dest
        tmp = dest.with_suffix(dest.suffix + ".part")
        with requests.get(url, stream=True, timeout=600) as r:
            r.raise_for_status()
            with tmp.open("wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 20):  # 1 MB
                    if chunk:
                        f.write(chunk)
        tmp.rename(dest)
        return dest

    # ---- CSV extract + projection ------------------------------------------

    def _extract_and_project(self, zip_path: Path) -> pd.DataFrame:
        """Stream-read the main pfile CSV, project to ``_KEEP_COLUMNS``, rename.

        The zip contains several pfiles (endpoint, othername, pl) — we
        match only ``npidata_pfile_YYYYMMDD-YYYYMMDD.csv`` (the main one)
        via the strict regex below.

        Chunked at 500k rows to bound peak memory below ~600 MB on the
        full file. The projection is applied at chunk boundary so we
        never materialize the ~7 GB CSV in memory.
        """
        with zipfile.ZipFile(zip_path) as zf:
            main_csv = next(
                (n for n in zf.namelist()
                 if re.match(r"npidata_pfile_\d{8}-\d{8}\.csv$", n)),
                None,
            )
            if not main_csv:
                raise RuntimeError(
                    f"Main npidata pfile CSV not found in {zip_path}. "
                    f"Archive contents: {zf.namelist()[:10]}"
                )

            # Sniff header to detect column-name drift. NPPES has been
            # stable for years but a future column rename should fail
            # loudly here rather than producing silently-empty cols.
            with zf.open(main_csv) as handle:
                header = pd.read_csv(handle, nrows=0)
            present = [c for c in _KEEP_COLUMNS if c in header.columns]
            missing = sorted(set(_KEEP_COLUMNS) - set(present))
            if missing:
                raise RuntimeError(
                    f"NPPES schema drift: missing expected columns {missing}. "
                    f"Found header has {len(header.columns)} columns. "
                    f"If CMS renamed a column, update _KEEP_COLUMNS + _RENAMES."
                )

            with zf.open(main_csv) as handle:
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

        return thin.rename(columns=_RENAMES)

    # ---- canonical NPI column population -----------------------------------

    @staticmethod
    def _add_canonical_columns(df: pd.DataFrame) -> pd.DataFrame:
        """Populate the ``ProviderRecordSchema`` canonical NPI columns.

        NPPES quirks vs. the PUFs:

        * ``Entity_Type_Code`` is ``"1"``/``"2"`` here, not ``"I"``/``"O"`` —
          we map before calling :func:`format_provider_name`. Rows with
          a blank entity code (rare; deactivated NPIs) are treated as
          individual.
        * ``state`` comes from ``Provider_Business_Practice_Location_Address_State``.
          Despite the raw column being named ``... State Name`` by CMS,
          ~96% of populated rows already hold the 2-letter abbreviation
          (e.g. ``"CA"``); only a handful (<0.01%) hold the full name
          (``"CALIFORNIA"``). We pass through valid 2-letter codes
          first, then fall back to :data:`_STATE_NAME_TO_ABBR` lookup
          for the long-name minority. Anything not in either path
          (foreign addresses, blanks) becomes ``None``.
        * ``data_year`` is always ``None`` — NPPES is an evergreen
          snapshot, not an annual cohort.
        """
        out = df.copy()

        # NPI — read the source ``NPI`` column and derive ONLY the canonical
        # zero-padded string ``npi``. NPPES occasionally ships a blank NPI for
        # fully-deactivated entries; coerce errors to NaN, then zero-pad the
        # survivors. We then DROP the raw ``NPI`` (v2.0): it duplicates ``npi``
        # value-for-value (NPIs never lead with a zero) and keeping both
        # collides under case-insensitive SQL engines. ``npi`` is the sole key.
        npi_numeric = pd.to_numeric(out["NPI"], errors="coerce")
        out["npi"] = npi_numeric.astype("Int64").astype("string").str.zfill(10)
        out = out.drop(columns=["NPI"])

        # Entity code mapping: NPPES "1" → "I" (individual), "2" → "O" (org).
        # Rows with a blank/NaN Entity_Type_Code (~3.6% of the registry —
        # mostly fully-deactivated entries) become ``None`` here so
        # ``format_provider_name`` short-circuits cleanly. pandas' default
        # NaN-fill for an unmapped value is a float NaN, which the helper
        # can't ``.strip()``; replace explicitly.
        _entity_lookup = {"1": "I", "2": "O"}
        entity_mapped = [_entity_lookup.get(v) for v in out["Entity_Type_Code"]]

        out["provider_name"] = [
            format_provider_name(
                entity_code=ec,
                last_or_org=last if ec == "O" else last_indiv,
                first=first,
                middle=None,  # NPPES doesn't carry MI in the projection
            )
            for ec, last, last_indiv, first in zip(
                entity_mapped,
                out["Provider_Organization_Name"],
                out["Provider_Last_Name_Legal_Name"],
                out["Provider_First_Name"],
            )
        ]

        state_raw = out["Provider_Business_Practice_Location_Address_State"].fillna("").str.upper().str.strip()
        state_pass = state_raw.where(state_raw.isin(_VALID_STATE_ABBRS))
        state_lookup = state_raw.map(_STATE_NAME_TO_ABBR)
        out["state"] = state_pass.fillna(state_lookup).replace("", pd.NA).astype("string")

        out["data_year"] = pd.Series([pd.NA] * len(out), dtype="Int64")
        return out
