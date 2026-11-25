"""Triage-agent drift guard — enforce that Stage 3 always resolves to the ONE canonical
triage prompt/agent, and that the deprecated reorder-only `rerank` agent and the standalone
`agent_triage_2023_v3` prompt/driver can never be referenced again.

Motivation: Stage 3 went through three generations — agent_investigation (v1) → agent_triage_v3
(v3) → the reorder-only `rerank` (v2 pipeline). On 2026-05-31 they were unified into the
canonical `triage` agent (exact v3 verdict behavior, enriched columns). This guard makes the
canonical the *only* reachable Stage-3 prompt: it fails if a deprecated prompt/module reappears
or if any live (non-legacy) code still imports/names the old `rerank` agent or schema.

Everything is plain text / `ast` (no imports of fraud_investigator), so the hook stays fast.

Exit: 0 = consistent · 1 = drift found.
Run:  python scripts/verify_triage_contract.py
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FI = REPO / "fraud-investigator-v2/src/fraud_investigator"

# ── The contract: ONE canonical Stage-3 prompt + agent + schema ────────────
CANONICAL_PROMPT = FI / "prompts/triage.md"
TRIAGE_AGENT = FI / "agents/triage.py"
SCHEMAS = FI / "pipeline_schemas.py"

# Deprecated artifacts that must NOT exist anywhere live (only under legacy/).
DEPRECATED_FILES = [
    FI / "prompts/rerank.md",
    FI / "agents/rerank.py",
    REPO / "prompts/agent_triage_2023_v3.md",
    REPO / "scripts/experiments/prepare_agent_triage_batches.py",
]
# Tokens that may no longer appear in live (non-legacy) code.
FORBIDDEN_REFS = ("import rerank", "agents.rerank", "RerankItem", "RerankOutput", "STAGE_RERANK")
# Markers proving triage.md is the canonical forensic-verdict prompt.
REQUIRED_MARKERS = ("HIGH_RISK", "MODERATE_RISK", "LOW_RISK", "DISMISS", "NO WEB SEARCH")

# Live tree to scan (exclude legacy/, caches, worktrees, this guard itself).
def _live_py() -> list[Path]:
    out = []
    for p in (list((FI).rglob("*.py")) + list((REPO / "scripts").rglob("*.py"))
              + list((REPO / "web").rglob("*.py"))):
        s = str(p)
        if "/legacy/" in s or "__pycache__" in s or "/.claude/" in s:
            continue
        if p.name == "verify_triage_contract.py":
            continue
        out.append(p)
    return out


def check() -> list[str]:
    v: list[str] = []

    # T1 — canonical prompt + agent + schema present
    if not CANONICAL_PROMPT.exists():
        v.append(f"T1: canonical prompt missing: {CANONICAL_PROMPT.relative_to(REPO)}")
    if not TRIAGE_AGENT.exists():
        v.append(f"T1: triage agent missing: {TRIAGE_AGENT.relative_to(REPO)}")

    # T2 — triage agent's PROMPT_PATH literally points at prompts/triage.md
    if TRIAGE_AGENT.exists():
        src = TRIAGE_AGENT.read_text()
        if 'prompts" / "triage.md' not in src and "prompts/triage.md" not in src:
            v.append("T2: agents/triage.py PROMPT_PATH does not resolve to prompts/triage.md")

    # T3 — canonical prompt carries the forensic-verdict markers
    if CANONICAL_PROMPT.exists():
        txt = CANONICAL_PROMPT.read_text()
        missing = [m for m in REQUIRED_MARKERS if m not in txt]
        if missing:
            v.append(f"T3: triage.md missing canonical markers: {missing}")

    # T4 — schema defines TriageItem(verdict=...) + TriageOutput, and NOT the old Rerank* classes
    if SCHEMAS.exists():
        tree = ast.parse(SCHEMAS.read_text())
        classes = {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
        if "TriageItem" not in classes or "TriageOutput" not in classes:
            v.append("T4: pipeline_schemas.py missing TriageItem/TriageOutput")
        if {"RerankItem", "RerankOutput"} & classes:
            v.append(f"T4: deprecated schema classes still defined: {sorted({'RerankItem','RerankOutput'} & classes)}")
        ti = next((n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "TriageItem"), None)
        if ti is not None:
            fields = {t.target.id for t in ti.body if isinstance(t, ast.AnnAssign) and isinstance(t.target, ast.Name)}
            if "verdict" not in fields:
                v.append("T4: TriageItem has no `verdict` field (the suspicion tier)")

    # T5 — deprecated files must not exist outside legacy/
    for f in DEPRECATED_FILES:
        if f.exists():
            v.append(f"T5: deprecated artifact still present (move to legacy/ or delete): {f.relative_to(REPO)}")

    # T6 — no live code may reference the old rerank agent/schema/stage
    for p in _live_py():
        body = p.read_text()
        for tok in FORBIDDEN_REFS:
            if tok in body:
                # allow the word inside a comment that documents the rename history? No —
                # keep it strict so the wrong symbol can never be imported by accident.
                v.append(f"T6: {p.relative_to(REPO)} still references deprecated '{tok}'")
                break
    return v


def main() -> int:
    violations = check()
    if violations:
        print("❌ Triage contract drift detected:", file=sys.stderr)
        for m in violations:
            print(f"   - {m}", file=sys.stderr)
        print("\n   Stage 3 must resolve to the canonical triage agent (prompts/triage.md + "
              "agents/triage.py + TriageItem/TriageOutput). Deprecated rerank/v3 artifacts must "
              "live only under legacy/. Set ALLOW_TRIAGE_DRIFT=1 to bypass.", file=sys.stderr)
        return 1
    print("✓ Triage contract OK — canonical prompts/triage.md + agents/triage.py + "
          "TriageItem/TriageOutput; no deprecated rerank/v3 references in live code.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
