"""Supporting agent — Auto Patcher (Tier-1 self-healing).

Reads a pipeline_engineer diagnosis and proposes config-only edits to fix
the failure. A validator filters proposals against an allowlist before any
edit is applied, then patches the file + commits with a distinctive prefix
so the auto-fix is easy to revert as a group.

Hard guardrails (also enforced in the prompt and again in code):
    1. File allowlist — only claude_p.py
    2. Dict allowlist — only TURN_BUDGET_BY_STEP
    3. Key allowlist — only the 8 main-floor agent role names
    4. Value bounds — TURN_BUDGET_BY_STEP[*] ∈ [5, 50]
    5. Magnitude bound — single proposal may not change by more than 2x
    6. Idempotency — one auto-patch per failed_run_id (marker file)

Invocation:
    from fraud_investigator.agents import auto_patcher
    auto_patcher.run(
        diagnosis_run_id="agent_pipeline_engineer_20260520T023912_839bbc",
        failed_run_id="run_20260519T212533_f2e34384",
        run_dir=Path("state/runs/agent_auto_patcher_<id>"),
        event_sink=EventSink(...),
    )
"""
from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import Any

from ..claude_p import DEFAULT_CLAUDE_BIN
from ..event_sink import (
    EventSink,
    EVENT_STAGE_COMPLETE,
    EVENT_STAGE_FAILED,
    EVENT_STAGE_LOG_LINE,
    EVENT_STAGE_START,
)
from ..pipeline_schemas import AutoPatcherOutput, AutoPatchProposal
from ..streaming import invoke_agent_streaming

LOG = logging.getLogger("fraud_investigator.auto_patcher")

PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "auto_patcher.md"
STAGE = "agent_auto_patcher"

REPO_ROOT = Path(__file__).resolve().parents[4]

# ─── Guardrail allowlists (Tier 1) ────────────────────────────────────────
ALLOWED_PATCHES: dict[str, dict[str, Any]] = {
    "fraud-investigator-v2/src/fraud_investigator/claude_p.py": {
        "TURN_BUDGET_BY_STEP": {
            "keys": {
                "investigator", "counsel_build", "counsel_rebuttal",
                "counsel_disposition", "defense", "legal_adv_response",
                "rebuttal_investigator", "case_writer",
            },
            "value_type": int,
            "min_value": 5,
            "max_value": 50,
        },
    },
}
MAGNITUDE_MULTIPLIER = 2.0  # single proposal may not change value by >2x


def _validate_proposal(p: AutoPatchProposal) -> tuple[bool, str]:
    """Return (ok, reason). ok=False ⇒ proposal rejected, reason filled."""
    allowed_dicts = ALLOWED_PATCHES.get(p.file_path)
    if allowed_dicts is None:
        return False, f"file_path {p.file_path!r} not on allowlist"

    dict_rules = allowed_dicts.get(p.dict_name)
    if dict_rules is None:
        return False, f"dict_name {p.dict_name!r} not on allowlist for {p.file_path}"

    if p.key not in dict_rules["keys"]:
        return False, f"key {p.key!r} not on allowlist for {p.dict_name}"

    vtype = dict_rules["value_type"]
    if not isinstance(p.new_value, vtype):
        return False, f"new_value must be {vtype.__name__}, got {type(p.new_value).__name__}"
    if not isinstance(p.old_value, vtype):
        return False, f"old_value must be {vtype.__name__}, got {type(p.old_value).__name__}"

    if p.new_value < dict_rules["min_value"] or p.new_value > dict_rules["max_value"]:
        return False, (f"new_value {p.new_value} outside bounds "
                       f"[{dict_rules['min_value']}, {dict_rules['max_value']}]")

    if p.old_value > 0:
        ratio = p.new_value / p.old_value
        if ratio > MAGNITUDE_MULTIPLIER or ratio < (1.0 / MAGNITUDE_MULTIPLIER):
            return False, (f"magnitude change {p.old_value}→{p.new_value} "
                           f"exceeds ±{MAGNITUDE_MULTIPLIER}x bound")

    return True, "ok"


def _apply_proposal(p: AutoPatchProposal) -> tuple[bool, str]:
    """Apply ONE validated proposal in-place via regex edit on the file.

    Returns (success, message). Uses a targeted regex so we never rewrite
    more than the single dict-key line. Verifies the post-edit file still
    parses (compiles) before returning success.
    """
    target = REPO_ROOT / p.file_path
    if not target.exists():
        return False, f"file does not exist: {target}"

    text = target.read_text()

    # Pattern: match the dict-entry line in TURN_BUDGET_BY_STEP for this key.
    # Matches:    "key": <int>,   (optionally with surrounding whitespace + trailing comment)
    pattern = re.compile(
        rf'(?P<lead>^\s*"{re.escape(p.key)}"\s*:\s*)'
        rf'(?P<old>{re.escape(str(p.old_value))})'
        rf'(?P<rest>[ \t,].*)?$',
        re.MULTILINE,
    )
    matches = list(pattern.finditer(text))
    if not matches:
        return False, (
            f"could not locate '\"{p.key}\": {p.old_value}' in {p.file_path}; "
            f"file may have changed since engineer diagnosed"
        )
    if len(matches) > 1:
        return False, (
            f"found {len(matches)} matches for '\"{p.key}\": {p.old_value}' — "
            f"refusing to patch ambiguously"
        )

    m = matches[0]
    new_line = f"{m.group('lead')}{p.new_value}{m.group('rest') or ''}"
    new_text = text[:m.start()] + new_line + text[m.end():]

    # Sanity check: file must still compile
    try:
        compile(new_text, str(target), "exec")
    except SyntaxError as e:
        return False, f"post-edit file fails compile: {e}"

    target.write_text(new_text)
    return True, f"patched {p.key}: {p.old_value} → {p.new_value}"


def _git_commit_patch(failed_run_id: str, applied: list[AutoPatchProposal]) -> tuple[bool, str]:
    """Stage + commit the patched files with a distinctive [auto-fix] prefix.

    Returns (ok, message). The commit is tagged distinctively so the user
    can revert all auto-fix commits as a group with:
        git log --grep=r"^\\[auto-fix\\]" --format="%H" | xargs git revert
    """
    if not applied:
        return False, "no proposals applied — nothing to commit"
    files = sorted({p.file_path for p in applied})
    try:
        subprocess.run(
            ["git", "add", *files],
            cwd=str(REPO_ROOT), check=True, capture_output=True, text=True,
        )
        body_lines = [
            "Auto-applied config patches in response to failed run",
            f"`{failed_run_id}`. All proposals validated against the Tier-1",
            "allowlist (file/dict/key/value-bound/magnitude).",
            "",
            "Patches applied:",
        ]
        for p in applied:
            body_lines.append(
                f"  - {p.file_path}:{p.dict_name}[{p.key}]"
                f" {p.old_value} → {p.new_value}"
            )
        body_lines.append("")
        body_lines.append("Rationale per proposal:")
        for p in applied:
            body_lines.append(f"  - {p.key}: {p.rationale}")
        body_lines.append("")
        body_lines.append(
            "To revert this auto-fix and any others, run:"
        )
        body_lines.append(
            "  git log --grep='^\\[auto-fix\\]' --format='%H' | xargs git revert"
        )

        title = f"[auto-fix] config patch for {failed_run_id}"
        msg = title + "\n\n" + "\n".join(body_lines)
        subprocess.run(
            ["git", "commit", "-m", msg],
            cwd=str(REPO_ROOT), check=True, capture_output=True, text=True,
        )
        return True, title
    except subprocess.CalledProcessError as e:
        return False, f"git failed: {e.stderr or e.stdout or e}"


def run(
    *,
    diagnosis_run_id: str,
    failed_run_id: str,
    run_dir: Path,
    event_sink: EventSink,
    timeout_sec: int = 300,   # 5 min — focused config edit, very small task
    claude_bin: str = DEFAULT_CLAUDE_BIN,
) -> AutoPatcherOutput:
    """Invoke the auto_patcher agent against an engineer's diagnosis."""
    out_dir = run_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    # Idempotency marker — orchestrator skips re-spawn if this exists
    (out_dir / "patched_run.txt").write_text(failed_run_id)

    if not PROMPT_PATH.exists():
        raise FileNotFoundError(f"auto_patcher prompt not found: {PROMPT_PATH}")
    base_prompt = PROMPT_PATH.read_text()
    diagnosis_path = (
        REPO_ROOT / "state" / "runs" / diagnosis_run_id
        / "pipeline_engineer.parsed.json"
    )
    prompt = (
        base_prompt
        + f"\n\n## Diagnosis to read\n\n"
        f"`diagnosis_path`: **{diagnosis_path}**\n"
        f"`failed_run_id`: **{failed_run_id}**\n\n"
        f"Begin by reading the diagnosis JSON. Then emit a structured "
        f"AutoPatcherOutput per the schema above. Do NOT use Edit/Write — "
        f"propose only; the orchestrator applies validated proposals."
    )

    event_sink.emit(STAGE, EVENT_STAGE_START, {
        "diagnosis_run_id": diagnosis_run_id,
        "failed_run_id": failed_run_id,
    })

    try:
        result = invoke_agent_streaming(
            prompt=prompt,
            schema=AutoPatcherOutput,
            out_dir=out_dir,
            step_name="auto_patcher",
            event_sink=event_sink,
            stage_name=STAGE,
            timeout_sec=timeout_sec,
            claude_bin=claude_bin,
            # Read-only proposal generation. NO Edit / NO Write / NO Bash —
            # we don't want the agent to alter ANYTHING; the validator
            # applies patches in Python after schema check.
            extra_args=["--disallowedTools", "Edit,Write,Bash,WebSearch,WebFetch"],
        )
    except Exception as e:
        event_sink.emit(STAGE, EVENT_STAGE_FAILED, {"error": str(e)})
        raise

    # ─── Validate every proposal ──────────────────────────────────────────
    validated: list[AutoPatchProposal] = []
    rejected: list[dict] = []
    for p in result.proposals:
        ok, reason = _validate_proposal(p)
        if ok:
            validated.append(p)
            event_sink.emit(STAGE, EVENT_STAGE_LOG_LINE, {
                "level": "info",
                "msg": f"proposal validated: {p.dict_name}[{p.key}] {p.old_value}→{p.new_value}",
            })
        else:
            rejected.append({
                "proposal": p.model_dump(),
                "reason": reason,
            })
            event_sink.emit(STAGE, EVENT_STAGE_LOG_LINE, {
                "level": "warn",
                "msg": f"proposal rejected ({reason}): {p.dict_name}[{p.key}] {p.old_value}→{p.new_value}",
            })

    # ─── Apply validated proposals + commit ────────────────────────────────
    applied: list[AutoPatchProposal] = []
    apply_errors: list[dict] = []
    for p in validated:
        ok, msg = _apply_proposal(p)
        if ok:
            applied.append(p)
            event_sink.emit(STAGE, EVENT_STAGE_LOG_LINE, {"level": "info", "msg": msg})
        else:
            apply_errors.append({"proposal": p.model_dump(), "error": msg})
            event_sink.emit(STAGE, EVENT_STAGE_LOG_LINE, {"level": "error", "msg": msg})

    commit_ok = False
    commit_msg = ""
    if applied:
        commit_ok, commit_msg = _git_commit_patch(failed_run_id, applied)
        event_sink.emit(STAGE, EVENT_STAGE_LOG_LINE, {
            "level": "info" if commit_ok else "error",
            "msg": f"git commit: {commit_msg}",
        })

    # Persist the report (validated/rejected/applied/commit) into the run dir
    import json as _json
    (run_dir / "patch_report.json").write_text(_json.dumps({
        "diagnosis_run_id": diagnosis_run_id,
        "failed_run_id": failed_run_id,
        "proposals_received": len(result.proposals),
        "proposals_validated": len(validated),
        "proposals_applied": len(applied),
        "proposals_rejected": rejected,
        "apply_errors": apply_errors,
        "git_commit_ok": commit_ok,
        "git_commit_msg": commit_msg,
        "summary": result.summary,
    }, indent=2))

    event_sink.emit(STAGE, EVENT_STAGE_COMPLETE, {
        "proposals_received": len(result.proposals),
        "proposals_applied": len(applied),
        "git_committed": commit_ok,
    })
    LOG.info(
        f"auto_patcher complete: received={len(result.proposals)}, "
        f"applied={len(applied)}, committed={commit_ok}"
    )
    return result
