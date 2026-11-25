"""HTML → markdown extraction via trafilatura.

trafilatura's article-extraction heuristics strip OIG page chrome,
navigation, footers, and boilerplate while preserving headings, lists,
and the main body. That matches how we want OIG Work Plan / Fraud
Alerts / Advisory Opinions chunked.

Two modes:

* :func:`extract_html_to_markdown` — full article body, preserving
  headings where trafilatura can detect them.
* :func:`extract_html_metadata` — title, date, URL, author — for the
  manifest entry.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class HTMLExtract:
    text: str
    title: str
    date: str  # ISO yyyy-mm-dd or empty
    url: str
    author: str


def extract_html_to_markdown(html: str, url: str = "") -> HTMLExtract:
    """Extract main article from raw HTML.

    Returns text as markdown (headings / lists preserved) plus metadata.
    Empty ``text`` field indicates trafilatura failed to find an article
    body — caller should log and skip rather than index empty content.
    """
    try:
        import trafilatura
    except ImportError as e:
        raise RuntimeError(
            "trafilatura is required for HTML extraction; install via "
            "`pip install trafilatura` (already in requirements.txt)."
        ) from e

    if not html or not html.strip():
        return HTMLExtract(text="", title="", date="", url=url, author="")

    text = trafilatura.extract(
        html,
        output_format="markdown",
        include_comments=False,
        include_tables=True,
        favor_precision=True,
        url=url or None,
    ) or ""

    meta: Any = trafilatura.extract_metadata(html, default_url=url or None)
    title = (getattr(meta, "title", "") or "") if meta else ""
    date = (getattr(meta, "date", "") or "") if meta else ""
    resolved_url = (getattr(meta, "url", "") or "") if meta else ""
    author = (getattr(meta, "author", "") or "") if meta else ""

    return HTMLExtract(
        text=text,
        title=title,
        date=date,
        url=resolved_url or url,
        author=author,
    )
