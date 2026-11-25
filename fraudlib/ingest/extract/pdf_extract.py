"""Layout-aware PDF → markdown extraction via pymupdf4llm.

Single entry point :func:`extract_pdf_to_markdown` so fetchers
(``fetch_policy_pdfs``, ``fetch_oig``, ``fetch_lcd``) don't each reach
into pymupdf internals. Returns markdown with H1/H2/H3 preserved where
pymupdf detects them, which :func:`fraudlib.ingest.extract.chunking.split_by_heading`
can split on cleanly.

We also expose :func:`page_count` for validation — fetchers assert the
downloaded PDF has the expected page count before extraction so a
truncated download doesn't silently produce a short corpus.
"""
from __future__ import annotations

from pathlib import Path


def extract_pdf_to_markdown(pdf_path: Path) -> str:
    """Return layout-aware markdown for the full PDF.

    Raises
    ------
    FileNotFoundError
        ``pdf_path`` does not exist.
    RuntimeError
        pymupdf4llm import failed (missing dep) or extraction returned
        an empty string.
    """
    if not pdf_path.exists():
        raise FileNotFoundError(pdf_path)

    try:
        import pymupdf4llm
    except ImportError as e:
        raise RuntimeError(
            "pymupdf4llm is required for PDF extraction; install via "
            "`pip install pymupdf4llm` (already in requirements.txt)."
        ) from e

    md = pymupdf4llm.to_markdown(str(pdf_path))
    if not md.strip():
        raise RuntimeError(
            f"Empty extraction for {pdf_path}. File may be image-only "
            f"(OCR required) or corrupt. Page count: {page_count(pdf_path)}."
        )
    return md


def page_count(pdf_path: Path) -> int:
    """Return the page count of ``pdf_path``. Used for download-integrity asserts."""
    import pymupdf

    with pymupdf.open(pdf_path) as doc:
        return doc.page_count
