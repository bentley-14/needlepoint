"""Case writer (Layer 5) — final summarizer."""
from __future__ import annotations

import datetime
import json
from pathlib import Path

from ..claude_p import invoke_agent
from ..schemas import CaseWriterOutput
from .base import load_prompt, render


def _autofix_markdown(text: str) -> str:
    """Deterministic markdown post-fix (scripts/fix_dossier_markdown.py).

    Escapes currency dollars, backticks snake_case identifiers, and
    normalizes ***bold-italic*** → **bold** outside code spans, so the
    generated prose can never fail the orchestrator's post-write lint
    (scripts/lint_dossier_markdown.py) on formatting. Fails open: if the
    fixer script is missing, the text ships unchanged (lint still warns).
    """
    try:
        try:
            from scripts.fix_dossier_markdown import fix_text  # repo root on sys.path
        except ImportError:
            import importlib.util
            fixer = Path(__file__).resolve().parents[4] / "scripts" / "fix_dossier_markdown.py"
            spec = importlib.util.spec_from_file_location("fix_dossier_markdown", fixer)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            fix_text = mod.fix_text
        return fix_text(text)
    except Exception:
        return text


def _json_safe(o):
    """JSON-encoder default that stringifies datetime + enum + Path values.

    final_state_summary is built from CaseState which has datetime fields
    (start_time, last_updated) and may have Enum values (FinalVerdict).
    Without this, json.dumps blows up with:
        TypeError: Object of type datetime is not JSON serializable
    """
    if isinstance(o, (datetime.datetime, datetime.date)):
        return o.isoformat()
    if isinstance(o, Path):
        return str(o)
    if hasattr(o, "value"):           # enum.Enum
        return o.value
    if hasattr(o, "model_dump"):      # Pydantic v2
        return o.model_dump()
    return str(o)


# Each prose deliverable is generated in its own call so a single generation
# never approaches the output-token ceiling. (doc_field, filename-for-prompt).
_DOCS: list[tuple[str, str]] = [
    ("case_summary",      "00_README.md"),
    ("full_case_report",  "01_Case_Report.md"),
    ("layperson_summary", "02_Layperson_Summary.md"),
]

# Evidence fields the prose docs actually cite. Verbose fields the writer never
# quotes verbatim (novelty_rationale, rationale) are dropped from the PROMPT
# INPUT only — the raw output artifacts (03_Full_State / 04_bundle) keep them.
_EVIDENCE_KEEP = ("id", "tier", "type", "fact", "supports_counts", "citation", "confidence")


def _slim_state_for_prompt(state: dict) -> dict:
    """Project the full CaseState dump down to what the PROSE writer needs.

    The full state is ~97% ``iterations`` (3× investigator + counsel_build dumps,
    much of it superseded intermediate work + verbose rationale the prose docs
    never cite). Sending all of it makes every per-doc call re-chew ~224 KB. This
    keeps the operative complaint, the merged/deduped evidence (trimmed to cited
    fields), a compact build/disposition trail, and the adversarial rounds —
    typically a 2-3× input reduction with no loss of report-relevant content.

    This trims the *prompt input only*. The orchestrator still writes
    ``03_Full_State.json`` + ``04_Full_Investigation_Bundle.json`` from the full
    canonical state (raw reason-trace), so nothing is lost on disk.
    """
    iters = state.get("iterations") or []
    advs = state.get("adversarial_rounds") or []

    # Merge evidence across all build iters + adversarial rebuttal passes,
    # dedup by id, keep only the fields the prose docs cite.
    seen: set = set()
    evidence: list[dict] = []

    def _collect(inv: dict | None) -> None:
        for e in (inv or {}).get("evidence") or []:
            eid = e.get("id")
            if eid in seen:
                continue
            seen.add(eid)
            evidence.append({k: e.get(k) for k in _EVIDENCE_KEEP if k in e})

    for it in iters:
        _collect(it.get("investigator"))
    for r in advs:
        _collect(r.get("rebuttal_investigator"))

    # The operative complaint = the latest counsel_build present (supersedes the
    # earlier iterations' drafts).
    final_complaint = None
    for it in reversed(iters):
        if it.get("counsel_build"):
            final_complaint = it["counsel_build"]
            break

    # Compact decision trail (actions + short reasoning), not the full dumps.
    build_trail: list[dict] = []
    for it in iters:
        rv = it.get("counsel_first_chair_review") or {}
        dp = it.get("counsel_disposition") or {}
        build_trail.append({
            "iteration": it.get("iteration"),
            "phase": it.get("phase"),
            "first_chair_action": rv.get("action"),
            "first_chair_reasoning": (rv.get("reasoning") or "")[:400],
            "disposition_action": dp.get("action"),
            "disposition_reasoning": (dp.get("reasoning") or "")[:400],
        })

    slim = {k: v for k, v in state.items() if k != "iterations"}
    slim["evidence_merged"] = evidence
    slim["final_complaint"] = final_complaint
    slim["build_trail"] = build_trail
    return slim


def run(
    *,
    subject: str,
    case_id: str,
    final_state_summary: dict,
    out_dir: Path,
) -> CaseWriterOutput:
    """Synthesize the prose deliverables — ONE ``claude -p`` call per document.

    ``03_Full_State.json`` + ``04_Full_Investigation_Bundle.json`` are NOT
    produced here; the orchestrator collates them deterministically from the
    canonical case state (raw reason-trace). This function emits only the three
    prose documents, each in its own call.
    """
    template = load_prompt("case_writer")
    # Slim the PROMPT INPUT (not the on-disk raw state) so each per-doc call
    # processes a compact projection instead of the full ~224 KB state.
    state_json = json.dumps(_slim_state_for_prompt(final_state_summary), indent=2, default=_json_safe)

    docs: dict[str, str] = {}
    for doc_field, target_document in _DOCS:
        prompt = render(
            template,
            subject=subject,
            case_id=case_id,
            final_state_summary=state_json,
            target_document=target_document,
        )
        # Distinct out_dir per doc so the three calls don't collide on
        # {step_name}.parsed.json (and each is independently cache-resumable);
        # step_name stays "case_writer" so each inherits the case_writer
        # model / effort / turn-budget.
        # schema=None → free-text mode: the agent returns the markdown document
        # directly (no fragile {"content": "..."} JSON envelope — a large report
        # reliably breaks that and the run dies in case_writer).
        result = invoke_agent(
            prompt=prompt,
            schema=None,
            out_dir=out_dir / f"_cw_{doc_field}",
            step_name="case_writer",
        )
        # Deterministic formatting auto-fix so a generation can never fail
        # on markdown escaping (the post-write lint becomes a backstop).
        docs[doc_field] = _autofix_markdown(result)

    assembled = CaseWriterOutput(case_id=case_id, **docs)
    # Write the combined parsed.json so the full-investigation bundle's
    # case_writer read-back (orchestrator._emit_full_state_bundle) still finds it.
    (out_dir / "case_writer.parsed.json").write_text(assembled.model_dump_json(indent=2))
    return assembled
