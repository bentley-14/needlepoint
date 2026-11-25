"""Stage 1 — Rule Derivation Agent driver.

Invokes a claude -p subprocess that designs the agent filter (which rows
the ML training panel drops), replacing the hand-tuned floor_0.50_* and
the algorithmic rederive scripts.

Output is written to ``state/runs/{run_id}/stage1_rederive.json`` and is
consumed by walk_forward_v4.py via the ``--filter-from`` flag.
"""
from __future__ import annotations

import logging
from pathlib import Path

from ..claude_p import DEFAULT_CLAUDE_BIN, DEFAULT_TIMEOUT_SEC
from ..event_sink import (
    EventSink,
    STAGE_RULE_DERIVATION,
    EVENT_STAGE_COMPLETE,
    EVENT_STAGE_FAILED,
    EVENT_STAGE_START,
)
from ..pipeline_schemas import RuleDerivationOutput
from ..streaming import invoke_agent_streaming

LOG = logging.getLogger("fraud_investigator.rule_derivation")

PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "rule_derivation.md"


def run(
    *,
    run_dir: Path,
    event_sink: EventSink,
    timeout_sec: int = 1800,   # 30 min; agent does multi-step analysis
    claude_bin: str = DEFAULT_CLAUDE_BIN,
) -> RuleDerivationOutput:
    """Invoke the Stage 1 rule derivation agent.

    Parameters
    ----------
    run_dir
        Per-run state directory (``state/runs/{run_id}/``). The agent's
        output lands at ``run_dir / "stage1_rederive.json"`` (the format
        expected by walk_forward_v4.py --filter-from).
    event_sink
        Where to emit STAGE_RULE_DERIVATION events as the agent runs.
    timeout_sec
        Max wall-clock for the agent (default 30 min).

    Returns
    -------
    Validated RuleDerivationOutput.
    """
    out_dir = run_dir / "stage1"
    out_dir.mkdir(parents=True, exist_ok=True)

    if not PROMPT_PATH.exists():
        raise FileNotFoundError(f"rule_derivation prompt not found: {PROMPT_PATH}")
    prompt = PROMPT_PATH.read_text()

    event_sink.emit(STAGE_RULE_DERIVATION, EVENT_STAGE_START, {})
    try:
        result = invoke_agent_streaming(
            prompt=prompt,
            schema=RuleDerivationOutput,
            out_dir=out_dir,
            step_name="rule_derivation",
            event_sink=event_sink,
            stage_name=STAGE_RULE_DERIVATION,
            timeout_sec=timeout_sec,
            claude_bin=claude_bin,
            # The agent doesn't need WebSearch — it's purely internal data analysis
            extra_args=["--disallowedTools", "WebSearch,WebFetch"],
        )
    except Exception as e:
        event_sink.emit(STAGE_RULE_DERIVATION, EVENT_STAGE_FAILED, {"error": str(e)})
        raise

    # Persist the canonical output file (expected by walk_forward --filter-from)
    canonical_path = run_dir / "stage1_rederive.json"
    canonical_path.write_text(result.model_dump_json(indent=2))

    event_sink.emit(
        STAGE_RULE_DERIVATION, EVENT_STAGE_COMPLETE,
        {
            "filter_path": str(canonical_path),
            "n_rules": len(result.rules),
            "drop_pct": result.evaluation.drop_pct,
            "pos_loss_pct": result.evaluation.pos_loss_pct,
            "n_pos_preserved": result.evaluation.n_pos_preserved,
        },
    )
    LOG.info(f"Stage 1 complete: {len(result.rules)} rules, "
             f"drop={result.evaluation.drop_pct}%, "
             f"pos_loss={result.evaluation.pos_loss_pct}% → {canonical_path}")
    return result
