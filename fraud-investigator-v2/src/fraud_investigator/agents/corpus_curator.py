"""Supporting agent — Corpus Curator.

Scrapes new DOJ press releases since a given date, NPPES-matches each
defendant to an NPI, and emits proposed v9 corpus additions for the
human to approve.

Invocation:
    from fraud_investigator.agents import corpus_curator
    corpus_curator.run(
        since_date="2026-04-30",
        run_dir=Path("state/runs/agent_curator_<id>"),
        event_sink=EventSink(...),
    )
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

from ..claude_p import DEFAULT_CLAUDE_BIN
from ..event_sink import (
    EventSink,
    EVENT_STAGE_COMPLETE,
    EVENT_STAGE_FAILED,
    EVENT_STAGE_START,
)
from ..pipeline_schemas import CorpusCuratorOutput
from ..streaming import invoke_agent_streaming

LOG = logging.getLogger("fraud_investigator.corpus_curator")

PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "corpus_curator.md"
STAGE = "agent_corpus_curator"

PROPOSALS_DIR = Path("data/extracted/proposals")


def run(
    *,
    since_date: str | None = None,
    leads_path: Path | None = None,
    run_dir: Path,
    event_sink: EventSink | None = None,
    timeout_sec: int = 1800,    # 30 min — agent does per-defendant NPPES + dedup (+ DOJ scrape in discovery mode)
    claude_bin: str = DEFAULT_CLAUDE_BIN,
) -> CorpusCuratorOutput:
    """Invoke the corpus_curator agent.

    Two mutually-exclusive modes (pass **exactly one** of ``leads_path`` /
    ``since_date``):

    * ``leads_path`` — **leads mode** (the gold_set-builder path). The press
      releases were already discovered + body-gated upstream and written to a
      JSONL of ``pipeline.leads.Lead`` rows; each lead's ``raw_text`` IS the
      press-release body, so the agent **skips discovery** (steps 1–2) and only
      extracts / NPPES-resolves / outputs. An **empty** leads file
      short-circuits to an empty result **without spawning the agent**.
    * ``since_date`` — **discovery mode** (the legacy manual workflow,
      unchanged). The agent self-discovers from the DOJ API on/after the date.

    ``event_sink`` is optional: if omitted, a sink is created at
    ``run_dir/events.jsonl`` so callers (e.g. the builder) don't have to make
    one.
    """
    if (leads_path is None) == (since_date is None):
        raise ValueError(
            "corpus_curator.run: pass exactly one of leads_path / since_date"
        )

    out_dir = run_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    PROPOSALS_DIR.mkdir(parents=True, exist_ok=True)
    if event_sink is None:
        event_sink = EventSink(out_dir / "events.jsonl")

    if not PROMPT_PATH.exists():
        raise FileNotFoundError(f"corpus_curator prompt not found: {PROMPT_PATH}")
    base_prompt = PROMPT_PATH.read_text()

    if leads_path is not None:
        leads_path = Path(leads_path)
        n_leads = _count_leads(leads_path)
        if n_leads == 0:
            # Idle run: never spawn the (expensive) agent for zero documents.
            LOG.info("corpus_curator: 0 leads at %s — empty run, agent not invoked", leads_path)
            return CorpusCuratorOutput(
                scraped_at=date.today().isoformat(),
                n_press_releases_reviewed=0,
                proposed_cases=[],
                skipped_already_in_corpus=0,
                skipped_corporate_only=0,
                skipped_other=0,
                summary=("No leads to process — empty run; no press releases "
                         "reviewed and no cases proposed."),
            )
        prompt = _leads_mode_prompt(base_prompt, leads_path, n_leads)
    else:
        prompt = _discovery_mode_prompt(base_prompt, since_date)

    event_sink.emit(STAGE, EVENT_STAGE_START,
                    {"since_date": since_date,
                     "leads_path": str(leads_path) if leads_path else None})
    try:
        result = invoke_agent_streaming(
            prompt=prompt,
            schema=CorpusCuratorOutput,
            out_dir=out_dir,
            step_name="corpus_curator",
            event_sink=event_sink,
            stage_name=STAGE,
            timeout_sec=timeout_sec,
            claude_bin=claude_bin,
            # WebSearch + WebFetch both ALLOWED with prompt-side budget cap.
        )
    except Exception as e:
        event_sink.emit(STAGE, EVENT_STAGE_FAILED, {"error": str(e)})
        raise

    # Persist proposed cases to the canonical proposals dir as JSONL
    today = date.today().isoformat()
    proposals_path = PROPOSALS_DIR / f"proposed_cases_{today}.jsonl"
    with proposals_path.open("w") as f:
        for case in result.proposed_cases:
            f.write(case.model_dump_json() + "\n")

    # Save the full agent output (summary + skipped counts) in the run dir
    (run_dir / "curator_summary.json").write_text(result.model_dump_json(indent=2))

    event_sink.emit(STAGE, EVENT_STAGE_COMPLETE, {
        "proposals_path": str(proposals_path),
        "n_proposed_cases": len(result.proposed_cases),
        "n_press_releases_reviewed": result.n_press_releases_reviewed,
        "skipped_already_in_corpus": result.skipped_already_in_corpus,
        "skipped_corporate_only": result.skipped_corporate_only,
    })
    LOG.info(f"Corpus curator complete: {len(result.proposed_cases)} proposed cases "
             f"(from {result.n_press_releases_reviewed} press releases) → {proposals_path}")
    return result


# ---------------------------------------------------------------------------
# prompt builders + leads helpers
# ---------------------------------------------------------------------------


def _count_leads(leads_path: Path) -> int:
    """Number of non-blank JSONL rows in the leads file (0 if missing/empty)."""
    if not leads_path.exists():
        return 0
    with leads_path.open() as f:
        return sum(1 for line in f if line.strip())


def _discovery_mode_prompt(base_prompt: str, since_date: str) -> str:
    """Legacy self-discovery prompt — the agent scrapes the DOJ API itself."""
    # WebSearch budget — curator needs search for discovering press release
    # URLs that didn't come from doj_press_releases.py, but should not
    # spend the entire turn budget on broad queries.
    MAX_WEBSEARCH_CALLS = 10
    return (
        base_prompt
        + f"\n\n## Scrape parameters\n\n"
        f"`since_date`: **{since_date}** (only press releases on/after this date)\n\n"
        f"Begin by enumerating press releases with "
        f'`python scripts/doj_pr_lookup.py search "health care fraud" '
        f"--date-after {since_date} --limit 500` (repeat for the other healthcare-fraud "
        f"query terms — medicare/medicaid fraud, kickback, opioid, False Claims Act — then "
        f"dedup by URL). It sorts newest-first and date-filters client-side; the API ignores "
        f"server-side date_>=.\n\n"
        f"## Billing-surface gate\n\n"
        f"After NPPES-resolving each defendant, run `python scripts/check_billing_surface.py "
        f"--npi <NPI> --years <comma-separated all_active_years>`. If the verdict is NO_SURFACE, "
        f"the case is valid DOJ truth but UNSCOREABLE by the Medicare-only model (commercial-plan "
        f"fraud, state-Medicaid-only fraud, or RN/biller/owner defendants) — keep it but set "
        f"confidence=low and say so in the rationale (see step 8 of the base prompt). Do not drop it.\n\n"
        f"## Search budget\n\n"
        f"You may make at most **{MAX_WEBSEARCH_CALLS} WebSearch calls** in this "
        f"invocation. WebFetch is unlimited for URLs you already have (e.g., "
        f"from the DOJ press-release index). Use WebSearch only for: "
        f"(a) NPPES disambiguation when multiple providers share a defendant name, "
        f"(b) corroborating defendant role / specialty when the press release is "
        f"ambiguous. If a search returns nothing useful in one attempt, DO NOT "
        f"retry with variants — accept the gap and move on; the human reviewer "
        f"can fill it during proposal review."
    )


def _leads_mode_prompt(base_prompt: str, leads_path: Path, n_leads: int) -> str:
    """Pre-discovered-leads prompt — skip discovery, extract from each ``raw_text``."""
    return (
        base_prompt
        + "\n\n## Mode: pre-discovered leads (DO NOT run discovery)\n\n"
        f"The press releases have already been discovered and body-gated for you. "
        f"There are **{n_leads} lead(s)** in `{leads_path}` — JSONL, one lead per "
        f"line. Read that file (your `Read` tool, or `cat {leads_path}`). Each lead "
        f"has: `lead_id`, `provenance`, `primary_source_url`, `title`, "
        f"`published_date`, and **`raw_text`** (the full press-release body, already "
        f"converted to plaintext).\n\n"
        "**Steps 1–2 of the base instructions (doj_pr_lookup / browser_fetch) DO "
        "NOT apply** — do not run any discovery or fetch. Each lead's `raw_text` IS "
        "the press-release body. For EACH lead, perform steps 3–9: extract "
        "defendant(s) → NPPES-match to NPI candidates → dedup vs the existing "
        "corpus → fraud period + outcome → fraud_types → billing-surface check → "
        "emit a proposed case. Set each proposal's `source_url` to that lead's "
        "`primary_source_url`.\n\n"
        "**The 'maximum 20 proposed cases' cap DOES NOT apply in this mode** — this "
        "is an automated batch, not a human-review batch. Emit one proposal per "
        "scoreable defendant; a single multi-defendant 'takedown' release yields "
        "multiple proposals.\n\n"
        "## Active years — the documented conduct window (NOT the billing pattern)\n\n"
        "`all_active_years` must be the fraud **conduct window the SOURCE documents** "
        "(e.g. 'between May 2013 and September 2018' → 2013..2018), NOT every year the "
        "provider happened to bill. **Never derive active_years from billing presence** "
        "— it over/under-counts (the Provider Kapok error: billing-derived [2013–2022] vs "
        "the settlement's true 2013–2019). If the lead's text does NOT state the period, "
        "**WebSearch for a corroborating source** (local news, court filing, qui tam "
        "complaint) that gives the date range and set the years from that. Only if no "
        "source gives a window, fall back to `[outcome year]` with confidence=low.\n\n"
        "Set **`active_years_source`** on each proposal to record how you got the window:\n"
        "  - `source_stated` — explicit in the lead's own text\n"
        "  - `web_corroborated` — found via WebSearch in a corroborating source\n"
        "  - `inferred` — inferred from indirect cues (e.g. a filing year); no explicit window\n"
        "  - `outcome_fallback` — no window found anywhere; used [outcome year]\n"
        "(The deterministic gate routes `inferred`/`outcome_fallback` to human review, so "
        "be honest — guessing the years is worse than admitting they're ungrounded.)\n\n"
        "## Billing-surface gate\n\n"
        "After resolving each defendant, run `python "
        "scripts/check_billing_surface.py --npi <NPI> --years <comma-separated "
        "all_active_years>`. NO_SURFACE → keep it but set confidence=low and say so "
        "in the rationale (valid DOJ truth but UNSCOREABLE by the Medicare-only "
        "model); do not drop it.\n\n"
        "## Search budget\n\n"
        "At most **10 WebSearch calls** this invocation (WebFetch unlimited for URLs "
        "you already have). Use WebSearch to disambiguate NPPES matches, corroborate "
        "role/specialty, and — critically — find the conduct window when the release "
        "omits it. One attempt per gap; if it returns nothing useful, move on."
    )
