"""Supporting agent — Data Intake.

Checks for new CMS PUFs, DOJ press releases, NPPES bulk refreshes.
Fetches what's available within size limits; recommends Corpus Curator
runs for fresh DOJ scrape work.

Invocation:
    from fraud_investigator.agents import data_intake
    data_intake.run(run_dir=Path("state/runs/agent_intake_001"),
                    event_sink=EventSink(...))
"""
from __future__ import annotations

import logging
from pathlib import Path

from ..claude_p import DEFAULT_CLAUDE_BIN
from ..event_sink import (
    EventSink,
    EVENT_STAGE_COMPLETE,
    EVENT_STAGE_FAILED,
    EVENT_STAGE_START,
)
from ..pipeline_schemas import DataIntakeOutput
from ..streaming import invoke_agent_streaming

LOG = logging.getLogger("fraud_investigator.data_intake")

PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "data_intake.md"
STAGE = "agent_data_intake"


def run(
    *,
    run_dir: Path,
    event_sink: EventSink,
    timeout_sec: int = 900,
    claude_bin: str = DEFAULT_CLAUDE_BIN,
) -> DataIntakeOutput:
    """Invoke the data_intake agent."""
    out_dir = run_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    if not PROMPT_PATH.exists():
        raise FileNotFoundError(f"data_intake prompt not found: {PROMPT_PATH}")
    # Inject a search-budget cap so the agent doesn't burn turns flailing
    # on broad searches. WebSearch is allowed (needed to discover new DOJ
    # press releases / CMS PUF release pages) but the agent is told to
    # cap at MAX_WEBSEARCH_CALLS so a slow or no-result query can't
    # consume the whole turn budget.
    MAX_WEBSEARCH_CALLS = 6
    prompt = (
        PROMPT_PATH.read_text()
        + f"\n\n## Search budget\n\n"
        f"You may make at most **{MAX_WEBSEARCH_CALLS} WebSearch calls** in "
        f"this invocation. Prefer WebFetch for URLs you already know "
        f"(CMS catalog, DOJ press-release index pages). Use WebSearch only "
        f"for discovery (e.g., 'CMS Part B 2024 release date', 'DOJ Florida "
        f"medicare fraud indictment 2026'). If a search returns nothing useful, "
        f"DO NOT retry the same query with a variant — move on to the next "
        f"discovery target or emit the structured output with what you have."
    )

    event_sink.emit(STAGE, EVENT_STAGE_START, {})
    try:
        result = invoke_agent_streaming(
            prompt=prompt,
            schema=DataIntakeOutput,
            out_dir=out_dir,
            step_name="data_intake",
            event_sink=event_sink,
            stage_name=STAGE,
            timeout_sec=timeout_sec,
            claude_bin=claude_bin,
            # WebSearch ALLOWED with prompt-side budget cap. WebFetch always
            # allowed for known URLs. No filesystem write outside out_dir.
        )
    except Exception as e:
        event_sink.emit(STAGE, EVENT_STAGE_FAILED, {"error": str(e)})
        raise

    (run_dir / "summary.json").write_text(result.model_dump_json(indent=2))

    event_sink.emit(STAGE, EVENT_STAGE_COMPLETE, {
        "checked_at": result.checked_at,
        "summary": result.summary[:200],
    })
    LOG.info(f"Data intake complete: {result.summary[:100]}")
    return result
