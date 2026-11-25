"""Physician Compare — DAC National Downloadable File scraper.

CMS publishes the **Doctors and Clinicians National Downloadable File**
(``DAC_NationalDownloadableFile.csv``) as part of the Care Compare
program (dataset ``mj5m-pzi6``). One row per clinician *enrollment*
record — a physician practicing at three group practices / addresses has
three rows. The field we care about most is **Group PAC ID**
(``Org_PAC_ID``): the group practice's billing-entity identity,
effectively the TIN-level grouping. NPPES gives only a practice
*address*; the DAC tells you who shares a *billing entity*, which is what
kickback / shared-group analysis needs (two NPIs under the same
``Org_PAC_ID`` explain correlated volume; two NPIs at the same address
under *different* ``Org_PAC_ID``s is the suspicious pattern).

Relationship to ``fraudlib/ingest/providers/care_compare.py``
-------------------------------------------------------------
That module is the **consumer-side** (ML / ranker) reader of the same
file: it fetches a single *current* snapshot to
``data/registries/care_compare.parquet`` and collapses it to one-row-per-
NPI features (``load_care_compare_features``). This scraper is the
**pipeline-side producer**: it writes **dated, historical** snapshots at
the raw per-enrollment grain (``physician_compare_<YYYY-MM>.parquet``)
plus a most-recent ``physician_compare.parquet`` pointer. The two are
intentionally decoupled (``pipeline → fraudlib``, never the reverse).
Migrating the ranker consumer onto these dated snapshots is a separate,
future team step; nothing here touches ``care_compare.py`` or the ranker.

⚠️ Multi-row per NPI (one row per enrollment). Downstream ranker
consumers that treat ``npi`` as a row key must collapse to one-row-per-NPI
first — the same contract ``cms_partb_svc`` and ``care_compare`` live
under. See ``data/docs/physician_compare.md`` §2.

Schema drift
------------
The DAC layout has drifted across snapshots: the current file ships
``org_pac_id`` / ``num_org_mem`` / ``Facility Name`` and dropped the
``Hosp_Afl_*`` hospital-affiliation columns that older snapshots carried;
casing has wobbled (``Org_PAC_ID`` vs ``org_pac_id``). Rather than pin an
exact raw schema, this scraper **projects to a curated canonical column
set** (the fields the fraud work needs), resolving each canonical column
from a list of known source aliases case-insensitively, and pads any
column a given snapshot doesn't ship with NA. This mirrors the projection
``care_compare._csv_to_parquet`` does, kept self-contained here so the
producer never imports from ``fraudlib``.

Snapshot cadence is quarterly-ish; pull whatever historical snapshots are
available. The CMS data API only exposes the current distribution; older
snapshots come from the archived-data page
(https://data.cms.gov/provider-data/archived-data/doctors-clinicians) and
are fed in via ``url=``/``local_csv=`` with an explicit ``snapshot=``.
"""
from __future__ import annotations

import re
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
from pipeline.scrapers.download import REGISTRIES_DIR, RAW_DIR, _download


# ---------------------------------------------------------------------------
# Source URL
# ---------------------------------------------------------------------------
#
# The DAC distribution's resource-hash download path rotates every CMS
# refresh, so a hardcoded URL goes stale (404) within weeks. The dataset id
# (``mj5m-pzi6``) is stable, so we resolve the live current URL from the
# provider-data metastore at fetch time rather than pinning a URL in code.
DAC_DATASET_ID = "mj5m-pzi6"
DAC_METASTORE_URL = (
    "https://data.cms.gov/provider-data/api/1/metastore/schemas/dataset/items/"
    f"{DAC_DATASET_ID}?show-reference-ids=true"
)


def resolve_current_dac_url() -> str:
    """Resolve the current DAC CSV download URL from the CMS metastore.

    Hits the stable dataset-item endpoint and returns the first ``.csv``
    distribution's ``downloadURL``. Raises if the metastore shape changes or
    no CSV distribution is present.
    """
    r = requests.get(DAC_METASTORE_URL, timeout=60)
    r.raise_for_status()
    for dist in r.json().get("distribution", []):
        data = dist.get("data", dist)
        url = data.get("downloadURL", "")
        if url.endswith(".csv"):
            return url
    raise RuntimeError(
        f"no CSV downloadURL in DAC metastore item {DAC_DATASET_ID}; "
        "check https://data.cms.gov/provider-data/dataset/mj5m-pzi6"
    )


# ---------------------------------------------------------------------------
# Canonical column projection (absorbs DAC schema drift)
# ---------------------------------------------------------------------------
#
# Each canonical output column maps to a list of known raw source-column
# aliases, tried in order, case-insensitively. The first alias present in
# a given snapshot wins; if none are present the column is padded NA. NPI
# is handled separately (it becomes the canonical ``npi`` via
# ProviderRecordSchema). Name parts feed ``provider_name`` and are not
# kept as separate output columns.
_ALIASES: dict[str, list[str]] = {
    "Ind_PAC_ID": ["Ind_PAC_ID"],                       # stable clinician identity across job changes
    "Org_PAC_ID": ["Org_PAC_ID", "org_pac_id"],         # ← Group PAC ID (billing entity / TIN-level)
    "Org_Lgl_Nm": ["Org_Lgl_Nm", "Facility Name"],      # group practice name
    "Num_Org_Mem": ["Num_Org_Mem", "num_org_mem"],      # group practice member count
    "pri_spec": ["pri_spec"],                            # primary specialty
    "Provider_Type": ["Provider Type", "Provider_Type"], # often absent in recent snapshots
    "adr_ln_1": ["adr_ln_1"],
    "adr_ln_2": ["adr_ln_2"],
    "City": ["City/Town", "City", "cty"],
    "ZIP_Code": ["ZIP Code", "Zip Code", "zip"],
    "Hosp_Afl_1": ["Hosp_Afl_1"],                        # Hospital Affiliation CCNs — present in older snapshots only
    "Hosp_Afl_2": ["Hosp_Afl_2"],
    "Hosp_Afl_3": ["Hosp_Afl_3"],
    "Hosp_Afl_4": ["Hosp_Afl_4"],
    "Hosp_Afl_5": ["Hosp_Afl_5"],
}

# Raw source aliases used only to derive the canonical NPI / name / state.
_NPI_ALIASES = ["NPI", "npi"]
_LAST_ALIASES = ["Provider Last Name", "lst_nm", "Last Name"]
_FIRST_ALIASES = ["Provider First Name", "frst_nm", "First Name"]
_MI_ALIASES = ["Provider Middle Name", "mid_nm", "Middle Name"]
_STATE_ALIASES = ["State", "st"]


_SNAPSHOT_RE = re.compile(r"^\d{4}-\d{2}$")


def _resolve(columns: list[str], aliases: list[str]) -> Optional[str]:
    """Return the first raw column matching any alias (case-insensitive)."""
    lower = {c.lower(): c for c in columns}
    for a in aliases:
        if a.lower() in lower:
            return lower[a.lower()]
    return None


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class PhysicianCompareSchema(ProviderRecordSchema):
    """Schema for ``physician_compare_<YYYY-MM>.parquet`` (multi-row per NPI).

    NPI-keyed but **not** unique: one row per clinician enrollment
    (NPI × group practice × address). ``data_year`` is null — the DAC is
    an evergreen snapshot, not an annual cohort; ``snapshot`` carries the
    YYYY-MM snapshot date instead.

    All curated columns are nullable: CMS schema drift means any given
    snapshot may not ship every column (e.g. ``Hosp_Afl_*`` are absent
    from recent files), and the projection pads those with NA.
    """

    # --- affiliation / identity (curated, canonical) ---
    Ind_PAC_ID: Series[str] = pa.Field(nullable=True)
    Org_PAC_ID: Series[str] = pa.Field(nullable=True)       # Group PAC ID — the headline field
    Org_Lgl_Nm: Series[str] = pa.Field(nullable=True)
    Num_Org_Mem: Series[int] = pa.Field(ge=0, nullable=True)
    pri_spec: Series[str] = pa.Field(nullable=True)
    Provider_Type: Series[str] = pa.Field(nullable=True)

    # --- practice address ---
    adr_ln_1: Series[str] = pa.Field(nullable=True)
    adr_ln_2: Series[str] = pa.Field(nullable=True)
    City: Series[str] = pa.Field(nullable=True)
    ZIP_Code: Series[str] = pa.Field(nullable=True)

    # --- hospital affiliations (CCNs; older snapshots only) ---
    Hosp_Afl_1: Series[str] = pa.Field(nullable=True)
    Hosp_Afl_2: Series[str] = pa.Field(nullable=True)
    Hosp_Afl_3: Series[str] = pa.Field(nullable=True)
    Hosp_Afl_4: Series[str] = pa.Field(nullable=True)
    Hosp_Afl_5: Series[str] = pa.Field(nullable=True)

    # --- snapshot provenance ---
    snapshot: Series[str] = pa.Field(str_matches=r"^\d{4}-\d{2}$")  # YYYY-MM


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------


class PhysicianCompareScraper(BaseScraper):
    """Scrape one DAC snapshot → dated parquet + manifest + latest pointer.

    One row per clinician enrollment (~2.5M rows; ~100s MB CSV). Writes
    ``data/registries/physician_compare_<YYYY-MM>.parquet`` and, when the
    snapshot is the newest seen, (re)points the
    ``data/registries/physician_compare.parquet`` symlink at it (the same
    "most-recent pointer" convention other rolling sources use).
    """

    source: ClassVar[str] = "physician_compare"
    schema: ClassVar[Type[BaseScraperSchema]] = PhysicianCompareSchema
    output_dir: ClassVar[Path] = REGISTRIES_DIR
    schema_version: ClassVar[str] = "v1.0"

    POINTER_NAME: ClassVar[str] = "physician_compare.parquet"

    def fetch(
        self,
        *,
        snapshot: str,
        url: Optional[str] = None,
        local_csv: Optional[str | Path] = None,
        update_pointer: bool = True,
    ) -> RunResult:
        """Fetch one DAC snapshot.

        ``snapshot`` is the snapshot date as ``"YYYY-MM"`` and determines
        the output filename. ``url`` defaults to the current DAC
        distribution (resolved live via :func:`resolve_current_dac_url`);
        pass an archived URL (or ``local_csv``) for a historical snapshot.
        ``update_pointer``
        repoints ``physician_compare.parquet`` at this file when it is the
        newest snapshot on disk (backfilling an *older* snapshot leaves the
        pointer alone).
        """
        if not _SNAPSHOT_RE.match(snapshot):
            raise ValueError(f"snapshot must be 'YYYY-MM', got {snapshot!r}")

        started = self._now()
        if local_csv is not None:
            csv_path = Path(local_csv)
            src = str(csv_path)
        else:
            url = url or resolve_current_dac_url()
            csv_path = RAW_DIR / "physician_compare" / f"DAC_{snapshot}.csv"
            _download(url, csv_path)
            src = url

        # Read every column as text: our canonical column names differ from
        # the raw CSV headers, so BaseScraper.string_read_dtypes (keyed on
        # canonical names) can't pin the raw columns at read time. Reading
        # all-str protects zero-padded ZIPs and CCNs before projection — the
        # same stance care_compare._csv_to_parquet takes.
        raw = pd.read_csv(csv_path, dtype=str, low_memory=False).fillna("")
        df = self._project(raw, snapshot=snapshot)
        df = self._stamp_base_columns(df)
        df = self._align_to_schema(df)   # pad columns this snapshot didn't ship (e.g. Hosp_Afl_*)

        result = self._write(
            df,
            name=f"physician_compare_{snapshot}",
            started_at_utc=started,
            inputs={"url": src, "snapshot": snapshot},
        )
        if update_pointer:
            self._update_latest_pointer(result.artifact_path, snapshot=snapshot)
        return result

    def _project(self, raw: pd.DataFrame, *, snapshot: str) -> pd.DataFrame:
        """Project the raw DAC frame to the curated canonical columns."""
        cols = list(raw.columns)
        out = pd.DataFrame(index=raw.index)

        for canonical, aliases in _ALIASES.items():
            match = _resolve(cols, aliases)
            out[canonical] = raw[match] if match else pd.NA

        # Canonical NPI (zero-padded 10-digit string).
        npi_col = _resolve(cols, _NPI_ALIASES)
        npi = raw[npi_col].astype(str).str.strip() if npi_col else pd.Series("", index=raw.index)
        out["npi"] = npi.str.zfill(10)

        # Canonical provider_name from the DAC name parts (DAC is individual
        # clinicians → entity "I"). Build a per-row name; the file is ~2.5M
        # rows but names vary per row, so vectorize the cheap path and only
        # format distinct (last, first, mi) tuples.
        last_c = _resolve(cols, _LAST_ALIASES)
        first_c = _resolve(cols, _FIRST_ALIASES)
        mi_c = _resolve(cols, _MI_ALIASES)
        name_src = pd.DataFrame({
            "last": raw[last_c] if last_c else "",
            "first": raw[first_c] if first_c else "",
            "mi": raw[mi_c] if mi_c else "",
        })
        uniq = name_src.drop_duplicates()
        name_map = {
            (r["last"], r["first"], r["mi"]): format_provider_name(
                entity_code="I", last_or_org=r["last"], first=r["first"], middle=r["mi"]
            )
            for _, r in uniq.iterrows()
        }
        out["provider_name"] = [
            name_map[(l, f, m)] for l, f, m in zip(name_src["last"], name_src["first"], name_src["mi"])
        ]

        state_c = _resolve(cols, _STATE_ALIASES)
        out["state"] = raw[state_c].astype("string").str.strip() if state_c else pd.NA
        out["data_year"] = pd.NA           # evergreen snapshot, not an annual cohort
        out["snapshot"] = snapshot

        # Num_Org_Mem is the one numeric curated column; blanks → NA so
        # Pandera coerces the rest to nullable int.
        out["Num_Org_Mem"] = pd.to_numeric(
            out["Num_Org_Mem"].replace("", pd.NA), errors="coerce"
        )
        return out

    def _update_latest_pointer(self, artifact_path: Path, *, snapshot: str) -> None:
        """Repoint ``physician_compare.parquet`` at ``artifact_path`` when
        ``snapshot`` is the newest on disk.

        The pointer is a *relative* symlink (target = filename only) so it
        survives directory moves and rclone sync to R2 (rclone copies the
        link target). Backfilling an older snapshot than the current
        pointer leaves it untouched.
        """
        pointer = self.output_dir / self.POINTER_NAME
        newest = self._latest_snapshot_on_disk()
        if newest is not None and newest > snapshot:
            return  # a newer snapshot already owns the pointer
        if pointer.exists() or pointer.is_symlink():
            pointer.unlink()
        pointer.symlink_to(artifact_path.name)

    def _latest_snapshot_on_disk(self) -> Optional[str]:
        """Newest ``YYYY-MM`` among existing dated snapshot parquets."""
        snaps = sorted(
            p.stem.replace("physician_compare_", "")
            for p in self.output_dir.glob("physician_compare_*.parquet")
        )
        return snaps[-1] if snaps else None
