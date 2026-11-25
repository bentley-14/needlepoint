"""Historical NPPES snapshots — one DataFrame per (year, month).

Sister module to ``nppes_ingest.py``. Where that module fetches the
*latest* rolling NPPES dump for point-in-time enrichment, this module
reaches backwards in time to grab a single historical snapshot so
temporal backtests can join the right-vintage NPPES.

Motivation
----------
Our only on-disk NPPES snapshot is April 2026. That's anachronistic
for any pre-2026 backtest — deactivated NPIs have been dropped from
the current file, and post-2023 new enumerations are present. Both
leak into features if a 2019 Part B backtest joins the April 2026
NPPES. One vintaged snapshot per year closes that leak.

URL pattern (confirmed 2026-04-23)
----------------------------------
CMS publishes full-file monthly zips at::

    https://download.cms.gov/nppes/NPPES_Data_Dissemination_{Month}_{YYYY}.zip

Where ``{Month}`` is the *full English month name* with the first
letter capitalized (``January``, ``February``, ... ``December``) and
``{YYYY}`` is a 4-digit year. Examples:

    NPPES_Data_Dissemination_January_2019.zip
    NPPES_Data_Dissemination_April_2020.zip
    NPPES_Data_Dissemination_July_2023.zip

CMS retains roughly a 5-year rolling window (confirmed back to ~2015
as of this writing). Older months (2007-2014) are not served by CMS
directly; third-party archives (NBER, CarePrecise) hold those.
Since our cohort starts at 2013-2014, the early-window gap is
handled by graceful-failure — fall back to an empty DataFrame rather
than raise.

The zip interior matches the rolling dump: one main CSV named
``npidata_pfile_<startdate>-<enddate>.csv`` plus a handful of
supplementary pfiles we don't need.

What this emits
---------------
:func:`fetch_nppes_snapshot` returns a DataFrame with the same columns
that ``nppes_ingest._RENAME`` produces (NPI, Entity_Type_Code,
Provider_Organization_Name, Provider_Last_Name_Legal_Name,
Provider_First_Name, Provider_Credential_Text,
Healthcare_Provider_Taxonomy_Code_1, Is_Sole_Proprietor,
Enumeration_Date, Last_Update_Date), **plus**:

  * ``snapshot_date`` — ISO date string (e.g. ``"2019-01-01"``)
    identifying the NPPES vintage. Downstream joins key on this so
    a 2019 backtest joins rows where ``snapshot_date == 2019-01-01``.

Graceful-failure contract
-------------------------
If the requested month is missing, we try adjacent months (±1, ±2)
before giving up. On total failure we emit a stderr warning and
return an empty DataFrame with the correct schema — *never* raise.
This matches the pattern in
``scripts/ingest/fetch_state_medicaid_ca.py``: one source failing
should not block a multi-year fetch loop.

Memory / streaming
------------------
The zip is ~700 MB compressed / ~7 GB uncompressed. We stream:
  * HTTP: ``requests.get(stream=True)`` → 1 MB chunks written to disk
  * CSV: ``pd.read_csv(chunksize=500_000)``; project to the thin
    column slice on each chunk before concat.

Not a replacement
-----------------
``nppes_ingest.py`` remains the canonical ingestion path for the
*current* rolling snapshot (feeds ``load_nppes_features`` and
``compute_nppes_features``). This module is only for temporally
correct backtests.
"""
from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path

import pandas as pd
import requests

# Reuse the thin-column schema from the rolling-snapshot module so both
# code paths emit identical frames. If nppes_ingest ever drifts, this
# module drifts with it — intentional.
from fraudlib.ingest.providers.nppes_ingest import _KEEP_COLUMNS, _RENAME

RAW = Path("data/raw") / "nppes_historical"

_BULK_URL_FMT = "https://download.cms.gov/nppes/NPPES_Data_Dissemination_{month}_{year}.zip"

# Full English month names, capitalized. The order the CLI tries adjacent
# fallbacks in starts with the user-specified month and walks ±1, ±2.
_MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

# Columns the caller of :func:`fetch_nppes_snapshot` can count on, even
# when the fetch fails and we emit an empty frame.
_OUTPUT_COLUMNS = list(_RENAME.values()) + ["snapshot_date"]


def _month_index(month: str) -> int:
    """Resolve a month name (any capitalization) → 1-based index."""
    norm = month.strip().capitalize()
    if norm not in _MONTHS:
        raise ValueError(f"unknown month name: {month!r}")
    return _MONTHS.index(norm) + 1


def _adjacent_months(year: int, month: str, radius: int = 2) -> list[tuple[int, str]]:
    """Return (year, month) candidates — user's choice first, then ±1, ±2.

    Walks both directions so an "April" request first tries April, then
    May, March, June, February (in that order) before giving up.
    """
    base_idx = _month_index(month)  # 1-based
    out: list[tuple[int, str]] = [(year, _MONTHS[base_idx - 1])]
    for offset in range(1, radius + 1):
        for sign in (1, -1):
            m = base_idx + sign * offset
            y = year
            while m > 12:
                m -= 12
                y += 1
            while m < 1:
                m += 12
                y -= 1
            out.append((y, _MONTHS[m - 1]))
    return out


def _empty_frame() -> pd.DataFrame:
    """Return a 0-row DataFrame with the expected output schema."""
    return pd.DataFrame({col: pd.Series(dtype="object") for col in _OUTPUT_COLUMNS})


def _download_zip(url: str, dest: Path, timeout: int = 600) -> bool:
    """Stream ``url`` → ``dest``. Return True on 200, False on 404.

    Raises on any non-404 error so unexpected failures (rate limits,
    DNS, timeouts) still surface — graceful-failure in the caller
    catches those explicitly.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=timeout) as r:
        if r.status_code == 404:
            return False
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):  # 1 MB
                if chunk:
                    f.write(chunk)
    return True


def _extract_main_csv(zip_path: Path) -> pd.DataFrame:
    """Stream the main npidata pfile CSV out of ``zip_path``, projected thin."""
    with zipfile.ZipFile(zip_path) as zf:
        main_csv = next(
            (
                n for n in zf.namelist()
                if re.match(r"npidata_pfile_\d{8}-\d{8}\.csv$", n)
            ),
            None,
        )
        if not main_csv:
            raise RuntimeError(
                f"main npidata pfile CSV not found in {zip_path.name}; "
                f"archive contents: {zf.namelist()[:10]}"
            )

        # Sniff header to tolerate column-name drift across years
        # (CMS has renamed a handful of columns historically).
        with zf.open(main_csv) as handle:
            header = pd.read_csv(handle, nrows=0)
        present = [c for c in _KEEP_COLUMNS if c in header.columns]
        missing = [c for c in _KEEP_COLUMNS if c not in header.columns]
        if missing:
            print(
                f"[nppes_historical] columns not in {main_csv}: {missing}",
                file=sys.stderr,
            )

        # Stream chunks to keep peak memory bounded.
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

    return thin.rename(columns=_RENAME)


def fetch_nppes_snapshot(
    year: int,
    month: str = "January",
    *,
    cache_dir: Path | None = None,
    keep_zip: bool = True,
) -> pd.DataFrame:
    """Download one historical NPPES monthly snapshot → thin DataFrame.

    Parameters
    ----------
    year
        4-digit publication year (e.g. 2019).
    month
        Full English month name (default "January"). Case-insensitive.
        We treat month-of-January as "the year's NPPES snapshot" for
        backtest joins; callers can override for seasonal studies.
    cache_dir
        Where to save the downloaded zip. Defaults to
        ``data/raw/nppes_historical/``. Reused across calls so the
        CLI fetching 2014-2023 sequentially doesn't re-download.
    keep_zip
        If False, delete the zip after extract. Default True — these
        zips are ~700 MB each so re-downloading is expensive.

    Returns
    -------
    pd.DataFrame
        Thin projected frame (same columns as nppes_ingest) plus a
        ``snapshot_date`` column (ISO ``YYYY-MM-01`` string for the
        *attempted* month, even if we fell back to an adjacent one —
        downstream joins can key on year alone and ignore month drift).

        On unrecoverable fetch failure, an empty DataFrame with the
        correct schema is returned and a warning is logged to stderr.

    Notes
    -----
    We try the user-specified month first, then ±1, ±2 months. CMS's
    ~5-year rolling archive means requests for 2014-2015 snapshots
    will very often return empty — that's expected and the caller
    should handle it.
    """
    cache_dir = cache_dir or RAW
    snapshot_date = f"{year:04d}-{_month_index(month):02d}-01"

    candidates = _adjacent_months(year, month, radius=2)
    last_http_error: Exception | None = None

    for try_year, try_month in candidates:
        url = _BULK_URL_FMT.format(month=try_month, year=try_year)
        zip_path = cache_dir / f"NPPES_Data_Dissemination_{try_month}_{try_year}.zip"

        try:
            if not zip_path.exists():
                ok = _download_zip(url, zip_path)
                if not ok:
                    # 404 — try the next candidate month.
                    continue
            else:
                print(
                    f"[nppes_historical] cache hit: {zip_path}",
                    file=sys.stderr,
                )

            df = _extract_main_csv(zip_path)
        except Exception as e:  # noqa: BLE001
            last_http_error = e
            print(
                f"[nppes_historical] {try_month} {try_year} failed "
                f"({type(e).__name__}: {e}); trying adjacent month",
                file=sys.stderr,
            )
            # Nuke a partial/corrupt zip before the next candidate.
            if zip_path.exists() and not keep_zip:
                try:
                    zip_path.unlink()
                except OSError:
                    pass
            continue

        # Success: tag with the *requested* snapshot_date so backtest
        # joins are deterministic across retries.
        df["snapshot_date"] = snapshot_date

        if not keep_zip and zip_path.exists():
            try:
                zip_path.unlink()
            except OSError:
                pass

        if try_year != year or try_month.lower() != month.lower():
            print(
                f"[nppes_historical] requested {month} {year} unavailable; "
                f"served {try_month} {try_year} instead (tagged snapshot_date={snapshot_date})",
                file=sys.stderr,
            )
        else:
            print(
                f"[nppes_historical] {month} {year}: {len(df):,} rows "
                f"(snapshot_date={snapshot_date})",
                file=sys.stderr,
            )
        return df

    # All candidates exhausted. Return empty frame + warning so a
    # multi-year CLI loop can continue with the next year.
    msg = (
        f"[nppes_historical] NO snapshot available for {month} {year} "
        f"(tried ±2 months around {month}). Returning empty frame."
    )
    if last_http_error is not None:
        msg += f" Last error: {type(last_http_error).__name__}: {last_http_error}"
    print(msg, file=sys.stderr)
    out = _empty_frame()
    out["snapshot_date"] = pd.Series(dtype="object")  # keep column present
    return out
