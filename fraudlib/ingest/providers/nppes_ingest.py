"""Bulk NPPES ingest + feature-table builder.

CMS publishes a full monthly NPPES dump at
https://download.cms.gov/nppes/NPI_Files.html — the "NPPES Data
Dissemination" file. The zip is ~189 MB and contains a CSV with
~330 columns per NPI (every person and organization in the registry).

For the fraud pipeline we only care about a thin slice of those columns
— the ones that turn into features joinable onto Part B:

  * ``NPI``                                       — join key
  * ``Enumeration_Date``                          — phantom-billing signal
  * ``Provider_Credential_Text``                  — MD / DO / LPN / DC / ...
  * ``Is_Sole_Proprietor``                        — shell-practice flag
  * ``Entity_Type_Code``                          — 1 = individual, 2 = organization
  * ``Healthcare_Provider_Taxonomy_Code_1``       — primary specialty taxonomy
  * ``Provider_Last_Name_Legal_Name``             — spot-check cross-ref
  * ``Provider_First_Name``                       — spot-check cross-ref

This module:

  1. :func:`download_nppes_bulk` — fetches the latest monthly zip,
     extracts the main CSV, projects down to the columns above, and
     writes ``data/registries/nppes_bulk.parquet``.
  2. :func:`load_nppes_features` — loads that parquet, coerces types,
     derives feature columns (``is_organization``, ``is_sole_proprietor``,
     ``years_enumerated_at_2013``, etc.), and returns an NPI-indexed
     ``pd.DataFrame`` ready to LEFT-JOIN onto the Part B panel.

If the bulk file is unavailable (rate-limited, connectivity issue), the
per-NPI :func:`fraudlib.labels.nppes_lookup.lookup` function is the fallback —
see the journal entry for that date for the enumeration-date spot-check
it yields for gold_set positives.
"""
from __future__ import annotations

import datetime as _dt
import re
import zipfile
from pathlib import Path
from typing import Iterable

import pandas as pd
import requests

PROCESSED = Path("data/registries")
RAW = Path("data/raw") / "nppes"

# The canonical monthly file URL pattern. CMS names files by the
# publication month-year, e.g. NPPES_Data_Dissemination_April_2026.zip.
# We try the current and previous month so this works seamlessly at
# month-end boundaries when the new file hasn't been posted yet.
_BULK_URL_FMT = "https://download.cms.gov/nppes/NPPES_Data_Dissemination_{month}_{year}.zip"

# The thin slice of NPPES columns we keep. Names match the CSV header
# exactly — case-sensitive.
_KEEP_COLUMNS = [
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
]

# Normalized snake_case names used in the parquet.
_RENAME = {
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
}


# ---------------------------------------------------------------------------
# Bulk download
# ---------------------------------------------------------------------------

def _candidate_urls(today: _dt.date | None = None) -> list[str]:
    """Return candidate monthly URLs — current month first, then previous.

    CMS publishes ~2nd week of each month, so at month-start the current
    month's file doesn't exist yet; we fall back to the prior month.
    """
    today = today or _dt.date.today()
    months = []
    for offset in range(0, 3):
        # Step back ``offset`` months
        y, m = today.year, today.month - offset
        while m <= 0:
            m += 12
            y -= 1
        month_name = _dt.date(y, m, 1).strftime("%B")
        months.append(_BULK_URL_FMT.format(month=month_name, year=y))
    return months


def download_nppes_bulk(
    out_path: Path | None = None,
    max_size_gb: float = 2.0,
) -> Path:
    """Download the latest NPPES monthly dump, extract the main CSV, write parquet.

    Parameters
    ----------
    out_path
        Where to write the thin parquet. Defaults to
        ``data/registries/nppes_bulk.parquet``.
    max_size_gb
        Abort-guard on compressed zip size. The instructions note that a
        >2 GB compressed file should fall back to per-NPI API enrichment
        rather than blowing disk quota. A ``ValueError`` is raised if the
        zip exceeds this.

    Returns
    -------
    Path to the written parquet.

    Notes
    -----
    The CSV inside the zip is typically named
    ``npidata_pfile_<startdate>-<enddate>.csv`` (the main "pfile"). We
    skip the ``endpoint``, ``othername``, and ``pl`` supplementary files
    in the same archive. To keep memory bounded we iterate in 500k-row
    chunks and keep only :data:`_KEEP_COLUMNS`.
    """
    out_path = out_path or (PROCESSED / "nppes_bulk.parquet")
    RAW.mkdir(parents=True, exist_ok=True)
    PROCESSED.mkdir(parents=True, exist_ok=True)

    # Probe candidate URLs with HEAD so we don't start a ~189 MB GET against
    # a 404'd month.
    zip_url = None
    for url in _candidate_urls():
        try:
            head = requests.head(url, timeout=15, allow_redirects=True)
            if head.status_code == 200:
                zip_url = url
                size_gb = int(head.headers.get("Content-Length", 0)) / (1024 ** 3)
                if size_gb > max_size_gb:
                    raise ValueError(
                        f"NPPES bulk at {url} is {size_gb:.2f} GB compressed "
                        f"(> {max_size_gb} GB cap). Fall back to per-NPI API "
                        f"enrichment via fraudlib.labels.nppes_lookup.lookup."
                    )
                break
        except requests.RequestException:
            continue

    if not zip_url:
        raise RuntimeError(
            "No NPPES monthly zip reachable at any candidate URL. "
            "Check https://download.cms.gov/nppes/NPI_Files.html for a "
            "filename drift and fall back to per-NPI lookup() if needed."
        )

    zip_path = RAW / Path(zip_url).name
    if not zip_path.exists():
        with requests.get(zip_url, stream=True, timeout=600) as r:
            r.raise_for_status()
            with open(zip_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 20):  # 1 MB
                    f.write(chunk)

    # Locate the main pfile CSV — name pattern: npidata_pfile_*-*.csv.
    # The same zip contains othername_pfile, endpoint_pfile, pl_pfile
    # which we don't need.
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

        # Read in chunks, project early, append to a list of frames.
        # ~8M NPIs × 10 columns × mostly-short-strings fits comfortably
        # in memory (<500 MB), so we don't need an incremental parquet writer.
        with zf.open(main_csv) as handle:
            # Sniff header to see which candidate columns are present
            # (column-name drift happens across monthly releases).
            header = pd.read_csv(handle, nrows=0)
            present = [c for c in _KEEP_COLUMNS if c in header.columns]
            missing = [c for c in _KEEP_COLUMNS if c not in header.columns]
            if missing:
                print(f"[nppes_ingest] warning: columns not in header: {missing}")

        # Re-open to stream chunks (pandas needs a fresh file handle).
        with zf.open(main_csv) as handle:
            chunks: list[pd.DataFrame] = []
            for chunk in pd.read_csv(
                handle,
                usecols=present,
                chunksize=500_000,
                dtype=str,  # keep as str; we coerce dates/flags on load.
                low_memory=False,
            ):
                chunks.append(chunk)
            thin = pd.concat(chunks, ignore_index=True)

    thin = thin.rename(columns=_RENAME)
    thin.to_parquet(out_path, index=False)
    print(f"[nppes_ingest] wrote {len(thin):,} rows → {out_path}")
    return out_path


# ---------------------------------------------------------------------------
# Feature table
# ---------------------------------------------------------------------------

def load_nppes_features(
    parquet_path: Path | None = None,
    reference_year: int = 2013,
) -> pd.DataFrame:
    """Load the NPPES bulk parquet and derive NPI-indexed feature columns.

    Returns a DataFrame with columns:

      * ``NPI`` (str, zero-padded to 10 digits)
      * ``enumeration_date`` (pd.Timestamp)
      * ``years_enumerated_at_<reference_year>`` (Int64)
      * ``is_organization`` (bool)
      * ``is_sole_proprietor`` (bool)
      * ``credential`` (str)
      * ``primary_taxonomy_code`` (str)

    The reference year defaults to 2013 because that's our primary Part B
    cohort year; callers building features for other years should pass
    the right ``reference_year`` so ``years_enumerated_at_<year>`` is
    computed against the right anchor.

    If the parquet doesn't exist, a FileNotFoundError is raised with a
    pointer to :func:`download_nppes_bulk`.
    """
    parquet_path = parquet_path or (PROCESSED / "nppes_bulk.parquet")
    if not parquet_path.exists():
        raise FileNotFoundError(
            f"{parquet_path} not found. Run "
            f"fraudlib.ingest.providers.nppes_ingest.download_nppes_bulk() first, "
            f"or fall back to per-NPI fraudlib.labels.nppes_lookup.lookup."
        )

    df = pd.read_parquet(parquet_path)

    out = pd.DataFrame()
    # Canonical zero-padded NPI key. The nppes scraper v2.0 emits only the
    # lowercase ``npi`` (the raw ``NPI`` was dropped — it collided with ``npi``
    # under case-insensitive SQL engines). Prefer it; fall back to the legacy
    # raw ``NPI`` so this loader still reads pre-v2.0 parquets unchanged.
    npi_src = df["npi"] if "npi" in df.columns else df["NPI"]
    out["NPI"] = npi_src.astype(str).str.zfill(10)

    # enumeration_date: CSV ships as "MM/DD/YYYY"; coerce to datetime.
    if "Enumeration_Date" in df.columns:
        out["enumeration_date"] = pd.to_datetime(
            df["Enumeration_Date"], errors="coerce", format="%m/%d/%Y"
        )
    else:
        out["enumeration_date"] = pd.NaT

    # Anchor-year "years_enumerated" — integer years between enumeration
    # and Dec 31 of reference_year. Negative means NPI did not exist when
    # the panel year's services were supposedly rendered (phantom-billing
    # red flag); we keep the sign intact so the JOIN user can filter it.
    anchor = pd.Timestamp(year=reference_year, month=12, day=31)
    years = ((anchor - out["enumeration_date"]).dt.days / 365.25)
    out[f"years_enumerated_at_{reference_year}"] = years.round().astype("Int64")

    # Organization: Entity_Type_Code == "2".
    out["is_organization"] = df.get("Entity_Type_Code", pd.Series(dtype=str)) == "2"

    # Sole proprietor: "Y" / "N" / "X" (not disclosed). Treat "Y" as True,
    # everything else as False.
    out["is_sole_proprietor"] = (
        df.get("Is_Sole_Proprietor", pd.Series(dtype=str)).fillna("").str.upper() == "Y"
    )

    out["credential"] = df.get("Provider_Credential_Text", pd.Series(dtype=str)).fillna("")
    out["primary_taxonomy_code"] = df.get(
        "Healthcare_Provider_Taxonomy_Code_1", pd.Series(dtype=str)
    ).fillna("")

    # Drop the (rare) rows with a nonsense NPI (missing/short).
    out = out[out["NPI"].str.len() == 10].drop_duplicates("NPI").reset_index(drop=True)
    return out


# ---------------------------------------------------------------------------
# Feature engineering: credential + taxonomy bucketing
# ---------------------------------------------------------------------------

# 7 credential buckets ordered most-specific → catch-all.
_CREDENTIAL_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("MD_DO",           re.compile(r"^M\.?D\.?$|^D\.?O\.?$", re.I)),
    ("NP_PA",           re.compile(r"^(N\.?P\.?|PA|P\.A\.|CRNP|ARNP|FNP|ANP|CNP|APN)(-?C|-?BC)?$", re.I)),
    ("CRNA",            re.compile(r"^C\.?R\.?N\.?A\.?$", re.I)),
    ("DC_DPM_MANUAL",   re.compile(r"^(D\.?C\.?|D\.?P\.?M\.?|PT|P\.?T\.?|O\.?T\.?|DPT)$", re.I)),
    ("PHD_NONCLINICAL", re.compile(r"^PH\.?D\.?$", re.I)),
    ("MENTAL_HEALTH",   re.compile(r"^(LCSW|LICSW|PSY\.?D\.?|PSYD)$", re.I)),
]

# NUCC taxonomy prefix (first 2 chars) → specialty bucket.
_TAXONOMY_PREFIX_MAP: dict[str, str] = {
    "21": "DENTAL",
    "22": "PODIATRY",
    "23": "OPTOMETRY",
    "24": "PROSTHETICS",
    "25": "AUDIOLOGY",
    "26": "CHIROPRACTIC",
    "27": "THERAPISTS",
    "28": "COUNSELORS",
    "29": "MISCELLANEOUS",
    "30": "NURSES",
    "32": "ALTERNATIVE_MED",
    "33": "NURSING_SUPPORT",
    "35": "MIDLEVEL_PRESCRIBERS",
    "36": "ALLIED_MISC",
    "37": "SERVICE_PROVIDERS",
    "39": "RESPITE_CARE",
    "40": "FACILITY_BASED",
    "41": "AMBULANCE",
    "42": "DMEPOS_SUPPLIER",
    "43": "HOSPITAL",
    "47": "GOV_AGENCIES",
    "48": "NATIONAL_ORGS",
    "49": "MCO",
    "50": "PHARMACY",
}

# Physician subspecialty splits (prefix "20").
# Matched against code[2:]; listed longest-match-first.
_PHYSICIAN_SUBSPECIALTY: list[tuple[str, str]] = [
    ("7RC",  "PHYSICIAN_CARDIOLOGY"),
    ("7RH",  "PHYSICIAN_ONCOLOGY"),
    ("6S",   "PHYSICIAN_ONCOLOGY"),
    ("7RG",  "PHYSICIAN_GASTRO"),
    ("7ZP",  "PHYSICIAN_PATHOLOGY"),
    ("7W",   "PHYSICIAN_OPHTHALMOLOGY"),
    ("85R",  "PHYSICIAN_RADIOLOGY"),
    ("84",   "PHYSICIAN_PSYCHIATRY"),
    ("82",   "PHYSICIAN_PAIN_MGMT"),
    ("7R",   "PHYSICIAN_INTERNAL_MED"),
]


def _bucket_credential(raw: str) -> str:
    raw = raw.strip()
    for bucket, pattern in _CREDENTIAL_PATTERNS:
        if pattern.match(raw):
            return bucket
    return "OTHER"


def _bucket_taxonomy(code: str) -> str:
    code = code.strip().upper()
    if not code or len(code) < 2:
        return "UNKNOWN"
    prefix = code[:2]
    if prefix != "20":
        return _TAXONOMY_PREFIX_MAP.get(prefix, "UNKNOWN")
    for suffix, bucket in _PHYSICIAN_SUBSPECIALTY:
        if code[2:].startswith(suffix):
            return bucket
    return "PHYSICIAN_OTHER"


def compute_nppes_features(year: int) -> pd.DataFrame:
    """Load NPPES bulk parquet and return an ML-ready, prefixed feature frame.

    Analogous to ``features.engineer()`` (Part B) and
    ``partd_stats.compute_partd_features()`` (Part D) — each source has one
    canonical ``compute_*_features(year)`` that owns all its engineering.

    Returns a DataFrame with:
      * ``npi``                     — str, 10-digit identifier (join key)
      * ``nppes_years_enumerated``  — years since NPI was issued
      * ``nppes_is_sole_proprietor``— shell-practice flag (bool)
      * ``nppes_is_organization``   — org vs individual (bool)
      * ``nppes_credential_bucket`` — 7-bucket credential grouping
      * ``nppes_taxonomy_bucket``   — 25-bucket NUCC specialty grouping

    Raises FileNotFoundError if nppes_bulk.parquet is not present
    (run download_nppes_bulk() first).
    """
    raw = load_nppes_features(reference_year=year).rename(columns={"NPI": "npi"})

    year_col = f"years_enumerated_at_{year}"
    out = pd.DataFrame({"npi": raw["npi"]})
    out["nppes_years_enumerated"]   = raw.get(year_col, pd.NA)
    out["nppes_is_sole_proprietor"] = raw.get("is_sole_proprietor", False).astype(bool)
    out["nppes_is_organization"]    = raw.get("is_organization", False).astype(bool)
    out["nppes_credential_bucket"]  = (
        raw.get("credential", pd.Series(dtype=str)).fillna("").map(_bucket_credential)
    )
    out["nppes_taxonomy_bucket"] = (
        raw.get("primary_taxonomy_code", pd.Series(dtype=str)).fillna("").map(_bucket_taxonomy)
    )
    return out


# ---------------------------------------------------------------------------
# Fallback: per-NPI API enrichment for a small NPI list
# ---------------------------------------------------------------------------

def enrich_via_api(npis: Iterable[str]) -> pd.DataFrame:
    """Per-NPI NPPES API enrichment for a short list (< ~1000 NPIs).

    This is the fallback when the bulk download is unavailable — walks
    :func:`fraudlib.labels.nppes_lookup.lookup` over ``npis`` and returns the
    same schema :func:`load_nppes_features` produces, so the join logic
    in ``query_tools.open_partb`` doesn't need to branch on ingest mode.

    Reference year defaults to 2013 for consistency with the cohort.
    """
    from fraudlib.labels.nppes_lookup import lookup as _lookup

    rows: list[dict] = []
    for npi in npis:
        prov = _lookup(str(npi))
        if not prov:
            continue
        rows.append({
            "NPI": str(prov.npi).zfill(10),
            "enumeration_date": prov.enumeration_date,
            "is_organization": prov.is_organization,
            "is_sole_proprietor": (prov.sole_proprietor or "").upper() == "YES",
            "credential": prov.credential or "",
            "primary_taxonomy_code": prov.primary_taxonomy_code or "",
        })
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["enumeration_date"] = pd.to_datetime(df["enumeration_date"], errors="coerce")
    anchor = pd.Timestamp(year=2013, month=12, day=31)
    years = ((anchor - df["enumeration_date"]).dt.days / 365.25)
    df["years_enumerated_at_2013"] = years.round().astype("Int64")
    return df
