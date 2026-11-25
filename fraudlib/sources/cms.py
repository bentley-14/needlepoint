"""CMS PUF panel loaders — pure-disk, never the network.

Reads the canonical annual CMS Part B / Part D / DMEPOS panels written by
``pipeline.scrapers.download``. If a panel is missing, raises
``FileNotFoundError`` with the regeneration command rather than silently
fetching from CMS.

Hydrate the panels with one of:

    make pull-data                 # fast path: rclone from R2 mirror
    make fetch-data YEAR=<year>    # slow path: fetch directly from CMS
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd


PANELS_DIR = Path(__file__).resolve().parents[2] / "data" / "panels"


def _load_panel(name: str) -> pd.DataFrame:
    path = PANELS_DIR / f"{name}.parquet"
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Run `make pull-data` to hydrate from R2, "
            f"or `make fetch-data YEAR=<year>` for a direct CMS fetch."
        )
    return pd.read_parquet(path)


def load_partb_panel(year: int) -> pd.DataFrame:
    """CMS Part B by-Provider PUF for ``year`` (one row per NPI)."""
    return _load_panel(f"cms_partb_{year}")


def load_partd_panel(year: int) -> pd.DataFrame:
    """CMS Part D by-Provider PUF for ``year`` (one row per NPI)."""
    return _load_panel(f"cms_partd_{year}")


def load_dmepos_panel(year: int) -> pd.DataFrame:
    """CMS DMEPOS by-Referring-Provider PUF for ``year`` (one row per NPI)."""
    return _load_panel(f"cms_dmepos_{year}")
