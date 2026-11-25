"""The ``Lead`` — the universal discovery → curator contract.

A **Lead is one document a discovery source found**, normalized into a single
source-agnostic shape so the corpus_curator (the *extraction* engine, link 2)
can read it without knowing which source produced it. The DOJ scraper, a
``/goal`` web-roaming agent, OIG, a news article — all adapt their output to
this one contract.

Separation of responsibilities (the load-bearing design choice)
---------------------------------------------------------------
A Lead carries the **document**, never extracted *facts*. The curator owns
100% of extraction — defendant, NPI candidates, ``active_years``, scheme,
outcome — from :attr:`Lead.raw_text`; the deterministic gate (link 3) then
verifies. Nothing about the defendant is pre-parsed upstream of the curator,
so "who owns active_years?" always has exactly one answer.

This implies a clean dichotomy for every producer:

* **Lead producer** (e.g. the DOJ scraper): hands over a *document* →
  curator extracts → gate decides.
* **Proposal producer** (e.g. a ``/goal`` run that already resolved the NPI,
  years, outcome): hands over *finished structured facts* → **skips the
  curator**, enters at the gate.

There is deliberately no half-extracted hybrid. The only non-document payload
a Lead carries is :attr:`Lead.discovery_meta` — breadcrumbs about *how/why*
the lead surfaced (which search terms hit, which agent session) — never facts
about the defendant.
"""
from __future__ import annotations

import html
import re
from html.parser import HTMLParser

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pipeline.base import url_to_doc_id


# ---------------------------------------------------------------------------
# Source-agnostic identity
# ---------------------------------------------------------------------------


def normalize_url(url: str) -> str:
    """Canonicalize a URL for stable identity + dedup.

    Lowercase, strip surrounding whitespace and any trailing slash. Mirrors
    the normalization the DOJ scraper applies before computing ``doc_id``
    (``url.rstrip("/").lower()``), so ``make_lead_id`` over a DOJ URL equals
    that release's ``curator_input`` ``doc_id``.
    """
    return (url or "").strip().rstrip("/").lower()


def make_lead_id(primary_source_url: str) -> str:
    """Source-agnostic Lead identity = 16-hex hash of the normalized URL.

    The one thing *every* producer has is the URL of the authoritative
    document it found, so the URL-hash is the universal key. It doubles as
    free cross-source dedup: two producers that find the same document compute
    the same ``lead_id``. Reuses :func:`pipeline.base.url_to_doc_id` (already
    the system's URL-hash), so a DOJ Lead's id is identical to its
    ``curator_input`` ``doc_id``.
    """
    return url_to_doc_id(normalize_url(primary_source_url))


# ---------------------------------------------------------------------------
# HTML → plaintext (dependency-free, paragraph-preserving)
# ---------------------------------------------------------------------------


class _TextExtractor(HTMLParser):
    """Collect visible text nodes, one per line, so block structure survives."""

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if text:
            self._parts.append(text)

    def text(self) -> str:
        return re.sub(r"\n{3,}", "\n\n", "\n".join(self._parts)).strip()


def strip_html(value: str | None) -> str:
    """HTML → plaintext, preserving paragraph breaks.

    Kept local (rather than importing ``scripts/extract/doj_api_fulltext``) so
    the ``pipeline`` package never depends on ``scripts/``. Paragraph breaks
    are retained because multi-defendant "takedown" releases read as lists,
    and the curator separates cases more reliably with the structure intact.
    """
    parser = _TextExtractor()
    parser.feed(value or "")
    return html.unescape(parser.text())


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------


class Lead(BaseModel):
    """One document a discovery source found, normalized for the curator.

    See the module docstring for why this carries a document and not extracted
    facts. ``extra="forbid"`` catches a source adapter that tries to smuggle
    fields in — anything source-specific belongs in :attr:`discovery_meta`.

    Pydantic gives the **type** checks for free (e.g. a non-string
    ``published_date`` or a non-dict ``discovery_meta`` is rejected at
    construction). The :meth:`_check` validator below adds the **business**
    rules pydantic can't infer — starting with "empty ``raw_text`` is an
    error, not just a valid empty string." It's the single place to grow more
    edge-case rules as we hit them.
    """

    model_config = ConfigDict(extra="forbid")

    lead_id: str = ""        # auto-derived from primary_source_url if left blank (see _check)
    provenance: str          # free-form source tag: "doj_direct", "goal_web", "oig", "news", ...
    primary_source_url: str  # the authoritative document every source must cite
    raw_text: str            # HTML stripped → plaintext; the curator extracts everything from this
    title: str = ""          # cheap context if the source has it (DOJ does; a bare URL may not)
    published_date: str | None = None  # ISO YYYY-MM-DD when known; ordering / since_date floor
    discovery_meta: dict = Field(default_factory=dict)  # how/why surfaced — NEVER extracted facts

    @model_validator(mode="after")
    def _check(self) -> "Lead":
        """Business rules beyond pydantic's type checks. Extend here as needed."""
        if not self.primary_source_url.strip():
            raise ValueError("Lead.primary_source_url is required (every source must cite its document)")
        if not self.raw_text.strip():
            raise ValueError("Lead.raw_text is empty — a Lead with no text is useless to the curator")
        if not self.lead_id:
            self.lead_id = make_lead_id(self.primary_source_url)
        return self
