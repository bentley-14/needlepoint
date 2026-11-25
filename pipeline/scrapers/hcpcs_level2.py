"""HCPCS Level II reference scraper.

Fetches CMS's Alpha-Numeric HCPCS file (the HCPCS Quarterly Update) and produces
the full HCPCS Level II code dictionary — every A/E/G/J/K/L/Q/… code with its
official descriptor, drug-vs-procedure flag, and parsed billing unit, including
the ~7,200 codes never billed under Part B. Lets agents resolve a code's
identity (e.g. ``J3304`` → triamcinolone, per 1 mg) from ground truth instead of
guessing.

Scope boundary: HCPCS **Level II** only. CPT (5-digit numeric) is AMA-copyrighted,
is not in the CMS file, and is not redistributable — CPT descriptors come from
the Part B PUF (billed codes) instead.

Output: ``data/ground_truth/hcpcs_level2_full.parquet`` (+ ``.manifest.json``).
Run:    ``python -m pipeline.scrapers.hcpcs_level2``
"""
from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Optional, Type

import pandas as pd

from pipeline.base import BaseScraper, BaseScraperSchema, RunResult
from pipeline.scrapers.download import RAW_DIR, _download

# CMS HCPCS Quarterly Update. Bump both for a newer quarter (the URL slug and the
# RELEASE stamp move together); the file layout is stable across quarters.
SRC_URL = "https://www.cms.gov/files/zip/january-2025-alpha-numeric-hcpcs-file.zip"
RELEASE = "2025-JAN"

GROUND_TRUTH_DIR = Path(__file__).resolve().parents[2] / "data" / "ground_truth"

_DOSE_UNITS = r"(mcg|mg|ml|gm|g|iu|meq|units|unit|billion units|million units|cc)"


def parse_dose_unit(desc: str) -> Optional[str]:
    """Drug-dose billing unit parsed from a CMS descriptor, or None (procedure).

    Drug J-codes encode their unit in the descriptor (``…, 1 mg`` / ``per 5 mg``
    / ``per dose``). Returning it is the guardrail against reading a per-mg unit
    count as an injection count.
    """
    d = (desc or "").lower().strip().rstrip(".")
    if "per dose" in d:
        return "per dose"
    m = re.search(rf"(\d+(?:\.\d+)?)\s*{_DOSE_UNITS}\b", d)
    return f"{m.group(1)} {m.group(2)}" if m else None


def code_type(code: str, is_drug: bool) -> str:
    if code[:1].isdigit():
        return "CPT (procedure/E&M)"
    if is_drug:
        return "HCPCS-II drug/biological"
    fam = {
        "A": "HCPCS-II supplies/transport", "E": "HCPCS-II DME", "K": "HCPCS-II DME",
        "L": "HCPCS-II orthotics/prosthetics", "G": "HCPCS-II temporary (G)",
        "Q": "HCPCS-II temporary (Q)", "C": "HCPCS-II outpatient (C)",
        "P": "HCPCS-II pathology", "J": "HCPCS-II (J)", "V": "HCPCS-II vision/hearing",
    }
    return fam.get(code[:1].upper(), "HCPCS-II")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


@dataclass(kw_only=True)
class HcpcsLevel2Schema(BaseScraperSchema):
    """Full HCPCS Level II code dictionary (one row per code).

    This is a reference table — neither NPI- nor URL-keyed — so it inherits the
    base stamps (``source`` / ``fetched_at_utc`` / ``schema_version``) directly
    rather than :class:`ProviderRecordSchema` / :class:`DocumentSchema`.
    """

    code: str
    descriptor: str
    short_descriptor: Optional[str] = None
    is_drug: bool = False
    unit_basis: str = "per service"     # "1 mg" / "per dose" for drugs; else "per service"
    code_type: str = ""
    release: str = ""                    # CMS quarter, e.g. "2025-JAN"


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------


class HcpcsLevel2Scraper(BaseScraper):
    """Scrape the CMS Alpha-Numeric HCPCS Level II file → parquet + manifest."""

    source: ClassVar[str] = "hcpcs_level2"
    schema: ClassVar[Type[BaseScraperSchema]] = HcpcsLevel2Schema
    output_dir: ClassVar[Path] = GROUND_TRUTH_DIR
    schema_version: ClassVar[str] = "v1.0"

    def fetch(self, *, url: str = SRC_URL, release: str = RELEASE) -> RunResult:
        started = self._now()
        zip_path = RAW_DIR / "hcpcs" / f"alpha-numeric-hcpcs-{release.lower()}.zip"
        _download(url, zip_path)
        df = self._parse(zip_path.read_bytes(), release=release)
        df = self._stamp_base_columns(df)
        return self._write(
            df,
            name="hcpcs_level2_full",
            started_at_utc=started,
            inputs={"url": url, "release": release, "local_zip": str(zip_path)},
        )

    @staticmethod
    def _parse(zip_bytes: bytes, *, release: str) -> pd.DataFrame:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
        xlsx = next(
            n for n in zf.namelist()
            if n.upper().endswith(".XLSX") and "ANWEB" in n.upper()
            and "CORRECTION" not in n.upper() and "TRANSACTION" not in n.upper()
        )
        with zf.open(xlsx) as fh:
            raw = pd.read_excel(fh, dtype=str)

        df = pd.DataFrame()
        df["code"] = raw["HCPC"].str.strip()
        df["descriptor"] = raw["LONG DESCRIPTION"].fillna(raw["SHORT DESCRIPTION"]).str.strip()
        df["short_descriptor"] = raw["SHORT DESCRIPTION"].str.strip()
        df = df[df["code"].notna() & (df["code"] != "")].copy()

        dose = [parse_dose_unit(d) for d in df["descriptor"]]
        df["is_drug"] = [u is not None for u in dose]
        df["unit_basis"] = [u if u else "per service" for u in dose]
        df["code_type"] = [code_type(c, b) for c, b in zip(df["code"], df["is_drug"])]
        df["release"] = release
        return df.drop_duplicates("code").sort_values("code").reset_index(drop=True)


def main() -> int:
    result = HcpcsLevel2Scraper().fetch()
    print(f"{result.source}: {result.rows} codes -> {result.artifact_path} "
          f"({result.bytes:,} bytes) + {result.artifact_path.stem}.manifest.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
