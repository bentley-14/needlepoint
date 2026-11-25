"""Supporting agent — Data Auditor.

Audits the v9 corpus for data-quality issues (Henghold/Ledger-style
errors where active_years, outcome.type, or outcome.amount_usd are wrong).

Invocation:
    from fraud_investigator.agents import data_auditor
    data_auditor.run(run_dir=Path("state/runs/agent_auditor_001"),
                     event_sink=EventSink(...))
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
from ..pipeline_schemas import DataAuditorOutput
from ..streaming import invoke_agent_streaming

LOG = logging.getLogger("fraud_investigator.data_auditor")

PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "data_auditor.md"
STAGE = "agent_data_auditor"

# Where audit reports land (relative to repo root)
AUDITS_DIR = Path("data/extracted/audits")


def run(
    *,
    run_dir: Path,
    event_sink: EventSink,
    timeout_sec: int = 1200,   # 20 min — agent loads ~1.3K row corpus + runs 8 rule checks
    claude_bin: str = DEFAULT_CLAUDE_BIN,
) -> DataAuditorOutput:
    """Invoke the data_auditor agent."""
    out_dir = run_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    AUDITS_DIR.mkdir(parents=True, exist_ok=True)

    if not PROMPT_PATH.exists():
        raise FileNotFoundError(f"data_auditor prompt not found: {PROMPT_PATH}")
    prompt = PROMPT_PATH.read_text()

    event_sink.emit(STAGE, EVENT_STAGE_START, {})
    try:
        result = invoke_agent_streaming(
            prompt=prompt,
            schema=DataAuditorOutput,
            out_dir=out_dir,
            step_name="data_auditor",
            event_sink=event_sink,
            stage_name=STAGE,
            timeout_sec=timeout_sec,
            claude_bin=claude_bin,
            extra_args=["--disallowedTools", "WebSearch,WebFetch"],
        )
    except Exception as e:
        event_sink.emit(STAGE, EVENT_STAGE_FAILED, {"error": str(e)})
        raise

    # Write the canonical audit report at data/extracted/audits/audit_YYYY-MM-DD.json
    today = date.today().isoformat()
    canonical_path = AUDITS_DIR / f"audit_{today}.json"
    canonical_path.write_text(result.model_dump_json(indent=2))

    # Also save in the run dir for traceability
    (run_dir / "audit_summary.json").write_text(result.model_dump_json(indent=2))

    event_sink.emit(STAGE, EVENT_STAGE_COMPLETE, {
        "audit_path": str(canonical_path),
        "n_rows_audited": result.n_rows_audited,
        "n_flagged": len(result.flagged),
        "pass_count": result.pass_count,
        "fail_count": result.fail_count,
    })
    LOG.info(f"Data audit complete: {result.fail_count} flagged of {result.n_rows_audited} "
             f"rows → {canonical_path}")
    return result
