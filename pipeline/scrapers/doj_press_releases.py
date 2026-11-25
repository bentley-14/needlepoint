"""DOJ press-release discovery scraper (link 1 of the DOJ → gold_set pipeline).

This is the **discovery** brick of the self-maintaining gold_set pipeline. It does
*only* discovery — it does **not** call the corpus_curator agent. The
agent + deterministic post-processing live in the sibling
:mod:`pipeline.scrapers.gold_set_builder` (links 2–4), filled in a later
session.

What ``fetch`` does
-------------------
1. **Sweep** the DOJ public JSON API by ``title=`` across
   :data:`TITLE_QUERY_TERMS`, newest-first, paginating and stopping client-
   side once releases predate ``since_date`` (the API silently ignores
   ``date_>=``; verified 2026-05-29).
2. **Dedup** discovered releases by ``doc_id`` (URL hash) against the warm
   cache, so every run is idempotent — re-running double-counts nothing.
3. **Body-gate** each *new* release (:func:`passes_body_gate`) — require a
   Medicare/FCA anchor + ≥2 billing keywords. The gate is the precision
   backstop *and* the only recall path for generically-titled cases
   ("Doctor Sentenced to 70 Months", whose title carries no fraud
   vocabulary but whose body says "Medicare").
4. **Warm the cache** — upsert every newly-discovered release (title-
   matched, pre-gate, matching the existing cache's semantics) into
   ``data/source/doj_api/raw_press_releases.jsonl`` so it stays a
   replayable corpus for iterating on the curator agent without
   re-scraping DOJ.
5. **Write the curator-input list** (``curator_input.jsonl``) — one JSONL
   row per newly-discovered release, each carrying ``passed_body_gate``. This
   is the list of press releases the curator (link 2) must read and turn into
   structured cases; it reads it filtered to ``passed_body_gate == True``.

Why title-search + body-gate (the recall finding)
-------------------------------------------------
Measured 2026-05-30: title-search alone covers only ~65% of known gold_set
labels — it systematically misses lab-kickback / bribe-for-referral,
pill-mill / drug-distribution, civil-FCA-settlement, and generic-title
cases, because DOJ titles them with *enforcement* vocabulary ("bribes",
"distributing drugs", "Settle Allegations") not *fraud* vocabulary. The
token-based :data:`TITLE_QUERY_TERMS` + the body gate recover most of that;
the residual (generic titles, non-press-release provenance) is a documented
structural ceiling for title-based discovery. Full analysis + per-term
numbers in the decision journal.

Output contract — two artifacts (+ the framework manifest sidecar)
-----------------------------------------------------------------
* ``curator_input.jsonl`` — **the curator's input list.** One row per
  newly-discovered release, with identifiers + the gate decision
  (``passed_body_gate``) but no body. This is the link-1 → link-2 interface:
  the list of press releases the curator (link 2) must read and structure;
  link 2 reads it filtered to ``passed_body_gate == True``. (Named for its
  consumer on purpose — it is NOT a "manifest", see below.)
* ``curator_input.manifest.json`` — the **framework manifest**: the
  :class:`~pipeline.base.RunResult` sidecar that *every* scraper writes
  (source, since_date, counts, schema, columns). "Manifest" means *this*
  everywhere in the repo; the JSONL above is the curator input list, not a
  manifest.
* ``raw_press_releases.jsonl`` — the warm **replay cache**: full release JSON
  incl. body, *every* release ever seen, updated as a side-effect; not
  schema-validated.

One line: cache = the *text* · curator_input = the *list of releases to read
+ the gate decision* · .manifest.json = run provenance.

Syncing to R2 (yes — these are durable, shared artifacts)
--------------------------------------------------------
All writes are **local disk only**; this scraper never touches R2 directly.
Everything above sits under ``data/source/``, so the team mechanism ships it::

    make push-data    # rclone sync data/source/ -> r2:needlepoint-data/ground_truth/v1

The future weekly cron will run this automatically, mirroring
``pipeline/scheduled_scripts/snapshot_nppes.py``. (Note: ``gold_set.jsonl``
lives under ``data/extracted/``, which ``make push-data`` does *not* cover —
its R2 push is the gold_set_builder's concern, link 4.)
"""
from __future__ import annotations

import datetime as _dt
import json
import time
from pathlib import Path
from typing import Any, ClassVar, Optional, Type

import pandas as pd
import pandera.pandas as pa
import requests
from pandera.typing import Series

from pipeline.base import (
    BaseScraper,
    BaseScraperSchema,
    DocumentSchema,
    RunResult,
    url_to_doc_id,
)
from pipeline.scrapers.download import PROJECT_ROOT

# ---------------------------------------------------------------------------
# Paths + API
# ---------------------------------------------------------------------------

DOJ_API_DIR = PROJECT_ROOT / "data" / "source" / "doj_api"
# The warm replay cache — full release JSON (incl. body), one per line.
# Pre-existing (5,937 releases, built by scripts/ingest/doj_api_ingest.py);
# this scraper upserts into it.
CACHE_PATH = DOJ_API_DIR / "raw_press_releases.jsonl"

API_BASE = "https://www.justice.gov/api/v1/press_releases.json"
TIMEOUT_SEC = 30
PAGE_SIZE = 50               # API max
MAX_PAGES_PER_TERM = 60      # safety cap; a date-scoped sweep stops well before this
# Throttle between API calls. justice.gov's JSON API (nginx, *not* the
# Akamai bot-wall that guards the HTML pages — verified 2026-05-31: the API
# returns 200 + full body inline to plain requests) tolerates ~4 req/sec.
# A full sweep is up to 20 terms × pagination, so we stay polite to avoid
# tripping rate-limit / Akamai bot detection. Mirrors doj_api_ingest.py.
# If DOJ ever walls the API too, the fallback is scripts/browser_fetch.py
# (headless browser) for per-release fetches — but that path is slow and
# we avoid it by design.
RATE_LIMIT_DELAY_SEC = 0.3
HEADERS = {
    "User-Agent": "fraud-investigator/1.0 (pipeline.scrapers.doj_press_releases)",
    "Accept": "application/json",
}


# ---------------------------------------------------------------------------
# v1 discovery config — validated 2026-05-30 (see decision journal)
# ---------------------------------------------------------------------------
#
# Title query terms — passed to the DOJ API as title= (the API tokenizes,
# so single tokens like "kickback" match broadly). Token-based, NOT phrase-
# based: phrases like "medicare durable medical equipment" never appear in
# DOJ titles and yielded 0 matches. Validated recall: 74.7% of all gold
# case_names, 92.3% cache coverage.
#
# Knob (demoted from v1): adding settlement verbs ("settle"/"resolve"/
# "allegation") lifts missed-set recall ~58% → ~70% but explodes non-
# healthcare over-pull — revisit if recall looks thin, and lean on the
# body gate for precision.
TITLE_QUERY_TERMS: list[str] = [
    "health care fraud", "medicare", "medicaid", "tricare",
    "false claims", "kickback", "bribe", "referral",
    "opioid", "controlled substance", "unlawful distribution",
    "prescription", "pill mill", "misbranded",
    "telemedicine", "telehealth", "home health", "hospice",
    "genetic testing", "compounding",
]

# Body-filter gate — applied to each release body. Require ≥1 ANCHOR (the
# program/statute that makes it federal health-fraud) AND ≥2 BILLING
# keywords. Validated: retains 96.4% of true gold-labeled releases while
# dropping ~21% of marginal over-pull.
ANCHORS: list[str] = [
    "medicare", "medicaid", "tricare", "false claims act",
    "anti-kickback", "hhs-oig", " oig ", "health care benefit program",
    "federal health care",
]
BILLING_KEYWORDS: list[str] = [
    "billing", "claims", "reimburs", "beneficiar", "provider", "physician",
    "doctor", "nurse", "clinic", "pharmacy", "home health", "hospice",
    "telemedicine", "telehealth", "durable medical", "dme", "laborator",
    "diagnostic", "medically unnecessary", "upcod", "phantom", "kickback",
    "bribe", "referral", "controlled substance", "opioid", "oxycodone",
    "prescrib", "distribut", "dispens", "genetic test", "compound", "misbrand",
]
MIN_BILLING_KEYWORDS = 2


def passes_body_gate(body: Optional[str]) -> bool:
    """True iff ``body`` reads like a Medicare/FCA fraud case.

    Requires ≥1 :data:`ANCHORS` term AND ≥:data:`MIN_BILLING_KEYWORDS`
    :data:`BILLING_KEYWORDS` terms. Mirrors (and supersedes)
    ``doj_api_ingest._is_healthcare_fraud`` with the tuned, validated
    vocabulary. Pure function over the body string so it can be re-run /
    re-tuned over the warm cache without re-scraping.
    """
    text = (body or "").lower()
    if not text:
        return False
    if not any(a in text for a in ANCHORS):
        return False
    return sum(1 for b in BILLING_KEYWORDS if b in text) >= MIN_BILLING_KEYWORDS


def matched_title_terms(title: Optional[str]) -> list[str]:
    """Which :data:`TITLE_QUERY_TERMS` literally appear in ``title``.

    Recorded on each curator_input row for auditing recall per term. Note
    this is a *substring* check; the live API matches more generously
    (tokenized), so a release can appear with no literal match here.
    """
    t = (title or "").lower()
    return [term for term in TITLE_QUERY_TERMS if term in t]


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class DojPressReleaseSchema(DocumentSchema):
    """Schema for ``curator_input.jsonl`` — one row per discovered release.

    Inherits the base stamps (``source``, ``fetched_at_utc``,
    ``schema_version``) and the URL-keyed document columns (``url``,
    ``doc_id``, ``publisher``, ``published_date``) from
    :class:`~pipeline.base.DocumentSchema`, and adds DOJ-specific metadata.

    The release **body is not stored here** — it lives in the warm cache
    (:data:`CACHE_PATH`), keyed by ``url``/``doc_id``. This is the *curator
    input list*: it identifies releases and records the discovery decision
    (``passed_body_gate``), not their full text.
    """

    title: Series[str] = pa.Field(nullable=True)
    component: Series[str] = pa.Field(nullable=True)       # issuing USAO / DOJ division
    teaser: Series[str] = pa.Field(nullable=True)          # first-paragraph plain-text summary
    uuid: Series[str] = pa.Field(nullable=True)            # DOJ stable id
    matched_terms: Series[str] = pa.Field(nullable=True)   # pipe-joined TITLE_QUERY_TERMS that hit the title
    passed_body_gate: Series[bool] = pa.Field(nullable=True)  # curator worklist filters on this
    body_chars: Series[int] = pa.Field(ge=0, nullable=True)   # length of cached body (sanity / staleness check)


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------


class DojPressReleasesScraper(BaseScraper):
    """Discover new DOJ healthcare-fraud press releases → ``curator_input.jsonl``.

    ``fetch`` takes no required argument: with no ``since_date`` it derives
    the cursor from the warm cache (``max(published_date) − lookback_days``)
    and relies on ``doc_id`` dedup for correctness. Pass ``since_date=`` to
    backfill or recover a specific window.
    """

    source: ClassVar[str] = "doj_press_releases"
    schema: ClassVar[Type[BaseScraperSchema]] = DojPressReleaseSchema
    output_dir: ClassVar[Path] = DOJ_API_DIR
    schema_version: ClassVar[str] = "v1.0"
    output_format: ClassVar[str] = "jsonl"

    # ---- fetch entrypoint --------------------------------------------------

    def fetch(
        self,
        *,
        since_date: Optional[str] = None,
        lookback_days: int = 30,
        limit_per_term: int = 500,
    ) -> RunResult:
        """Discover releases published on/after ``since_date``.

        Parameters
        ----------
        since_date
            ISO ``YYYY-MM-DD``. If ``None``, derived from the warm cache's
            newest ``published_date`` minus ``lookback_days`` (greenfield:
            falls back to ``today − lookback_days``).
        lookback_days
            Overlap window subtracted from the derived cursor so late-posted
            / revised releases and failed prior runs are re-checked.
            ``doc_id`` dedup makes the overlap free.
        limit_per_term
            Max releases collected per title term before stopping (safety;
            a date-scoped sweep normally stops at the cursor first).
        """
        started = self._now()
        cache = self._load_cache()
        resolved_since = since_date or self._derive_since_date(cache, lookback_days)
        print(
            f"[doj_press_releases] sweeping {len(TITLE_QUERY_TERMS)} title terms "
            f"since {resolved_since} (cache has {len(cache)} releases)",
            flush=True,
        )

        discovered = self._sweep_titles(resolved_since, limit_per_term)
        new = {
            doc_id: rec
            for doc_id, rec in discovered.items()
            if (rec.get("url") or "").rstrip("/").lower() not in cache
        }
        print(
            f"[doj_press_releases] discovered {len(discovered)} releases, "
            f"{len(new)} new after cache dedup",
            flush=True,
        )

        # Body-gate each new release; tag the decision onto the record.
        n_gated = 0
        for rec in new.values():
            passed = passes_body_gate(rec.get("body"))
            rec["_passed_body_gate"] = passed
            n_gated += int(passed)
        print(
            f"[doj_press_releases] {n_gated}/{len(new)} new releases passed the body gate",
            flush=True,
        )

        # Warm the cache with ALL new releases (title-matched, pre-gate) —
        # keeps it a broad replay corpus consistent with its existing
        # semantics, so the gate can be re-tuned over it later.
        # NOTE: this writes to LOCAL DISK only (data/source/doj_api/). Nothing
        # in this scraper touches R2 — the team's `make push-data` (rclone)
        # syncs data/ to R2 as a separate step (see pipeline/base.py).
        self._upsert_cache(cache, new)

        df = self._build_curator_input_df(new)
        df = self._stamp_base_columns(df)
        df = self._align_to_schema(df)

        return self._write(
            df,
            name="curator_input",
            started_at_utc=started,
            inputs={
                "api_base": API_BASE,
                "since_date": resolved_since,
                "n_terms": str(len(TITLE_QUERY_TERMS)),
                "n_new": str(len(new)),
                "n_passed_gate": str(n_gated),
                "cache_path": str(CACHE_PATH),
            },
        )

    # ---- title sweep -------------------------------------------------------

    def _sweep_titles(self, since_date: str, limit_per_term: int) -> dict[str, dict]:
        """Title-search every term, newest-first, stop at ``since_date``.

        Returns ``{doc_id: release}`` deduped by ``doc_id`` across all terms.
        Mirrors the verified-working path in ``scripts/doj_pr_lookup.py``:
        the API honors ``sort=date&direction=DESC`` but *ignores* ``date_>=``,
        so we paginate newest-first and filter client-side.
        """
        after_u = self._to_unix(since_date)
        out: dict[str, dict] = {}
        for term in TITLE_QUERY_TERMS:
            collected = 0
            page = 0
            stop = False
            while collected < limit_per_term and page < MAX_PAGES_PER_TERM and not stop:
                res = self._api(
                    {
                        "title": term,
                        "pagesize": PAGE_SIZE,
                        "page": page,
                        "sort": "date",
                        "direction": "DESC",
                    }
                )
                batch = res.get("results") or []
                if not batch:
                    break
                for rec in batch:
                    dt = int(rec.get("date") or 0)
                    if dt < after_u:
                        stop = True  # sorted DESC → everything past here is older
                        break
                    url = (rec.get("url") or "").strip()
                    if not url:
                        continue
                    doc_id = url_to_doc_id(url.rstrip("/").lower())
                    if doc_id not in out:
                        out[doc_id] = rec
                    collected += 1
                    if collected >= limit_per_term:
                        break
                page += 1
                time.sleep(RATE_LIMIT_DELAY_SEC)  # stay under the API's rate limit
        return out

    @staticmethod
    def _api(params: dict[str, Any]) -> dict:
        """Single DOJ API GET. Returns parsed JSON ({} on transport error)."""
        try:
            r = requests.get(API_BASE, params=params, headers=HEADERS, timeout=TIMEOUT_SEC)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as exc:
            print(f"[doj_press_releases] API error ({params.get('title')!r} p{params.get('page')}): {exc}",
                  flush=True)
            return {}

    # ---- cursor derivation -------------------------------------------------

    def _derive_since_date(self, cache: dict[str, dict], lookback_days: int) -> str:
        """``max(published_date in cache) − lookback_days``; greenfield → today − lookback."""
        max_ts = 0
        for rec in cache.values():
            try:
                max_ts = max(max_ts, int(rec.get("date") or 0))
            except (ValueError, TypeError):
                continue
        if max_ts > 0:
            anchor = _dt.datetime.fromtimestamp(max_ts, tz=_dt.timezone.utc).date()
        else:
            anchor = _dt.date.today()
        return (anchor - _dt.timedelta(days=lookback_days)).isoformat()

    @staticmethod
    def _to_unix(iso_date: str) -> int:
        return int(
            _dt.datetime.strptime(iso_date, "%Y-%m-%d")
            .replace(tzinfo=_dt.timezone.utc)
            .timestamp()
        )

    # ---- warm cache --------------------------------------------------------

    @staticmethod
    def _load_cache() -> dict[str, dict]:
        """Load the warm cache as ``{normalized_url: release}``. Empty if absent."""
        cache: dict[str, dict] = {}
        if not CACHE_PATH.exists():
            return cache
        with CACHE_PATH.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                url = (rec.get("url") or "").rstrip("/").lower()
                if url:
                    cache[url] = rec
        return cache

    def _upsert_cache(self, cache: dict[str, dict], new: dict[str, dict]) -> None:
        """Add ``new`` releases to the cache and rewrite it atomically.

        Strips the transient ``_passed_body_gate`` marker before persisting
        so the cache stays a clean mirror of the DOJ API payload.
        """
        for rec in new.values():
            url = (rec.get("url") or "").rstrip("/").lower()
            if not url:
                continue
            persisted = {k: v for k, v in rec.items() if k != "_passed_body_gate"}
            cache[url] = persisted
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_PATH.with_suffix(".jsonl.part")
        with tmp.open("w") as f:
            for rec in cache.values():
                f.write(json.dumps(rec) + "\n")
        tmp.rename(CACHE_PATH)

    # ---- curator-input assembly --------------------------------------------

    def _build_curator_input_df(self, new: dict[str, dict]) -> pd.DataFrame:
        """One row per newly-discovered release → the ``curator_input.jsonl`` frame.

        **curator_input vs. cache** (the two JSONL artifacts this scraper writes):

        * **curator_input** (this frame → ``curator_input.jsonl``) = the
          schema-validated *run output* and the curator's *input list*. One
          row per release discovered *this run*, carrying identifiers + the
          gate decision (``passed_body_gate``) but **NOT the body**. Small,
          strict, the thing link 2 reads.
        * **cache** (``raw_press_releases.jsonl``) = the warm *replay corpus*:
          the **full release JSON incl. body**, for **every** release ever
          seen (not just this run), **not** schema-validated. Upserted as a
          side-effect so an improved curator can be re-run over the same text
          without re-scraping DOJ.

        In short: curator_input says *which* releases to read and *whether*
        they passed the gate; the cache holds *the text itself* for replay.
        """
        rows: list[dict] = []
        for doc_id, rec in new.items():
            url = (rec.get("url") or "").strip()
            published = self._epoch_to_iso(rec.get("date"))
            terms = matched_title_terms(rec.get("title"))
            body = rec.get("body") or ""
            rows.append(
                {
                    "url": url,
                    "doc_id": doc_id,
                    "publisher": "DOJ",
                    "published_date": published,
                    "title": rec.get("title"),
                    "component": self._component_str(rec.get("component")),
                    "teaser": (rec.get("teaser") or "").strip() or None,
                    "uuid": rec.get("uuid"),
                    "matched_terms": "|".join(terms) if terms else None,
                    "passed_body_gate": bool(rec.get("_passed_body_gate")),
                    "body_chars": len(body),
                }
            )
        # Explicit columns so an empty result still produces the right shape.
        cols = [
            "url", "doc_id", "publisher", "published_date", "title",
            "component", "teaser", "uuid", "matched_terms",
            "passed_body_gate", "body_chars",
        ]
        return pd.DataFrame(rows, columns=cols)

    @staticmethod
    def _epoch_to_iso(epoch: Any) -> Optional[str]:
        try:
            return _dt.datetime.fromtimestamp(int(epoch), tz=_dt.timezone.utc).date().isoformat()
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _component_str(component: Any) -> Optional[str]:
        """DOJ ``component`` is sometimes a list of dicts; flatten to names."""
        if not component:
            return None
        if isinstance(component, list):
            names = [c.get("name") if isinstance(c, dict) else str(c) for c in component]
            joined = ", ".join(n for n in names if n)
            return joined or None
        if isinstance(component, dict):
            return component.get("name")
        return str(component)
