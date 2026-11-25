"""Supporting agent — Data Repair (Tier-1 corpus self-healing).

Sibling of auto_patcher, but operates on JSONL rows in the v9 ground-truth
corpus instead of dict literals in Python code. Reads a data_auditor
report, proposes field-level corrections, validates each against a strict
allowlist + canonical vocabularies, applies validated proposals in place
(preserving originals in `_original_<field>` keys), and commits with a
`[data-fix]` prefix so the human can revert as a group.

Hard guardrails (also enforced in the prompt and again in code):
    1. Target file allowlist — only the canonical v9 JSONL
    2. Field allowlist — only outcome.type / outcome.amount_usd /
       fraud_types / all_active_years
    3. Value bounds:
        - outcome.type ∈ canonical set
        - outcome.amount_usd ∈ [1_000, 10_000_000_000]
        - fraud_types ⊆ canonical vocab, ≥1 entry
        - all_active_years TRIM-ONLY (may only remove years, never add),
          post-fix span ≤10 years
    4. Source evidence required (≥40 chars)
    5. Per-(case_id, field_path) idempotency via `_original_<field>` keys
       — a row that already has _original_outcome_type set is skipped

Invocation:
    from fraud_investigator.agents import data_repair
    data_repair.run(
        audit_run_id="agent_data_auditor_20260520T023235_144cb8",
        run_dir=Path("state/runs/agent_data_repair_<id>"),
        event_sink=EventSink(...),
    )
"""
from __future__ import annotations

import json
import logging
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
from ..pipeline_schemas import DataRepairOutput, DataRepairProposal
from ..streaming import invoke_agent_streaming

LOG = logging.getLogger("fraud_investigator.data_repair")

PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "data_repair.md"
STAGE = "agent_data_repair"

REPO_ROOT = Path(__file__).resolve().parents[4]
CORPUS_PATH = REPO_ROOT / "data" / "extracted" / "gold_set.jsonl"

# ─── Allowlists ────────────────────────────────────────────────────────────

ALLOWED_FIELDS = {
    "outcome.type",
    "outcome.amount_usd",
    "fraud_types",
    "all_active_years",
}

OUTCOME_TYPE_VOCAB = {
    "indictment", "settlement", "plea", "conviction", "judgment",
    "dismissed", "deferred_prosecution_agreement",
}

# Canonical fraud_types = the keys archetypes.py::_FRAUD_TYPE_TO_SCHEMES maps
# to scheme heads. A term outside this set silently falls back to
# PHANTOM_BILLING in scheme stamping, so the validator must not admit one.
# ("kickbacks" is a legacy typo in old rows — repairs propose "kickback".)
FRAUD_TYPES_VOCAB = {
    "phantom_billing", "medically_unnecessary", "kickback",
    "stark_violation", "compounding_fraud", "opioid_diversion",
    "controlled_substance", "addiction_treatment_fraud", "drug_diversion",
    "hha_poc_falsification", "hospice_eligibility", "dme_scheme",
    "upcoding", "unbundling", "npi_theft",
    "telemedicine_fraud", "genetic_testing_fraud", "lab_requisition_fraud",
    "falsification_of_records", "other",
}

AMOUNT_MIN = 1_000
AMOUNT_MAX = 10_000_000_000
MAX_ACTIVE_YEAR_SPAN = 10


# ─── Validation ────────────────────────────────────────────────────────────

def _validate_proposal(p: DataRepairProposal, corpus_row: dict | None) -> tuple[bool, str]:
    """Return (ok, reason). Validates a single proposal against the allowlist
    + vocab + bounds. `corpus_row` is the actual row keyed by case_id, used
    for old-value match check and the `_original_*` idempotency guard.
    """
    if p.field_path not in ALLOWED_FIELDS:
        return False, f"field_path {p.field_path!r} not on allowlist"

    if not p.source_evidence or len(p.source_evidence) < 40:
        return False, "source_evidence missing or <40 chars"

    if corpus_row is None:
        return False, f"case_id {p.case_id!r} not found in corpus"

    # Idempotency: skip if this field already has a preserved original
    orig_key = "_original_" + p.field_path.replace(".", "_")
    if orig_key in corpus_row:
        return False, f"row already has {orig_key} — skipping (already repaired once)"

    # Old-value match check — the agent must observe current state correctly
    current = _get_field(corpus_row, p.field_path)
    if current != p.old_value:
        return False, f"old_value mismatch: corpus has {current!r}, proposal says {p.old_value!r}"

    # Per-field value validation
    if p.field_path == "outcome.type":
        if not isinstance(p.new_value, str):
            return False, "outcome.type new_value must be string"
        if p.new_value not in OUTCOME_TYPE_VOCAB:
            return False, f"outcome.type {p.new_value!r} not in canonical vocab"

    elif p.field_path == "outcome.amount_usd":
        if not isinstance(p.new_value, (int, float)):
            return False, "outcome.amount_usd new_value must be numeric"
        if p.new_value < AMOUNT_MIN or p.new_value > AMOUNT_MAX:
            return False, f"outcome.amount_usd {p.new_value} outside bounds [{AMOUNT_MIN}, {AMOUNT_MAX}]"

    elif p.field_path == "fraud_types":
        if not isinstance(p.new_value, list) or not p.new_value:
            return False, "fraud_types must be non-empty list"
        for v in p.new_value:
            if v not in FRAUD_TYPES_VOCAB:
                return False, f"fraud_types entry {v!r} not in canonical vocab"

    elif p.field_path == "all_active_years":
        if not isinstance(p.new_value, list) or not p.new_value:
            return False, "all_active_years must be non-empty list"
        if not all(isinstance(y, int) for y in p.new_value):
            return False, "all_active_years entries must all be int"
        # Trim-only check: new years must be a subset of old years
        old_set = set(p.old_value or [])
        new_set = set(p.new_value)
        if not new_set.issubset(old_set):
            return False, "all_active_years TRIM-ONLY: new_value adds years not in old_value"
        span = max(p.new_value) - min(p.new_value) + 1
        if span > MAX_ACTIVE_YEAR_SPAN:
            return False, f"all_active_years post-fix span {span} > {MAX_ACTIVE_YEAR_SPAN}"

    return True, "ok"


# ─── Helpers ───────────────────────────────────────────────────────────────

def _get_field(row: dict, path: str) -> Any:
    """Get a value via dot-path (e.g. 'outcome.type')."""
    cur: Any = row
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _set_field(row: dict, path: str, value: Any) -> None:
    """Set a value via dot-path. Creates intermediate dicts if needed."""
    parts = path.split(".")
    cur = row
    for part in parts[:-1]:
        if part not in cur or not isinstance(cur[part], dict):
            cur[part] = {}
        cur = cur[part]
    cur[parts[-1]] = value


def _index_corpus_by_case_id() -> dict[str, dict]:
    """One-time pass: build a {case_id → row_index} map for fast lookup."""
    out: dict[str, dict] = {}
    with CORPUS_PATH.open() as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            cid = row.get("case_id")
            if cid:
                out[cid] = {"line_index": i, "row": row}
    return out


# ─── Apply ─────────────────────────────────────────────────────────────────

def _apply_proposals(validated: list[DataRepairProposal]) -> tuple[list[DataRepairProposal], list[dict]]:
    """Apply validated proposals to the corpus JSONL in a single rewrite.

    Returns (applied_proposals, errors). Each applied row gets:
      - new value at field_path
      - original preserved at _original_<field_path-with-underscores>
      - _last_repaired_at timestamp + _last_repair_audit_run_id markers

    Uses an atomic write (tmp file + rename) so a crash mid-write doesn't
    corrupt the corpus.
    """
    from datetime import datetime, timezone

    applied: list[DataRepairProposal] = []
    errors: list[dict] = []

    # Build proposals_by_case for one-pass apply
    by_case: dict[str, list[DataRepairProposal]] = {}
    for p in validated:
        by_case.setdefault(p.case_id, []).append(p)

    tmp_path = CORPUS_PATH.with_suffix(".jsonl.tmp")
    now_iso = datetime.now(timezone.utc).isoformat()
    with CORPUS_PATH.open() as src, tmp_path.open("w") as dst:
        for line in src:
            line_stripped = line.strip()
            if not line_stripped:
                dst.write(line)
                continue
            try:
                row = json.loads(line_stripped)
            except json.JSONDecodeError:
                dst.write(line)
                continue
            cid = row.get("case_id")
            if cid not in by_case:
                dst.write(line)
                continue
            # Apply each proposal for this case
            row_changed = False
            for p in by_case[cid]:
                orig_key = "_original_" + p.field_path.replace(".", "_")
                if orig_key in row:
                    errors.append({
                        "case_id": cid, "field_path": p.field_path,
                        "error": f"{orig_key} already exists — re-apply blocked",
                    })
                    continue
                current = _get_field(row, p.field_path)
                if current != p.old_value:
                    errors.append({
                        "case_id": cid, "field_path": p.field_path,
                        "error": f"old_value mismatch at apply time: {current!r} vs {p.old_value!r}",
                    })
                    continue
                row[orig_key] = current
                _set_field(row, p.field_path, p.new_value)
                row_changed = True
                applied.append(p)
            if row_changed:
                row["_last_repaired_at"] = now_iso
            dst.write(json.dumps(row) + "\n")

    # Atomic replace
    tmp_path.replace(CORPUS_PATH)
    return applied, errors


def _git_commit(audit_run_id: str, applied: list[DataRepairProposal]) -> tuple[bool, str]:
    """Commit the corpus change with a distinctive [data-fix] prefix."""
    if not applied:
        return False, "no proposals applied — nothing to commit"
    rel_corpus = str(CORPUS_PATH.relative_to(REPO_ROOT))
    try:
        subprocess.run(
            ["git", "add", rel_corpus],
            cwd=str(REPO_ROOT), check=True, capture_output=True, text=True,
        )
        # Group proposals by case_id for a tidy commit body
        by_case: dict[str, list[DataRepairProposal]] = {}
        for p in applied:
            by_case.setdefault(p.case_id, []).append(p)

        body_lines = [
            f"Auto-applied corpus corrections in response to audit",
            f"`{audit_run_id}`. All proposals validated against the Tier-1",
            f"allowlist (field-path / vocab / bounds / trim-only / source-",
            f"evidence-required / idempotency).",
            "",
            f"Cases corrected: {len(by_case)}",
            f"Total proposals applied: {len(applied)}",
            "",
            "Patches applied:",
        ]
        for cid, props in by_case.items():
            body_lines.append(f"  - {cid}:")
            for p in props:
                body_lines.append(f"      {p.field_path}: {_short_repr(p.old_value)} → {_short_repr(p.new_value)}")
        body_lines.extend([
            "",
            "Each row preserves its original values under _original_<field>",
            "keys. To revert this and any other auto-corpus-fix commits:",
            "  git log --grep='^\\[data-fix\\]' --format='%H' | xargs git revert",
        ])

        title = f"[data-fix] corpus correction for audit {audit_run_id}"
        msg = title + "\n\n" + "\n".join(body_lines)
        subprocess.run(
            ["git", "commit", "-m", msg],
            cwd=str(REPO_ROOT), check=True, capture_output=True, text=True,
        )
        return True, title
    except subprocess.CalledProcessError as e:
        return False, f"git failed: {e.stderr or e.stdout or e}"


def _short_repr(v: Any) -> str:
    """Compact repr for commit message readability."""
    if isinstance(v, list) and len(v) > 6:
        return f"[{v[0]}…{v[-1]}, n={len(v)}]"
    return repr(v)[:80]


# ─── Entry ─────────────────────────────────────────────────────────────────

def run(
    *,
    audit_run_id: str,
    run_dir: Path,
    event_sink: EventSink,
    timeout_sec: int = 1800,    # 30 min — bounded by 25 turns + 10 WebSearches
    claude_bin: str = DEFAULT_CLAUDE_BIN,
) -> DataRepairOutput:
    """Invoke the data_repair agent against a specific data_auditor run."""
    out_dir = run_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "repaired_audit.txt").write_text(audit_run_id)

    if not PROMPT_PATH.exists():
        raise FileNotFoundError(f"data_repair prompt not found: {PROMPT_PATH}")

    audit_report_path = (
        REPO_ROOT / "state" / "runs" / audit_run_id / "data_auditor.parsed.json"
    )
    if not audit_report_path.exists():
        raise FileNotFoundError(f"audit report not found: {audit_report_path}")

    base_prompt = PROMPT_PATH.read_text()
    prompt = (
        base_prompt
        + f"\n\n## Audit report to read\n\n"
        f"`audit_report_path`: **{audit_report_path}**\n"
        f"`audit_run_id`: **{audit_run_id}**\n"
        f"`corpus_path`: **{CORPUS_PATH}**\n\n"
        f"Begin by reading the audit report. Focus on the easiest, "
        f"highest-confidence repairs first — span_too_large cases with "
        f"clear source URLs, outcome.type mismatches with explicit "
        f"settlement language, etc. SKIP anything ambiguous."
    )

    event_sink.emit(STAGE, EVENT_STAGE_START, {"audit_run_id": audit_run_id})
    try:
        result = invoke_agent_streaming(
            prompt=prompt,
            schema=DataRepairOutput,
            out_dir=out_dir,
            step_name="data_repair",
            event_sink=event_sink,
            stage_name=STAGE,
            timeout_sec=timeout_sec,
            claude_bin=claude_bin,
            # Read corpus + audit, allow Bash for grep / wc / head, allow
            # WebSearch + WebFetch within the prompt-budget. NO Edit / NO
            # Write — the validator/applier in pure Python performs all
            # corpus mutations.
            extra_args=["--disallowedTools", "Edit,Write"],
        )
    except Exception as e:
        event_sink.emit(STAGE, EVENT_STAGE_FAILED, {"error": str(e)})
        raise

    # ─── Validate every proposal against the corpus ───────────────────────
    corpus_index = _index_corpus_by_case_id()
    validated: list[DataRepairProposal] = []
    rejected: list[dict] = []
    for p in result.proposals:
        entry = corpus_index.get(p.case_id)
        row = entry["row"] if entry else None
        ok, reason = _validate_proposal(p, row)
        if ok:
            validated.append(p)
            event_sink.emit(STAGE, EVENT_STAGE_LOG_LINE, {
                "level": "info",
                "msg": f"proposal validated: {p.case_id}/{p.field_path}",
            })
        else:
            rejected.append({"proposal": p.model_dump(), "reason": reason})
            event_sink.emit(STAGE, EVENT_STAGE_LOG_LINE, {
                "level": "warn",
                "msg": f"proposal rejected ({reason}): {p.case_id}/{p.field_path}",
            })

    # ─── Apply + commit ────────────────────────────────────────────────────
    applied: list[DataRepairProposal]
    apply_errors: list[dict]
    if validated:
        applied, apply_errors = _apply_proposals(validated)
    else:
        applied, apply_errors = [], []
        event_sink.emit(STAGE, EVENT_STAGE_LOG_LINE, {
            "level": "info",
            "msg": "no validated proposals to apply",
        })

    commit_ok = False
    commit_msg = ""
    if applied:
        commit_ok, commit_msg = _git_commit(audit_run_id, applied)
        event_sink.emit(STAGE, EVENT_STAGE_LOG_LINE, {
            "level": "info" if commit_ok else "error",
            "msg": f"git commit: {commit_msg}",
        })

    # ─── Persist the patch report ─────────────────────────────────────────
    (run_dir / "patch_report.json").write_text(json.dumps({
        "audit_run_id": audit_run_id,
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
        f"data_repair complete: received={len(result.proposals)}, "
        f"applied={len(applied)}, committed={commit_ok}"
    )
    return result
