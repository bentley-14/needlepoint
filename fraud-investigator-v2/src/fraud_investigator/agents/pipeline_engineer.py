"""Supporting agent — Pipeline Engineer.

Diagnoses failed pipeline runs. Read-only role: examines events.jsonl,
manifests, stage logs, and current system state to identify the most
likely cause + recommend remediation steps for the human to enact.

Invocation:
    from fraud_investigator.agents import pipeline_engineer
    pipeline_engineer.run(failed_run_id="run_2026...",
                          run_dir=Path("state/runs/agent_engineer_<id>"),
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
from ..pipeline_schemas import PipelineEngineerOutput
from ..streaming import invoke_agent_streaming

LOG = logging.getLogger("fraud_investigator.pipeline_engineer")

PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "pipeline_engineer.md"
STAGE = "agent_pipeline_engineer"


def run(
    *,
    failed_run_id: str,
    run_dir: Path,
    event_sink: EventSink,
    timeout_sec: int = 600,    # 10 min — focused diagnostic, not deep investigation
    claude_bin: str = DEFAULT_CLAUDE_BIN,
) -> PipelineEngineerOutput:
    """Invoke the pipeline_engineer agent against a specific failed run."""
    out_dir = run_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    # Marker file so the orchestrator's auto-spawn knows this failed_run_id
    # has already been diagnosed and doesn't double-fire on a retry.
    (out_dir / "diagnosed_run.txt").write_text(failed_run_id)

    if not PROMPT_PATH.exists():
        raise FileNotFoundError(f"pipeline_engineer prompt not found: {PROMPT_PATH}")
    base_prompt = PROMPT_PATH.read_text()
    # Inject the failed run id into the prompt context
    prompt = (
        base_prompt
        + f"\n\n## Failed run to diagnose\n\n"
        f"`failed_run_id`: **{failed_run_id}**\n\n"
        f"Begin by reading `state/runs/{failed_run_id}/manifest.json` and "
        f"`state/runs/{failed_run_id}/events.jsonl`.\n"
    )

    event_sink.emit(STAGE, EVENT_STAGE_START, {"diagnosing": failed_run_id})
    try:
        result = invoke_agent_streaming(
            prompt=prompt,
            schema=PipelineEngineerOutput,
            out_dir=out_dir,
            step_name="pipeline_engineer",
            event_sink=event_sink,
            stage_name=STAGE,
            timeout_sec=timeout_sec,
            claude_bin=claude_bin,
            # Strict read-only: NO Write/Edit, NO WebSearch
            extra_args=["--disallowedTools", "Write,Edit,WebSearch,WebFetch"],
        )
    except Exception as e:
        event_sink.emit(STAGE, EVENT_STAGE_FAILED, {"error": str(e)})
        raise

    # Write the diagnosis into the FAILED run's dir (alongside its events.jsonl)
    failed_run_dir = Path("state/runs") / failed_run_id
    if failed_run_dir.exists():
        diag_path = failed_run_dir / "diagnosis.md"
        diag_lines = [
            f"# Diagnosis — {failed_run_id}",
            "",
            f"**Failed stage:** {result.failure_stage}",
            f"**Can resume:** {result.can_resume}",
            "",
            "## Likely cause",
            "",
            result.likely_cause,
            "",
            "## Evidence",
            "",
        ]
        for ev in result.evidence:
            diag_lines.append(f"- {ev}")
        diag_lines.extend(["", "## Remediation steps", ""])
        for i, step in enumerate(result.remediation_steps, 1):
            diag_lines.append(f"{i}. {step}")
        diag_lines.extend(["", "## Summary", "", result.summary, ""])
        diag_path.write_text("\n".join(diag_lines))

    (run_dir / "diagnosis_summary.json").write_text(result.model_dump_json(indent=2))

    event_sink.emit(STAGE, EVENT_STAGE_COMPLETE, {
        "diagnosed_run_id": failed_run_id,
        "failure_stage": result.failure_stage,
        "can_resume": result.can_resume,
        "n_remediation_steps": len(result.remediation_steps),
    })
    LOG.info(f"Pipeline diagnosis complete: {result.likely_cause[:100]}")

    # ─── Chain: spawn auto_patcher to act on this diagnosis ──────────────
    # Detached subprocess so we don't block this engineer run. The patcher
    # reads pipeline_engineer.parsed.json and proposes config-only edits
    # against an allowlist. Idempotent via a marker file inside the patcher's
    # own run dir.
    _spawn_auto_patcher_chain(
        diagnosis_run_id=run_dir.name,
        failed_run_id=failed_run_id,
    )

    return result


def _spawn_auto_patcher_chain(*, diagnosis_run_id: str, failed_run_id: str) -> None:
    """Detached spawn of the auto_patcher in response to a fresh diagnosis.

    Idempotent: scans state/runs/agent_auto_patcher_* for a patched_run.txt
    marker matching this failed_run_id, skips if found.
    """
    import subprocess as _sp
    import sys as _sys
    from pathlib import Path as _Path
    repo_root = _Path(__file__).resolve().parents[4]
    state_runs = repo_root / "state" / "runs"

    # Idempotency check
    if state_runs.exists():
        for d in state_runs.iterdir():
            if not d.is_dir() or not d.name.startswith("agent_auto_patcher_"):
                continue
            marker = d / "patched_run.txt"
            if marker.exists() and marker.read_text().strip() == failed_run_id:
                LOG.info(
                    f"auto_patcher already attempted on {failed_run_id!r} "
                    f"(see {d.name}); skipping chain spawn."
                )
                return

    cmd = [
        _sys.executable, str(repo_root / "scripts" / "run_support_agent.py"),
        "--agent", "auto_patcher",
        "--failed-run-id", failed_run_id,
        "--diagnosis-run-id", diagnosis_run_id,
    ]
    log_path = state_runs / f"_agent_launcher_auto_patcher_chain_{failed_run_id}.log"
    log_fh = log_path.open("a")
    try:
        proc = _sp.Popen(
            cmd, cwd=str(repo_root),
            stdout=log_fh, stderr=_sp.STDOUT, stdin=_sp.DEVNULL,
            start_new_session=True,
        )
        LOG.info(
            f"=== Diagnosis complete → chained auto_patcher "
            f"(pid={proc.pid}) for run {failed_run_id} ===\n"
            f"    Patcher log: {log_path}"
        )
    finally:
        log_fh.close()
