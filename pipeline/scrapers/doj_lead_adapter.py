"""DOJ adapter — ``curator_input`` rows + warm-cache bodies → :class:`Lead`s.

The thin per-source shim the journal calls for ("the DOJ scraper feeds the
curator via a thin adapter"). The DOJ scraper (link 1,
:mod:`pipeline.scrapers.doj_press_releases`) writes two artifacts:

* ``curator_input.jsonl`` — one row per discovered release: identifiers + the
  body-gate decision (``passed_body_gate``), but **no body**.
* ``raw_press_releases.jsonl`` — the warm replay cache: full release JSON
  **including the body**, keyed by normalized URL.

This module joins them (on the normalized URL, the cache's own key) and emits
the source-agnostic :class:`~pipeline.leads.Lead` the curator (link 2)
consumes. **All DOJ-format knowledge stops here** — everything downstream
(curator, gate, merge) sees only ``Lead`` / proposals, never DOJ JSON.

``provenance="doj_direct"`` — the high-trust tier in the merge router (a
DOJ-direct lead with an unambiguous high-confidence NPI can auto-merge; see
the gold_set_builder skeleton). A roundup / "takedown" release naming many
defendants still yields exactly **one Lead** here; turning that into N
proposals is the curator's job, not the adapter's.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

from pipeline.leads import Lead, make_lead_id, normalize_url, strip_html
from pipeline.scrapers.doj_press_releases import (
    CACHE_PATH,
    DOJ_API_DIR,
    DojPressReleasesScraper,
    matched_title_terms,
    passes_body_gate,
)

# Link 1 writes the curator input list here (named for its consumer, not a
# "manifest"). Defined from DOJ_API_DIR rather than imported from
# gold_set_builder to avoid a circular import (the builder imports this).
CURATOR_INPUT_PATH = DOJ_API_DIR / "curator_input.jsonl"

PROVENANCE = "doj_direct"


def doj_leads(
    curator_input_path: Path = CURATOR_INPUT_PATH,
    cache_path: Path = CACHE_PATH,
    *,
    gated_only: bool = True,
) -> list[Lead]:
    """Build :class:`Lead`s from gated ``curator_input`` rows + cached bodies.

    Parameters
    ----------
    gated_only
        If True (default), only rows with ``passed_body_gate == True`` become
        Leads — the curator should never spend an LLM turn on a release the
        cheap body gate already rejected.

    A row whose body is absent from the cache, or empty, is **skipped with a
    warning** (a Lead with no ``raw_text`` is useless to the curator and
    silently emitting one would burn a turn). This is also the signal that the
    warm cache is stale relative to ``curator_input`` — i.e. the cache refresh
    that produced these rows has not landed on this branch yet.
    """
    cache = _load_cache_bodies(cache_path)
    leads: list[Lead] = []
    missing: list[str] = []

    for row in _read_jsonl(curator_input_path):
        if gated_only and not row.get("passed_body_gate"):
            continue
        url = (row.get("url") or "").strip()
        rec = cache.get(normalize_url(url))
        raw_text = strip_html((rec or {}).get("body"))
        if not raw_text:
            missing.append(row.get("doc_id") or url or "<no-url>")
            continue
        leads.append(
            Lead(
                lead_id=make_lead_id(url),
                provenance=PROVENANCE,
                primary_source_url=url,
                raw_text=raw_text,
                title=(row.get("title") or "").strip(),
                published_date=_norm_published_date(row.get("published_date")),
                discovery_meta={
                    "uuid": row.get("uuid"),
                    "component": row.get("component"),
                    "matched_terms": (
                        (row.get("matched_terms") or "").split("|")
                        if row.get("matched_terms")
                        else []
                    ),
                    "body_chars": row.get("body_chars"),
                },
            )
        )

    if missing:
        shown = ", ".join(missing[:5]) + (" …" if len(missing) > 5 else "")
        print(
            f"[doj_lead_adapter] WARNING: {len(missing)} gated row(s) had no cached "
            f"body and were skipped (stale/unmerged cache?): {shown}",
            flush=True,
        )
    return leads


def cache_leads(
    cache_path: Path = CACHE_PATH,
    *,
    gated_only: bool = True,
) -> list[Lead]:
    """Build :class:`Lead`s from the **full warm cache** — the historical-backfill
    path (peer to :func:`doj_leads`, the incremental-window path).

    Why a second producer: :func:`doj_leads` joins the *incremental*
    ``curator_input.jsonl`` (only releases link-1 discovery has emitted since its
    last ``since_date`` sweep) against the cache. The warm cache
    (``raw_press_releases.jsonl``) holds the **entire** DOJ corpus ever fetched,
    so to backfill over all of history we read leads straight from it and re-apply
    the body gate per record — no DOJ API re-crawl.

    The body gate (:func:`passes_body_gate`) is a pure function over the body
    text; the persisted cache strips the transient ``_passed_body_gate`` marker
    (``doj_press_releases`` line ~445), so we recompute it here. Identifiers
    (``lead_id``/``doc_id``), gate, and field mapping match :func:`doj_leads`
    exactly, so the same release yields an identical :class:`Lead` either way.

    Parameters
    ----------
    gated_only
        If True (default), only records whose body passes the gate become Leads
        — same filter the incremental path applies via ``passed_body_gate``.
    """
    leads: list[Lead] = []
    skipped_no_body = 0
    for rec in _load_cache_bodies(cache_path).values():
        url = (rec.get("url") or "").strip()
        raw_text = strip_html(rec.get("body"))
        if not raw_text:
            skipped_no_body += 1
            continue
        if gated_only and not passes_body_gate(rec.get("body")):
            continue
        leads.append(
            Lead(
                lead_id=make_lead_id(url),
                provenance=PROVENANCE,
                primary_source_url=url,
                raw_text=raw_text,
                title=(rec.get("title") or "").strip(),
                published_date=_epoch_to_iso(rec.get("date")),
                discovery_meta={
                    "uuid": rec.get("uuid"),
                    # Raw cache `component` is a list of dicts; the incremental
                    # path (curator_input) stores it flattened to a name string
                    # via _component_str. Flatten here too so a backfilled Lead is
                    # identical to the incrementally-discovered one for the same release.
                    "component": DojPressReleasesScraper._component_str(rec.get("component")),
                    "matched_terms": matched_title_terms(rec.get("title")),
                    "body_chars": len(raw_text),
                    "from_cache_backfill": True,
                },
            )
        )
    print(
        f"[doj_lead_adapter] cache_leads: {len(leads)} lead(s) from warm cache "
        f"({'gated' if gated_only else 'ungated'}; {skipped_no_body} had no body)",
        flush=True,
    )
    return leads


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _norm_published_date(value) -> str | None:
    """Normalize a curator_input ``published_date`` to ISO ``YYYY-MM-DD`` or None.

    The Lead contract types ``published_date`` as ``str | None``. Depending on how
    the JSONL was serialized, this field can arrive as: a plain ``YYYY-MM-DD``
    string; a full ISO timestamp (``2026-06-22T00:00:00.000Z``, from a datetime64
    column written with ``date_format="iso"``); or — from older writes — an
    epoch-**millisecond** int (pandas ``to_json`` default for datetimes). Coerce
    all three to a bare date so a stray int can never crash Lead construction.
    """
    if value in (None, ""):
        return None
    # Numeric (or all-digit string) → epoch; ms if it's too big to be seconds.
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.lstrip("-").isdigit()):
        secs = int(value)
        if abs(secs) >= 10**12:        # epoch milliseconds → seconds
            secs //= 1000
        return _epoch_to_iso(secs)
    # String: take the date component of an ISO timestamp ("...T..." or "... ...").
    return str(value).split("T")[0].split(" ")[0] or None


def _epoch_to_iso(value) -> str | None:
    """Cache ``date`` is Unix epoch seconds (e.g. ``"1231416000"``); → ISO
    ``YYYY-MM-DD``. None/unparseable → None (Lead.published_date is optional)."""
    if value in (None, ""):
        return None
    try:
        import datetime as _dt

        return _dt.datetime.fromtimestamp(
            int(str(value).strip()), tz=_dt.timezone.utc
        ).strftime("%Y-%m-%d")
    except (ValueError, TypeError, OSError, OverflowError):
        return None


def _load_cache_bodies(cache_path: Path = CACHE_PATH) -> dict[str, dict]:
    """``{normalized_url: release}`` from the warm cache (matches the scraper's key)."""
    out: dict[str, dict] = {}
    if not cache_path.exists():
        return out
    with cache_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            url = normalize_url(rec.get("url") or "")
            if url:
                out[url] = rec
    return out


def _read_jsonl(path: Path) -> Iterator[dict]:
    if not path.exists():
        return
    with path.open() as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


if __name__ == "__main__":  # smoke: summarize what the adapter would feed the curator
    built = doj_leads()
    print(f"[doj_lead_adapter] built {len(built)} Lead(s)")
    for lead in built[:3]:
        print(
            f"  - {lead.lead_id}  {lead.provenance}  "
            f"{len(lead.raw_text):>6} chars  {lead.title[:70]!r}"
        )
