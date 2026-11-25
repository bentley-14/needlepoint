"""gold_set builder — proposals → labels (links 2–4) — SKELETON.

⚠️ **Skeleton only.** Every action raises ``NotImplementedError``; this
module is scaffolding. It is safe to import — it will never write or mutate
``gold_set.jsonl`` in its current state.

Naming + placement (why ``GoldSetBuilder``, not ``...Scraper``)
--------------------------------------------------------------
This is **not** a scraper (it fetches nothing) and **not** DOJ-specific — it
consumes *proposals* from any discovery producer (the DOJ scraper today, a
future exploratory web agent as a peer) and builds the canonical label set.
Hence ``GoldSetBuilder`` in ``gold_set_builder.py``, not ``DojGoldSetScraper``.

It currently subclasses ``BaseScraper`` and lives in ``pipeline/scrapers/``
because PR #115 removed the separate ``BaseTransform`` ABC and PR #116 made
``transform()`` a hook on ``BaseScraper`` (its canonical user is exactly this
chain). **Open question for the links-2–4 session:** ``pipeline/transforms/``
already exists and its ``__init__`` names ``transforms/curator/`` as this
chain's intended home — so whether this stays a ``BaseScraper`` in
``scrapers/`` or moves to ``transforms/`` is an unresolved architectural
call tied to the pipeline restructure. Flagged, not decided here.

Where this sits
--------------
``DojPressReleasesScraper`` (link 1, ``doj_press_releases.py``) does
discovery → ``curator_input.jsonl`` (the curator's input list) + warm cache.
**This** module does the rest of the proposals → gold_set pipeline:

```
2. EXTRACT    transform() calls the corpus_curator agent on the gated
              curator_input rows → structured proposals  (fuzzy, LLM, WebSearch)
3. POST-PROC  transform() runs the deterministic gate on each proposal:
              NPPES-verify the NPI · outcome-cap active_years · billing-surface check
4. MERGE      identity-merge survivors into data/extracted/gold_set.jsonl,
              preserving manual overrides, under the drift guard
```

Two-step by design (Q3): the LLM's fuzzy judgment (link 2) is kept separate
from reproducible label construction (links 3–4). The agent **proposes**; the
deterministic gate **decides**. The agent itself stays in
``fraud-investigator-v2/`` — this module imports its ``run()`` entry point,
it does not embed the agent code.

Hard constraints recorded for the implementation (see journal Q4):
  * **Canonical file = ``data/extracted/gold_set.jsonl``** (the ranker's
    loader treats it canonical; ``data/source/`` is legacy; LEIE rows are
    not carried forward).
  * **Merge by identity (NPI + fuzzy case match), preserve ``_``-overrides.**
    New case → append. Follow-up release on a known case → update *derived*
    fields only (e.g. outcome → re-cap active_years); never touch
    ``_original_all_active_years`` / ``_active_years_source`` / ``_outcome_cap``
    / ``_scheme_summary_source`` / ``_extraction_confidence`` or audit fixes.
  * **Respect the drift guard**: back up
    (``gold_set.jsonl.bak_pre_<reason>_<ISO>``) + add a ``docs/GOLD_SET.md``
    changelog entry + write via temp-file→atomic-rename. The pre-commit hook
    hard-blocks otherwise. **Do not use the inherited ``_write`` for the
    gold_set artifact** — its naive overwrite skips backup + changelog. A
    dedicated drift-guarded writer is required.
  * **A wrong NPI is worse than a missing case** — it puts ``y=1`` on an
    innocent provider and leaves the real one unlabeled. Low-confidence NPI
    resolutions stay human-gated, never auto-merged.
  * **Merge trust router keys on ``(provenance, npi_confidence)``** — source
    authority and NPI-resolution confidence are different axes (the wrong-NPI
    risk lives in resolution, not the source). ``doj_direct`` + unambiguous
    high-confidence NPI → auto-merge; everything else (web/OIG/board sources,
    or any ambiguous NPI even from DOJ) → human review. The deterministic
    gate always runs regardless — "auto" skips the *human* step, not the
    *checks*.

Discovery architecture (see the journal's "Discovery architecture" section)
--------------------------------------------------------------------------
The curator is a **generalized extraction engine**: it accepts a normalized
``Lead`` (``provenance``, ``primary_source_url``, ``raw_text``, ``hints``) and
returns a ``Proposal``. Each discovery source adapts to that contract — the
DOJ scraper via a thin adapter, a future exploratory web-roaming agent (peer
producer, source #2) via its own formatting step. This module is the DOJ
path; both paths converge on the deterministic gate + merge below.
"""
from __future__ import annotations

import datetime as _dt
import json
import shutil
from pathlib import Path
from typing import Any, ClassVar, Optional, Type

import pandas as pd
import pandera.pandas as pa
from pandera.typing import Series

from pipeline.base import BaseScraper, BaseScraperSchema, RunResult
from pipeline.leads import Lead, normalize_url
from pipeline.scrapers.doj_lead_adapter import doj_leads
from pipeline.scrapers.doj_takedown_lead_adapter import takedown_leads
from pipeline.scrapers.download import PROJECT_ROOT

# Canonical label file (Q4). The drift-guarded merge writes HERE — not via
# the inherited _write overwrite.
GOLD_SET_PATH = PROJECT_ROOT / "data" / "extracted" / "gold_set.jsonl"

# Clean-room backfill output — a SEPARATE, from-scratch rebuild. Never the
# canonical file: the historical backfill writes here so it can be diffed
# against canonical before anyone adopts it (see GOLD_SET_BACKFILL.md).
BACKFILL_PATH = PROJECT_ROOT / "data" / "extracted" / "gold_set_backfill.jsonl"

# The curator input list this builder consumes (link 1's output).
CURATOR_INPUT = (
    PROJECT_ROOT / "data" / "source" / "doj_api" / "curator_input.jsonl"
)

# Per-run cap on NEW leads sent to the curator (≈ DEFAULT_MAX_NEW / chunk_size
# `claude -p` calls). Bounds weekly spend so a takedown backlog drains a slice
# per run rather than in one shot; the curated ledger means each run picks up
# where the last left off. 16 ≈ two 8-lead chunks/run — conservative; override
# with the GOLDSET_MAX_NEW env var (run_gold_set_builder reads it).
DEFAULT_MAX_NEW = 16

# Where the curator agent drops proposals (matches corpus_curator.PROPOSALS_DIR).
PROPOSALS_DIR = PROJECT_ROOT / "data" / "extracted" / "proposals"

# Rolling human-review worklist (updates + holds + parks the merge defers). The
# merge APPENDS here (dedup by kind+npi); the weekly review-digest email reads
# the whole open queue. Resolution = a human deletes the line when handled.
REVIEW_QUEUE = PROPOSALS_DIR / "needs_review.jsonl"

# Per-run scratch/output dirs: the leads.jsonl handoff to the curator + the
# curator's own events/proposals/summary land under one folder per run.
RUNS_DIR = PROJECT_ROOT / "state" / "runs"

# Local CMS panels the billing-surface check reads (Part B/D/DMEPOS parquets).
PANELS_DIR = PROJECT_ROOT / "data" / "panels"


# ---------------------------------------------------------------------------
# Gate helpers (link 3) — pure, deterministic, unit-testable (no network).
# Decision model: pipeline/docs/GOLD_SET_GATE.md.
# ---------------------------------------------------------------------------


def _outcome_year_of(outcome_date: Any) -> Optional[int]:
    """Year from an ISO ``outcome_date`` (``YYYY-MM-DD``); None if absent/bad."""
    if not outcome_date:
        return None
    try:
        return int(str(outcome_date)[:4])
    except (ValueError, TypeError):
        return None


def _cap_active_years(
    active_years: Any, outcome_year: Optional[int]
) -> tuple[list[int], Optional[list[int]], str]:
    """Cap fraud-active years at the outcome year (port of
    ``scripts/cap_active_years_by_outcome.py``).

    Returns ``(capped, original_if_changed, tag)``. ``original_if_changed`` is
    None when nothing changed, so the merge only writes
    ``_original_all_active_years`` on a real change. ``tag`` → ``_outcome_cap``.
    """
    ay = [int(y) for y in (active_years or [])]
    if not ay:
        return [], None, "no_active_years"
    if outcome_year is None:
        return ay, None, "no_outcome_date"
    capped = [y for y in ay if y <= outcome_year]
    if capped == ay:
        return ay, None, "in_bounds"
    if not capped:                       # all years post-outcome → keep just the outcome year
        return [outcome_year], ay, "all_post_outcome_collapsed_to_year"
    return capped, ay, f"truncated_at_{outcome_year}"


def _split_name(full_name: Any) -> tuple[str, str]:
    """(first, last) from a full name; ('', '') if empty."""
    parts = str(full_name or "").split()
    if not parts:
        return "", ""
    return parts[0], parts[-1]


def _route_proposal(
    *,
    npi: Any,
    exists: bool,
    entity_type: Optional[str],
    name_match: Optional[str],
    surface_verdict: str,
    n_candidates: int,
    active_years_source: Optional[str] = None,
) -> tuple[str, str]:
    """Pure router → ``(gate_decision, gate_reason)``. See pipeline/docs/GOLD_SET_GATE.md.

    Two confidence axes gate ACCEPT; **either** weak → HOLD:
      * **NPI confidence** — a single, unambiguous, name-matched individual NPI.
        An uncertain NPI also makes the surface verdict untrustworthy (it may
        have been checked against the wrong provider).
      * **Active-years grounding** — the conduct window must come from the
        source (or web corroboration), not be guessed. ``inferred`` /
        ``outcome_fallback`` (and a missing/``unknown`` signal) → HOLD, so we
        never auto-mint a label with guessed years (the Provider Kapok failure).

    Only the two *definitive* structural skips (no NPI, or an organization NPI)
    short-circuit before the confidence gate.
    """
    # ── Definitive structural skips (independent of surface / confidence) ──
    if not npi or not exists:
        return "skip", "no resolvable NPI in NPPES"
    if entity_type != "NPI-1":
        return "skip", f"NPI is {entity_type or 'unknown'} (organization), not an individual provider"
    # ── Confidence gate (NPI + years) — either weakness routes to human review ──
    flags: list[str] = []
    if n_candidates != 1:
        flags.append(f"{n_candidates} NPI candidates (need exactly 1)")
    if name_match not in ("exact", "prefix"):
        flags.append(f"name match is '{name_match}' (not exact/prefix)")
    if active_years_source not in ("source_stated", "web_corroborated"):
        flags.append(f"active-years grounding '{active_years_source or 'unknown'}' (not source-stated/web-corroborated)")
    if flags:
        return "hold", "; ".join(flags) + " — needs human confirmation before this becomes a label"
    # ── Confident NPI + grounded years → the surface verdict is now trustworthy ──
    if surface_verdict == "SURFACE_FOUND":
        return "accept", "single individual NPI, name matches, billing surface found in a fraud year"
    if surface_verdict == "UNVALIDATABLE_NO_PANELS":
        return "park", "CMS panels for the active years not published yet — re-check when data lands"
    return "skip", "NO_SURFACE — confident individual NPI but no Medicare billing surface in any fraud year (structural)"


def _classify_merge(proposal: dict, gold_rows: list[dict]) -> tuple[str, Optional[int], str]:
    """Decide how an ACCEPT proposal merges into ``gold_rows`` (link 4 identity).

    Returns ``(action, target_index, reason)`` where action ∈
    ``{"append", "update", "hold"}`` (target_index set only for ``update``).
    See pipeline/docs/GOLD_SET_GATE.md — Fork-1 rule. NOTE a ``source_url`` is
    **per-release, not per-defendant** — one release can name many billing NPIs
    (each its own row), so source_url alone never implies "same row"; it must be
    paired with an NPI match (or a null-NPI row to resolve).

      * known NPI + (overlapping years OR same source_url) → UPDATE (same
        defendant / follow-up).
      * known NPI + neither → HOLD (distinct case? never auto-append a 2nd row).
      * new NPI + source_url matches an UNRESOLVED (npi=null) row → UPDATE
        (resolves the Kapok case: same press release, NPI was null).
      * new NPI otherwise → APPEND (a genuinely new defendant — incl. another
        defendant from a release that's already partially curated).
    """
    npi = str(proposal.get("npi") or "")
    src = normalize_url(proposal.get("source_url") or "")
    p_years = set(proposal.get("all_active_years") or [])

    if not npi:
        return "hold", None, "ACCEPT row has no NPI (unexpected) — human review"

    # ── A) same defendant already in gold_set? (NPI match) ──────────────────
    npi_hits = [i for i, r in enumerate(gold_rows) if str(r.get("npi") or "") == npi]
    if npi_hits:
        same_case = [
            i for i in npi_hits
            if (p_years & set(gold_rows[i].get("all_active_years") or []))
            or (src and normalize_url(gold_rows[i].get("source_url") or "") == src)
        ]
        if len(same_case) == 1:
            return "update", same_case[0], "NPI matches an existing row (overlapping years or same source_url) — same case/follow-up"
        if len(same_case) > 1:
            return "hold", None, f"NPI matches {len(same_case)} existing rows (ambiguous)"
        return "hold", None, (
            f"known NPI ({len(npi_hits)} existing row(s)) but no overlapping years / source_url — "
            "distinct case or mis-grounded follow-up? human confirms (never auto-append a 2nd row for a known NPI)"
        )

    # ── B) new NPI, but does this release have an UNRESOLVED (npi=null) row? ──
    if src:
        null_rows = [
            i for i, r in enumerate(gold_rows)
            if normalize_url(r.get("source_url") or "") == src and not str(r.get("npi") or "")
        ]
        if len(null_rows) == 1:
            return "update", null_rows[0], "resolves an unresolved (npi=null) row for the same press release"
        if len(null_rows) > 1:
            return "hold", None, f"{len(null_rows)} unresolved rows share this source_url — which defendant? human confirms"

    # ── C) genuinely new defendant (incl. another NPI from a partly-curated release) ──
    return "append", None, "new NPI — not in gold_set (new defendant)"


# ---------------------------------------------------------------------------
# Schema (PROVISIONAL — finalized next session)
# ---------------------------------------------------------------------------


class GoldSetSchema(BaseScraperSchema):
    """Provisional schema for ``gold_set.jsonl``.

    ⚠️ Incomplete. The live gold_set carries ~35 columns; declared here are
    the **label-load-bearing** fields (read by
    ``fraudlib.labels.v4_gold.load_v4_positives_scoped``) plus the curator-
    override / provenance fields the merge must preserve. The remaining
    audit/billing-validation columns are added when link 4 is implemented and
    the exact on-disk shape is pinned. Until then this schema is **not** used
    for a real ``_write`` (fetch/transform are stubs).

    Dtype notes
    -----------
    ``all_active_years`` / ``fraud_types`` / ``covered_years`` / ``outcome`` and
    the ``_original_all_active_years`` / ``_outcome_cap`` override fields hold
    Python ``list`` / ``dict`` values; pandas stores those as ``object``, so
    they are declared ``Series[object]`` (coercion is a no-op for object).

    The five curator-override / provenance fields are ``_``-prefixed on disk.
    Pandera treats a leading-underscore class attribute as private metadata and
    would silently drop it from the schema, so each is declared under a normal
    attribute name with an explicit ``alias`` carrying the real column name.
    """

    # --- label-load-bearing (the y=1 join) ---
    # Canonical join key: `npi`, matching the `npi` column on every feature
    # source (ProviderRecordSchema: CMS panels, NPPES, …). The on-disk gold_set
    # now uses `npi` too — the legacy `v4_npi` column was renamed in PR #125
    # (loaders keep a `v4_npi` fallback for legacy files). See docs/GOLD_SET.md.
    npi: Series[str] = pa.Field(str_matches=r"^(\d{10})?$", nullable=True)  # zero-padded resolved NPI — the join key
    all_active_years: Series[object] = pa.Field(nullable=True)   # years that get y=1 (outcome-capped)
    case_id: Series[str] = pa.Field(nullable=True)               # drives the train/eval split hash

    # --- carried metadata (for agents / dedup / display) ---
    defendant_name: Series[str] = pa.Field(nullable=True)
    defendant_specialty: Series[str] = pa.Field(nullable=True)
    defendant_state: Series[str] = pa.Field(nullable=True)
    fraud_types: Series[object] = pa.Field(nullable=True)
    outcome: Series[object] = pa.Field(nullable=True)            # {type, date, amount_usd}
    scheme_summary: Series[str] = pa.Field(nullable=True)
    source_url: Series[str] = pa.Field(nullable=True)            # dedup key vs. curator_input
    covered_years: Series[object] = pa.Field(nullable=True)      # active_years ∩ CMS-available years

    # --- curator overrides / provenance (MERGE MUST PRESERVE these) ---
    original_all_active_years: Series[object] = pa.Field(
        alias="_original_all_active_years", nullable=True
    )
    active_years_source: Series[str] = pa.Field(alias="_active_years_source", nullable=True)
    outcome_cap: Series[object] = pa.Field(alias="_outcome_cap", nullable=True)
    scheme_summary_source: Series[str] = pa.Field(alias="_scheme_summary_source", nullable=True)
    extraction_confidence: Series[str] = pa.Field(alias="_extraction_confidence", nullable=True)


# ---------------------------------------------------------------------------
# Scraper (SKELETON)
# ---------------------------------------------------------------------------


class GoldSetBuilder(BaseScraper):
    """Links 2–4: proposals → gold_set labels (extract + gate + merge). **Skeleton.**

    Mental-model placement (this is the box after ``proposals``)::

        DOJ scraper ─┐                                    (link 1 — doj_press_releases.py, DONE)
                     ├─► corpus_curator ─► proposals ─► [ THIS MODULE ] ─► gold_set
        exploratory ─┘   (fuzzy LLM                        gate + merge
        agent (peer)      extraction)                      (links 3–4)

    So, to the review question "is this the curator, or what feeds it?":
    **neither.** This module is the *orchestrator + deterministic gate + merge*.
    It is NOT a discovery source and NOT the curator. It:

      * ``_run_curator`` (link 2) — adapts leads to the curator's ``Lead``
        contract and calls the curator (which lives in fraud-investigator-v2/);
        the *curator* does the fuzzy name→NPI / scheme / outcome extraction.
      * ``transform`` (link 3) — runs the **deterministic** post-processing on
        the curator's proposals (NPI-verify · outcome-cap · billing-surface).
        "Deterministic post-processing" = no LLM; the LLM step is the curator,
        upstream of here.
      * ``_merge_into_gold_set`` (link 4) — drift-guarded, trust-routed merge.

    Everything in this class is deterministic; the only fuzzy/LLM hop is the
    curator it calls in link 2.
    """

    source: ClassVar[str] = "gold_set"
    schema: ClassVar[Type[BaseScraperSchema]] = GoldSetSchema
    output_dir: ClassVar[Path] = GOLD_SET_PATH.parent  # data/extracted/
    schema_version: ClassVar[str] = "v0.1"             # skeleton
    output_format: ClassVar[str] = "jsonl"

    # ---- fetch entrypoint --------------------------------------------------

    def fetch(self, *, since_date: Optional[str] = None) -> RunResult:
        """Orchestrate links 2–4 for the DOJ path.

        Link 2 (curator wiring) is implemented; links 3–4 (gate + merge) are
        still stubs, so a *full* ``fetch`` raises at :meth:`transform` until
        those land (Q3/Q4). To run **only** link 2 (leads → proposals), call
        :meth:`produce_proposals`.

        Flow::

            run_dir = self._new_run_dir()
            leads   = doj_leads()                              # link 2a (DOJ adapter)
            out     = self._run_curator(leads, run_dir=...)    # link 2b (curator)
            df      = self.transform(self._proposals_to_frame(out))   # link 3 (stub)
            return self._merge_into_gold_set(df, ...)          # link 4 (stub)
        """
        started = self._now()
        run_dir = self._new_run_dir()
        leads = doj_leads()                                       # link 2a: DOJ adapter
        curator_out = self._run_curator(leads, run_dir=run_dir)   # link 2b: curator
        df = self.transform(self._proposals_to_frame(curator_out))  # link 3 (Q3 — stub)
        return self._merge_into_gold_set(df, started_at_utc=started)  # link 4 (Q4 — stub)

    # ---- link 2 entry (runnable now) ---------------------------------------

    def produce_proposals(self, *, run_dir: Optional[Path] = None):
        """Run **link 2 only**: DOJ adapter → leads → curator → proposals.

        Returns the curator's ``CorpusCuratorOutput``, or ``None`` if there were
        no leads this run (the idle case — no agent invoked). The curator also
        persists the proposals to its proposals dir (see ``corpus_curator.run``).
        This is the runnable surface until the gate (Q3) + merge (Q4) land.
        """
        run_dir = run_dir or self._new_run_dir()
        return self._run_curator(doj_leads(), run_dir=run_dir)

    # ---- full chain entry (links 2–4) — the weekly cron calls this ----------

    def run_once(
        self, *, dry_run: bool = True, run_dir: Optional[Path] = None, chunk_size: int = 8,
        max_new: int = DEFAULT_MAX_NEW, ledger_path: Optional[Path] = None,
    ):
        """Run links 2–4 once: leads → curator → gate → merge. Returns
        ``(report, digest)`` where ``digest`` is the human-readable email body.

        ``dry_run=False`` performs the real drift-guarded write (appends only;
        updates/holds deferred to the review queue). Empty leads (idle week) flow
        through cleanly to a quiet, zero-change report.

        **Cross-run dedup + spend cap.** Discovery re-finds the same releases each
        run, so before spending any tokens we drop every lead already in the
        curated ledger (:mod:`pipeline.scrapers.curated_ledger`, persisted to R2
        between runs) and then take at most ``max_new`` of the rest. A takedown
        backlog therefore drains a slice per run instead of blowing the org spend
        limit in one shot; the ledger advances only with leads whose chunk
        actually completed, so anything cut by the cap *or* killed mid-run is
        retried next week (no loss). On ``dry_run`` the ledger is **not** advanced
        (testing must not mutate shared cross-run state).

        The curator is invoked in **chunks** of ``chunk_size`` (default 8 — the
        validated batch size), via the same :meth:`_curate_chunks` the backfill
        uses. This is load-bearing: a single ``claude -p`` call over a large batch
        overruns the agent's turn budget (``error_max_turns``); chunking also
        isolates per-chunk failures instead of aborting the whole run.
        """
        from pipeline.scrapers.curated_ledger import LEDGER_PATH, append_ledger, load_ledger

        run_dir = run_dir or self._new_run_dir()
        ledger_path = ledger_path or LEDGER_PATH
        already = load_ledger(ledger_path)

        # Both discovery sources feed one pool: DOJ press releases (link 1) +
        # the National Takedown court-docs/summaries (link 1b). The ledger +
        # cap throttle the combined stream, so a takedown backlog drains a slice
        # per run alongside the weekly press-release trickle. (A federal case
        # found by BOTH paths yields two leads with different URLs/ids; the
        # gate's already-labeled dedup collapses them to one label downstream.)
        all_leads = doj_leads() + takedown_leads()
        fresh = [ld for ld in all_leads if ld.lead_id not in already]
        capped = fresh[:max_new] if max_new else fresh
        print(
            f"[gold_set_builder] {len(fresh)} unseen lead(s) "
            f"({len(already)} already curated in ledger); this run curates "
            f"{len(capped)} (cap max_new={max_new})",
            flush=True,
        )

        curated: list[tuple[str, str]] = []
        if capped:
            chunks = [capped[i:i + chunk_size] for i in range(0, len(capped), chunk_size)]
            print(
                f"[gold_set_builder] {len(capped)} leads → {len(chunks)} chunk(s) "
                f"of ≤{chunk_size}; invoking curator",
                flush=True,
            )
            all_cases, _, curated = self._curate_chunks(
                chunks, run_dir=run_dir, workers=1, resume=False
            )
        else:
            print("[gold_set_builder] 0 new leads — skipping curator (idle / caught-up run)", flush=True)
            all_cases = []

        proposals_df = self.transform(pd.DataFrame(all_cases))
        report = self._merge_into_gold_set(proposals_df, dry_run=dry_run, run_dir=run_dir)

        # Advance the ledger ONLY on a real run, ONLY for completed-chunk leads.
        if not dry_run and curated:
            added = append_ledger(curated, ledger_path)
            print(f"[gold_set_builder] curated-ledger += {added} lead(s) → {ledger_path}", flush=True)

        proposals = proposals_df.to_dict("records") if len(proposals_df) else []
        return report, format_run_digest(report, proposals)

    # ---- historical backfill (links 2–4 over the FULL corpus) --------------

    def run_backfill(
        self,
        *,
        dry_run: bool = True,
        chunk_size: int = 8,
        limit: Optional[int] = None,
        offset: int = 0,
        workers: int = 1,
        deadline_minutes: Optional[int] = None,
        clean_room: bool = False,
        out_path: Optional[Path] = None,
        run_dir: Optional[Path] = None,
        resume: bool = False,
        leads: Optional[list] = None,
    ):
        """Run links 2–4 over the **entire** DOJ corpus, not the trailing week.

        Same deterministic gate as :meth:`run_once`; the differences are the
        *input* (the full warm cache via :func:`cache_leads`, ~thousands of
        releases), that the curator is invoked in **chunks** (the journal's
        ``error_max_turns`` lesson) optionally across ``workers`` concurrent
        threads, and an optional **clean-room** mode.

        Two modes (``clean_room``):

        * **incremental** (default) — dedupe against the canonical gold_set and
          drift-guard-merge ACCEPTs into it (only ADDS new defendants / EXTENDS
          known NPIs; never shrinks/removes — see GOLD_SET_BACKFILL.md).
        * **clean-room** — treat the canonical gold_set as **empty** (Step-0
          dedupe disabled) and build a *fresh* label set into ``out_path``
          (default ``data/extracted/gold_set_backfill.jsonl``), **never touching
          the canonical file**. ACCEPTs are deduped by NPI (active-years unioned)
          → one row per confident individual. The result is a from-scratch,
          uniformly-provenanced rebuild you diff against canonical before adopting.

        Parameters
        ----------
        chunk_size   leads per curator invocation (default 8 — the validated batch size).
        workers      concurrent curator subprocesses (default 1). The curator is
                     I/O-bound (``claude -p``) and each chunk has its own run_dir,
                     so threads parallelize cleanly; raise to fit a full sweep into
                     one overnight run (mind LLM rate limits).
        offset       skip the first ``offset`` leads — for non-overlapping waves.
        limit        cap leads taken *after* the offset (smoke runs / wave size); None = to the end.
        clean_room   build a fresh set into ``out_path`` instead of merging into canonical.
        out_path     clean-room output file (default BACKFILL_PATH); ignored when incremental.
        resume       reuse a prior ``run_dir``'s completed chunks (skip their curator calls).
        leads        inject leads (tests); default = :func:`cache_leads` (full corpus).

        Returns ``(report, digest)``.
        """
        import time

        from pipeline.scrapers.doj_lead_adapter import cache_leads

        run_dir = Path(run_dir) if run_dir else self._new_backfill_run_dir()
        run_dir.mkdir(parents=True, exist_ok=True)
        leads = cache_leads() if leads is None else leads
        total = len(leads)
        leads = leads[offset:]
        if limit is not None:
            leads = leads[:limit]
        chunks = [leads[i:i + chunk_size] for i in range(0, len(leads), chunk_size)]
        deadline = time.monotonic() + deadline_minutes * 60 if deadline_minutes else None
        print(f"[gold_set_builder] BACKFILL ({'clean-room' if clean_room else 'incremental'}): "
              f"leads {offset}..{offset + len(leads)} of {total} → {len(chunks)} chunk(s) of "
              f"≤{chunk_size}, {workers} worker(s)"
              f"{f', deadline {deadline_minutes}min' if deadline_minutes else ''} "
              f"(resume={resume}, dry_run={dry_run}) → {run_dir}", flush=True)

        all_cases, completed_chunks, _curated = self._curate_chunks(
            chunks, run_dir=run_dir, workers=workers, resume=resume, deadline=deadline)

        # Checkpoint: how far into the corpus this wave got (exact contiguous prefix).
        leads_done = sum(len(c) for c in chunks[:completed_chunks])
        next_offset = offset + leads_done
        done = next_offset >= total

        # Clean-room routes against an EMPTY corpus (gold_rows=[]); incremental
        # dedupes against the canonical set inside transform().
        proposals_df = self.transform(
            pd.DataFrame(all_cases), gold_rows=[] if clean_room else None
        )
        if clean_room:
            report = self._build_clean_room(
                proposals_df, out_path=Path(out_path) if out_path else BACKFILL_PATH,
                dry_run=dry_run, run_dir=run_dir,
            )
        else:
            report = self._merge_into_gold_set(proposals_df, dry_run=dry_run, run_dir=run_dir)
        report["backfill"] = {"leads": len(leads), "offset": offset, "total": total,
                              "chunks": len(chunks), "completed_chunks": completed_chunks,
                              "workers": workers, "clean_room": clean_room,
                              "next_offset": next_offset, "done": done, "run_dir": str(run_dir)}
        proposals = proposals_df.to_dict("records") if len(proposals_df) else []

        # Single accumulated review CSV (the email attachment) — clean-room collects
        # its review items in the report; incremental reuses the rolling review queue.
        review_items = report.get("review_items") if clean_room else read_review_queue()
        report["review_csv"] = str(write_review_csv(review_items or [], run_dir / "needs_review.csv"))

        # Reliability-audit CSV (the diff vs canonical) — the audit deliverable.
        if clean_room:
            report["diff_csv"] = str(write_diff_csv(
                report.get("diff_vs_canonical", {}).get("detail", []), run_dir / "diff_vs_canonical.csv"))

        return report, format_backfill_digest(report, proposals)

    def _curate_chunks(
        self, chunks, *, run_dir: Path, workers: int, resume: bool,
        deadline: Optional[float] = None,
    ) -> tuple[list[dict], int, list[tuple[str, str]]]:
        """Curate chunks (resume-aware), optionally across ``workers`` threads, up to
        an optional wall-clock ``deadline`` (``time.monotonic()`` value).

        Processes chunks **in order, in waves of ``workers``**, so the chunks that
        complete form an exact contiguous prefix — the caller turns that into the
        next-offset checkpoint. When ``deadline`` is reached, no further waves start
        (in-flight ones finish). Each chunk's proposals persist to
        ``run_dir/chunk_<i>/chunk_proposals.jsonl`` so a killed/timed-out run resumes
        without re-paying the curator.

        Returns ``(all_cases, completed_chunks, curated)`` where ``completed_chunks``
        is the length of the contiguous prefix that finished within the deadline, and
        ``curated`` is the ``(lead_id, primary_source_url)`` of every lead in a chunk
        that **completed** (succeeded — including a clean run that proposed nothing).
        A chunk that FAILED (curator error, rate limit, spend limit) contributes
        nothing to ``curated``, so its leads stay un-marked and are retried next run
        — the no-loss property the cross-run ledger relies on (see ``run_once``).
        """
        import time
        from concurrent.futures import ThreadPoolExecutor

        n = len(chunks)

        def do_chunk(ci_chunk: tuple[int, list]) -> tuple[bool, list[dict]]:
            """Returns ``(completed, cases)``. ``completed`` is False only on a curator
            failure — distinct from a clean chunk that simply yielded 0 proposals."""
            ci, chunk = ci_chunk
            chunk_dir = run_dir / f"chunk_{ci}"
            cached = chunk_dir / "chunk_proposals.jsonl"
            if resume and cached.exists():
                cases = [json.loads(l) for l in cached.read_text().splitlines() if l.strip()]
                print(f"[gold_set_builder]   chunk {ci+1}/{n} — resumed {len(cases)} proposal(s)", flush=True)
                return True, cases
            # Isolate per-chunk failures: a transient curator error (timeout, rate
            # limit, bad lead) must NOT abort an overnight sweep of ~600 chunks. The
            # failed chunk contributes nothing and is NOT cached, so `--resume`
            # retries only it. (Persist happens only on success, below.)
            try:
                out = self._run_curator(chunk, run_dir=chunk_dir)
            except Exception as e:  # noqa: BLE001 — deliberately broad for unattended robustness
                print(f"[gold_set_builder]   chunk {ci+1}/{n} — FAILED ({type(e).__name__}: {e}); "
                      f"not cached, will retry on --resume", flush=True)
                return False, []
            cases = [] if out is None else [c.model_dump() for c in out.proposed_cases]
            chunk_dir.mkdir(parents=True, exist_ok=True)
            cached.write_text("\n".join(json.dumps(c, default=str) for c in cases))
            print(f"[gold_set_builder]   chunk {ci+1}/{n} — {len(chunk)} leads → {len(cases)} proposal(s)", flush=True)
            return True, cases

        items = list(enumerate(chunks))
        all_cases: list[dict] = []
        curated: list[tuple[str, str]] = []
        completed = 0
        wave = max(1, workers)
        for w in range(0, n, wave):
            if deadline is not None and time.monotonic() >= deadline:
                print(f"[gold_set_builder] deadline reached after {completed}/{n} chunk(s) — "
                      f"checkpointing", flush=True)
                break
            batch = items[w:w + wave]
            if wave == 1:
                results = [do_chunk(batch[0])]
            else:
                with ThreadPoolExecutor(max_workers=wave) as ex:
                    results = list(ex.map(do_chunk, batch))
            for (_ci, chunk), (chunk_done, cases) in zip(batch, results):
                if chunk_done:
                    all_cases.extend(cases)
                    # Only real Leads carry lead_id (run_once); the backfill path
                    # ignores `curated` and its tests inject bare objects.
                    curated.extend(
                        (ld.lead_id, ld.primary_source_url)
                        for ld in chunk if getattr(ld, "lead_id", None)
                    )
            completed += len(batch)
        return all_cases, completed, curated

    def _build_clean_room(
        self, df: pd.DataFrame, *, out_path: Path, dry_run: bool, run_dir: Optional[Path] = None,
    ) -> dict:
        """Build a fresh gold_set from the gate's ACCEPTs into ``out_path`` (clean-room).

        A from-empty rebuild, so there are no UPDATE/manual-override concerns: ACCEPTs
        are deduped by NPI (active-years unioned across releases) → one row per
        confident individual. HOLD/PARK become the human-review worklist; SKIP is
        dropped+logged. Writes nothing on ``dry_run``. Never touches the canonical file.
        """
        decisions = df.to_dict("records") if df is not None and len(df) else []
        accepts = [d for d in decisions if d.get("gate_decision") == "accept"]

        # ACCUMULATE across waves: seed from any existing rows already in out_path
        # (a prior wave of the same chain), so successive offset-waves grow ONE file.
        # New rows merge by NPI (years/covered_years/fraud_types unioned).
        by_npi: dict[str, dict] = {}
        if out_path.exists():
            for line in out_path.read_text().splitlines():
                if line.strip():
                    r = json.loads(line)
                    npi = str(r.get("npi") or "")
                    if npi:
                        by_npi[npi] = r
        carried = len(by_npi)
        for p in accepts:
            npi = str(p.get("npi") or "")
            if not npi:
                continue
            if npi not in by_npi:
                by_npi[npi] = self._proposal_to_gold_row(p)
            else:                                  # same defendant in another release → merge evidence
                row, other = by_npi[npi], self._proposal_to_gold_row(p)
                # Union the list-valued, evidence-bearing fields so a second release's
                # validated years / scheme types aren't dropped. Scalar provenance
                # (outcome, source_url, specialty/state) keeps the first occurrence.
                row["all_active_years"] = sorted(
                    set(row.get("all_active_years") or []) | set(other.get("all_active_years") or []))
                row["covered_years"] = sorted(
                    set(row.get("covered_years") or []) | set(other.get("covered_years") or []))
                row["fraud_types"] = list(dict.fromkeys(
                    (row.get("fraud_types") or []) + (other.get("fraud_types") or [])))
        rows = list(by_npi.values())

        review_items = (
            [{"kind": "gate_hold", "npi": d.get("npi"), "defendant_name": d.get("defendant_name"),
              "defendant_specialty": d.get("defendant_specialty"), "defendant_state": d.get("defendant_state"),
              "all_active_years": d.get("all_active_years"), "source_url": d.get("source_url"),
              "reason": d.get("gate_reason")} for d in decisions if d.get("gate_decision") == "hold"]
            + [{"kind": "park", "npi": d.get("npi"), "defendant_name": d.get("defendant_name"),
                "defendant_specialty": d.get("defendant_specialty"), "defendant_state": d.get("defendant_state"),
                "all_active_years": d.get("all_active_years"), "source_url": d.get("source_url"),
                "reason": d.get("gate_reason")} for d in decisions if d.get("gate_decision") == "park"]
        )
        diff = self._diff_against_canonical(rows)
        counts = {
            "proposals": len(decisions), "accept": len(accepts), "unique_npis": len(rows),
            "carried_in": carried, "new_this_wave": len(rows) - carried,
            "gate_hold": sum(1 for d in decisions if d.get("gate_decision") == "hold"),
            "park": sum(1 for d in decisions if d.get("gate_decision") == "park"),
            "skip": sum(1 for d in decisions if d.get("gate_decision") == "skip"),
        }
        # Reuse the merge report shape so the digest/runner read it uniformly:
        # clean-room ACCEPTs are all "appends" into the fresh file.
        report = {
            "dry_run": dry_run, "clean_room": True, "out_path": str(out_path), "counts": counts,
            "append": [{"npi": r.get("npi"), "defendant_name": r.get("defendant_name"),
                        "all_active_years": r.get("all_active_years")} for r in rows],
            "update": [], "merge_hold": [], "review_items": review_items, "diff_vs_canonical": diff,
        }
        print(f"[gold_set_builder] CLEAN-ROOM — {counts['accept']} accept → {len(rows)} unique NPI(s); "
              f"{counts['gate_hold']} hold, {counts['park']} park, {counts['skip']} skip", flush=True)
        print(f"[gold_set_builder] diff vs canonical: {diff['new']} new, "
              f"{diff['year_disagreements']} year-disagree, {diff['missing']} not re-derived", flush=True)

        if run_dir is not None:
            (Path(run_dir) / "clean_room_report.json").write_text(json.dumps(report, indent=2, default=str))

        if not dry_run:
            self._write_clean_room_file(rows, out_path)
            report["written"] = bool(rows)
            print(f"[gold_set_builder] WROTE {len(rows)} rows → {out_path} (canonical untouched)", flush=True)
        return report

    @staticmethod
    def _write_clean_room_file(rows: list[dict], out_path: Path) -> None:
        """Write the clean-room rows to ``out_path`` (backup any existing file first).
        This is a *separate* artifact, not the canonical gold_set, so the canonical
        drift-guard (changelog/pre-commit hook) deliberately does not apply."""
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if out_path.exists():
            iso = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H%M%S")
            shutil.copy2(out_path, out_path.with_name(out_path.name + f".bak_{iso}"))
        tmp = out_path.with_suffix(out_path.suffix + ".tmp")
        with tmp.open("w") as f:
            for row in rows:
                f.write(json.dumps(row, default=str) + "\n")
        tmp.replace(out_path)

    @classmethod
    def _diff_against_canonical(cls, rows: list[dict]) -> dict:
        """Compare a clean-room rebuild (``rows``) to the canonical gold_set by NPI.

        The reliability audit: which canonical defendants the rebuild could NOT
        re-derive (suspect/stale/non-DOJ), which it re-derived with DIFFERENT
        active years (the disagreements worth a human look), and which are net-new.

        Returns counts (for the email summary) plus ``detail`` — the **full,
        uncapped** per-provider list (one dict per diff item) that becomes the
        attached ``backfill_diff_vs_canonical.csv``.
        """
        canon = {str(r.get("npi") or ""): r for r in cls._load_gold_rows() if r.get("npi")}
        built = {str(r.get("npi") or ""): r for r in rows if r.get("npi")}
        canon_npis, built_npis = set(canon), set(built)

        def _years(r):
            return sorted(set(r.get("all_active_years") or []))

        detail: list[dict] = []
        for npi in sorted(built_npis - canon_npis):                 # net-new
            b = built[npi]
            detail.append({"category": "net_new", "npi": npi,
                           "defendant_name": b.get("defendant_name"),
                           "defendant_specialty": b.get("defendant_specialty"),
                           "defendant_state": b.get("defendant_state"),
                           "canonical_years": "", "rebuilt_years": _years(b),
                           "source_url": b.get("source_url")})
        disagreements = 0
        for npi in sorted(canon_npis & built_npis):                 # year disagreements
            cy, by = _years(canon[npi]), _years(built[npi])
            if cy != by:
                disagreements += 1
                b = built[npi]
                detail.append({"category": "year_disagreement", "npi": npi,
                               "defendant_name": b.get("defendant_name"),
                               "defendant_specialty": b.get("defendant_specialty"),
                               "defendant_state": b.get("defendant_state"),
                               "canonical_years": cy, "rebuilt_years": by,
                               "source_url": b.get("source_url")})
        for npi in sorted(canon_npis - built_npis):                 # not re-derived
            c = canon[npi]
            detail.append({"category": "not_re_derived", "npi": npi,
                           "defendant_name": c.get("defendant_name"),
                           "defendant_specialty": c.get("defendant_specialty"),
                           "defendant_state": c.get("defendant_state"),
                           "canonical_years": _years(c), "rebuilt_years": "",
                           "source_url": c.get("source_url")})
        return {
            "canonical_total": len(canon_npis), "rebuilt_total": len(built_npis),
            "new": len(built_npis - canon_npis), "missing": len(canon_npis - built_npis),
            "year_disagreements": disagreements, "detail": detail,
        }

    @staticmethod
    def _new_backfill_run_dir() -> Path:
        """A fresh per-run dir under ``state/runs/`` tagged as a backfill."""
        ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
        run_dir = RUNS_DIR / f"goldset_backfill_{ts}"
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir

    # ---- link 3: deterministic post-processing -----------------------------

    def transform(
        self, df: pd.DataFrame, *, panels_dir: Optional[Path] = None,
        gold_rows: Optional[list[dict]] = None, **kwargs: Any
    ) -> pd.DataFrame:
        """Deterministic gate over curator proposals (link 3).

        Per proposal: outcome-cap → NPI-verify → billing-surface → route to
        ``accept`` / ``hold`` / ``skip`` / ``park`` (decision model:
        ``pipeline/docs/GOLD_SET_GATE.md``). No LLM — re-runs the deterministic tools
        (``nppes_lookup`` by NPI, ``check_billing_surface``) on the curator's
        candidate; the curator's *claimed* verdicts are never trusted.

        Adds these columns: ``npi``, ``npi_verified``, ``nppes_entity_type``,
        ``name_match``, ``nppes_enumeration_year``, ``nppes_specialty``,
        ``nppes_states``, ``outcome_capped_years``, ``_outcome_cap``,
        ``covered_years``, ``surface_verdict``, ``gate_decision``,
        ``gate_reason``. ACCEPT rows feed the merge (link 4); HOLD/PARK rows
        feed the review/parked queues (link 5); SKIP rows are logged + dropped.
        """
        if df is None or len(df) == 0:
            return df if df is not None else pd.DataFrame()

        # Imported here (not at module top) so importing this module never
        # requires the scripts/ tools / panels — only running the gate does.
        from scripts.check_billing_surface import (
            load_surfaces,
            load_surfaces_r2,
            verdict_for,
        )

        records = df.to_dict("records")

        # Pass 1: outcome-cap each row (decides which years we'll check).
        capped_all = [
            _cap_active_years(p.get("all_active_years"), _outcome_year_of(p.get("outcome_date")))
            for p in records
        ]
        # Surface load for the union of all capped years. Default: query the
        # canonical panels straight from R2 via DuckDB httpfs, pushing the small
        # candidate-NPI set down (a few MB of range reads), so CI never has to
        # hydrate the 27 GiB panel store onto a disk-limited runner. Only the
        # *first* NPI candidate per proposal is ever verdict-checked (see the
        # loop below), so that's the set we push down. A caller that explicitly
        # passes panels_dir opts into the local-file loader (tests / offline).
        union_years = sorted({y for capped, _, _ in capped_all for y in capped})
        union_npis = sorted({
            str(cands[0])
            for p in records
            if (cands := [c for c in (p.get("defendant_npi_candidates") or []) if c])
        })
        if not union_years:
            surf, no_panel = {}, set()
        elif panels_dir is not None:
            surf, no_panel = load_surfaces(str(panels_dir), union_years)
        else:
            surf, no_panel = load_surfaces_r2(union_years, union_npis)

        # Index current gold_set NPIs → active years so the gate can dedupe
        # proposals for already-labeled providers (skip dups, surface new years).
        # `gold_rows=[]` (clean-room backfill) disables the already-labeled dedupe
        # so every proposal is routed from scratch against an empty corpus.
        gold_idx = self._gold_year_index(
            self._load_gold_rows() if gold_rows is None else gold_rows
        )

        out: list[dict] = []
        for p, (capped, original, cap_tag) in zip(records, capped_all):
            candidates = [c for c in (p.get("defendant_npi_candidates") or []) if c]
            npi = str(candidates[0]) if candidates else None
            first, _ = _split_name(p.get("defendant_name"))
            info = self._verify_npi(npi, first)

            if npi and capped:
                verdict, hits, _unval = verdict_for(npi, capped, surf, no_panel)
                covered = sorted({y for ys in hits.values() for y in ys})
            else:
                verdict, covered = "NO_SURFACE", []

            known = gold_idx.get(npi) if npi else None
            if known is not None:
                # Already a confirmed gold_set label — the NPI is trusted, so the
                # NPPES name / grounding holds are moot. Dedupe on active years:
                # no NEW years → pure duplicate (SKIP); new years → a potential
                # conduct-window extension, surfaced as an UPDATE for review.
                existing_years, case_ids = known
                new_years = sorted(set(capped) - existing_years)
                cases = ", ".join(case_ids) or "unknown case"
                if not new_years:
                    decision, reason = "skip", (
                        f"already_labeled — NPI already in gold_set ({cases}); "
                        "no new active years vs the existing label"
                    )
                else:
                    decision, reason = "accept", (
                        f"known gold_set label ({cases}) + new active years "
                        f"{new_years} → surfaced as UPDATE for review"
                    )
            else:
                decision, reason = _route_proposal(
                    npi=npi,
                    exists=info["exists"],
                    entity_type=info["entity_type"],
                    name_match=info["name_match"],
                    surface_verdict=verdict,
                    n_candidates=len(candidates),
                    active_years_source=p.get("active_years_source"),
                )

            row = dict(p)
            row["all_active_years"] = capped            # apply the outcome-cap to the label years
            if original is not None:
                row["_original_all_active_years"] = original
            row.update(
                npi=npi,
                npi_verified=info["exists"],
                nppes_entity_type=info["entity_type"],
                name_match=info["name_match"],
                nppes_enumeration_year=info["enum_year"],
                nppes_specialty=info["nppes_specialty"],
                nppes_states=info["nppes_states"],
                outcome_capped_years=capped,
                _outcome_cap=cap_tag,
                covered_years=covered,
                surface_verdict=verdict,
                gate_decision=decision,
                gate_reason=reason,
            )
            out.append(row)

        result = pd.DataFrame(out)
        counts = result["gate_decision"].value_counts().to_dict()
        print(f"[gold_set_builder] gate: {len(result)} proposals → {counts}", flush=True)
        return result

    @staticmethod
    def _verify_npi(npi: Optional[str], proposal_first_name: str) -> dict:
        """Look the candidate NPI up in NPPES (by number) and return structured
        verification info. Verifies the *candidate* — does NOT re-resolve from
        the name (re-resolving 'Emily' would reject the correct 'Emilee').
        """
        info = {
            "exists": False, "entity_type": None, "name_match": None,
            "enum_year": None, "nppes_specialty": None, "nppes_states": [],
        }
        if not npi:
            return info
        from scripts.nppes_lookup import _query, _first_name_strength

        res = _query({"number": str(npi)})
        results = res.get("results") or []
        if not results:
            return info
        r = results[0]
        b = r.get("basic", {})
        info["exists"] = True
        info["entity_type"] = r.get("enumeration_type")           # NPI-1 / NPI-2
        if info["entity_type"] == "NPI-1":
            info["name_match"] = _first_name_strength(proposal_first_name, b.get("first_name") or "")
        ed = b.get("enumeration_date")
        if ed:
            try:
                info["enum_year"] = int(str(ed)[:4])
            except (ValueError, TypeError):
                pass
        taxes = r.get("taxonomies") or []
        info["nppes_specialty"] = (
            next((t.get("desc") for t in taxes if t.get("primary") and t.get("desc")), None)
            or next((t.get("desc") for t in taxes if t.get("desc")), None)
        )
        info["nppes_states"] = sorted({a.get("state") for a in (r.get("addresses") or []) if a.get("state")})
        return info

    # ---- link 2: curator agent (helper, imported entry point) --------------

    def _run_curator(self, leads: list[Lead], *, run_dir: Path):
        """Link 2: write the leads to a file and invoke the corpus_curator agent.

        The DOJ adapter (``doj_leads``) already produced ``Lead``s. The curator
        is a separate **process** (an LLM agent), so we serialize the leads to
        ``run_dir/leads.jsonl`` and hand it that path; the agent reads the file,
        extracts a proposal per lead, and returns a ``CorpusCuratorOutput``.

        **Empty leads short-circuit to ``None``** without importing or invoking
        the curator — an idle week shouldn't spend an LLM turn (mirrors the
        curator's own empty-leads guard).
        """
        if not leads:
            print("[gold_set_builder] 0 leads — skipping curator (idle run)", flush=True)
            return None
        run_dir.mkdir(parents=True, exist_ok=True)
        leads_path = run_dir / "leads.jsonl"
        self._write_leads(leads, leads_path)
        print(
            f"[gold_set_builder] wrote {len(leads)} leads → {leads_path}; invoking curator",
            flush=True,
        )
        return self._invoke_curator(leads_path, run_dir)

    @staticmethod
    def _write_leads(leads: list[Lead], path: Path) -> None:
        """Serialize ``Lead``s to JSONL (the curator's file interface)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as f:
            for lead in leads:
                f.write(lead.model_dump_json() + "\n")

    @staticmethod
    def _invoke_curator(leads_path: Path, run_dir: Path):
        """Lazily import + call the corpus_curator agent (the LLM step).

        Imported **here**, not at module top, so importing this module never
        requires ``fraud-investigator-v2`` to be installed — only *running* the
        curator does (it needs the agent / pydantic-ai stack + the Claude CLI).
        The data-pipeline env can build leads without that dependency; the
        scheduled job's env must have it to invoke the curator.
        """
        try:
            from fraud_investigator.agents import corpus_curator
        except ModuleNotFoundError as e:  # pragma: no cover - env-dependent
            raise RuntimeError(
                "corpus_curator requires fraud-investigator-v2 installed in this "
                "environment (`pip install -e fraud-investigator-v2`). It is the "
                "agent/LLM step — the data-pipeline env needs it only to invoke "
                "the curator, not to build leads."
            ) from e
        return corpus_curator.run(leads_path=leads_path, run_dir=run_dir)

    @staticmethod
    def _proposals_to_frame(curator_out) -> pd.DataFrame:
        """Flatten a ``CorpusCuratorOutput`` (or ``None``) into a proposals frame."""
        cases = [] if curator_out is None else [c.model_dump() for c in curator_out.proposed_cases]
        return pd.DataFrame(cases)

    @staticmethod
    def _new_run_dir() -> Path:
        """A fresh per-run scratch/output dir under ``state/runs/``."""
        ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
        run_dir = RUNS_DIR / f"goldset_{ts}"
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir

    # ---- link 4: drift-guarded identity merge ------------------------------

    def _merge_into_gold_set(
        self, df: pd.DataFrame, *, dry_run: bool = True,
        run_dir: Optional[Path] = None, started_at_utc: Any = None,
    ) -> dict:
        """Identity-merge the gate's ACCEPT rows into ``GOLD_SET_PATH`` (link 4).

        **Dry-run only for now** (``dry_run=True`` default). It classifies each
        ACCEPT proposal as append / update / hold (:func:`_classify_merge`) and
        reports the diff vs the current gold_set, writing **nothing**. The real
        drift-guarded write (backup + ``docs/GOLD_SET.md`` changelog + atomic
        temp-file→rename) lands in a follow-up *after* this dry-run is reviewed —
        ``dry_run=False`` raises until then.
        """
        if df is None or len(df) == 0:
            print("[gold_set_builder] merge: no proposals", flush=True)
            return {"dry_run": dry_run, "counts": {}, "append": [], "update": [], "merge_hold": []}

        gold_rows = self._load_gold_rows()
        decisions = df.to_dict("records")
        accepts = [d for d in decisions if d.get("gate_decision") == "accept"]

        appends: list[dict] = []
        updates: list[dict] = []
        merge_holds: list[dict] = []
        for p in accepts:
            action, idx, reason = _classify_merge(p, gold_rows)
            if action == "append":
                appends.append({"npi": p.get("npi"), "defendant_name": p.get("defendant_name"),
                                "all_active_years": p.get("all_active_years"), "reason": reason,
                                "new_row": self._proposal_to_gold_row(p)})
            elif action == "update":
                tgt = gold_rows[idx]
                appends_overrides = any(k.startswith("_") and tgt.get(k) for k in tgt)
                updates.append({"npi": p.get("npi"), "defendant_name": p.get("defendant_name"),
                                "target_case_id": tgt.get("case_id"),
                                "target_existing_npi": tgt.get("npi"), "reason": reason,
                                "target_has_manual_overrides": appends_overrides,
                                "changes": self._update_changes(tgt, p)})
            else:
                merge_holds.append({"npi": p.get("npi"), "defendant_name": p.get("defendant_name"),
                                    "reason": reason})

        counts = {
            "proposals": len(decisions),
            "accept": len(accepts), "append": len(appends), "update": len(updates),
            "merge_hold": len(merge_holds),
            "gate_hold": sum(1 for d in decisions if d.get("gate_decision") == "hold"),
            "park": sum(1 for d in decisions if d.get("gate_decision") == "park"),
            "skip": sum(1 for d in decisions if d.get("gate_decision") == "skip"),
        }
        report = {"dry_run": dry_run, "counts": counts,
                  "append": appends, "update": updates, "merge_hold": merge_holds}

        print(f"[gold_set_builder] MERGE DRY-RUN — {counts['accept']} accept → "
              f"{counts['append']} append, {counts['update']} update, "
              f"{counts['merge_hold']} hold-at-merge "
              f"(gate: {counts['gate_hold']} hold, {counts['park']} park, {counts['skip']} skip)",
              flush=True)
        for a in appends:
            print(f"   +APPEND {a['npi']}  {a['defendant_name']}  {a['all_active_years']}", flush=True)
        for u in updates:
            tag = " [⚠ target has manual overrides]" if u["target_has_manual_overrides"] else ""
            print(f"   ~UPDATE {u['npi']}  {u['defendant_name']}  → [{u['target_case_id']}]"
                  f" fields={list(u['changes'])}{tag}", flush=True)
        for h in merge_holds:
            print(f"   ?HOLD   {h['npi']}  {h['defendant_name']}  ({h['reason']})", flush=True)

        # Everything that needs a human (or the idea-executor) — never dropped.
        review_items = (
            [{"kind": "update", **u} for u in updates]
            + [{"kind": "merge_hold", **h} for h in merge_holds]
            + [{"kind": "gate_hold", "npi": d.get("npi"), "defendant_name": d.get("defendant_name"),
                "reason": d.get("gate_reason")} for d in decisions if d.get("gate_decision") == "hold"]
            + [{"kind": "park", "npi": d.get("npi"), "defendant_name": d.get("defendant_name")}
               for d in decisions if d.get("gate_decision") == "park"]
        )

        if run_dir is not None:
            run_dir = Path(run_dir)
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "merge_dryrun.json").write_text(json.dumps(report, indent=2, default=str))

        if dry_run:
            return report

        # ── REAL WRITE (drift-guarded) ──────────────────────────────────────
        # Conservative first-write policy: AUTO-APPEND new NPIs only. Every
        # update/hold is deferred to the review queue — we never auto-modify an
        # existing gold_set row (it may carry manual fixes, e.g. the Kapok #134).
        review_path = self._write_review_file(review_items, run_dir)
        report["review_file"] = str(review_path) if review_path else None
        if not appends:
            report["written"] = False
            print(f"[gold_set_builder] real write: 0 appends — gold_set unchanged; "
                  f"{len(review_items)} item(s) → review", flush=True)
            return report
        backup = self._drift_guarded_append(
            [a["new_row"] for a in appends],
            added=[(a["npi"], a["defendant_name"]) for a in appends],
            n_review=len(review_items),
        )
        report["written"] = True
        report["backup"] = str(backup)
        print(f"[gold_set_builder] WROTE {len(appends)} new rows → {GOLD_SET_PATH.name} "
              f"(backup {backup.name}); {len(review_items)} deferred → {report['review_file']}", flush=True)
        return report

    # ---- drift-guarded write helpers (link 4) ------------------------------

    @staticmethod
    def _write_review_file(items: list[dict], run_dir: Optional[Path]) -> Optional[Path]:
        """Append update/hold/park items to the rolling review queue (``REVIEW_QUEUE``).

        Updates and holds are NEVER auto-applied to gold_set — they accumulate
        here as a standing worklist the weekly review-digest email surfaces.
        Dedup by ``(kind, npi)`` so re-running the same batch doesn't duplicate;
        resolution is a human deleting the line once handled.
        """
        if not items:
            return REVIEW_QUEUE if REVIEW_QUEUE.exists() else None
        REVIEW_QUEUE.parent.mkdir(parents=True, exist_ok=True)
        existing = read_review_queue()
        seen = {(it.get("kind"), str(it.get("npi"))) for it in existing}
        merged = list(existing)
        for it in items:
            key = (it.get("kind"), str(it.get("npi")))
            if key not in seen:
                seen.add(key)
                merged.append(it)
        with REVIEW_QUEUE.open("w") as f:
            for it in merged:
                f.write(json.dumps(it, default=str) + "\n")
        if run_dir is not None:
            (Path(run_dir) / "needs_review.jsonl").write_text(REVIEW_QUEUE.read_text())
        return REVIEW_QUEUE

    @classmethod
    def _drift_guarded_append(cls, new_rows: list[dict], *, added: list, n_review: int) -> Path:
        """Append ``new_rows`` to gold_set under the drift guard: backup →
        atomic temp-file→rename → ``docs/GOLD_SET.md`` changelog. Returns the
        backup path. (Append-only — existing rows are copied verbatim, never
        modified.)
        """
        iso = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H%M%S")
        backup = GOLD_SET_PATH.with_name(GOLD_SET_PATH.name + f".bak_pre_curator_merge_{iso}")
        shutil.copy2(GOLD_SET_PATH, backup)

        existing = [l for l in GOLD_SET_PATH.read_text().splitlines() if l.strip()]
        tmp = GOLD_SET_PATH.with_suffix(".jsonl.tmp")
        with tmp.open("w") as f:
            for line in existing:
                f.write(line + "\n")
            for row in new_rows:
                f.write(json.dumps(row) + "\n")
        tmp.replace(GOLD_SET_PATH)  # atomic

        cls._append_goldset_changelog(n_before=len(existing), added=added, backup=backup, n_review=n_review)
        return backup

    @staticmethod
    def _append_goldset_changelog(*, n_before: int, added: list, backup: Path, n_review: int) -> None:
        """Insert a dated entry right after '## Changelog' in docs/GOLD_SET.md
        (required by the drift-guard pre-commit hook)."""
        doc = PROJECT_ROOT / "docs" / "GOLD_SET.md"
        if not doc.exists():
            return
        date = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
        names = ", ".join(f"{nm} ({npi})" for npi, nm in added)
        entry = (
            f"### {date} — Automated curator merge (+{len(added)} rows)\n"
            f"First automated gold_set append by the curator→gate→merge pipeline "
            f"(`pipeline.scrapers.gold_set_builder`). Appended {len(added)} new individual-NPI "
            f"row(s) — gate ACCEPT (verified NPI-1, grounded active-years, Medicare billing "
            f"surface): {names}. Rows {n_before}→{n_before + len(added)}. {n_review} update/hold "
            f"item(s) were deferred to the review queue "
            f"(`data/extracted/proposals/needs_review.jsonl`), NOT auto-applied — no existing "
            f"row was modified. Backup: `{backup.name}`.\n\n"
        )
        text = doc.read_text()
        marker = "## Changelog\n"
        i = text.find(marker)
        if i == -1:
            doc.write_text(text.rstrip() + "\n\n" + marker + "\n" + entry)
        else:
            j = i + len(marker)
            doc.write_text(text[:j] + "\n" + entry + text[j:].lstrip("\n"))

    @staticmethod
    def _load_gold_rows() -> list[dict]:
        """Current gold_set rows (empty list if the file is absent)."""
        if not GOLD_SET_PATH.exists():
            return []
        with GOLD_SET_PATH.open() as f:
            return [json.loads(line) for line in f if line.strip()]

    @staticmethod
    def _gold_year_index(gold_rows: list[dict]) -> dict:
        """Map ``npi`` → ``(set_of_all_active_years, sorted_case_ids)``.

        The gate uses this to dedupe proposals for **already-labeled** NPIs: a
        re-found label that brings no NEW active years is a pure duplicate (SKIP),
        while new years are a potential extension of the conduct window worth
        surfacing for human review (the curator may have found earlier/later
        fraud years than the existing row carries).
        """
        idx: dict = {}
        for r in gold_rows:
            npi = str(r.get("npi") or "")
            if not npi:
                continue
            years, cases = idx.setdefault(npi, (set(), set()))
            years.update(r.get("all_active_years") or [])
            if r.get("case_id"):
                cases.add(r["case_id"])
        return {npi: (years, sorted(cases)) for npi, (years, cases) in idx.items()}

    # Derived fields an UPDATE may change; ``_``-prefixed manual overrides are
    # NEVER in this list (the merge must not clobber human fixes like Kapok #134).
    _UPDATE_DERIVED_FIELDS = (
        "npi", "all_active_years", "covered_years", "fraud_types", "outcome",
        "defendant_specialty", "defendant_state", "npi_confidence", "source_url",
    )

    @staticmethod
    def _update_changes(existing: dict, proposal: dict) -> dict:
        """Preview of the derived-field changes an UPDATE would apply (old→new).

        Only :data:`_UPDATE_DERIVED_FIELDS`; never touches ``_``-overrides.
        """
        proposed = GoldSetBuilder._proposal_to_gold_row(proposal)
        changes: dict = {}
        for k in GoldSetBuilder._UPDATE_DERIVED_FIELDS:
            new = proposed.get(k)
            old = existing.get(k)
            if new is not None and new != old:
                changes[k] = {"from": old, "to": new}
        return changes

    @staticmethod
    def _proposal_to_gold_row(p: dict) -> dict:
        """Map a gate ACCEPT proposal → a gold_set row (load-bearing + provenance
        fields). The ``nppes_*`` / ``billing_validation`` / ``source_review`` audit
        blocks are deferred to the real-write PR (not needed for the dry-run diff).
        The curator's ``active_years_source`` becomes the gold_set provenance field
        ``_active_years_source``.
        """
        return {
            "npi": p.get("npi"),
            "case_id": p.get("case_id"),
            "defendant_name": p.get("defendant_name"),
            "defendant_specialty": p.get("defendant_specialty") or p.get("nppes_specialty"),
            "defendant_state": p.get("defendant_state"),
            "all_active_years": p.get("all_active_years"),
            "covered_years": p.get("covered_years"),
            "fraud_types": p.get("fraud_types"),
            "outcome": {"type": p.get("outcome_type"), "date": p.get("outcome_date"),
                        "amount_usd": p.get("outcome_amount_usd")},
            "source_url": p.get("source_url"),
            "npi_confidence": "high",
            "nppes_entity_type": p.get("nppes_entity_type"),
            "name_match": p.get("name_match"),
            "_active_years_source": p.get("active_years_source"),
            "_outcome_cap": p.get("_outcome_cap"),
            "_original_all_active_years": p.get("_original_all_active_years"),
            "provenance": "doj_direct",
        }


# ---------------------------------------------------------------------------
# Human-readable run digest (for the weekly cron email)
# ---------------------------------------------------------------------------


def format_run_digest(report: dict, proposals: list[dict]) -> str:
    """Render a gold_set builder run as a human-readable, **actionable** email.

    Leads with what needs a human (deferred updates + HOLDs + PARKs, each with a
    concrete ACTION), then what was auto-added. The weekly cron emails this;
    pair with quiet-on-zero (skip the email when nothing was added AND nothing
    needs review).
    """
    by_npi = {str(p.get("npi") or ""): p for p in proposals}
    c = report.get("counts", {})
    appended = report.get("append", [])
    updates = report.get("update", [])
    holds = [p for p in proposals if p.get("gate_decision") == "hold"]
    parks = [p for p in proposals if p.get("gate_decision") == "park"]
    n_review = len(updates) + len(holds) + len(parks)

    L: list[str] = []
    L.append(f"gold_set builder — {len(appended)} label(s) added, {n_review} need review")
    L.append("=" * 66)
    L.append(f"proposals: {c.get('accept', 0)} accept · {c.get('gate_hold', 0)} hold · "
             f"{c.get('park', 0)} park · {c.get('skip', 0)} skip")
    L.append("")

    if n_review:
        L.append(f"NEEDS REVIEW  ({n_review})")
        L.append("-" * 66)
        for u in updates:
            p = by_npi.get(str(u.get("npi")), {})
            warn = "  [target has MANUAL overrides — don't clobber]" if u.get("target_has_manual_overrides") else ""
            L.append(f"  • UPDATE  {u.get('defendant_name')}  (NPI {u.get('npi')})")
            L.append(f"      existing row {u.get('target_case_id')}{warn}")
            L.append(f"      curator would change: {list((u.get('changes') or {}).keys())}")
            L.append(f"      source: {p.get('source_url', '?')}")
            L.append(f"      ACTION: confirm whether to apply this update to the existing row.")
            L.append("")
        for p in holds:
            reason = p.get("gate_reason", "")
            action = ("find/confirm the right NPI (name didn't match exactly)" if "name match" in reason
                      else "supply the documented conduct window (years couldn't be grounded)" if "grounding" in reason
                      else "resolve the flagged ambiguity")
            L.append(f"  • HOLD    {p.get('defendant_name')}  (NPI {p.get('npi')}, "
                     f"{p.get('defendant_specialty')}/{p.get('defendant_state')})")
            L.append(f"      why: {reason}")
            L.append(f"      years {p.get('all_active_years')}  ·  source: {p.get('source_url', '?')}")
            L.append(f"      ACTION: {action}.")
            L.append("")
        for p in parks:
            L.append(f"  • PARK    {p.get('defendant_name')}  (NPI {p.get('npi')})  years {p.get('all_active_years')}")
            L.append(f"      ACTION: re-check when CMS publishes panels for these years.")
            L.append("")

    if appended:
        L.append(f"ADDED TO gold_set  ({len(appended)})")
        L.append("-" * 66)
        for a in appended:
            p = by_npi.get(str(a.get("npi")), {})
            L.append(f"  + {a.get('defendant_name')}  (NPI {a.get('npi')}, "
                     f"{p.get('defendant_specialty')}/{p.get('defendant_state')})")
            L.append(f"      years {a.get('all_active_years')}  ·  {p.get('source_url', '?')}")
        if report.get("backup"):
            L.append("")
            L.append(f"backup: {report['backup']}")

    return "\n".join(L)


def write_review_csv(items: list[dict], path: Path) -> Path:
    """Write the accumulated review worklist to a single CSV (the backfill email
    attachment). One row per item needing a human, with a concrete ACTION column.
    Always writes a file (header-only when ``items`` is empty) so the workflow can
    attach it unconditionally."""
    import csv

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = ["kind", "defendant_name", "npi", "defendant_specialty", "defendant_state",
            "all_active_years", "source_url", "reason", "action"]
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for it in items:
            row = dict(it)
            row["action"] = _review_action(it)
            if isinstance(row.get("all_active_years"), (list, tuple)):
                row["all_active_years"] = ", ".join(str(y) for y in row["all_active_years"])
            w.writerow(row)
    return path


def write_diff_csv(detail: list[dict], path: Path) -> Path:
    """Write the clean-room reliability audit (rebuilt vs canonical) to a CSV — the
    email's audit attachment. One row per diff item, tagged ``category`` (net_new /
    year_disagreement / not_re_derived). Header-only when there's nothing to report,
    so it can be attached unconditionally. Unlike the review CSV, this is a FULL
    snapshot of the diff each wave (the diff is recomputed over the whole rebuild),
    so callers overwrite it rather than append."""
    import csv

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = ["category", "defendant_name", "npi", "defendant_specialty", "defendant_state",
            "canonical_years", "rebuilt_years", "source_url"]
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for it in detail or []:
            row = dict(it)
            for k in ("canonical_years", "rebuilt_years"):
                if isinstance(row.get(k), (list, tuple)):
                    row[k] = ", ".join(str(y) for y in row[k])
            w.writerow(row)
    return path


def format_backfill_digest(report: dict, proposals: list[dict]) -> str:
    """Email body for a backfill run: a clean-room / reliability-audit header
    (the diff vs canonical) followed by the standard per-proposal run digest."""
    L: list[str] = []
    if report.get("clean_room"):
        c = report.get("counts", {})
        d = report.get("diff_vs_canonical", {})
        n_review = len(report.get("review_items", []))
        L.append(f"gold_set CLEAN-ROOM backfill — {c.get('unique_npis', 0)} confident label(s), "
                 f"{n_review} need review")
        L.append("=" * 66)
        L.append(f"output file: {report.get('out_path')}  (canonical gold_set untouched)")
        L.append(f"proposals: {c.get('accept', 0)} accept ({c.get('unique_npis', 0)} unique NPI) · "
                 f"{c.get('gate_hold', 0)} hold · {c.get('park', 0)} park · {c.get('skip', 0)} skip")
        L.append("")
        L.append("RELIABILITY AUDIT — rebuilt vs canonical (by NPI)")
        L.append("-" * 66)
        L.append(f"  canonical labels:        {d.get('canonical_total', 0)}")
        L.append(f"  rebuilt (confident):     {d.get('rebuilt_total', 0)}")
        L.append(f"  NET-NEW (missed before): {d.get('new', 0)}")
        L.append(f"  YEAR DISAGREEMENTS:      {d.get('year_disagreements', 0)}  (rebuilt years ≠ canonical)")
        L.append(f"  NOT re-derived:          {d.get('missing', 0)}  (in canonical, rebuild couldn't confirm)")
        L.append("  → per-provider detail: attached backfill_diff_vs_canonical.csv")
        L.append("  → cases needing a human: attached backfill_needs_review.csv")
        L.append("")
    return "\n".join(L) + format_run_digest(report, proposals)


def read_review_queue() -> list[dict]:
    """The open human-review worklist (``REVIEW_QUEUE``); empty list if absent."""
    if not REVIEW_QUEUE.exists():
        return []
    return [json.loads(line) for line in REVIEW_QUEUE.read_text().splitlines() if line.strip()]


def _review_action(item: dict) -> str:
    """The concrete next step for one review item, from its kind/reason."""
    reason = item.get("reason", "") or ""
    if item.get("kind") == "update":
        return "confirm whether to apply the curator's update to the existing row"
    if "name match" in reason:
        return "find/confirm the right NPI (name didn't match exactly)"
    if "grounding" in reason:
        return "supply the documented conduct window (years couldn't be grounded)"
    if item.get("kind") == "park":
        return "re-check when CMS publishes panels for these years"
    return "resolve the flagged ambiguity"


def format_review_backlog(items: list[dict]) -> str:
    """Human-readable email of the **standing** review worklist (the whole open
    queue, surfaced every week until a human clears each line). Quiet-on-empty:
    callers should skip the email when ``items`` is empty.
    """
    if not items:
        return "gold_set review queue: empty — nothing needs human review."
    L = [f"gold_set — {len(items)} item(s) need human review",
         "=" * 66,
         "(Resolve each, then delete its line from "
         "data/extracted/proposals/needs_review.jsonl so it stops re-appearing.)",
         "",
         "REMINDER — what each ACTION means (in plain English):",
         "  • find/confirm the right NPI — Every provider has an ID number called an",
         "      NPI. We're not 100% sure we matched this person to the right one, so",
         "      double-check it's really them before they go on the list.",
         "  • supply the conduct window  — We couldn't tell which years this person was",
         "      actually committing fraud. Read the source and write down the start",
         "      and end years.",
         "  • confirm the update         — This person is already on the list. The",
         "      system wants to change some of their details — look and say if it's OK.",
         "  • re-check later (PARK)       — The case is too recent; the Medicare data we",
         "      need isn't published yet. Nothing to do now — it'll come back on its own.",
         "-" * 66,
         ""]
    for it in items:
        L.append(f"  • {str(it.get('kind', '?')).upper():6}  {it.get('defendant_name')}  (NPI {it.get('npi')})")
        if it.get("reason"):
            L.append(f"      why: {it.get('reason')}")
        if it.get("target_case_id"):
            L.append(f"      existing row: {it.get('target_case_id')}")
        L.append(f"      ACTION: {_review_action(it)}.")
        L.append("")
    return "\n".join(L)
