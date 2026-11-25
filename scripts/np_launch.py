#!/usr/bin/env python3
"""NeedlePoint maintainer launcher.

    python3 scripts/np_launch.py investigate <NPI> [--rounds N] [--resume CASE-ID]
        Launches the full investigator loop (Layers 3-5) on one NPI. Auto-finds a triage-queue entry for the NPI;
        falls back to local panels, then to a bare-NPI subject — so it works
        on a machine with partial or no local data.

Makefile sugar:  `make investigate NPI=1234567890`

Account portability: every agent step runs on the local `claude` CLI (default
backend `tmux` — interactive panes; `--backend claude_p` for headless), so each
teammate's runs bill the Claude account the local `claude` CLI is logged into.
No shared keys; `claude /login` once per machine.

Run from a TOP-LEVEL terminal (not from inside a Claude Code session) — the
nested-autonomy guard blocks orchestrator spawns from inside a session.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

try:  # keys and settings may live in a gitignored .env at the repo root
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
except ImportError:
    pass

REPO_ROOT = Path(__file__).resolve().parents[1]


# ── R2 configuration check (pure — no side effects) ─────────────────────────

def r2_configured(env: dict | None = None, dotenv_path: Path | None = None) -> bool:
    """Return True if an R2 remote is usable: rclone remote OR three env vars.

    Pure function (no subprocess for the env check): accepts an env dict so
    callers and tests can inject the environment without monkeypatching os.environ.
    The rclone-remote check does call `rclone listremotes` — it is guarded so
    that a missing or broken rclone binary simply returns False.
    """
    if env is None:
        env = dict(os.environ)
    # Check env vars (environment wins over .env file)
    keys = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")
    if all(env.get(k, "").strip() for k in keys):
        return True
    # Check .env file
    if dotenv_path is None:
        dotenv_path = REPO_ROOT / ".env"
    if dotenv_path.is_file():
        content = dotenv_path.read_text()
        def _parse(name: str) -> str:
            for line in content.splitlines():
                if line.startswith(f"{name}="):
                    return line[len(name) + 1:].strip().strip("\"'")
            return ""
        if all(_parse(k) for k in keys):
            return True
    # Check rclone remote
    try:
        out = subprocess.run(
            ["rclone", "listremotes"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        if "r2:" in out.splitlines():
            return True
    except Exception:
        pass
    return False


# ── preflight ────────────────────────────────────────────────────────────────

def _fail(msg: str) -> int:
    print(f"✗ {msg}", file=sys.stderr)
    return 1


_PROVIDER_KEY_VARS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
                      "OPENROUTER_API_KEY", "MISTRAL_API_KEY", "GROQ_API_KEY", "AZURE_API_KEY")


def _preflight_api_backend() -> str | None:
    """Checks for FRAUD_INV_EXEC_BACKEND=api: LiteLLM installed + a provider key or local endpoint."""
    try:
        import litellm  # noqa: F401
    except ImportError:
        return "FRAUD_INV_EXEC_BACKEND=api needs LiteLLM: pip install 'litellm>=1.40'"
    if not (os.environ.get("FRAUD_INV_API_BASE") or any(os.environ.get(k) for k in _PROVIDER_KEY_VARS)):
        return ("no model provider key found. Set one of "
                + ", ".join(_PROVIDER_KEY_VARS)
                + " (or FRAUD_INV_API_BASE for a local model server).")
    return None


def _preflight_claude() -> str | None:
    """Return an error string if the configured agent backend isn't usable, else None."""
    if os.environ.get("FRAUD_INV_EXEC_BACKEND", "").strip().lower() == "api":
        return _preflight_api_backend()
    bin_ = os.environ.get("CLAUDE_BIN") or shutil.which("claude")
    if not bin_:
        return ("the `claude` CLI is not on PATH. Install it "
                "(https://claude.com/claude-code), then run `claude /login` "
                "with YOUR account — agent runs bill the locally-logged-in "
                "account.")
    try:
        subprocess.run([bin_, "--version"], capture_output=True, timeout=15)
    except Exception as e:
        return f"`claude --version` failed: {e}"
    return None


# ── investigate: one NPI, full loop ─────────────────────────────────────────

def build_investigate_cmd(npi: str, *, rounds: int, resume: str | None,
                          budget: bool, python: str = sys.executable) -> list[str]:
    """Pure command construction (unit-tested; no side effects)."""
    if not (npi.isdigit() and len(npi) == 10 and npi[0] in "12"):
        raise ValueError(
            f"NPI must be exactly 10 digits starting with 1 or 2, got {npi!r}")
    cmd = [python, "-m", "scripts.orchestrate_pipeline",
           "--invocation-mode", "investigate", "--npi", npi,
           "--max-adversarial", str(rounds)]
    if resume:
        cmd += ["--resume-case", resume]
    if budget:
        cmd += ["--budget-mode"]
    return cmd


def cmd_investigate(args) -> int:
    # Apply the chosen backend BEFORE preflight so we check the backend that will run.
    os.environ["FRAUD_INV_EXEC_BACKEND"] = args.backend
    err = _preflight_claude()
    if err:
        return _fail(err)
    try:
        cmd = build_investigate_cmd(args.npi, rounds=args.rounds,
                                    resume=args.resume, budget=args.budget)
    except ValueError as e:
        return _fail(str(e))
    # Execution backend + model profile — exported to the env so the
    # orchestrate_pipeline subprocess (which inherits os.environ) and the
    # claude_p resolvers pick them up. Works whether invoked via `make` (which
    # also sets these env vars) or directly with --backend/--profile.
    os.environ["FRAUD_INV_EXEC_BACKEND"] = args.backend
    os.environ["FRAUD_INV_PROFILE"] = args.profile
    if not r2_configured():
        print("note: R2 sync not configured — artifacts will stay local (.env.example, storage section)")
    print(f"• launching investigation: NPI {args.npi} "
          f"({args.rounds} adversarial rounds max)")
    print(f"• backend: {args.backend}   ·   profile: {args.profile}"
          + ("   (interactive tmux panes — personal usage limits)"
             if args.backend == "tmux" else ""))
    print(f"• cmd: {' '.join(cmd)}\n")
    return subprocess.call(cmd, cwd=REPO_ROOT)


# ── main ─────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="np_launch", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)

    i = sp.add_parser("investigate", help="full investigator loop on one NPI")
    i.add_argument("npi", help="10-digit NPI")
    i.add_argument("--rounds", type=int, default=4,
                   help="max adversarial rounds (default 4, the full loop)")
    i.add_argument("--resume", default=None, metavar="CASE-ID",
                   help="resume an existing case id (crash recovery is "
                        "automatic — completed steps reload from cache)")
    i.add_argument("--budget", action="store_true",
                   help="budget mode (fewer iterations)")
    i.add_argument("--backend", choices=["claude_p", "tmux", "api"],
                   default=os.environ.get("FRAUD_INV_EXEC_BACKEND", "tmux"),
                   help="execution backend. Default 'tmux' for true investigations: "
                        "interactive panes are observable/modifiable and run from an "
                        "attended top-level terminal, where their steps stay short "
                        "(full toolset) so the lack of a --max-turns bound is benign. "
                        "Calibration uses 'claude_p' (its restrictive export jail makes "
                        "steps run long, which needs headless --max-turns + silent "
                        "auto-deny — see scripts/calibration/). 'api' runs every agent "
                        "through any LLM provider (LiteLLM) with your own API key. "
                        "Override via $FRAUD_INV_EXEC_BACKEND.")
    i.add_argument("--profile", choices=["production", "draft"],
                   default=os.environ.get("FRAUD_INV_PROFILE", "production"),
                   help="model profile: 'production' (opus[1m]/max per step) or "
                        "'draft' (sonnet/medium everywhere — cheap orchestration "
                        "smoke-testing). Default from $FRAUD_INV_PROFILE.")
    i.set_defaults(fn=cmd_investigate)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
