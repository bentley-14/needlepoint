"""tmux interactive-session execution backend for the investigator loop.

WHY THIS EXISTS
---------------
As of 2026-06-15 Claude Code split billing: ``claude -p`` / Agent-SDK runs draw
from a *separate* monthly Agent-SDK credit (metered at full API rates), while
**interactive** ``claude`` sessions draw from the user's personal usage limits.
Our loop spawns one ``claude -p`` subprocess per agent step (see
``claude_p.invoke_agent``), so every run now bills the SDK credit — prohibitive
for real case production.

This backend runs the SAME loop using **interactive ``claude`` sessions driven
inside tmux panes**, so runs draw from personal limits. It is a drop-in
replacement for the execution primitive ONLY: the orchestrator, Pydantic
schemas, per-step caching, EventSink, scoring, and guardrails are untouched.
``claude_p.invoke_agent`` routes here when ``FRAUD_INV_EXEC_BACKEND=tmux``.

HOW IT WORKS (file-based I/O — no TUI scraping for the payload)
---------------------------------------------------------------
One tmux *session* per orchestrator process, one *window* per ``step_name``
(reused across iterations, so the interactive context carries over for free —
this is the session-continuity benefit without ``--session-id`` juggling). For
each step:

  1. Write the rendered prompt to ``{out_dir}/{step}.prompt.md`` (same artifact
     name ``invoke_agent`` uses).
  2. Ensure the step's pane exists, launched once with the step's model/effort.
  3. ``tmux send-keys`` a SHORT directive telling the pane to read that prompt
     file and write ONLY its JSON result to ``{step}.result.json``, then create
     an empty marker ``{step}.done``. The big prompt stays on disk — send-keys
     only carries a one-liner.
  4. Poll the filesystem for the marker (scanning the pane for the billing-wall
     signature meanwhile). On the marker, validate ``result.json`` against the
     caller's schema; on a validation failure, send a focused repair directive
     and re-poll (same retry budget / repair text as ``invoke_agent``).
  5. Persist ``{step}.parsed.json`` + ``{step}.meta.json`` (same on-disk
     contract → resume + console keep working) and return the validated model.

PERMISSIONS
-----------
Panes launch with ``--permission-mode bypassPermissions`` (auto-approves every
tool; the interactive equivalent of headless ``--dangerously-skip-permissions``)
when run from a top-level terminal. From INSIDE a Claude Code session that is
blocked by the nested-autonomy guard, so when ``FRAUD_INV_SAFE_PERMS=1`` we
launch under ``--permission-mode default`` + the same broad allowlist
``claude_p`` uses (allowlisted tools auto-run without a prompt) — mirroring the
existing SAFE-PERMS launch mode.
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from .claude_p import (
    DEFAULT_CLAUDE_BIN,
    DEFAULT_TIMEOUT_SEC,
    DISPLAY_PARSED_JSON,
    MAX_PARSE_RETRIES,
    SAFE_PERMS,
    SAFE_PERMS_BASE_ALLOWED,
    ClaudePHardStopError,
    ClaudePInvocationError,
    _coerce_and_persist,
    _display_parsed_json,
    _hard_stop_message,
    _load_cached_parsed,
    _resolve_effort_for_step,
    _resolve_model_for_step,
    _step_meta_payload,
    _strip_code_fence,
    get_default_cwd,
    is_hard_stop_failure,
)
from .event_sink import (
    EVENT_STAGE_TEXT_TURN,
    EVENT_STAGE_TOOL_RESULT,
    EVENT_STAGE_TOOL_USE,
)

# Hook script registered (via --settings) on each pane; streams every tool
# boundary to a per-pane spool dir that the backend drains into the EventSink.
HOOK_SCRIPT = Path(__file__).resolve().parent / "_tmux_tool_hook.py"

LOG = logging.getLogger("fraud_investigator.tmux_backend")

T = TypeVar("T", bound=BaseModel)

# ── tunables ────────────────────────────────────────────────────────────────
POLL_INTERVAL_SEC = 2.0       # filesystem poll cadence for the done-marker
HEARTBEAT_EVERY_SEC = 15.0    # console STAGE_PROGRESS heartbeat cadence
BOOT_TIMEOUT_SEC = 90.0       # max wait for a pane's interactive TUI to be ready
PANE_WIDTH = 220
PANE_HEIGHT = 50
CAPTURE_SCROLLBACK = 160      # lines of pane history to scan (hard-stop / exit)
_EXIT_SENTINEL = "CLAUDE_EXITED_"   # printed by the pane's shell if claude exits

# Interactive panes have NO --max-turns bound (unlike headless `claude -p`, which
# the orchestrator passes 600s + a turn budget), so a step's wall-time is highly
# variable — the SAME counsel_first_chair_review ran 65s one iteration and 1928s
# the next. The headless 600s default would kill a healthy, working agent — so
# the tmux backend FLOORS every step's timeout at TMUX_MIN_STEP_TIMEOUT_SEC.
# At the nominal budget it sends a "stop and write your JSON now" finalize nudge,
# then keeps polling for FINALIZE_SALVAGE_SEC more so a late-but-COMPLETE result
# (the agent wrote its .done a couple minutes past the budget) is ACCEPTED rather
# than discarded — the 2026-06-16 over-run analysis showed the agent needed ~368s
# after the nudge, well past the old 240s grace, and its valid result was thrown
# away 128s before it landed.
TMUX_MIN_STEP_TIMEOUT_SEC = int(os.environ.get("FRAUD_INV_TMUX_TIMEOUT_SEC", "1500"))
FINALIZE_SALVAGE_SEC = float(os.environ.get("FRAUD_INV_TMUX_SALVAGE_SEC", "600"))

# Permission mode for CALIBRATION panes. `default` — but the actual allow/deny
# decision is made by the PreToolUse GUARD HOOK (see `_tmux_tool_hook.py
# --guard`), not by this mode. Why: an interactive pane has no way to silently
# auto-deny a non-allowlisted tool the way headless `default` does — it PROMPTS,
# hanging an unattended pane (the Part-B verify, 2026-06-15, showed acceptEdits
# still prompts on non-allowlisted investigative tools like ca_exclusion_lookup,
# and bypassPermissions prompts a one-time accept-warning at launch). The guard
# hook returns permissionDecision allow/deny so allowed tools run without a
# prompt and everything else is silently denied — reproducing the headless jail.
# Web stays hard-denied by --disallowedTools (deny-rules win in every mode).
CAL_PANE_PERMISSION_MODE = os.environ.get("FRAUD_INV_TMUX_CAL_PERM_MODE", "default")


class TmuxUnavailableError(ClaudePInvocationError):
    """tmux is not installed / not on PATH — the tmux backend can't run."""


# ── low-level tmux helpers ────────────────────────────────────────────────────
def _tmux_bin() -> str:
    found = shutil.which("tmux")
    if not found:
        raise TmuxUnavailableError(
            "FRAUD_INV_EXEC_BACKEND=tmux but `tmux` is not installed / not on "
            "PATH. Install it (`brew install tmux`) or unset the backend toggle."
        )
    return found


def _tmux(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run a tmux command (no shell; args passed literally)."""
    return subprocess.run(
        [_tmux_bin(), *args],
        capture_output=True,
        text=True,
        check=check,
    )


def _sanitize_window(step_name: str) -> str:
    """tmux window name: keep it simple/addressable."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", step_name)[:60] or "step"


# ── the pane/session manager ──────────────────────────────────────────────────
class TmuxOrchestra:
    """Manages one tmux session with one interactive-``claude`` pane per step.

    Panes are launched lazily (``ensure_pane``) and reused across iterations.
    ``teardown`` kills the whole session; it is registered with ``atexit`` so a
    crashing orchestrator still cleans up its panes.
    """

    def __init__(self, session_name: str, cwd: Path):
        self.session = session_name
        self.cwd = Path(cwd).resolve()
        self._windows: dict[str, str] = {}   # step_name -> window name
        self._spools: dict[str, Path] = {}   # step_name -> hook-event spool dir
        self._session_created = False

    # -- session ---------------------------------------------------------------
    def _ensure_session(self) -> None:
        if self._session_created:
            return
        # Kill any stale session of the same name first (defensive — a prior
        # crash may have left one), then create a fresh detached session.
        _tmux("kill-session", "-t", self.session, check=False)
        # The session is created with a throwaway holder window; per-step
        # windows are added by ensure_pane. The holder keeps the session alive.
        _tmux(
            "new-session", "-d", "-s", self.session,
            "-x", str(PANE_WIDTH), "-y", str(PANE_HEIGHT),
            "-c", str(self.cwd),
            "-n", "_holder",
            "sh -c 'while :; do sleep 3600; done'",
        )
        self._session_created = True
        atexit.register(self.teardown)
        LOG.info(f"[tmux] created session {self.session!r} (cwd={self.cwd})")

    # -- launch flags ----------------------------------------------------------
    @staticmethod
    def _launch_flags(model: str | None, effort: str | None,
                      spool: Path | None = None,
                      cal_launch_flags: list[str] | None = None,
                      cal_guard: tuple[Path, Path] | None = None) -> list[str]:
        """Flags for an interactive ``claude`` launch.

        Permission policy:
        - CALIBRATION (``cal_launch_flags`` given): use the clean-room jail flags
          verbatim (``--permission-mode <mode> --allowedTools <...> --disallowedTools
          <...> --add-dir <...>``) — reproduces the per-step ``claude -p`` jail at
          the pane level. NOT bypassPermissions.
        - SAFE_PERMS: default mode + the broad allowlist (launchable from inside a
          Claude Code session).
        - otherwise: bypassPermissions (top-level terminal).

        When ``spool`` is given, injects PreToolUse + PostToolUse hooks (via
        ``--settings`` inline JSON — merges, no shared-settings pollution) that
        stream every tool boundary to that spool dir for real-time forwarding.
        """
        flags: list[str] = ["--setting-sources", "user,project,local"]
        if model:
            flags += ["--model", model]
        if effort:
            flags += ["--effort", effort]
        if cal_launch_flags is not None:
            flags += cal_launch_flags          # clean-room jail (overrides perms)
        elif SAFE_PERMS:
            flags += ["--permission-mode", "default",
                      "--allowedTools", SAFE_PERMS_BASE_ALLOWED]
        else:
            flags += ["--permission-mode", "bypassPermissions"]
        if spool is not None:
            flags += ["--settings", _hook_settings_json(spool, cal_guard)]
        return flags

    # -- panes -----------------------------------------------------------------
    def ensure_pane(self, step_name: str, model: str | None, effort: str | None,
                    stream_events: bool = False,
                    cal_launch_flags: list[str] | None = None,
                    cal_guard: tuple[Path, Path] | None = None) -> str:
        """Return the tmux target for ``step_name``'s pane, launching it (and
        waiting for the interactive TUI to be ready) on first use.

        When ``stream_events`` is True, a per-pane hook-event spool dir is
        created and tool-streaming hooks are injected at launch. When
        ``cal_launch_flags`` is given, the pane launches under the calibration
        clean-room jail instead of bypass/SAFE_PERMS.
        """
        self._ensure_session()
        if step_name in self._windows:
            return f"{self.session}:{self._windows[step_name]}"

        win = _sanitize_window(step_name)
        # Disambiguate a sanitize collision across distinct step names.
        if win in self._windows.values():
            win = f"{win}-{len(self._windows)}"
        spool: Path | None = None
        if stream_events:
            spool = Path(tempfile.mkdtemp(prefix=f"nptmux_{win}_"))
            self._spools[step_name] = spool
        flags = self._launch_flags(model, effort, spool, cal_launch_flags, cal_guard)
        # The shell wrapper prints an exit sentinel + idles, so a claude that
        # exits (crash / quota) is detectable in the pane and the window stays
        # open for inspection instead of collapsing the session.
        claude_cmd = " ".join(
            ["claude", *(_shq(f) for f in flags)]
        )
        wrapped = f"{claude_cmd}; echo {_EXIT_SENTINEL}$?; while :; do sleep 3600; done"
        _tmux("new-window", "-t", self.session, "-n", win,
              "-c", str(self.cwd), wrapped)
        self._windows[step_name] = win
        target = f"{self.session}:{win}"
        self._wait_until_ready(target, step_name)
        LOG.info(f"[tmux] pane ready for step={step_name!r} "
                 f"(model={model or '<inherit>'}, effort={effort or '<inherit>'})")
        return target

    def _wait_until_ready(self, target: str, step_name: str) -> None:
        """Block until the pane's interactive TUI is accepting input, dismissing
        the first-launch 'trust this folder' prompt if it appears."""
        deadline = time.time() + BOOT_TIMEOUT_SEC
        trusted = False
        while time.time() < deadline:
            text = self.capture(target)
            low = text.lower()
            if not trusted and ("trust this folder" in low
                                or "is this a project you" in low):
                _tmux("send-keys", "-t", target, "Enter")
                trusted = True
                time.sleep(1.5)
                continue
            if _EXIT_SENTINEL in text:
                raise ClaudePInvocationError(
                    f"[{step_name}] interactive claude exited during boot:\n"
                    + _tail(text, 20)
                )
            # Readiness: the input box / footer is rendered.
            if any(s in low for s in (
                "? for shortcuts", "/effort", "bypass permissions",
                "accept edits", 'try "',
            )):
                time.sleep(0.5)   # settle
                return
            time.sleep(1.0)
        raise ClaudePInvocationError(
            f"[{step_name}] interactive claude did not become ready within "
            f"{BOOT_TIMEOUT_SEC:.0f}s. Last pane:\n" + _tail(self.capture(target), 20)
        )

    def send_directive(self, target: str, directive: str) -> None:
        """Type a one-line directive into the pane and submit it."""
        # Literal text first (no shell interpretation), then Enter as its own
        # key event so the buffered text is what gets submitted.
        _tmux("send-keys", "-t", target, "-l", directive)
        time.sleep(0.4)
        _tmux("send-keys", "-t", target, "Enter")

    def capture(self, target: str) -> str:
        """Capture recent pane content (visible + a little scrollback)."""
        cp = _tmux("capture-pane", "-t", target, "-p",
                   "-S", f"-{CAPTURE_SCROLLBACK}", check=False)
        return cp.stdout or ""

    def spool_for(self, step_name: str) -> Path | None:
        """The hook-event spool dir for a step (None if streaming is off)."""
        return self._spools.get(step_name)

    def teardown(self) -> None:
        if self._session_created:
            _tmux("kill-session", "-t", self.session, check=False)
            self._session_created = False
            LOG.info(f"[tmux] killed session {self.session!r}")
        for spool in self._spools.values():
            shutil.rmtree(spool, ignore_errors=True)
        self._spools.clear()


def _shq(s: str) -> str:
    """Minimal shell-quote for a single arg embedded in the new-window command.

    tmux runs the new-window command through the user's shell, so any glob
    metacharacter must be quoted. Notably model names like ``opus[1m]`` contain
    ``[ ]`` which zsh treats as a glob pattern (→ "no matches found") — so the
    safe-char set deliberately EXCLUDES brackets, forcing them to be quoted.
    """
    if re.fullmatch(r"[A-Za-z0-9_./:=,-]+", s):
        return s
    return "'" + s.replace("'", "'\\''") + "'"


def _tail(text: str, n: int) -> str:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines[-n:])


# ── tool-streaming via Claude Code hooks ──────────────────────────────────────
def _hook_settings_json(spool: Path, guard: tuple[Path, Path] | None = None) -> str:
    """Inline ``--settings`` JSON registering PreToolUse + PostToolUse hooks that
    run ``_tmux_tool_hook.py <spool>`` (this interpreter), spooling each tool
    boundary for the backend to drain into the run's EventSink.

    When ``guard`` (``(case_root, export)``) is given — calibration — the hook
    also acts as the PreToolUse permission authority (``--guard <case_root>
    <export>``), silently allowing sandbox tools + scoped file-I/O and denying
    everything else (reproducing the headless jail so an unattended pane never
    blocks on a permission prompt)."""
    parts = [sys.executable, str(HOOK_SCRIPT), str(spool)]
    if guard is not None:
        parts += ["--guard", str(guard[0]), str(guard[1])]
    cmd = " ".join(shlex.quote(p) for p in parts)
    entry = [{"matcher": "*", "hooks": [{"type": "command", "command": cmd}]}]
    return json.dumps({"hooks": {"PreToolUse": entry, "PostToolUse": entry}})


def _stringify_tool_response(resp) -> str:
    """Collapse a PostToolUse tool_response into a single string for the UI."""
    if resp is None:
        return ""
    if isinstance(resp, str):
        return resp
    if isinstance(resp, dict):
        for k in ("output", "stdout", "content", "text", "result"):
            v = resp.get(k)
            if isinstance(v, str) and v:
                return v
        try:
            return json.dumps(resp, default=str)
        except Exception:
            return str(resp)
    return str(resp)


def _forward_hook_event(ev: dict, sink, stage: str) -> None:
    """Map one spooled hook event to an EventSink emit (tool use / result)."""
    name = ev.get("hook_event_name")
    agent = ev.get("agent_type")   # present only for sub-agent tool calls
    if name == "PreToolUse":
        payload = {
            "name": ev.get("tool_name", ""),
            "input": ev.get("tool_input", {}),
            "id": ev.get("tool_use_id", ""),
        }
        if agent:
            payload["agent_type"] = agent
        sink.emit(stage, EVENT_STAGE_TOOL_USE, payload)
    elif name == "PostToolUse":
        content = _stringify_tool_response(ev.get("tool_response"))
        if len(content) > 4096:
            content = content[:4096] + f"… (truncated; {len(content)} bytes)"
        payload = {"tool_use_id": ev.get("tool_use_id", ""), "content": content}
        if agent:
            payload["agent_type"] = agent
        sink.emit(stage, EVENT_STAGE_TOOL_RESULT, payload)


def _drain_spool(spool: Path | None, sink, stage: str) -> None:
    """Forward + delete every spooled hook event (sorted = chronological).

    Filenames are ``<ns>_<pid>_<rand>.json`` so a lexical sort is chronological.
    Tolerates a partial write (skips unparseable files, leaves a stray .tmp).
    """
    if spool is None or sink is None:
        return
    try:
        files = sorted(spool.glob("*.json"))
    except Exception:
        return
    for fp in files:
        try:
            ev = json.loads(fp.read_text())
        except Exception:
            try:
                fp.unlink()
            except OSError:
                pass
            continue
        try:
            _forward_hook_event(ev, sink, stage)
        except Exception as e:
            LOG.debug(f"[tmux] forward hook event failed: {e}")
        try:
            fp.unlink()
        except OSError:
            pass


# ── reasoning text (best-effort pane scrape; no hook exists for it) ───────────
# The interactive TUI prints assistant text turns prefixed with the ● bullet;
# tool invocations share that bullet but look like `Name(args)`. We forward the
# NON-tool bullet lines as reasoning (tool calls already come from the hooks).
_BULLET = "⏺"   # ● record bullet the TUI uses for assistant turns
_TOOL_LINE_RE = re.compile(r"^[A-Z][\w .\-/]*\(")   # e.g. "Bash(", "Web Search("


def _scrape_reasoning(orch: "TmuxOrchestra", target: str, sink, stage: str,
                      seen: set) -> None:
    """Emit STAGE_TEXT_TURN for new assistant reasoning lines seen in the pane."""
    if sink is None:
        return
    try:
        text = orch.capture(target)
    except Exception:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if _BULLET not in line:
            continue
        rest = line.split(_BULLET, 1)[1].strip()
        if not rest or _TOOL_LINE_RE.match(rest):
            continue   # blank or a tool invocation (covered by hooks)
        if len(rest) < 4:
            continue
        key = rest[:160]
        if key in seen:
            continue
        seen.add(key)
        try:
            sink.emit(stage, EVENT_STAGE_TEXT_TURN, {"text": rest})
        except Exception:
            pass
    if len(seen) > 400:        # bound memory across a long step
        seen.clear()


# ── one orchestra per process (one orchestrator run) ──────────────────────────
_ORCHESTRA: TmuxOrchestra | None = None


def _get_orchestra() -> TmuxOrchestra:
    global _ORCHESTRA
    if _ORCHESTRA is None:
        cwd = get_default_cwd() or Path.cwd()
        session = os.environ.get("FRAUD_INV_TMUX_SESSION") or f"npfraud-{os.getpid()}"
        _ORCHESTRA = TmuxOrchestra(session, Path(cwd))
    return _ORCHESTRA


# ── directives ────────────────────────────────────────────────────────────────
def _run_directive(prompt_path: Path, result_path: Path, done_path: Path) -> str:
    return (
        f"Read and follow the instructions in {prompt_path} exactly. "
        f"When finished, write ONLY the JSON result (no prose, no code fence) to "
        f"{result_path}, then create an empty file {done_path}. "
        f"Print nothing else to me."
    )


def _repair_directive(result_path: Path, done_path: Path, error_msg: str) -> str:
    return (
        f"The JSON you wrote to {result_path} failed schema validation: "
        f"{error_msg[:1000]} "
        f"Do NOT re-run the task. Re-write {result_path} with the SAME content and "
        f"ONLY the flagged fields corrected, then re-create the empty file "
        f"{done_path}. Output nothing else."
    )


def _finalize_directive(result_path: Path, done_path: Path) -> str:
    """Sent near the deadline — convert ran-out-of-time into a clean partial
    result (mirrors claude_p's error_max_turns finalize).

    Deliberately CALM, task-anchored wording: an urgency-laden "STOP now /
    immediately / output nothing else" phrasing reads like a prompt-injection
    and a security-conscious agent will (correctly) refuse it. This frames it as
    a normal wrap-up referencing the agent's own instructions + schema.
    """
    return (
        "Please wrap up this step now and record your conclusions. Based on the "
        "evidence you have already gathered (you do not need to run further tools "
        "or sub-agents), write your final JSON result — matching the schema in the "
        f"instructions you were given — to {result_path}, then create the empty "
        f"file {done_path}. If any line of inquiry is unfinished, note it briefly "
        "in your notes / open-questions field so it can be continued next round."
    )


# ── calibration: reproduce the clean-room jail at the pane level ──────────────
def _case_root(out_dir: Path) -> Path:
    """The run's case-root dir (``CALIBRATION-*`` / ``CASE-*``) — the common
    ancestor of every per-step dir (``iter-NN``, ``adv-rN``, …). A persistent
    cal pane is granted Read/Write scope here once, so reuse across iterations
    doesn't fall outside the granted scope. Falls back to out_dir if no such
    ancestor is found (keeps the scope no wider than the run's own tree)."""
    p = out_dir.resolve()
    for anc in (p, *p.parents):
        if anc.name.startswith(("CALIBRATION-", "CASE-")):
            return anc
    return p


def _calibration_pane_flags(extra_args: list[str] | None, out_dir: Path) -> list[str]:
    """Build a calibration pane's launch flags from the per-step
    --allowed-tools / --disallowed-tools that claude_p put in extra_args.

    Adds Read+Write scope on the CASE ROOT (the ``CALIBRATION-*``/``CASE-*`` dir
    that holds every ``iter-NN``/``adv-rN`` step dir), which the file-based I/O
    contract needs: the agent reads its prompt and writes result.json + the done
    marker there. We scope to the case root, not the per-step out_dir, because a
    cal pane is PERSISTENT and reused across iterations — if scoped to iter-01's
    dir, an iter-02 ``touch …/iter-02/X.done`` falls outside the granted scope
    and acceptEdits can no longer auto-approve it (it prompts, hanging the
    unattended pane — observed 2026-06-15). This is leak-safe: the case root
    holds only the run's OWN artifacts (all watchdog-scanned), and the globbed
    ``Read(<case_root>/**)`` blocks ``../`` traversal back into the repo's answer
    keys (same mechanism the headless jail relies on). The export stays the data
    source via the inherited ``Read(<export>/**)`` + cwd.
    """
    allowed = disallowed = ""
    args = list(extra_args or [])
    for i, a in enumerate(args):
        if a in ("--allowed-tools", "--allowedTools") and i + 1 < len(args):
            allowed = args[i + 1]
        elif a in ("--disallowed-tools", "--disallowedTools") and i + 1 < len(args):
            disallowed = args[i + 1]
    od = str(_case_root(out_dir))
    io_scope = f"Read({od}/**),Write({od}/**)"
    allowed = f"{allowed},{io_scope}" if allowed else io_scope
    flags = ["--permission-mode", CAL_PANE_PERMISSION_MODE,
             "--allowedTools", allowed, "--add-dir", od]
    if disallowed:
        flags += ["--disallowedTools", disallowed]
    return flags


# ── console events (best-effort; lower fidelity than the streaming backend) ────
def _emit(sink, stage: str, type_: str, payload: dict | None = None) -> None:
    if sink is None:
        return
    try:
        sink.emit(stage, type_, payload or {})
    except Exception as e:   # never let telemetry break a run
        LOG.debug(f"[tmux] event emit failed ({type_}): {e}")


# ── the public execution primitive ────────────────────────────────────────────
def run_step(
    *,
    prompt: str,
    schema: "type[T] | None",
    out_dir: Path,
    step_name: str,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    claude_bin: str = DEFAULT_CLAUDE_BIN,   # accepted for signature parity; unused
    extra_args: list[str] | None = None,    # in calibration: per-step --allowed/--disallowed-tools
    session_id: str | None = None,          # continuity is implicit in the persistent pane
    is_continuation: bool = False,
    event_sink: object | None = None,
    stage_name: str | None = None,
    calibration: bool = False,              # launch the pane under the clean-room jail
) -> "T | str":
    """Execute one agent step in an interactive tmux pane. Drop-in replacement
    for ``claude_p.invoke_agent`` — same artifacts, same validated return."""
    out_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = (out_dir / f"{step_name}.prompt.md").resolve()
    result_path = (out_dir / f"{step_name}.result.json").resolve()
    done_path = (out_dir / f"{step_name}.done").resolve()
    parsed_path = out_dir / f"{step_name}.parsed.json"
    meta_path = out_dir / f"{step_name}.meta.json"
    stage = stage_name or step_name

    # Calibration: translate the per-step --allowed/--disallowed-tools in
    # extra_args into the pane's clean-room jail flags (+ Read/Write scope on
    # out_dir for the file-based I/O). Outside calibration, extra_args isn't
    # actionable in an interactive pane.
    cal_launch_flags = _calibration_pane_flags(extra_args, out_dir) if calibration else None
    if extra_args and not calibration:
        LOG.warning(f"[{step_name}] tmux backend ignores extra_args outside "
                    f"calibration mode: {extra_args}")

    # Floor the per-step timeout: interactive panes aren't turn-bounded, so the
    # headless 600s default kills healthy long-running passes (see constant note).
    timeout_sec = max(timeout_sec, TMUX_MIN_STEP_TIMEOUT_SEC)

    # Cache-load — identical semantics to invoke_agent (the resume primitive).
    # Uses the shared _load_cached_parsed so schema=None (free-text steps like
    # case_writer) NEVER cache-loads — an unguarded schema.model_validate_json with
    # schema=None raised None.model_validate_json and crashed the final step (the
    # #316/#318 bug class, previously only fixed in claude_p + streaming).
    if os.environ.get("FRAUD_INV_NO_CACHE_LOAD") != "1":
        cached = _load_cached_parsed(schema, parsed_path)
        if cached is not None:
            LOG.info(f"[{step_name}] CACHED — loaded existing {parsed_path.name}")
            _emit(event_sink, stage, "STAGE_START", {"cached": True})
            _emit(event_sink, stage, "STAGE_COMPLETE", {"cached": True})
            if DISPLAY_PARSED_JSON:
                _display_parsed_json(step_name, cached, 0.0)
            return cached

    model = _resolve_model_for_step(step_name)
    effort = _resolve_effort_for_step(step_name)

    orch = _get_orchestra()
    # In calibration the hook also enforces the permission jail (silent
    # allow/deny — interactive panes can't auto-deny like headless), so it must
    # be injected even when no EventSink wants streaming. Scope = (case root,
    # export cwd): the only dirs the agent may read/write.
    cal_guard = (_case_root(out_dir), orch.cwd) if calibration else None
    target = orch.ensure_pane(step_name, model, effort,
                              stream_events=(event_sink is not None) or calibration,
                              cal_launch_flags=cal_launch_flags,
                              cal_guard=cal_guard)
    spool = orch.spool_for(step_name)

    prompt_path.write_text(prompt)
    attempts: list[dict] = []
    _emit(event_sink, stage, "STAGE_START",
          {"backend": "tmux", "model": model or "<inherit>", "effort": effort or "<inherit>"})

    for attempt in range(1, MAX_PARSE_RETRIES + 1):
        # Clear stale I/O so the poll waits for THIS attempt's fresh marker.
        for p in (result_path, done_path):
            try:
                p.unlink()
            except FileNotFoundError:
                pass

        if attempt == 1:
            directive = _run_directive(prompt_path, result_path, done_path)
        # (repair directive set at the end of the previous iteration)
        started = datetime.utcnow()
        LOG.info(f"[{step_name}] attempt {attempt}/{MAX_PARSE_RETRIES} "
                 f"(tmux pane {target}, model={model or '<inherit>'})")
        orch.send_directive(target, directive)

        got_marker = _poll_for_marker(
            orch, target, done_path, result_path, timeout_sec, step_name,
            event_sink, stage, spool,
        )
        # Final flush: forward any tool events spooled between the last poll
        # and the done-marker so the console sees the step's complete trace.
        _drain_spool(spool, event_sink, stage)
        finished = datetime.utcnow()
        rec = {
            "attempt": attempt,
            "started": started.isoformat(),
            "finished": finished.isoformat(),
            "duration_sec": (finished - started).total_seconds(),
            "backend": "tmux",
            # Resolved routing (parity with the claude_p/streaming meta.json).
            "model": model,
            "effort": effort,
        }

        if not got_marker:
            rec["error"] = f"no done-marker within {timeout_sec}s"
            attempts.append(rec)
            meta_path.write_text(json.dumps(_step_meta_payload(step_name, attempts), indent=2))
            _emit(event_sink, stage, "STAGE_FAILED", {"error": rec["error"]})
            raise ClaudePInvocationError(
                f"[{step_name}] tmux pane produced no result within {timeout_sec}s. "
                f"Last pane:\n" + _tail(orch.capture(target), 25)
            )

        raw = _read_result(result_path)
        cleaned = _strip_code_fence(raw)
        try:
            # Shared coerce: schema=None (free-text, e.g. case_writer) writes the raw
            # document + returns str; otherwise validate + write the model dump. This
            # is what fixes the case_writer crash on the tmux backend (#316/#318 class).
            result = _coerce_and_persist(cleaned, schema, parsed_path)
        except (ValidationError, ValueError) as e:
            rec["error"] = f"schema-validation: {type(e).__name__}"
            rec["validation_error"] = str(e)[:2000]
            attempts.append(rec)
            meta_path.write_text(json.dumps(_step_meta_payload(step_name, attempts), indent=2))
            if attempt >= MAX_PARSE_RETRIES:
                break
            LOG.warning(f"[{step_name}] result.json failed validation; "
                        f"sending repair directive (attempt {attempt} → {attempt+1})")
            directive = _repair_directive(result_path, done_path, str(e))
            continue

        # Success — _coerce_and_persist already wrote parsed.json (raw text for a
        # free-text schema=None step, else the model dump).
        rec["status"] = "ok"
        attempts.append(rec)
        meta_path.write_text(json.dumps(_step_meta_payload(step_name, attempts), indent=2))
        LOG.info(f"[{step_name}] attempt {attempt} OK ({rec['duration_sec']:.1f}s)")
        _emit(event_sink, stage, "STAGE_COMPLETE",
              {"backend": "tmux", "duration_sec": round(rec["duration_sec"], 1)})
        if schema is not None and DISPLAY_PARSED_JSON:
            _display_parsed_json(step_name, result, rec["duration_sec"])
        return result

    # Retries exhausted
    meta_path.write_text(json.dumps(_step_meta_payload(step_name, attempts), indent=2))
    _emit(event_sink, stage, "STAGE_FAILED", {"error": "retries exhausted"})
    raise ClaudePInvocationError(
        f"[{step_name}] failed after {MAX_PARSE_RETRIES} attempts (tmux). See {meta_path}."
    )


def _poll_for_marker(
    orch: TmuxOrchestra,
    target: str,
    done_path: Path,
    result_path: Path,
    timeout_sec: int,
    step_name: str,
    event_sink: object | None,
    stage: str,
    spool: Path | None = None,
) -> bool:
    """Poll for the done-marker. Returns True on success, False on timeout.

    Each poll (~POLL_INTERVAL_SEC) it drains the hook spool → STAGE_TOOL_USE /
    STAGE_TOOL_RESULT events (real-time, complete, incl. sub-agents) and scrapes
    new assistant reasoning lines → STAGE_TEXT_TURN. Every HEARTBEAT_EVERY_SEC it
    also scans the pane for the billing-wall signature (raises
    ClaudePHardStopError — resumable) and the claude-exited sentinel. Transient
    API blips are handled by the interactive session itself (it auto-retries),
    so we simply keep waiting.
    """
    # Nudge the agent to finalize AT the nominal budget, then keep polling for
    # FINALIZE_SALVAGE_SEC more — interactive turns are unbounded and the agent
    # often needs several minutes to wrap up after the nudge; accepting that
    # late-but-complete result beats discarding finished work (2026-06-16).
    nudge_at = time.time() + timeout_sec
    hard_deadline = nudge_at + FINALIZE_SALVAGE_SEC
    last_hb = 0.0
    seen_text: set = set()
    nudged = False
    while time.time() < hard_deadline:
        # Stream tool calls/results + reasoning every poll (low latency).
        _drain_spool(spool, event_sink, stage)
        _scrape_reasoning(orch, target, event_sink, stage, seen_text)
        if done_path.exists():
            return True
        if not nudged and time.time() >= nudge_at:
            LOG.warning(f"[{step_name}] reached {timeout_sec:.0f}s budget — sending "
                        f"finalize nudge (+{FINALIZE_SALVAGE_SEC:.0f}s salvage window "
                        f"to write its JSON)")
            orch.send_directive(target, _finalize_directive(result_path, done_path))
            _emit(event_sink, stage, "STAGE_PROGRESS", {"kind": "finalize_nudge"})
            nudged = True
        now = time.time()
        if now - last_hb >= HEARTBEAT_EVERY_SEC:
            text = orch.capture(target)
            if is_hard_stop_failure(text):
                _emit(event_sink, stage, "STAGE_FAILED", {"error": "ORG_SPEND_LIMIT"})
                raise ClaudePHardStopError(_hard_stop_message(step_name))
            if _EXIT_SENTINEL in text:
                _emit(event_sink, stage, "STAGE_FAILED", {"error": "claude exited"})
                raise ClaudePInvocationError(
                    f"[{step_name}] interactive claude exited mid-step:\n" + _tail(text, 20)
                )
            _emit(event_sink, stage, "STAGE_PROGRESS", {"kind": "heartbeat"})
            last_hb = now
        time.sleep(POLL_INTERVAL_SEC)
    return done_path.exists()


def _read_result(result_path: Path, retries: int = 5) -> str:
    """Read the result file, tolerating a brief marker-before-flush race."""
    for _ in range(retries):
        try:
            txt = result_path.read_text()
            if txt.strip():
                return txt
        except FileNotFoundError:
            pass
        time.sleep(0.4)
    # Last attempt (let a genuine empty/missing file surface as a validation error)
    try:
        return result_path.read_text()
    except FileNotFoundError:
        return ""
