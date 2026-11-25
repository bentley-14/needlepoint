"""Fetch MedPAC Reports to the Congress (March + June annual editions).

MedPAC is the independent congressional advisory commission on
Medicare. Each March it publishes *Medicare Payment Policy*; each June,
*Medicare and the Health Care Delivery System*. Together the two
reports frame what Congress sees as the live tensions in the program —
coverage gaps, overpayment patterns, specialty-specific
overutilization, and areas where CMS policy is already being
reconsidered. That context is directly on-thesis for novel-fraud
hunting: MedPAC is often the first place a pattern is named before
CMS acts on it.

The MedPAC site has migrated URL schemes twice in the window we care
about (2020-2024):

* 2020 reports live under ``/wp-content/uploads/import_data/scrape_files/``
* 2021 March + June are split (wp-content year-folder vs. the
  import_data path); one 2021 filename has a typo (``Congressz``).
* 2022 onward live under the clean ``/wp-content/uploads/YYYY/MM/`` path
  but inject ``_v3`` / ``_v5`` suffixes per errata reissue.

We hardcode the full HEAD-verified URLs rather than inferring them —
inference broke three times during development.

Run from the repo root:

    /opt/anaconda3/bin/python -m fraudlib.ingest.policy.fetch_medpac
    /opt/anaconda3/bin/python -m fraudlib.ingest.policy.fetch_medpac --force
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import requests

from fraudlib.ingest.extract.chunking import Chunk, split_by_heading, split_by_window
from fraudlib.ingest.extract.normalize import PolicyRecord, chunks_to_records, write_jsonl
from fraudlib.ingest.extract.pdf_extract import extract_pdf_to_markdown, page_count

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
RAW_DIR = PROJECT_ROOT / "data" / "raw" / "medpac"
CORPUS_PATH = PROJECT_ROOT / "data" / "ground_truth" / "policy_guidelines.jsonl"
MANIFEST_PATH = PROJECT_ROOT / "data" / "ground_truth" / "policy_manifest.json"

_USER_AGENT = "fraudlib-ingest/0.1 (research corpus; healthcare-fraud-detection)"
_HTTP_TIMEOUT_S = 300


@dataclass(frozen=True)
class ReportSpec:
    """One MedPAC Report to the Congress PDF."""

    year: int
    month: int  # 3 = March (payment policy), 6 = June (delivery system)
    title: str
    url: str
    min_pages: int = 150  # reports are long — degraded download if much shorter

    @property
    def doc_id_root(self) -> str:
        return f"medpac_{self.year}_{self.month:02d}"

    @property
    def local_name(self) -> str:
        return f"medpac_{self.year}_{self.month:02d}.pdf"

    @property
    def effective_date(self) -> str:
        # Reports land the 15th; good enough for retrieval-date filters.
        return f"{self.year}-{self.month:02d}-15"


# HEAD-verified 2026-04-21. Keep these pinned — regenerating from the
# document-type listing is flaky (MedPAC paginates old years, and some
# 2020-2021 entries have /document/ slugs that themselves contain the
# raw filename).
MEDPAC_REPORTS: list[ReportSpec] = [
    ReportSpec(
        2020, 3,
        "March 2020 Report to the Congress: Medicare Payment Policy",
        "https://www.medpac.gov/wp-content/uploads/import_data/scrape_files/docs/default-source/reports/mar20_entirereport_sec.pdf",
    ),
    ReportSpec(
        2020, 6,
        "June 2020 Report to the Congress: Medicare and the Health Care Delivery System",
        "https://www.medpac.gov/wp-content/uploads/import_data/scrape_files/docs/default-source/reports/jun20_reporttocongress_sec.pdf",
    ),
    ReportSpec(
        2021, 3,
        "March 2021 Report to the Congress: Medicare Payment Policy",
        # Note "Congressz" — MedPAC typo in the published filename.
        "https://www.medpac.gov/wp-content/uploads/2021/03/Mar21_MedPAC_Report_To_The_Congressz_v2_SEC.pdf",
    ),
    ReportSpec(
        2021, 6,
        "June 2021 Report to the Congress: Medicare and the Health Care Delivery System",
        "https://www.medpac.gov/wp-content/uploads/import_data/scrape_files/docs/default-source/reports/jun21_medpac_report_to_congress_sec.pdf",
    ),
    ReportSpec(
        2022, 3,
        "March 2022 Report to the Congress: Medicare Payment Policy",
        "https://www.medpac.gov/wp-content/uploads/2022/03/Mar22_MedPAC_ReportToCongress_v3_SEC.pdf",
    ),
    ReportSpec(
        2022, 6,
        "June 2022 Report to the Congress: Medicare and the Health Care Delivery System",
        "https://www.medpac.gov/wp-content/uploads/2022/06/Jun22_MedPAC_Report_to_Congress_v5_SEC.pdf",
    ),
    ReportSpec(
        2023, 3,
        "March 2023 Report to the Congress: Medicare Payment Policy",
        "https://www.medpac.gov/wp-content/uploads/2023/03/Mar23_MedPAC_Report_To_Congress_SEC.pdf",
    ),
    ReportSpec(
        2023, 6,
        "June 2023 Report to the Congress: Medicare and the Health Care Delivery System",
        "https://www.medpac.gov/wp-content/uploads/2023/06/Jun23_MedPAC_Report_To_Congress_SEC.pdf",
    ),
    ReportSpec(
        2024, 3,
        "March 2024 Report to the Congress: Medicare Payment Policy",
        "https://www.medpac.gov/wp-content/uploads/2024/03/Mar24_MedPAC_Report_To_Congress_SEC.pdf",
    ),
    ReportSpec(
        2024, 6,
        "June 2024 Report to the Congress: Medicare and the Health Care Delivery System",
        "https://www.medpac.gov/wp-content/uploads/2024/06/Jun24_MedPAC_Report_To_Congress_SEC.pdf",
    ),
]

_MIN_CHUNKS_PER_REPORT = 10


def _raw_pdf_path(spec: ReportSpec) -> Path:
    return RAW_DIR / spec.local_name


def _download_pdf(spec: ReportSpec, *, force: bool = False) -> Path:
    path = _raw_pdf_path(spec)
    if path.exists() and not force and path.stat().st_size > 500_000:
        return path

    path.parent.mkdir(parents=True, exist_ok=True)
    resp = requests.get(
        spec.url,
        headers={"User-Agent": _USER_AGENT},
        timeout=_HTTP_TIMEOUT_S,
        stream=True,
    )
    resp.raise_for_status()
    with open(path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=64 * 1024):
            if chunk:
                f.write(chunk)
    return path


def _report_to_chunks(pdf_path: Path) -> list[Chunk]:
    markdown = extract_pdf_to_markdown(pdf_path)
    chunks = split_by_heading(markdown)
    if not chunks:
        chunks = split_by_window(markdown)
    return chunks


def _load_manifest() -> dict:
    if not MANIFEST_PATH.exists():
        return {"version": 1, "sources": {}}
    try:
        return json.loads(MANIFEST_PATH.read_text())
    except json.JSONDecodeError:
        return {"version": 1, "sources": {}}


def _save_manifest(manifest: dict) -> None:
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2) + "\n")


def _existing_corpus_hashes() -> set[str]:
    if not CORPUS_PATH.exists():
        return set()
    hashes: set[str] = set()
    for line in CORPUS_PATH.read_text().splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        h = rec.get("content_hash") or ""
        if h:
            hashes.add(h)
    return hashes


def run(*, force: bool = False, fetch_date: str | None = None) -> dict:
    fetch_date = fetch_date or dt.date.today().isoformat()
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    manifest = _load_manifest()
    already = _existing_corpus_hashes()

    per_report: list[dict] = []
    all_new: list[PolicyRecord] = []

    for spec in MEDPAC_REPORTS:
        pdf_path = _download_pdf(spec, force=force)
        pages = page_count(pdf_path)
        if pages < spec.min_pages:
            raise RuntimeError(
                f"MedPAC {spec.year}-{spec.month:02d} has {pages} pages — "
                f"expected ≥ {spec.min_pages}. Download likely corrupt; "
                f"inspect {pdf_path}."
            )

        chunks = _report_to_chunks(pdf_path)
        if len(chunks) < _MIN_CHUNKS_PER_REPORT:
            raise RuntimeError(
                f"MedPAC {spec.year}-{spec.month:02d} extraction produced "
                f"only {len(chunks)} chunks — expected ≥ "
                f"{_MIN_CHUNKS_PER_REPORT}."
            )

        records = chunks_to_records(
            chunks,
            source="medpac",
            title=spec.title,
            url=spec.url,
            fetch_date=fetch_date,
            doc_id_root=spec.doc_id_root,
            effective_date=spec.effective_date,
            extra={"year": spec.year, "month": spec.month},
        )
        new = [r for r in records if r.content_hash not in already]
        for r in new:
            already.add(r.content_hash)
        all_new.extend(new)

        per_report.append({
            "year": spec.year,
            "month": spec.month,
            "pages": pages,
            "chunks": len(chunks),
            "new_records": len(new),
            "dupe_records": len(records) - len(new),
        })

        if new:
            bucket = manifest.setdefault("sources", {}).setdefault("medpac", [])
            bucket.append({
                "year": spec.year,
                "month": spec.month,
                "title": spec.title,
                "url": spec.url,
                "pdf_local": str(pdf_path.relative_to(PROJECT_ROOT)),
                "pdf_bytes": pdf_path.stat().st_size,
                "pdf_pages": pages,
                "fetch_date": fetch_date,
                "chunks": len(new),
                "doc_id_root": spec.doc_id_root,
            })

    written = 0
    if all_new:
        written = write_jsonl(all_new, CORPUS_PATH, append=True)
        _save_manifest(manifest)

    return {
        "status": "ok",
        "source": "medpac",
        "fetch_date": fetch_date,
        "reports": per_report,
        "records_written": written,
        "corpus_path": str(CORPUS_PATH),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--force", action="store_true", help="Re-download PDFs.")
    parser.add_argument("--fetch-date", default=None, help="Override fetch_date.")
    args = parser.parse_args(argv)

    try:
        summary = run(force=args.force, fetch_date=args.fetch_date)
    except Exception as e:
        print(f"fetch_medpac FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
