"""Supporting agent — Feature Engineer.

Given a fraud-scheme description, proposes 5-15 new features + writes a
skeleton compute script the human can refine + promote to scripts/.

Invocation:
    from fraud_investigator.agents import feature_engineer
    feature_engineer.run(
        scheme_description="P-Stim upcoding: providers bill 63650 ...",
        scheme_slug="pstim_upcoding",
        run_dir=Path("state/runs/agent_feateng_<id>"),
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
from ..pipeline_schemas import FeatureEngineerOutput
from ..streaming import invoke_agent_streaming

LOG = logging.getLogger("fraud_investigator.feature_engineer")

PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "feature_engineer.md"
STAGE = "agent_feature_engineer"

# Where proposals land
PROPOSALS_ROOT = Path("scripts/proposals")


def run(
    *,
    scheme_description: str,
    scheme_slug: str,
    run_dir: Path,
    event_sink: EventSink,
    timeout_sec: int = 1500,    # 25 min — agent reads catalogue + runs validation queries
    claude_bin: str = DEFAULT_CLAUDE_BIN,
) -> FeatureEngineerOutput:
    """Invoke the feature_engineer agent for one scheme description."""
    out_dir = run_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    # Sandboxed dir for this proposal (the agent writes its skeleton script here)
    today = date.today().isoformat()
    proposal_dir = PROPOSALS_ROOT / f"{scheme_slug}_{today}"
    proposal_dir.mkdir(parents=True, exist_ok=True)

    if not PROMPT_PATH.exists():
        raise FileNotFoundError(f"feature_engineer prompt not found: {PROMPT_PATH}")
    base_prompt = PROMPT_PATH.read_text()
    prompt = (
        base_prompt
        + f"\n\n## Scheme to engineer features for\n\n"
        f"**Slug:** `{scheme_slug}`\n\n"
        f"**Description:**\n\n{scheme_description}\n\n"
        f"**Write the skeleton compute script to:**\n"
        f"`{proposal_dir}/compute_{scheme_slug}_features.py`\n\n"
        f"**Write the proposal markdown to:**\n"
        f"`{proposal_dir}/proposal.md`\n"
    )

    event_sink.emit(STAGE, EVENT_STAGE_START, {
        "scheme_slug": scheme_slug,
        "proposal_dir": str(proposal_dir),
    })
    try:
        result = invoke_agent_streaming(
            prompt=prompt,
            schema=FeatureEngineerOutput,
            out_dir=out_dir,
            step_name="feature_engineer",
            event_sink=event_sink,
            stage_name=STAGE,
            timeout_sec=timeout_sec,
            claude_bin=claude_bin,
            # Agent CAN write — but only into the scripts/proposals/ sandbox
            # (the prompt enforces this; we use --disallowedTools to block external lookups)
            extra_args=["--disallowedTools", "WebSearch,WebFetch"],
        )
    except Exception as e:
        event_sink.emit(STAGE, EVENT_STAGE_FAILED, {"error": str(e)})
        raise

    # Persist the structured output alongside the agent's free-form proposal.md
    (run_dir / "feature_proposal_summary.json").write_text(result.model_dump_json(indent=2))
    (proposal_dir / "summary.json").write_text(result.model_dump_json(indent=2))

    event_sink.emit(STAGE, EVENT_STAGE_COMPLETE, {
        "target_scheme": result.target_scheme,
        "n_proposed_features": len(result.proposed_features),
        "proposal_dir": str(proposal_dir),
        "skeleton_script_path": result.skeleton_script_path,
    })
    LOG.info(f"Feature engineer complete: {len(result.proposed_features)} features for "
             f"{scheme_slug} → {proposal_dir}")
    return result
