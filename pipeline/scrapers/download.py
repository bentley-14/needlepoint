"""Idempotent fetchers for CMS PUF + LEIE datasets.

Every fetcher:
  * writes raw artifacts under ``data/raw/<source>/``
  * writes a parquet snapshot under one of:
      - ``data/panels/<source>_<year>.parquet``      (annual CMS panels)
      - ``data/registries/<source>.parquet``         (non-annual registries)
      - ``data/legacy/leie/leie[_<year>].parquet``   (LEIE; retired from active pipeline)
  * skips the download if the parquet already exists (idempotent)
  * returns a ``pandas.DataFrame``

Direct-CSV permalinks resolved from CMS's ``data.json`` catalog. Covers
years 2013–2023 for Part B (by Provider and by Provider & Service), Part D
(by Provider and by Provider & Drug), and DMEPOS (by Referring Provider
and by Supplier). LEIE has a live feed plus Wayback-Machine snapshots for
historical labels (used by paper 10).

Manual download pages:
  Part B (by Provider & Service):
    https://data.cms.gov/provider-summary-by-type-of-service/medicare-physician-other-practitioners/medicare-physician-other-practitioners-by-provider-and-service
  Part B (by Provider):
    https://data.cms.gov/provider-summary-by-type-of-service/medicare-physician-other-practitioners/medicare-physician-other-practitioners-by-provider
  Part D (by Provider & Drug):
    https://data.cms.gov/provider-summary-by-type-of-service/medicare-part-d-prescribers/medicare-part-d-prescribers-by-provider-and-drug
  Part D (by Provider):
    https://data.cms.gov/provider-summary-by-type-of-service/medicare-part-d-prescribers/medicare-part-d-prescribers-by-provider
  DMEPOS (Referring Provider):
    https://data.cms.gov/provider-summary-by-type-of-service/medicare-durable-medical-equipment-devices-supplies/medicare-durable-medical-equipment-devices-supplies-by-referring-provider
  LEIE:
    https://oig.hhs.gov/exclusions/exclusions_list.asp
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import pandas as pd
import requests
from tqdm import tqdm

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RAW_DIR = PROJECT_ROOT / "data" / "raw"
PANELS_DIR = PROJECT_ROOT / "data" / "panels"
REGISTRIES_DIR = PROJECT_ROOT / "data" / "registries"
LEIE_DIR = PROJECT_ROOT / "data" / "legacy" / "leie"
for _d in (PANELS_DIR, REGISTRIES_DIR, LEIE_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def _parquet_path(name: str) -> Path:
    """Route a parquet artifact name to the correct sub-directory.

    Panel artifacts (annual CMS PUFs) go under ``data/panels/``; the LEIE
    snapshot goes under ``data/legacy/leie/`` (retired from the active
    pipeline, kept for historical reproductions); everything else is
    treated as a registry and goes under ``data/registries/``.
    """
    if name.startswith(("cms_partb_", "cms_partd_", "cms_dmepos_", "cms_partb_svc_")):
        return PANELS_DIR / f"{name}.parquet"
    if name == "leie" or name.startswith("leie_"):
        return LEIE_DIR / f"{name}.parquet"
    return REGISTRIES_DIR / f"{name}.parquet"

# URL catalog convention
# ----------------------
# Every CMS PUF download URL lives under one domain prefix and varies only
# by ``{release_date}/{uuid}/{filename}``. We store the variable tail per
# source as a ``dict[int, str]`` (year → relative path); :func:`cms_url`
# prepends ``CMS_FILES_BASE`` at fetch time. Full URLs are also accepted
# by the helper so dev pasting a fresh data.cms.gov link in still works
# without manual prefix-stripping — but the canonical form in committed
# code is the relative path.
#
# Per-source URL maps live on each scraper module:
#
# * Part B      → :data:`pipeline.scrapers.cms_partb.URLS`
# * Part B Svc  → :data:`pipeline.scrapers.cms_partb_svc.URLS`
# * Part D      → :data:`pipeline.scrapers.cms_partd.URLS`
# * DMEPOS      → :data:`pipeline.scrapers.cms_dmepos.URLS`
#
# Every annual CMS PUF in the catalog now owns its URL map. Sources
# without a scraper class (none today) would keep their map here until
# the scraper lands.
CMS_FILES_BASE = "https://data.cms.gov/sites/default/files/"


def cms_url(rel_or_full: str) -> str:
    """Resolve a CMS PUF URL.

    Accepts either a relative path (the canonical form, e.g.
    ``"2025-11/<uuid>/<filename>.csv"``) or a full URL. Full URLs pass
    through unchanged so a dev can paste a fresh link straight from
    data.cms.gov when updating a rotated URL.
    """
    if rel_or_full.startswith(("http://", "https://")):
        return rel_or_full
    return f"{CMS_FILES_BASE}{rel_or_full}"


DEFAULT_YEAR = 2023

LEIE_URL = "https://oig.hhs.gov/exclusions/downloadables/UPDATED.csv"
WAYBACK_AVAILABLE_API = "http://archive.org/wayback/available"

# Care Compare (Doctors and Clinicians National Downloadable File) — the
# CMS-published URL rotates each refresh; update by visiting the landing
# page at https://data.cms.gov/provider-data/dataset/mj5m-pzi6 and
# copying the "Download CSV" link target.
CARE_COMPARE_URL_CANDIDATES = [
    "https://data.cms.gov/provider-data/sites/default/files/resources/"
    "69a75aa9d3dc1aed6b881725cf0ddbf2_1712275222/"
    "DAC_NationalDownloadableFile.csv",
]

# Facility PUFs (Care Compare General Information files). Each source
# publishes one current snapshot — they're not year-keyed like Part B.
# URLs rotate per CMS refresh; update by visiting the landing page and
# copying the "Download CSV" link target. Use curated candidate lists so
# the fetcher probes in order and picks the first live URL.
FACILITY_URL_CANDIDATES = {
    # CMS rotates the resource-hash path each publish cycle; current
    # URLs resolved 2026-04-23 from each dataset's metastore endpoint
    # (https://data.cms.gov/provider-data/api/1/metastore/schemas/dataset/items/<uuid>).
    # When these 404, refetch from metastore — the dataset UUID is stable.
    # Hospital General Information — dataset xubh-q36u
    "cms_facility_hospital": [
        "https://data.cms.gov/provider-data/sites/default/files/resources/"
        "893c372430d9d71a1c52737d01239d47_1770163599/Hospital_General_Information.csv",
    ],
    # Nursing Home / SNF Provider Information — dataset 4pq5-n9py
    "cms_facility_snf": [
        "https://data.cms.gov/provider-data/sites/default/files/resources/"
        "3059e5643c76d35f1185eb1ee2f38d63_1773439550/NH_ProviderInfo_Mar2026.csv",
    ],
    # Home Health Agency General Information — dataset 6jpm-sxkc
    "cms_facility_hha": [
        "https://data.cms.gov/provider-data/sites/default/files/resources/"
        "f9a309e9463cdf0a7d7828f8d8d0e653_1775505949/HH_Provider_Apr2026.csv",
    ],
    # Hospice General Information — dataset 252m-zfp9
    "cms_facility_hospice": [
        "https://data.cms.gov/provider-data/sites/default/files/resources/"
        "3e583faac30f654ddde3559ffd1a4946_1770242744/Hospice_Provider_Feb2026.csv",
    ],
}

CHUNK = 1 << 20  # 1 MB


def _download(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    tmp = dest.with_suffix(dest.suffix + ".part")
    with requests.get(url, stream=True, timeout=300) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        with tmp.open("wb") as f, tqdm(
            total=total or None,
            unit="B",
            unit_scale=True,
            desc=dest.name,
        ) as bar:
            for chunk in r.iter_content(CHUNK):
                if chunk:
                    f.write(chunk)
                    bar.update(len(chunk))
    tmp.rename(dest)
    return dest


def _cache_parquet(name: str, frame: pd.DataFrame) -> Path:
    path = _parquet_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(path, index=False)
    return path


def _load_or_fetch(
    source: str,
    year: int,
    local_csv: Optional[str | Path],
    raw_subdir: str,
    urls: dict[int, str],
) -> pd.DataFrame:
    parquet = _parquet_path(f"{source}_{year}")
    if parquet.exists():
        return pd.read_parquet(parquet)

    if local_csv is not None:
        csv_path = Path(local_csv)
    else:
        if year not in urls:
            raise ValueError(
                f"No direct-download URL for {source} year {year}. "
                f"Pass local_csv= to a manually downloaded CSV."
            )
        csv_path = RAW_DIR / raw_subdir / f"{source}_{year}.csv"
        _download(cms_url(urls[year]), csv_path)

    df = pd.read_csv(csv_path, low_memory=False)
    _cache_parquet(f"{source}_{year}", df)
    return df


# ---------------------------------------------------------------------------
# Legacy CMS PUF fetchers (DataFrame-returning)
# ---------------------------------------------------------------------------
#
# TODO(pipeline-restructure): migrate callers off these and remove. The
# replacement is the per-source ``BaseScraper`` class — e.g.
# ``PartBScraper().fetch(year=year)`` — which produces the stamped
# parquet + sibling manifest the new ABC defines. These wrappers exist
# only because ``build_notebooks.py``, ``reproduce_all.py``,
# ``fraudlib/ranker/features.py``, etc. still import them; delete once
# those call sites move to the scraper API.


def fetch_cms_partb(year: int = DEFAULT_YEAR, local_csv: Optional[str | Path] = None) -> pd.DataFrame:
    """Medicare Physician & Other Practitioners — by Provider (aggregated, ~1 row/NPI)."""
    # Lazy import: cms_partb imports helpers from this module, so a
    # top-level reverse import would create a cycle.
    from pipeline.scrapers.cms_partb import URLS

    return _load_or_fetch("cms_partb", year, local_csv, "cms_partb", URLS)


def fetch_cms_partb_service(year: int = DEFAULT_YEAR, local_csv: Optional[str | Path] = None) -> pd.DataFrame:
    """Medicare Physician & Other Practitioners — by Provider & Service (multi-row per NPI).

    Warning: large (5–10 GB). Used by paper 10's "Summary by Provider"
    construction and by the HCPCS-bipartite graph for paper 01.
    """
    # Lazy import: cms_partb_svc imports helpers from this module, so a
    # top-level reverse import would create a cycle.
    from pipeline.scrapers.cms_partb_svc import URLS

    return _load_or_fetch("cms_partb_svc", year, local_csv, "cms_partb_svc", URLS)


def fetch_cms_partd(year: int = DEFAULT_YEAR, local_csv: Optional[str | Path] = None) -> pd.DataFrame:
    """Medicare Part D Prescribers — by Provider (aggregated)."""
    # Lazy import: cms_partd imports helpers from this module, so a
    # top-level reverse import would create a cycle.
    from pipeline.scrapers.cms_partd import URLS

    return _load_or_fetch("cms_partd", year, local_csv, "cms_partd", URLS)


def fetch_cms_dmepos(year: int = DEFAULT_YEAR, local_csv: Optional[str | Path] = None) -> pd.DataFrame:
    """Medicare DMEPOS — by Referring Provider (aggregated)."""
    # Lazy import: cms_dmepos imports helpers from this module, so a
    # top-level reverse import would create a cycle.
    from pipeline.scrapers.cms_dmepos import URLS

    return _load_or_fetch("cms_dmepos", year, local_csv, "cms_dmepos", URLS)


def fetch_leie(local_csv: Optional[str | Path] = None) -> pd.DataFrame:
    """OIG List of Excluded Individuals/Entities (LEIE). ~80k rows.

    Retired from the active training pipeline (see ``data/legacy/leie/
    README.md``); kept in the fetcher for historical paper reproductions
    in ``reproduce_all.py``.
    """
    parquet = _parquet_path("leie")
    if parquet.exists():
        return pd.read_parquet(parquet)

    if local_csv is not None:
        csv_path = Path(local_csv)
    else:
        csv_path = RAW_DIR / "leie" / "leie.csv"
        _download(LEIE_URL, csv_path)

    df = pd.read_csv(csv_path, low_memory=False, dtype=str).fillna("")
    df.columns = [c.strip().upper() for c in df.columns]
    _cache_parquet("leie", df)
    return df


def fetch_leie_historical(snapshot_year: int) -> pd.DataFrame:
    """Historical LEIE snapshot from Wayback Machine.

    Queries archive.org for an archived copy of the OIG LEIE CSV close
    to Jan 1 of ``snapshot_year``. Used by paper 10 (Data-Centric AI)
    to simulate the label state available at an earlier point in time.

    Falls back to the current LEIE with a warning if no snapshot found.
    Cached per-year at ``data/legacy/leie/leie_<year>.parquet``.
    """
    parquet = _parquet_path(f"leie_{snapshot_year}")
    if parquet.exists():
        return pd.read_parquet(parquet)

    target_date = f"{snapshot_year}0101"
    try:
        r = requests.get(
            WAYBACK_AVAILABLE_API,
            params={"url": LEIE_URL, "timestamp": target_date},
            timeout=30,
        )
        r.raise_for_status()
        snapshot = r.json().get("archived_snapshots", {}).get("closest", {})
        snap_url = snapshot.get("url", "")
    except Exception as exc:
        print(f"  [wayback] API error for {snapshot_year}: {exc}; falling back to current LEIE")
        snap_url = ""

    if not snap_url:
        print(f"  [wayback] no snapshot for {snapshot_year}; falling back to current LEIE")
        df = fetch_leie()
        _cache_parquet(f"leie_{snapshot_year}", df)
        return df

    csv_path = RAW_DIR / "leie" / f"leie_{snapshot_year}.csv"
    try:
        _download(snap_url, csv_path)
        df = pd.read_csv(csv_path, low_memory=False, dtype=str).fillna("")
        df.columns = [c.strip().upper() for c in df.columns]
    except Exception as exc:
        print(f"  [wayback] download failed for {snapshot_year}: {exc}; using current LEIE")
        df = fetch_leie()
    _cache_parquet(f"leie_{snapshot_year}", df)
    return df


def fetch_pecos_ordering_referring(
    local_csv: Optional[str | Path] = None,
    url: Optional[str] = None,
) -> pd.DataFrame:
    """PECOS Ordering & Referring quarterly file.

    Thin wrapper around
    :func:`fraudlib.ingest.providers.pecos.download_pecos_ordering_referring`
    kept here for discoverability alongside the other CMS fetchers.
    Returns the projected parquet as a DataFrame.
    """
    from fraudlib.ingest.providers.pecos import (
        _csv_to_parquet as _pecos_csv_to_parquet,
        download_pecos_ordering_referring,
    )

    out_path = _parquet_path("pecos_ordering_referring")
    if out_path.exists():
        return pd.read_parquet(out_path)

    if local_csv is not None:
        _pecos_csv_to_parquet(Path(local_csv), out_path)
    else:
        download_pecos_ordering_referring(out_path=out_path, url=url)
    return pd.read_parquet(out_path)


def fetch_care_compare(
    local_csv: Optional[str | Path] = None,
    url: Optional[str] = None,
) -> pd.DataFrame:
    """Medicare Care Compare Doctors and Clinicians National Downloadable File.

    Writes ``data/registries/care_compare.parquet`` (one row per
    clinician-enrollment record — collapse to per-NPI via
    :func:`fraudlib.ingest.providers.care_compare.load_care_compare_features`).
    """
    from fraudlib.ingest.providers.care_compare import _csv_to_parquet as _cc_csv_to_parquet

    out_path = _parquet_path("care_compare")
    if out_path.exists():
        return pd.read_parquet(out_path)

    if local_csv is not None:
        _cc_csv_to_parquet(Path(local_csv), out_path)
    else:
        candidate_urls = [url] if url else CARE_COMPARE_URL_CANDIDATES
        csv_path = RAW_DIR / "care_compare" / "DAC_NationalDownloadableFile.csv"
        last_exc: Exception | None = None
        for candidate in candidate_urls:
            try:
                _download(candidate, csv_path)
                break
            except Exception as exc:
                last_exc = exc
                continue
        else:
            raise RuntimeError(
                "No Care Compare URL candidate returned; visit "
                "https://data.cms.gov/provider-data/dataset/mj5m-pzi6 "
                f"and pass url= or local_csv=. Last error: {last_exc!r}"
            )
        _cc_csv_to_parquet(csv_path, out_path)
    return pd.read_parquet(out_path)


def _fetch_facility(
    source: str,
    local_csv: Optional[str | Path] = None,
    url: Optional[str] = None,
) -> pd.DataFrame:
    """Generic facility-PUF fetcher. Writes a projected parquet at
    ``data/registries/<source>.parquet`` (no year suffix — facility PUFs
    are single rolling snapshots, not annual cohorts).
    """
    out_path = _parquet_path(source)
    if out_path.exists():
        return pd.read_parquet(out_path)

    if local_csv is not None:
        csv_path = Path(local_csv)
    else:
        csv_path = RAW_DIR / source / f"{source}.csv"
        candidates = [url] if url else FACILITY_URL_CANDIDATES.get(source, [])
        last_exc: Exception | None = None
        for candidate in candidates:
            try:
                _download(candidate, csv_path)
                break
            except Exception as exc:
                last_exc = exc
                continue
        else:
            raise RuntimeError(
                f"No {source} URL candidate returned. Update "
                f"FACILITY_URL_CANDIDATES[{source!r}] with the current "
                f"CMS link or pass url=/local_csv=. Last error: {last_exc!r}"
            )

    df = pd.read_csv(csv_path, dtype=str, low_memory=False).fillna("")
    df.columns = [c.strip() for c in df.columns]
    _cache_parquet(source, df)
    return df


def fetch_cms_facility_hospital(
    local_csv: Optional[str | Path] = None,
    url: Optional[str] = None,
) -> pd.DataFrame:
    """Hospital General Information PUF (one row per Medicare-certified
    hospital ~5K). Fields: CMS Certification Number (CCN), Facility Name,
    Address, Type, Ownership, Hospital overall rating.
    """
    return _fetch_facility("cms_facility_hospital", local_csv=local_csv, url=url)


def fetch_cms_facility_snf(
    local_csv: Optional[str | Path] = None,
    url: Optional[str] = None,
) -> pd.DataFrame:
    """Nursing Home (SNF) Provider Information PUF (~15K facilities).
    Primary fraud-relevant fields: Ownership Type (for-profit chains
    over-index on SNF fraud), Overall Rating, Number of Certified Beds,
    Reported Total Nurse Staffing Hours per Resident per Day.
    """
    return _fetch_facility("cms_facility_snf", local_csv=local_csv, url=url)


def fetch_cms_facility_hha(
    local_csv: Optional[str | Path] = None,
    url: Optional[str] = None,
) -> pd.DataFrame:
    """Home Health Agency General Information PUF (~12K agencies).
    Primary fraud-relevant fields: Ownership Type, Type of Ownership
    (Proprietary vs Voluntary Non-profit — proprietary HHAs have
    documented higher plan-of-care falsification rates), Certification
    Date, Quality of Patient Care Star Rating.
    """
    return _fetch_facility("cms_facility_hha", local_csv=local_csv, url=url)


def fetch_cms_facility_hospice(
    local_csv: Optional[str | Path] = None,
    url: Optional[str] = None,
) -> pd.DataFrame:
    """Hospice General Information PUF (~5K hospices). Primary fraud-
    relevant fields: Ownership Type, Certification Date, state.
    Hospice-eligibility fraud (billing for patients not actually at
    end of life) is a core scheme; the facility roster is the grain
    for the first facility-level rule.
    """
    return _fetch_facility("cms_facility_hospice", local_csv=local_csv, url=url)


def all_processed() -> dict[str, Path]:
    """Return every processed parquet currently on disk (panels + registries + leie)."""
    found: dict[str, Path] = {}
    for d in (PANELS_DIR, REGISTRIES_DIR, LEIE_DIR):
        for p in d.glob("*.parquet"):
            found[p.stem] = p
    return found
