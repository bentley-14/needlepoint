#!/usr/bin/env python3
"""make doctor — one-command machine diagnosis for the needlepoint stack.

Turns every "it doesn't work on my machine" into a five-second readout.
Checks are grouped: HARD failures block running cases (exit 1); WARNs are
degraded-but-working (exit 0). Nothing here mutates anything.

    make doctor          # or: python3 scripts/doctor.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OK, WARN, FAIL = "✓", "△", "✗"


def _run(cmd: list[str], timeout: int = 15) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or p.stderr).strip()
    except Exception as e:
        return 127, str(e)


def courtlistener_configured(env: dict | None = None, dotenv_path: Path | None = None) -> bool:
    """True if a CourtListener token is set (env wins over .env file).

    Accepts either COURTLISTENER_TOKEN or the older COURTLISTENER_API_TOKEN —
    scripts/courtlistener_search.py reads both. Pure function so tests can inject
    the environment without touching os.environ.
    """
    if env is None:
        env = dict(os.environ)
    names = ("COURTLISTENER_TOKEN", "COURTLISTENER_API_TOKEN")
    if any(env.get(n, "").strip() for n in names):
        return True
    if dotenv_path is None:
        dotenv_path = REPO_ROOT / ".env"
    if dotenv_path.is_file():
        for line in dotenv_path.read_text().splitlines():
            for n in names:
                if line.startswith(f"{n}=") and line[len(n) + 1:].strip().strip("\"'"):
                    return True
    return False


def checks() -> list[tuple[str, str, str]]:
    """[(level, name, detail)] — level ∈ {OK, WARN, FAIL}."""
    out: list[tuple[str, str, str]] = []

    # ── interpreter story (the one-python convention) ──
    venv_py = REPO_ROOT / ".venv" / "bin" / "python3"
    if venv_py.exists():
        rc, v = _run([str(venv_py), "--version"])
        out.append((OK, "python (repo venv)", v))
    else:
        rc, v = _run(["python3", "--version"])
        major, minor = sys.version_info[:2]
        lvl = WARN
        out.append((lvl, "python (PATH fallback — run `make install` to pin the venv)", v))
    if (sys.version_info[:2]) < (3, 11):
        out.append((FAIL, "python version", f"{sys.version.split()[0]} < 3.11 — repo needs 3.11+"))

    # ── claude CLI (the engine — runs bill THIS machine's login) ──
    api_backend = os.environ.get("FRAUD_INV_EXEC_BACKEND", "").strip().lower() == "api"
    if api_backend:
        try:
            import litellm  # noqa: F401
            out.append((OK, "api backend", "LiteLLM installed"))
        except ImportError:
            out.append((FAIL, "api backend", "pip install 'litellm>=1.40'"))
        keys = [k for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
                            "OPENROUTER_API_KEY", "MISTRAL_API_KEY") if os.environ.get(k)]
        out.append((OK, "provider key", ", ".join(keys)) if keys or os.environ.get("FRAUD_INV_API_BASE")
                   else (FAIL, "provider key", "set a provider API key (e.g. ANTHROPIC_API_KEY)"))
    claude = os.environ.get("CLAUDE_BIN") or shutil.which("claude")
    if not claude and api_backend:
        out.append((WARN, "claude CLI", "not installed (optional: api backend selected)"))
    elif not claude:
        out.append((FAIL, "claude CLI", "not on PATH — install Claude Code, then `claude /login`"))
    else:
        rc, v = _run([claude, "--version"])
        out.append((OK if rc == 0 else FAIL, "claude CLI", v if rc == 0 else f"`claude --version` failed: {v}"))

    # ── R2 (artifact sync) ──
    if not shutil.which("rclone"):
        out.append((WARN, "rclone", "missing — artifacts stay local-only (.env.example, storage section)"))
    else:
        try:
            sys.path.insert(0, str(REPO_ROOT / "scripts"))
            from np_launch import r2_configured  # type: ignore
            ok = r2_configured(dict(os.environ), REPO_ROOT / ".env")
            out.append((OK, "R2 sync", "configured") if ok else
                       (WARN, "R2 sync", "rclone present but no `r2` remote / R2_* creds — artifacts stay local"))
        except Exception as e:
            out.append((WARN, "R2 sync", f"check unavailable ({e})"))

    # ── CourtListener token (case-law lookups) ──
    if courtlistener_configured():
        out.append((OK, "CourtListener", "token set"))
    else:
        out.append((WARN, "CourtListener",
                    "unauthenticated (rate-limited) — set COURTLISTENER_TOKEN in .env"))

    # ── data presence ──
    years = sorted({int(p.stem.rsplit("_", 1)[-1])
                    for sub in ("processed", "panels")
                    for p in (REPO_ROOT / "data" / sub).glob("cms_partb_[0-9]*.parquet")})
    if years:
        out.append((OK, "Part B panels", f"{len(years)} years ({years[0]}–{years[-1]})"))
    else:
        out.append((WARN, "Part B panels", "none — runs fall back to bare-NPI subjects (`make pull-data`)"))

    gs = REPO_ROOT / "data" / "extracted" / "gold_set.jsonl"
    out.append((OK, "gold_set", f"{gs.stat().st_size // 1_000_000} MB") if gs.exists()
               else (WARN, "gold_set", "missing — hindsight/labels unavailable"))

    scored = list(REPO_ROOT.glob("results/walk_forward_v4_top*/scored_full_*.parquet"))
    out.append((OK, "scored panels", f"{len(scored)} files") if scored
               else (WARN, "scored panels", "none — subjects fall back to raw panels"))

    # ── queue freshness ──
    queues = sorted(REPO_ROOT.glob("validation/triage/**/*.json"),
                    key=lambda q: q.stat().st_mtime, reverse=True)
    if queues:
        age_d = (time.time() - queues[0].stat().st_mtime) / 86400
        lvl = OK if age_d < 45 else WARN
        out.append((lvl, "triage queue", f"newest {queues[0].name} is {age_d:.0f}d old"
                    + ("" if age_d < 45 else " — consider a re-rank/triage pass")))
    else:
        out.append((WARN, "triage queue", "no queue files — `make investigate` still works ad-hoc"))

    # ── hooks + disk ──
    rc, hookpath = _run(["git", "-C", str(REPO_ROOT), "config", "core.hooksPath"])
    out.append((OK, "pre-commit guards", hookpath) if hookpath == "scripts/git-hooks"
               else (WARN, "pre-commit guards", "not active — run `make install-hooks` (needed only if you commit)"))
    try:
        free_gb = shutil.disk_usage(REPO_ROOT).free / 1e9
        out.append((OK if free_gb > 20 else WARN, "disk free", f"{free_gb:.0f} GB"
                    + ("" if free_gb > 20 else " — low; run scripts/r2_janitor.py")))
    except Exception:
        pass
    return out


def main() -> int:
    results = checks()
    width = max(len(n) for _, n, _ in results) + 2
    hard_fail = False
    for lvl, name, detail in results:
        print(f" {lvl}  {name:<{width}} {detail}")
        if lvl == FAIL:
            hard_fail = True
    print()
    if hard_fail:
        print("✗ hard failures above block running cases — fix those first.")
        return 1
    warns = sum(1 for l, _, _ in results if l == WARN)
    print(f"✓ ready to run cases ({warns} warning(s) — degraded but working)"
          if warns else "✓ fully healthy")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
