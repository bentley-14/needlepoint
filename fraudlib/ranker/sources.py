"""DataSource hierarchy — one class per CMS data source.

Each subclass owns its own feature engineering inside ``load()``, reading
from on-disk parquets via ``fraudlib.sources`` loaders. The pattern is
identical across all three live sources:

    PartBSource.load()  →  features.engineer(load_partb_panel(year))
    PartDSource.load()  →  partd_stats.compute_partd_features(year)  [+ reshape]
    NppesSource.load()  →  nppes_ingest.compute_nppes_features(year)

Loaders never reach the network. If a panel is missing, the loader raises
``FileNotFoundError`` with the regeneration command. To populate panels:
``make pull-data`` (fast path, R2) or ``make fetch-data YEAR=<y>`` (slow
path, direct CMS).

Adding a new source
-------------------
1. Write compute_<source>_features(year) in the relevant pipeline module.
2. Subclass DataSource, implement load() as a single delegation.
3. Set name and (if needed) join_strategy as ClassVar.
4. Add to SOURCE_REGISTRY at the bottom.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import pandas as pd

from fraudlib.ranker import features
from fraudlib.sources.cms import load_partb_panel
from fraudlib.ingest.cms import partd_stats


@dataclass
class DataSource(ABC):
    """Base class for a CMS data source joinable on NPI.

    Subclasses set class-level ``name`` / ``join_strategy`` and implement
    ``load()``. Feature engineering belongs inside ``load()`` — each source
    owns its own transformations (same pattern as PartBSource calling
    features.engineer()).

    join_strategy
        ``"multisource"`` — outer-joined via features.concat_multisource
                            (handles prefix, has_* flag, zero-fill).
        ``"left"``        — plain left-merge onto the spine after concat.
    """

    year: int

    name: ClassVar[str]
    join_strategy: ClassVar[str] = "multisource"

    @abstractmethod
    def load(self) -> pd.DataFrame:
        """Fetch, cache, engineer, and return a provider-level DataFrame.

        Contract:
        - Must contain a ``"npi"`` column (str, 10-digit identifier).
        - All feature columns must be prefixed ``"<name>_"`` to avoid
          column collisions when joined with other sources.
        """

    def is_available(self) -> bool:
        """Return True if the on-disk cache already exists."""
        return True


# ---------------------------------------------------------------------------
# Part B
# ---------------------------------------------------------------------------

@dataclass
class PartBSource(DataSource):
    """CMS Part B physician/supplier PUF — the mandatory spine.

    Engineering: normalize_schema() renames CMS columns to snake_case,
    then engineer() adds 4 derived ratios (services_per_beneficiary,
    payment_per_service, submitted_to_allowed_ratio, hcpcs_diversity).
    """

    name: ClassVar[str] = "partb"
    join_strategy: ClassVar[str] = "multisource"

    def load(self) -> pd.DataFrame:
        return features.engineer(load_partb_panel(self.year))


# ---------------------------------------------------------------------------
# Part D
# ---------------------------------------------------------------------------

@dataclass
class PartDSource(DataSource):
    """CMS Part D prescriber PUF — opioid share, cost/claim, utilisation.

    Downloads ~100 MB on first run, cached as parquet after that.
    Engineering is handled by partd_stats.compute_partd_features().
    """

    name: ClassVar[str] = "partd"
    join_strategy: ClassVar[str] = "multisource"

    def load(self) -> pd.DataFrame:
        df = (
            partd_stats.compute_partd_features(year=self.year)
            .reset_index()
            .rename(columns={"Rndrng_NPI": "npi"})
        )
        df["npi"] = df["npi"].astype(str)
        return df


# ---------------------------------------------------------------------------
# NPPES
# ---------------------------------------------------------------------------

@dataclass
class NppesSource(DataSource):
    """NPPES provider registry — enumeration date, credential, sole-proprietor.

    Engineering is handled by nppes_ingest.compute_nppes_features(year),
    which owns all bucketing logic (credential → 7 buckets, NUCC taxonomy
    → 25 specialty buckets). Mirrors the pattern of PartBSource delegating
    to features.engineer() and PartDSource delegating to compute_partd_features().

    Requires download_nppes_bulk() once (~189 MB). is_available() returns
    False until that parquet is present so load_training_frame skips silently.
    """

    name: ClassVar[str] = "nppes"
    join_strategy: ClassVar[str] = "left"

    _PARQUET: ClassVar[Path] = Path("data/registries/nppes_bulk.parquet")

    def is_available(self) -> bool:
        return self._PARQUET.exists()

    def load(self) -> pd.DataFrame:
        from fraudlib.ingest.providers.nppes_ingest import compute_nppes_features
        return compute_nppes_features(year=self.year)


# ---------------------------------------------------------------------------
# DMEPOS (stub — available 2014+, ingest not yet implemented)
# ---------------------------------------------------------------------------

@dataclass
class DmeposSource(DataSource):
    """CMS DMEPOS referring-provider PUF — brace mills, DME fraud signals.

    Available 2014-2023. Reads ``data/panels/cms_dmepos_<year>.parquet``
    and applies ``fraudlib.ingest.cms.dmepos_stats.compute_dmepos_features``
    — see that module for the engineered feature set (supplier
    concentration, category mix DME/POS/Drug, submitted-to-allowed
    ratios, suppression flags).

    join_strategy = "multisource" — joins on NPI alongside Part B and
    Part D in concat_multisource. Each output column is prefixed
    ``dmepos_`` by the multisource step.
    """

    name: ClassVar[str] = "dmepos"
    join_strategy: ClassVar[str] = "multisource"

    _PANEL: ClassVar[Path] = Path("data/panels")

    def is_available(self) -> bool:
        return (self._PANEL / f"cms_dmepos_{self.year}.parquet").exists()

    def load(self) -> pd.DataFrame:
        from fraudlib.ingest.cms.dmepos_stats import compute_dmepos_features
        return compute_dmepos_features(self.year)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

@dataclass
class HcpcsSource(DataSource):
    """HCPCS-level code-mix features built from the Part B by-Provider-by-Service PUF.

    Pre-engineered parquets at ``data/panels/cms_hcpcs_features_<year>.parquet``
    produced by ``fraudlib.ingest.cms.hcpcs_stats.write_year``. Indexed by
    ``Rndrng_NPI`` with 15 ratios (E&M upcoding, J-code shares, top-3 code
    concentration, etc.) — see hcpcs_stats.py docstring.

    Available: 2014, 2016, 2017, 2018, 2019-2023. Years 2013, 2015 have no
    published Prov_Svc PUF; is_available() returns False for those.

    join_strategy = "left" — joins onto the existing Part B/D/NPPES panel
    on NPI. Output columns are prefixed ``hcpcs_`` to align with the
    partb_/partd_/nppes_ naming convention used by concat_multisource.
    """

    name: ClassVar[str] = "hcpcs"
    join_strategy: ClassVar[str] = "left"

    def is_available(self) -> bool:
        return (Path("data/panels") / f"cms_hcpcs_features_{self.year}.parquet").exists()

    def load(self) -> pd.DataFrame:
        path = Path("data/panels") / f"cms_hcpcs_features_{self.year}.parquet"
        df = pd.read_parquet(path)
        # Index → column for the standard NPI join, prefix all features.
        df = df.reset_index().rename(columns={"Rndrng_NPI": "npi"})
        df["npi"] = df["npi"].astype(str)
        feat_cols = [c for c in df.columns if c != "npi"]
        df = df.rename(columns={c: f"hcpcs_{c}" for c in feat_cols})
        return df


SOURCE_REGISTRY: dict[str, type[DataSource]] = {
    "partb":  PartBSource,
    "partd":  PartDSource,
    "nppes":  NppesSource,
    "dmepos": DmeposSource,
    "hcpcs":  HcpcsSource,
}
