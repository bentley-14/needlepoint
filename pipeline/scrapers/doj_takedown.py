"""National Health Care Fraud Takedown scraper (discovery, link 1b).

A second discovery source for the gold_set pipeline, peer to
:mod:`pipeline.scrapers.doj_press_releases`. Where that one sweeps the DOJ
press-release **API**, this one ingests the annual **National Health Care
Fraud Takedown** — a curated index DOJ posts once a year that the API path
covers only partially (it announces the takedown as district *roundups*, not
one press release per defendant, and the ~130 **state-AG** cases never appear
in the DOJ API at all).

Two pages per takedown (see ``journals`` / the session that scoped this):

* **Court Documents** — a table of every case: jurisdiction, case name,
  civil/criminal, doc type, and a link to the charging PDF. This is the
  authoritative per-case **citation** (``primary_source_url``).
* **Case Summaries** — one prose paragraph per defendant (name, age, location,
  charge, scheme, dollar amounts, program). This is ideal curator ``raw_text``
  — same shape as the press-release bodies the curator already extracts from —
  so we use it instead of parsing 261 long indictment PDFs.

The join is anchor-based and dependency-free: the court-docs table gives the
canonical list of defendant names; we locate each in the summaries prose and
slice the paragraph between consecutive names. (Validated: 253/261 of the 2026
defendants locate cleanly; the misses are blank rows / spelling variants and
are skipped with a log.)

Output is a single per-case worklist, ``takedown_cases.jsonl``, carrying the
summary as ``raw_text``; :mod:`pipeline.scrapers.doj_takedown_lead_adapter`
turns each row into a source-agnostic :class:`~pipeline.leads.Lead`
(``provenance="doj_takedown"``). Cross-run dedup + the per-run spend cap live in
``GoldSetBuilder.run_once`` (the curated ledger), so a fresh takedown drains a
slice per weekly run rather than all 261 at once.

Resilience: justice.gov HTML sits behind an Akamai bot-wall (unlike the JSON
API). We fetch with a browser UA; **any fetch/parse failure logs and yields an
empty run** rather than raising — a blocked takedown fetch must never break the
press-release path that shares the weekly job.
"""
from __future__ import annotations

import datetime as _dt
import re
from html import unescape
from pathlib import Path
from typing import ClassVar, Optional, Type

import pandas as pd
import pandera.pandas as pa
import requests
from pandera.typing import Series

from pipeline.base import BaseScraper, BaseScraperSchema, DocumentSchema, RunResult, url_to_doc_id
from pipeline.scrapers.download import PROJECT_ROOT

# ---------------------------------------------------------------------------
# Paths + fetch config
# ---------------------------------------------------------------------------

TAKEDOWN_DIR = PROJECT_ROOT / "data" / "source" / "doj_takedown"

DOJ_HOST = "https://www.justice.gov"
ENFORCEMENT_INDEX = f"{DOJ_HOST}/criminal/criminal-fraud/health-care-fraud-unit/national-enforcement-actions"
# URL templates for a given takedown year (verified against 2026).
COURT_DOCS_TMPL = f"{DOJ_HOST}/criminal/criminal-fraud/health-care-fraud-unit/{{year}}-national-hcf-court-documents"
SUMMARIES_TMPL = f"{DOJ_HOST}/criminal/criminal-fraud/health-care-fraud-unit/{{year}}-national-hcf-case-summaries"

# Shortest summary slice we'll treat as usable curator input. Adjacent
# co-defendant names ("A and B, both of …") leave the first name a tiny slice;
# below this we drop raw_text (→ the case is skipped) rather than feed the
# curator a name fragment.
MIN_SUMMARY_CHARS = 60

TIMEOUT_SEC = 45
# Plain requests get a 403 from the Akamai wall on these HTML pages; a browser
# UA clears it (verified 2026-06). Mirrors scripts/browser_fetch.py's headers.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
}

# Strip the "United States v. " / "State of X v. " / etc. wrapper + " et al".
_CASE_PREFIX = re.compile(
    r"^(?:United States|State of [A-Za-z ]+|Commonwealth of [A-Za-z ]+|People of [A-Za-z ]+)\s+v\.?\s+",
    re.I,
)


# ---------------------------------------------------------------------------
# Pure parsing helpers (unit-tested against saved HTML)
# ---------------------------------------------------------------------------


def defendant_name(case_name: str) -> str:
    """``United States v. Alex Example, et al`` → ``Alex Example``."""
    s = _CASE_PREFIX.sub("", case_name or "")
    s = re.split(r",?\s+et al", s, flags=re.I)[0]
    return s.strip().rstrip(".")


def _clean(fragment: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", unescape(fragment))).strip()


def _abs_url(href: str) -> str:
    href = (href or "").strip()
    if href.startswith("http"):
        return href
    return DOJ_HOST + href if href.startswith("/") else href


def parse_court_docs(html: str) -> list[dict]:
    """Parse the Court-Documents page → one dict per case.

    Each table is either the federal (``USAO District``) or state (``State``)
    listing; the header's first column tells us which, so every row is tagged
    with ``scope``. Rows missing a case name or document link are skipped.
    """
    body = re.sub(r"<script[\s\S]*?</script>", "", html)
    body = re.sub(r"<style[\s\S]*?</style>", "", body)
    out: list[dict] = []
    for table in re.findall(r"<table[\s\S]*?</table>", body):
        rows = re.findall(r"<tr[\s\S]*?</tr>", table)
        if not rows:
            continue
        header = [_clean(c) for c in re.findall(r"<t[hd][\s\S]*?</t[hd]>", rows[0])]
        scope = "federal" if header and "district" in header[0].lower() else "state"
        for row in rows[1:]:
            cells = re.findall(r"<t[hd][\s\S]*?</t[hd]>", row)
            if len(cells) < 4:
                continue
            case_name = _clean(cells[1])
            hrefs = re.findall(r'href="([^"]+)"', cells[3])
            if not case_name or not hrefs:
                continue
            out.append(
                {
                    "scope": scope,
                    "jurisdiction": _clean(cells[0]),
                    "case_name": case_name,
                    "civ_crim": _clean(cells[2]),
                    "doc_type": _clean(cells[3]),
                    "url": _abs_url(hrefs[0]),
                }
            )
    return out


def _summaries_text(html: str) -> str:
    """Ordered visible text of the summaries page as one string (for offset slicing)."""
    body = re.sub(r"<script[\s\S]*?</script>", "", html)
    body = re.sub(r"<style[\s\S]*?</style>", "", body)
    parts = [unescape(m.group(1)).strip() for m in re.finditer(r">([^<]+)<", body)]
    return re.sub(r"\s+", " ", " ".join(p for p in parts if p))


def segment_summaries(html: str, cases: list[dict]) -> list[dict]:
    """Attach a ``raw_text`` summary to each case by name-anchored slicing.

    Locate every case's defendant name in the summaries prose, then slice each
    paragraph from its name to the next name's offset (document order). A case
    whose name can't be located gets ``raw_text=None`` (caller skips it).
    """
    full = _summaries_text(html)
    low = full.lower()
    # Find each case's earliest offset (full name, else last-name word-boundary).
    located: list[tuple[int, int]] = []   # (offset, case_index)
    for i, c in enumerate(cases):
        nm = defendant_name(c["case_name"])
        if not nm:
            continue
        off = low.find(nm.lower())
        if off < 0:
            m = re.search(r"\b" + re.escape(nm.split()[-1]) + r"\b", full, re.I)
            off = m.start() if m else -1
        if off >= 0:
            located.append((off, i))
    located.sort()
    # Slice each located case's paragraph up to the next located offset. A slice
    # shorter than MIN_SUMMARY_CHARS is an adjacent-co-defendant fragment, not a
    # real summary → leave raw_text None so the case is skipped, not mis-fed.
    for rank, (off, ci) in enumerate(located):
        end = located[rank + 1][0] if rank + 1 < len(located) else len(full)
        slice_ = full[off:end].strip()
        cases[ci]["raw_text"] = slice_ if len(slice_) >= MIN_SUMMARY_CHARS else None
    for c in cases:
        c.setdefault("raw_text", None)
    return cases


def discover_takedown_year(index_html: str) -> Optional[int]:
    """Most recent takedown year advertised on the enforcement-actions index."""
    years = {int(y) for y in re.findall(r"(\d{4})-national-hcf-court-documents", index_html)}
    return max(years) if years else None


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class TakedownCaseSchema(DocumentSchema):
    """One row per takedown case. Inherits ``url``/``doc_id``/``publisher``/
    ``published_date`` + base stamps from :class:`DocumentSchema`."""

    case_name: Series[str] = pa.Field(nullable=True)
    scope: Series[str] = pa.Field(nullable=True)            # federal | state
    jurisdiction: Series[str] = pa.Field(nullable=True)     # USAO district or state
    civ_crim: Series[str] = pa.Field(nullable=True)
    doc_type: Series[str] = pa.Field(nullable=True)         # Indictment | Information | Complaint | ...
    raw_text: Series[str] = pa.Field(nullable=True)         # the case-summary paragraph (curator input)
    summary_chars: Series[int] = pa.Field(ge=0, nullable=True)


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------


class DojTakedownScraper(BaseScraper):
    """Discover the latest takedown's cases → ``takedown_cases.jsonl``."""

    source: ClassVar[str] = "doj_takedown"
    schema: ClassVar[Type[BaseScraperSchema]] = TakedownCaseSchema
    output_dir: ClassVar[Path] = TAKEDOWN_DIR
    schema_version: ClassVar[str] = "v1.0"
    output_format: ClassVar[str] = "jsonl"

    def fetch(self, *, year: Optional[int] = None) -> RunResult:
        """Fetch + parse the takedown into a per-case worklist.

        ``year`` pins a specific takedown; default resolves the live year by
        probing the current calendar year (see :meth:`_resolve_year`). Fetch/parse
        failures degrade to an **empty** run (logged), never an exception — the
        weekly job's press-release path must survive a walled/empty takedown.
        """
        started = self._now()
        try:
            cases = self._collect(year)
        except Exception as exc:  # noqa: BLE001 — resilience: never break the shared weekly job
            print(f"[doj_takedown] fetch failed ({type(exc).__name__}: {exc}) — emitting empty run", flush=True)
            cases = []

        df = self._build_df(cases)
        df = self._stamp_base_columns(df)
        df = self._align_to_schema(df)
        return self._write(
            df,
            name="takedown_cases",
            started_at_utc=started,
            inputs={"n_cases": str(len(cases)), "with_summary": str(sum(1 for c in cases if c.get("raw_text")))},
        )

    # ---- internals ---------------------------------------------------------

    def _collect(self, year: Optional[int]) -> list[dict]:
        if year is None:
            year = self._resolve_year()
        court_url = COURT_DOCS_TMPL.format(year=year)
        summaries_url = SUMMARIES_TMPL.format(year=year)
        print(f"[doj_takedown] year={year} · court_docs={court_url}", flush=True)

        cases = parse_court_docs(self._get(court_url))
        print(f"[doj_takedown] parsed {len(cases)} case(s) from the court-documents table", flush=True)
        try:
            cases = segment_summaries(self._get(summaries_url), cases)
            n_sum = sum(1 for c in cases if c.get("raw_text"))
            print(f"[doj_takedown] matched summaries for {n_sum}/{len(cases)} case(s)", flush=True)
        except Exception as exc:  # noqa: BLE001 — summaries optional; court-docs alone still IDs cases
            print(f"[doj_takedown] summaries fetch/parse failed ({exc}); cases will have no raw_text", flush=True)
        return cases

    def _resolve_year(self) -> int:
        """Pick the takedown year to ingest.

        The enforcement-actions **index lags** — it doesn't link the current
        year's takedown until weeks after the pages go live (verified 2026-06:
        the index lists 2023–2025 while the 2026 pages already return 200). So we
        **probe the current calendar year first**; if its court-docs page exists,
        that's the live takedown. Only if it's not posted yet do we fall back to
        the newest year the index advertises (else last year).
        """
        this_year = _dt.date.today().year
        if self._url_ok(COURT_DOCS_TMPL.format(year=this_year)):
            return this_year
        try:
            idx_year = discover_takedown_year(self._get(ENFORCEMENT_INDEX))
        except Exception:  # noqa: BLE001 — index unreachable
            idx_year = None
        return idx_year or (this_year - 1)

    @staticmethod
    def _get(url: str) -> str:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT_SEC)
        r.raise_for_status()
        return r.text

    @classmethod
    def _url_ok(cls, url: str) -> bool:
        """True if ``url`` fetches 200 (current-year takedown probe). Any error → False."""
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT_SEC)
            return r.status_code == 200
        except requests.RequestException:
            return False

    def _build_df(self, cases: list[dict]) -> pd.DataFrame:
        cols = ["url", "doc_id", "publisher", "published_date", "case_name", "scope",
                "jurisdiction", "civ_crim", "doc_type", "raw_text", "summary_chars"]
        published = _dt.date.today().isoformat()   # takedown posting date isn't per-row; stamp run date
        rows = [
            {
                "url": c["url"],
                "doc_id": url_to_doc_id((c["url"] or "").rstrip("/").lower()),
                "publisher": "DOJ",
                "published_date": published,
                "case_name": c.get("case_name"),
                "scope": c.get("scope"),
                "jurisdiction": c.get("jurisdiction"),
                "civ_crim": c.get("civ_crim"),
                "doc_type": c.get("doc_type"),
                "raw_text": c.get("raw_text"),
                "summary_chars": len(c.get("raw_text") or ""),
            }
            for c in cases
        ]
        return pd.DataFrame(rows, columns=cols)
