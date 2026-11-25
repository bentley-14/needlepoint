"""Bulk CMS Open Payments ingest + per-NPI feature-table builder.

The Open Payments (Sunshine Act) program is CMS' mandatory disclosure of
payments and transfers of value from pharmaceutical manufacturers and
group purchasing organizations to covered recipients (physicians,
teaching hospitals, and since 2021, a wider class of practitioners).
Program year data is refreshed annually around June 30.

For the fraud pipeline the signal is Anti-Kickback-Statute proximity:
outsized manufacturer-to-physician cash flow co-occurring with outlier
prescribing or ordering is a near-diagnostic pattern. Kickback schemes
prosecuted by DOJ (Insys/Subsys, Novartis speaker-program cases, etc.)
leave a clean fingerprint in this dataset: the paid-to specialty is
usually unchanged but the dollar volume and manufacturer-concentration
are in the top few percent of peers.

This module mirrors :mod:`fraudlib.ingest.providers.nppes_ingest`:

  1. :func:`download_openpayments_bulk` — fetches the annual General
     Payments CSV zip from the CMS public download host, projects down
     to the columns we need, writes
     ``data/panels/open_payments_general_<year>.parquet``.
  2. :func:`load_openpayments_features` — loads that parquet and emits
     an NPI-indexed feature frame (totals, counts by nature-of-payment,
     top manufacturer, peer percentile within specialty × state).
  3. :func:`enrich_via_api` — per-NPI Socrata-API fallback for a short
     list; uses the same output schema as ``load_openpayments_features``
     so the JOIN logic in ``query_tools.open_partb`` stays ingest-mode
     agnostic.

Bulk URL pattern (as of April 2026):
``https://download.cms.gov/openpayments/PGYY_P0130<PUB>_<date>.zip``
— CMS has rotated this naming repeatedly, so we probe the catalog
endpoint first and fall back to a hand-curated pattern list.

"""
from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path
from typing import Iterable

import pandas as pd
import requests

PROCESSED = Path("data/panels")
RAW = Path("data/raw") / "openpayments"

# Socrata "catalog" endpoint lists every dataset CMS publishes on the
# Open Payments portal. We filter it for the General Payments dataset of
# the requested program year and pull its ``download_url``. Using the
# catalog rather than hard-coded URLs insulates us from the annual
# filename churn.
_SOCRATA_CATALOG = "https://openpaymentsdata.cms.gov/api/catalog/v1"

# Per-NPI API fallback uses the Socrata resource endpoint. Dataset IDs
# rotate yearly; we resolve via catalog lookup in :func:`_resolve_dataset_id`.
_SOCRATA_RESOURCE = "https://openpaymentsdata.cms.gov/resource/{dataset_id}.json"

# Columns kept in the thin parquet. Names match the CMS CSV header
# exactly (case-sensitive, underscored).
_KEEP_COLUMNS = [
    "Record_ID",
    "Covered_Recipient_NPI",
    "Covered_Recipient_Type",
    "Covered_Recipient_Specialty_1",
    "Recipient_State",
    "Applicable_Manufacturer_or_Applicable_GPO_Making_Payment_Name",
    "Total_Amount_of_Payment_USDollars",
    "Date_of_Payment",
    "Nature_of_Payment_or_Transfer_of_Value",
    "Name_of_Drug_or_Biological_or_Device_or_Medical_Supply_1",
    "Program_Year",
]

# Normalized snake_case names used in the parquet and downstream features.
_RENAME = {
    "Record_ID": "record_id",
    "Covered_Recipient_NPI": "NPI",
    "Covered_Recipient_Type": "recipient_type",
    "Covered_Recipient_Specialty_1": "specialty",
    "Recipient_State": "state",
    "Applicable_Manufacturer_or_Applicable_GPO_Making_Payment_Name": "manufacturer",
    "Total_Amount_of_Payment_USDollars": "amount_usd",
    "Date_of_Payment": "date_of_payment",
    "Nature_of_Payment_or_Transfer_of_Value": "nature_of_payment",
    "Name_of_Drug_or_Biological_or_Device_or_Medical_Supply_1": "product_name",
    "Program_Year": "program_year",
}

# Nature-of-payment buckets the feature table tallies. CMS uses verbose
# phrases like "Food and Beverage"; we normalize to short keys.
_NATURE_BUCKETS: dict[str, str] = {
    "food and beverage": "meals",
    "travel and lodging": "travel",
    "compensation for services other than consulting, including serving as faculty or as a speaker at a venue other than a continuing education program": "speaker",
    "compensation for serving as faculty or as a speaker for a non-accredited and noncertified continuing education program": "speaker",
    "compensation for serving as faculty or as a speaker for an accredited or certified continuing education program": "education",
    "royalty or license": "royalty",
    "consulting fee": "consulting",
    "education": "education",
    "honoraria": "honoraria",
    "gift": "gift",
    "grant": "grant",
    "entertainment": "entertainment",
    "charitable contribution": "charitable",
    "current or prospective ownership or investment interest": "ownership",
    "space rental or facility fees (teaching hospital only)": "space_rental",
    "debt forgiveness": "debt_forgiveness",
}


# ---------------------------------------------------------------------------
# Dataset-id resolution (Socrata catalog)
# ---------------------------------------------------------------------------

def _resolve_dataset_id(program_year: int, kind: str = "general") -> str | None:
    """Resolve the Socrata dataset id for a program year + kind.

    ``kind`` is one of ``"general"``, ``"research"``, ``"ownership"``.
    Returns ``None`` if the catalog query fails or no matching dataset is
    found — callers should then fall back to a hand-curated URL.
    """
    kind_keyword = {
        "general": "general payments",
        "research": "research payments",
        "ownership": "ownership",
    }[kind]
    params = {
        "only": "dataset",
        "q": f"open payments {program_year} {kind_keyword}",
        "limit": 25,
    }
    try:
        r = requests.get(_SOCRATA_CATALOG, params=params, timeout=30)
        r.raise_for_status()
        payload = r.json()
    except (requests.RequestException, ValueError):
        return None

    for entry in payload.get("results", []):
        name = (entry.get("resource", {}).get("name") or "").lower()
        if str(program_year) in name and kind_keyword in name:
            return entry["resource"].get("id")
    return None


# ---------------------------------------------------------------------------
# Bulk download
# ---------------------------------------------------------------------------

# Hand-curated download URLs for program years where the Socrata catalog
# look-up fails. CMS has historically hosted the annual zips under
# /openpayments/ — the date stamp changes each refresh cycle.
_BULK_URL_CANDIDATES: dict[int, list[str]] = {
    # 2023 was the most recent full PY at sprint start (2026-04); PY2024
    # lands June 2026. Keep the 2024 slot as a placeholder probe.
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
    # PY2013-PY2017 were archived off the live Search Tool; they remain
    # downloadable from the same /openpayments/ download host. Starting
    # with the June-2023 refresh CMS shifted the layout to a per-publish
    # sub-directory (e.g. /PGYR17_P063023/OP_DTL_GNRL_PGYR2017_P06302023.csv)
    # rather than a flat per-year zip. We probe both the old flat zip
    # name AND the newer sub-directory zip in case CMS has re-issued the
    # archive as a bundle (many downstream ingest tools — ours included —
    # prefer the zip). The sub-directory CSV is listed as a commented
    # fallback; downloading CSVs directly requires a small adapter to the
    # extraction path in download_openpayments_bulk(), which is out of
    # scope for this URL-map extension. See
    # data/archive/working_notes/open_payments_coverage.md.
    2017: [
        # Jan-2025 refresh (matches the zip name documented in the CMS
        # Methodology & Data Dictionary: OP_DTL_PGYR2017_P01172025.zip).
        "https://download.cms.gov/openpayments/PGYR2017_P01172025/OP_DTL_PGYR2017_P01172025.zip",
        "https://download.cms.gov/openpayments/PGYR17_P01172025.zip",
        # Jan-2024 and Jun-2023 refreshes (per-publish sub-dir confirmed).
        "https://download.cms.gov/openpayments/PGYR17_P011824/OP_DTL_PGYR2017_P01182024.zip",
        "https://download.cms.gov/openpayments/PGYR17_P063023/OP_DTL_PGYR2017_P06302023.zip",
        # Flat-zip legacy fallback (old pre-2023 layout).
        "https://download.cms.gov/openpayments/PGYR17_P012019.zip",
        # Fallback, not a zip — kept for reference (direct CSV, would need
        # a code path change to consume):
        # https://download.cms.gov/openpayments/PGYR17_P063023/OP_DTL_GNRL_PGYR2017_P06302023.csv
    ],
    2016: [
        "https://download.cms.gov/openpayments/PGYR2016_P01172025/OP_DTL_PGYR2016_P01172025.zip",
        "https://download.cms.gov/openpayments/PGYR16_P01172025.zip",
        "https://download.cms.gov/openpayments/PGYR16_P011824/OP_DTL_PGYR2016_P01182024.zip",
        "https://download.cms.gov/openpayments/PGYR16_P063023/OP_DTL_PGYR2016_P06302023.zip",
        "https://download.cms.gov/openpayments/PGYR16_P012018.zip",
        # Direct-CSV fallback (reference only):
        # https://download.cms.gov/openpayments/PGYR16_P063023/OP_DTL_GNRL_PGYR2016_P06302023.csv
    ],
    2015: [
        "https://download.cms.gov/openpayments/PGYR2015_P01172025/OP_DTL_PGYR2015_P01172025.zip",
        "https://download.cms.gov/openpayments/PGYR15_P01172025.zip",
        "https://download.cms.gov/openpayments/PGYR15_P011824/OP_DTL_PGYR2015_P01182024.zip",
        "https://download.cms.gov/openpayments/PGYR15_P063023/OP_DTL_PGYR2015_P06302023.zip",
        # Original 2016 publication (pre-archival).
        "https://download.cms.gov/openpayments/PGYR15_P063016.zip",
        # Known CSV filename for the original publication:
        # OP_DTL_GNRL_PGYR2015_P06302016.csv
    ],
    2014: [
        "https://download.cms.gov/openpayments/PGYR2014_P01172025/OP_DTL_PGYR2014_P01172025.zip",
        "https://download.cms.gov/openpayments/PGYR14_P01172025.zip",
        "https://download.cms.gov/openpayments/PGYR14_P011824/OP_DTL_PGYR2014_P01182024.zip",
        "https://download.cms.gov/openpayments/PGYR14_P063023/OP_DTL_PGYR2014_P06302023.zip",
        # Original 2015 publication of PY2014.
        "https://download.cms.gov/openpayments/PGYR14_P063015.zip",
    ],
    2013: [
        # Note: PY2013 covers only Aug 1 - Dec 31 2013 (five months of
        # data — first program year post-Sunshine-Act). Row counts are
        # ~4.4M vs. >10M for a full year; features that normalize on
        # payment counts need to treat this year specially.
        "https://download.cms.gov/openpayments/PGYR2013_P01172025/OP_DTL_PGYR2013_P01172025.zip",
        "https://download.cms.gov/openpayments/PGYR13_P01172025.zip",
        "https://download.cms.gov/openpayments/PGYR13_P011824/OP_DTL_PGYR2013_P01182024.zip",
        "https://download.cms.gov/openpayments/PGYR13_P063023/OP_DTL_PGYR2013_P06302023.zip",
        # Original 2014 publication of PY2013 (first-ever OP release).
        "https://download.cms.gov/openpayments/PGYR13_P093014.zip",
    ],
}


def _candidate_urls(program_year: int) -> list[str]:
    """Probe URLs for a program year, most-likely first."""
    return list(_BULK_URL_CANDIDATES.get(program_year, []))


def download_openpayments_bulk(
    program_year: int,
    out_path: Path | None = None,
    max_size_gb: float = 2.0,
) -> Path:
    """Download the annual Open Payments General Payments zip, extract, write parquet.

    Parameters
    ----------
    program_year
        The CMS program year (2013 onwards). PY2013-PY2017 are archived
        off the live Search Tool but remain downloadable via
        download.cms.gov; the per-publish sub-directory layout CMS
        adopted in 2023 may mean the zip probes miss and a CSV-direct
        adapter is needed. See data/archive/working_notes/open_payments_coverage.md.
    out_path
        Target parquet path. Defaults to
        ``data/panels/open_payments_general_<year>.parquet``.
    max_size_gb
        Abort if the compressed zip exceeds this cap. General Payments
        zips have historically been ~800 MB-1.5 GB; 2.0 GB is a generous
        ceiling that guards against a pathological refresh.

    Raises
    ------
    RuntimeError
        If none of the candidate URLs respond.
    ValueError
        If the zip exceeds ``max_size_gb``.
    """
    out_path = out_path or (PROCESSED / f"open_payments_general_{program_year}.parquet")
    RAW.mkdir(parents=True, exist_ok=True)
    PROCESSED.mkdir(parents=True, exist_ok=True)

    zip_url: str | None = None
    for url in _candidate_urls(program_year):
        try:
            head = requests.head(url, timeout=15, allow_redirects=True)
            if head.status_code == 200:
                zip_url = url
                size_gb = int(head.headers.get("Content-Length", 0)) / (1024 ** 3)
                if size_gb > max_size_gb:
                    raise ValueError(
                        f"Open Payments PY{program_year} at {url} is "
                        f"{size_gb:.2f} GB compressed (> {max_size_gb} GB cap). "
                        f"Fall back to Socrata API via enrich_via_api() "
                        f"for a short NPI list."
                    )
                break
        except requests.RequestException:
            continue

    if not zip_url:
        raise RuntimeError(
            f"No Open Payments zip reachable for PY{program_year}. "
            f"Check https://www.cms.gov/OpenPayments/Data/Dataset-Downloads "
            f"for a filename change and add the URL to "
            f"_BULK_URL_CANDIDATES. In the meantime, enrich_via_api() "
            f"works for short NPI lists."
        )

    zip_path = RAW / Path(zip_url).name
    if not zip_path.exists():
        with requests.get(zip_url, stream=True, timeout=600) as r:
            r.raise_for_status()
            with open(zip_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    f.write(chunk)

    # The zip contains multiple CSVs — the General Payments detail file
    # is prefixed "OP_DTL_GNRL_PGYR". Research payments are DTL_RSRCH
    # and ownership are DTL_OWNRSHP; we skip those here.
    with zipfile.ZipFile(zip_path) as zf:
        general_csv = next(
            (n for n in zf.namelist()
             if re.match(r"OP_DTL_GNRL_PGYR\d{4}_P\d{8}.*\.csv$", n, re.IGNORECASE)),
            None,
        )
        if not general_csv:
            raise RuntimeError(
                f"General Payments CSV not found in {zip_path}. "
                f"Archive contents: {zf.namelist()[:10]}"
            )

        with zf.open(general_csv) as handle:
            header = pd.read_csv(handle, nrows=0, low_memory=False)
            present = [c for c in _KEEP_COLUMNS if c in header.columns]
            missing = [c for c in _KEEP_COLUMNS if c not in header.columns]
            if missing:
                print(f"[openpayments_ingest] warning: columns not in header: {missing}")

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

    thin = thin.rename(columns=_RENAME)
    # Drop rows with no NPI — Open Payments covers teaching hospitals
    # under a separate recipient type that has no NPI. Those don't join
    # onto Part B, so we discard them at ingest.
    thin = thin[thin["NPI"].notna() & (thin["NPI"].str.strip() != "")]
    thin["amount_usd"] = pd.to_numeric(thin["amount_usd"], errors="coerce")
    thin["date_of_payment"] = pd.to_datetime(thin["date_of_payment"], errors="coerce")
    thin.to_parquet(out_path, index=False)
    print(f"[openpayments_ingest] wrote {len(thin):,} rows → {out_path}")
    return out_path


# ---------------------------------------------------------------------------
# Feature table
# ---------------------------------------------------------------------------

def _nature_bucket(raw: str) -> str:
    """Map a CMS nature-of-payment string to a short bucket key."""
    if not isinstance(raw, str):
        return "other"
    key = raw.strip().lower()
    # Startswith match on a few long phrases first.
    for prefix, bucket in _NATURE_BUCKETS.items():
        if key == prefix or key.startswith(prefix):
            return bucket
    # Fallback: a substring scan catches CMS reworded variants.
    for prefix, bucket in _NATURE_BUCKETS.items():
        short = prefix.split(" or ")[0]
        if short in key:
            return bucket
    return "other"


def load_openpayments_features(
    program_year: int,
    parquet_path: Path | None = None,
) -> pd.DataFrame:
    """Aggregate the raw parquet into an NPI-indexed feature frame.

    Returns a DataFrame with columns:

      * ``NPI`` (str, zero-padded to 10 digits)
      * ``op_total_received_usd`` (float)
      * ``op_n_payments`` (Int64)
      * ``op_manufacturers_count`` (Int64)
      * ``op_top_manufacturer`` (str — dollar-weighted top)
      * ``op_n_meals`` / ``op_n_travel`` / ``op_n_speaker`` /
        ``op_n_royalty`` / ``op_n_consulting`` / ``op_n_education``
        (Int64 counts by nature-of-payment bucket)
      * ``op_has_royalty_income`` (bool)
      * ``op_specialty_pct`` (float — percentile of ``op_total_received_usd``
        within the NPI's specialty; NaN if specialty missing)

    If the parquet doesn't exist, ``FileNotFoundError`` is raised with a
    pointer to :func:`download_openpayments_bulk`.
    """
    parquet_path = parquet_path or (
        PROCESSED / f"open_payments_general_{program_year}.parquet"
    )
    if not parquet_path.exists():
        raise FileNotFoundError(
            f"{parquet_path} not found. Run "
            f"fraudlib.ingest.providers.openpayments.download_openpayments_bulk"
            f"({program_year}) first, or use enrich_via_api() for a short NPI list."
        )

    df = pd.read_parquet(parquet_path)
    if df.empty:
        return pd.DataFrame(columns=[
            "NPI", "op_total_received_usd", "op_n_payments",
            "op_manufacturers_count", "op_top_manufacturer",
            "op_n_meals", "op_n_travel", "op_n_speaker", "op_n_royalty",
            "op_n_consulting", "op_n_education",
            "op_has_royalty_income", "op_specialty_pct",
        ])

    df["NPI"] = df["NPI"].astype(str).str.zfill(10)
    df["bucket"] = df["nature_of_payment"].map(_nature_bucket)
    # Normalize amount to float, coerce NaN to 0 for aggregation.
    df["amount_usd"] = pd.to_numeric(df["amount_usd"], errors="coerce").fillna(0.0)

    # Totals, counts, manufacturer breadth.
    agg_main = df.groupby("NPI").agg(
        op_total_received_usd=("amount_usd", "sum"),
        op_n_payments=("record_id", "count"),
        op_manufacturers_count=("manufacturer", "nunique"),
    )

    # Dollar-weighted top manufacturer per NPI.
    top_manu = (
        df.groupby(["NPI", "manufacturer"])["amount_usd"]
        .sum()
        .reset_index()
        .sort_values(["NPI", "amount_usd"], ascending=[True, False])
        .drop_duplicates("NPI", keep="first")
        .set_index("NPI")["manufacturer"]
    )

    # Nature-of-payment bucket counts (wide).
    bucket_counts = (
        df.pivot_table(
            index="NPI",
            columns="bucket",
            values="record_id",
            aggfunc="count",
            fill_value=0,
        )
    )
    # Rename to op_n_<bucket>; we only expose a curated subset.
    exposed = ["meals", "travel", "speaker", "royalty", "consulting", "education"]
    for b in exposed:
        if b not in bucket_counts.columns:
            bucket_counts[b] = 0
    bucket_counts = bucket_counts[exposed].rename(
        columns={b: f"op_n_{b}" for b in exposed}
    )

    out = agg_main.join(bucket_counts, how="left").fillna(0)
    out["op_top_manufacturer"] = top_manu
    out["op_has_royalty_income"] = out["op_n_royalty"] > 0

    # Peer percentile of total_received within specialty. The parquet
    # carries specialty per-row; collapse to NPI-level mode first.
    npi_specialty = (
        df.dropna(subset=["specialty"])
        .groupby("NPI")["specialty"]
        .agg(lambda s: s.mode().iloc[0] if len(s.mode()) else None)
    )
    out = out.join(npi_specialty.rename("specialty"), how="left")
    # rank(pct=True) over non-null specialty groups.
    spec_pct = (
        out.dropna(subset=["specialty"])
        .groupby("specialty")["op_total_received_usd"]
        .rank(pct=True, method="average")
        * 100.0
    )
    out["op_specialty_pct"] = spec_pct
    out = out.drop(columns=["specialty"])

    # Int64 on counts, float on dollars.
    for c in ["op_n_payments", "op_manufacturers_count",
              "op_n_meals", "op_n_travel", "op_n_speaker",
              "op_n_royalty", "op_n_consulting", "op_n_education"]:
        out[c] = out[c].astype("Int64")
    out["op_total_received_usd"] = out["op_total_received_usd"].astype(float)

    out = out.reset_index()
    out = out[out["NPI"].str.len() == 10].drop_duplicates("NPI").reset_index(drop=True)
    return out


# ---------------------------------------------------------------------------
# Fallback: per-NPI Socrata-API enrichment
# ---------------------------------------------------------------------------

def enrich_via_api(
    npis: Iterable[str],
    program_year: int,
    dataset_id: str | None = None,
    page_size: int = 1000,
) -> pd.DataFrame:
    """Per-NPI Socrata-API fallback for a short NPI list.

    Same output schema as :func:`load_openpayments_features`. Use when
    the bulk parquet isn't on disk (rate-limit, connectivity, or a
    pathological year where the zip exceeds the size cap).

    ``dataset_id`` — optional explicit Socrata dataset id. If ``None``,
    looked up via :func:`_resolve_dataset_id`. Returns an empty frame
    if resolution fails.
    """
    dataset_id = dataset_id or _resolve_dataset_id(program_year, kind="general")
    if not dataset_id:
        print(
            f"[openpayments_ingest] could not resolve Socrata dataset id for "
            f"PY{program_year} — pass dataset_id explicitly or ingest the bulk zip."
        )
        return pd.DataFrame()

    npi_list = [str(n).strip().lstrip("0") for n in npis if str(n).strip()]
    if not npi_list:
        return pd.DataFrame()

    all_rows: list[dict] = []
    url = _SOCRATA_RESOURCE.format(dataset_id=dataset_id)
    for npi in npi_list:
        offset = 0
        while True:
            params = {
                "covered_recipient_npi": npi,
                "$limit": page_size,
                "$offset": offset,
            }
            try:
                r = requests.get(url, params=params, timeout=60)
                r.raise_for_status()
                page = r.json()
            except (requests.RequestException, ValueError):
                break
            if not page:
                break
            all_rows.extend(page)
            if len(page) < page_size:
                break
            offset += page_size

    if not all_rows:
        return pd.DataFrame()

    api_df = pd.DataFrame(all_rows)
    # Socrata column names are lower_snake and a bit different from the
    # bulk CSV. Map what we need to the same internal schema before
    # delegating to the aggregator.
    api_rename = {
        "covered_recipient_npi": "NPI",
        "covered_recipient_type": "recipient_type",
        "covered_recipient_specialty_1": "specialty",
        "recipient_state": "state",
        "applicable_manufacturer_or_applicable_gpo_making_payment_name": "manufacturer",
        "total_amount_of_payment_usdollars": "amount_usd",
        "date_of_payment": "date_of_payment",
        "nature_of_payment_or_transfer_of_value": "nature_of_payment",
        "name_of_drug_or_biological_or_device_or_medical_supply_1": "product_name",
        "program_year": "program_year",
        "record_id": "record_id",
    }
    api_df = api_df.rename(columns=api_rename)
    needed = set(_RENAME.values())
    for col in needed:
        if col not in api_df.columns:
            api_df[col] = pd.NA
    api_df["amount_usd"] = pd.to_numeric(api_df["amount_usd"], errors="coerce")

    # Write to a temp parquet and delegate to the aggregator so feature
    # logic stays in one place.
    tmp = PROCESSED / f"_openpayments_api_tmp_{program_year}.parquet"
    PROCESSED.mkdir(parents=True, exist_ok=True)
    api_df.to_parquet(tmp, index=False)
    try:
        feats = load_openpayments_features(program_year, parquet_path=tmp)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    return feats
