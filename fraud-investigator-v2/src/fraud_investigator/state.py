"""Per-case state directory + transcript writers.

Layout under ``state/{case_id}/``:

    summary.json                   ← top-level CaseState (Pydantic-serialized)
    transcript.json                ← canonical full transcript (every call concatenated)
    iter-01/
      investigator.prompt.md       ← prompt sent
      investigator.raw.json        ← raw claude -p wrapper output
      investigator.parsed.json     ← validated Pydantic model dump
      investigator.stderr.log
      investigator.meta.json       ← per-attempt timings + errors
      counsel_build.{prompt.md, raw.json, parsed.json, stderr.log, meta.json}
      counsel_rebuttal.{...}
      counsel_disposition.{...}
    iter-02/
      ...
    adv-r1/
      defense.{...}
      legal_adv.{...}
      rebuttal_investigator.{...}    (optional)
    case-writer.{...}
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Iterable

from .checkpoint import assert_checkpoint_invariant
from .schemas import CaseState

STATE_ROOT_DEFAULT = Path(__file__).resolve().parents[2] / "state"


def case_dir(case_id: str, root: Path = STATE_ROOT_DEFAULT) -> Path:
    d = root / case_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def iter_dir(case_id: str, iteration: int, root: Path = STATE_ROOT_DEFAULT) -> Path:
    d = case_dir(case_id, root) / f"iter-{iteration:02d}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def adv_dir(case_id: str, round_num: int, root: Path = STATE_ROOT_DEFAULT) -> Path:
    d = case_dir(case_id, root) / f"adv-r{round_num}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_summary(state: CaseState, root: Path = STATE_ROOT_DEFAULT) -> Path:
    """Persist the top-level case state. Atomic via tmp+rename.

    Guardrail-4 invariant (failure_mode_taxonomy_2026-07.md #5 / CASE-6A2B3445):
    a NEEDS_HUMAN_CHECKPOINT verdict without a ReviewRequest is unresumable and
    hides the pause reason. Enforced here — the single write chokepoint — so
    every call site is covered, not just the ones checkpoint.py itself raises.
    """
    assert_checkpoint_invariant(state.final_verdict, state.review_request)
    state.last_updated = datetime.utcnow()
    target = case_dir(state.case_id, root) / "summary.json"
    tmp = target.with_suffix(".tmp.json")
    tmp.write_text(state.model_dump_json(indent=2))
    tmp.replace(target)
    return target


def load_summary(case_id: str, root: Path = STATE_ROOT_DEFAULT) -> CaseState | None:
    target = case_dir(case_id, root) / "summary.json"
    if not target.exists():
        return None
    return CaseState.model_validate_json(target.read_text())


def write_transcript(case_id: str, calls: Iterable[dict], root: Path = STATE_ROOT_DEFAULT) -> Path:
    """Write the canonical concatenated transcript.

    ``calls`` is an iterable of ``{step_name, prompt, response, duration_sec, attempts}``
    dicts in chronological order. The transcript is the full reproducible log.
    """
    target = case_dir(case_id, root) / "transcript.json"
    target.write_text(json.dumps(list(calls), indent=2, default=str))
    return target


def build_transcript_from_disk(case_id: str, root: Path = STATE_ROOT_DEFAULT) -> list[dict]:
    """Walk the case dir and assemble the chronological transcript from on-disk artifacts.

    Useful when we want to regenerate transcript.json from the raw step dirs
    after a crashed run, or to inspect a case offline.
    """
    cdir = case_dir(case_id, root)
    calls: list[dict] = []

    # Sort step dirs by name: iter-01, iter-02, ..., adv-r1, adv-r2, ...
    step_dirs = sorted(
        [p for p in cdir.iterdir() if p.is_dir()],
        key=lambda p: (p.name.startswith("adv-"), p.name),
    )

    for sdir in step_dirs:
        for prompt_path in sorted(sdir.glob("*.prompt.md")):
            step = prompt_path.stem.removesuffix(".prompt")
            entry = {
                "phase": sdir.name,
                "step": step,
                "prompt": prompt_path.read_text() if prompt_path.exists() else "",
            }
            raw = sdir / f"{step}.raw.json"
            if raw.exists():
                try:
                    entry["raw_wrapper"] = json.loads(raw.read_text())
                except json.JSONDecodeError:
                    entry["raw_wrapper"] = {"_parse_error": True, "_raw": raw.read_text()[:5000]}
            parsed = sdir / f"{step}.parsed.json"
            if parsed.exists():
                try:
                    entry["parsed"] = json.loads(parsed.read_text())
                except json.JSONDecodeError:
                    entry["parsed"] = None
            meta = sdir / f"{step}.meta.json"
            if meta.exists():
                try:
                    entry["meta"] = json.loads(meta.read_text())
                except json.JSONDecodeError:
                    entry["meta"] = None
            calls.append(entry)
    return calls
