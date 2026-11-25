#!/usr/bin/env python3
"""Claude Code hook → event-spool writer (+ calibration permission guard) for
the tmux execution backend.

Registered (via ``--settings``) as a PreToolUse + PostToolUse hook on the
interactive ``claude`` panes that ``tmux_backend.py`` drives. Claude Code runs
this once per tool boundary, piping the hook JSON on stdin. We write that JSON
verbatim as its OWN atomically-renamed file in the spool directory passed as
argv[1]; ``tmux_backend._drain_spool`` then reads, forwards (to the run's
EventSink as STAGE_TOOL_USE / STAGE_TOOL_RESULT), and deletes them.

Why one-file-per-event instead of appending to a shared log: parallel tool
calls fire hooks concurrently, and concurrent appends interleave/corrupt lines
(observed). A unique temp file + atomic ``os.replace`` makes interleaving
impossible.

CALIBRATION GUARD (``--guard <case_root> <export>``): an interactive pane has no
way to *silently* auto-deny a non-allowlisted tool the way headless
``--permission-mode default`` does — it PROMPTS, hanging an unattended pane
(observed 2026-06-15: the investigator's ``python3 scripts/ca_exclusion_lookup.py``
and an iter-02 ``touch`` both blocked on a prompt). So in calibration we make
THIS PreToolUse hook the decision authority: it emits ``permissionDecision``
allow/deny on stdout, reproducing the headless jail (sandbox tools + scoped
file-I/O allowed; everything else silently denied → the agent gets a "denied"
result and moves on). Web stays hard-denied by ``--disallowedTools`` regardless
(deny-rules win in every mode). Fail-closed: a decision error denies.

Stdlib-only; spooling is fail-open (a hiccup never blocks a tool call), while
the guard fails CLOSED (deny) so an error can never silently widen access.
"""
import json
import os
import random
import re
import sys
import time

# Bash commands the calibration jail permits: the sandbox tools (``python3
# scripts/<x>.py``) and the file-I/O-contract sentinel creators. Everything else
# (cat/curl/cp/… that could read or exfiltrate the repo's answer keys) is denied.
_ALLOWED_BASH = re.compile(r"^(python3?\s+scripts/[A-Za-z0-9_]+\.py(\s|$)|touch\s|mkdir\s)")
_FILE_TOOLS = ("Read", "Edit", "Write", "MultiEdit", "NotebookEdit")
_PASS_TOOLS = ("Task", "TodoWrite", "TodoRead")   # orchestration; sub-agent tool calls are themselves guarded


def _under(path: str, roots: list[str]) -> bool:
    if not path:
        return False
    try:
        rp = os.path.realpath(path)
    except Exception:
        return False
    for r in roots:
        try:
            rr = os.path.realpath(r)
        except Exception:
            continue
        if rp == rr or rp.startswith(rr + os.sep):
            return True
    return False


def _decide(tool_name: str, ti: dict, roots: list[str]) -> tuple[str, str]:
    """Reproduce the headless clean-room jail: allow sandbox tools + scoped
    file-I/O, deny everything else. Returns (decision, reason)."""
    if tool_name in _FILE_TOOLS:
        fp = ti.get("file_path") or ti.get("notebook_path") or ti.get("path") or ""
        if _under(fp, roots):
            return "allow", "scoped file I/O (case root / export)"
        return "deny", "file path outside the calibration export / case root"
    if tool_name == "Bash":
        cmd = (ti.get("command") or "").strip()
        if _ALLOWED_BASH.match(cmd):
            return "allow", "sandbox tool / I/O-contract sentinel"
        return "deny", "non-sandbox bash blocked in the calibration jail"
    if tool_name in _PASS_TOOLS:
        return "allow", f"{tool_name} (sub-agent tool calls are individually guarded)"
    return "deny", f"{tool_name} not in the calibration allowlist"


def main() -> int:
    spool = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("NP_TMUX_HOOK_SPOOL", "")
    guard_roots: list[str] | None = None
    if "--guard" in sys.argv:
        gi = sys.argv.index("--guard")
        guard_roots = [a for a in sys.argv[gi + 1:gi + 3]]

    data = b""
    try:
        data = sys.stdin.buffer.read()
    except Exception:
        data = b""

    # Spool the event for streaming (fail-open — never block a tool call).
    try:
        if spool and data:
            os.makedirs(spool, exist_ok=True)
            stem = f"{time.time_ns()}_{os.getpid()}_{random.randint(0, 99999)}"
            tmp = os.path.join(spool, stem + ".tmp")
            fin = os.path.join(spool, stem + ".json")
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, fin)   # atomic; no interleaving
    except Exception:
        pass

    # Calibration permission decision (PreToolUse only). Fail CLOSED: any error
    # in deciding denies, so a hook hiccup can never silently widen access.
    if guard_roots:
        try:
            ev = json.loads(data) if data else {}
        except Exception:
            ev = {}
        if ev.get("hook_event_name") == "PreToolUse":
            try:
                decision, reason = _decide(
                    ev.get("tool_name", ""), ev.get("tool_input") or {}, guard_roots)
            except Exception:
                decision, reason = "deny", "calibration guard error (fail-closed)"
            sys.stdout.write(json.dumps({"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": decision,
                "permissionDecisionReason": reason,
            }}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
