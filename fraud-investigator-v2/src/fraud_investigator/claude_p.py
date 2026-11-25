"""Subprocess wrapper for ``claude -p`` (Claude Code headless mode).

One call per agent step. Each invocation:
  1. Writes the rendered prompt to ``state/{case_id}/{step}/prompt.md``.
  2. Spawns ``claude -p`` with ``--output-format json``.
  3. Captures stdout + stderr to the same directory.
  4. Extracts the model's textual response from the wrapper JSON.
  5. Strips markdown code fences (``` ```json``) and parses the inner JSON.
  6. Validates against the caller's Pydantic schema; retries on validation
     errors up to ``MAX_PARSE_RETRIES`` (re-prompts with the error message).

The orchestrator holds all chat state. Each ``claude -p`` invocation sees
only its own prompt — no carryover context.

Tools: each subprocess inherits the CWD's ``.mcp.json`` and ``settings.json``.
Register CourtListener MCP at the project level (``./.mcp.json``) so all
agents get it.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError

LOG = logging.getLogger("fraud_investigator.claude_p")

MAX_PARSE_RETRIES = 3

# Transient API / network failures: claude -p exits 1 but the cause is an
# upstream blip (socket dropped mid-stream, model overloaded, rate-limit),
# NOT a bad prompt. These are RETRYABLE by simply resuming the session after
# a backoff — distinct from error_max_turns (needs a focused finalize prompt)
# and from parse errors (needs the error appended). A 2026-06-12 Maple
# run lost ~1h40m of work when counsel_rebuttal hit "socket connection was
# closed unexpectedly" and hard-raised on the first attempt.
import re as _re
_TRANSIENT_API_RE = _re.compile(
    r"socket connection was closed|connection (?:was )?closed unexpectedly|"
    r"connection reset|ECONNRESET|read timed out|broken pipe|"
    r"overloaded|\b429\b|\b5(?:00|02|03|04|29)\b|rate.?limit|"
    r"internal server error|service unavailable|temporarily unavailable|"
    r"API Error|api_error|connection error",
    _re.IGNORECASE,
)


def is_transient_api_failure(*texts: str) -> bool:
    """True if any text carries a transient-API/network signature (retryable
    by resuming the session). Shared by both invoke paths."""
    return any(t and _TRANSIENT_API_RE.search(t) for t in texts)


def transient_backoff_sec(attempt: int) -> float:
    """Backoff before resuming after a transient API failure — the upstream
    needs a moment to recover, so longer than a parse-retry. attempt is the
    one that JUST failed (1-based)."""
    import random
    return min(30.0, 3.0 * (2 ** (attempt - 1))) + random.uniform(0, 1.0)


# Hard-stop failures: claude -p exits 1 but the cause is a BILLING wall, not a
# transient blip — the org has hit its monthly spend limit (a hard ceiling an
# admin must raise at claude.ai/admin). Retrying is futile and wasteful: every
# resume hits the same wall. WORSE, the spend-limit kill surfaces as a result
# event with subtype "error_during_execution" (a "recoverable" subtype) AND is
# NOT matched by the transient detector, so without this guard the streaming
# path burned all 3 retries spinning on it before failing (Bradley
# CASE-6A2D49E8-2026 adv-r3 defense, 2026-06-13). We classify it FIRST — before
# both the max-turns/recoverable-subtype retry and the transient retry — and
# fail fast with a clear, resume-able error.
_HARD_STOP_RE = _re.compile(
    r"monthly spend limit|spend limit|claude\.ai/admin|"
    r"organization (?:has )?reached its .*limit",
    _re.IGNORECASE,
)


def is_hard_stop_failure(*texts: str) -> bool:
    """True if any text carries an org-spend-limit (billing-wall) signature.

    A hard stop is NON-retryable: an admin must raise the cap at
    claude.ai/admin. Distinct from `is_transient_api_failure` (network blip,
    resume-retryable) and error_max_turns (focused finalize). Checked before
    both retry branches in both invoke paths."""
    return any(t and _HARD_STOP_RE.search(t) for t in texts)

# Resolve claude binary: prefer env var, then PATH lookup, then known
# desktop-app install location.
_DESKTOP_APP_BIN_GLOB = (
    "/Users/*/Library/Application Support/Claude/claude-code/*/claude.app/Contents/MacOS/claude"
)


def _resolve_claude_bin() -> str:
    """Find the claude binary or return 'claude' (PATH fallback)."""
    env_val = os.environ.get("CLAUDE_BIN", "").strip()
    if env_val:
        return env_val
    on_path = shutil.which("claude")
    if on_path:
        return on_path
    # Last-ditch: glob for the bundled desktop-app binary
    import glob
    matches = sorted(glob.glob(_DESKTOP_APP_BIN_GLOB), reverse=True)
    if matches:
        return matches[0]
    return "claude"  # will fail on subprocess.run with FileNotFoundError


DEFAULT_CLAUDE_BIN = _resolve_claude_bin()
DEFAULT_TIMEOUT_SEC = int(os.environ.get("CLAUDE_TIMEOUT_SEC", "600"))


# ─────────────────────────────────────────────────────────────────────────────
# Per-step model selection
# ─────────────────────────────────────────────────────────────────────────────
#
# Each `claude -p` subprocess can use a different model via --model. Defaults
# below balance cost/latency against the cognitive load of each step:
#
#   - Tool-heavy + reasoning           → "sonnet" (best agentic tool reliability)
#   - Strategy / adversarial / gating reasoning (counsel_*, defense,
#     foreclosure_check, legal_adv_response) → "opus". History: disposition
#     started haiku → post-iter-1 Maple rerun showed haiku mis-classified
#     count-level convergence (declared READY against floors the orchestrator
#     had to override) → sonnet → later opus, since these are the decision
#     gates that control whether a case advances; each runs ≤6 times per case.
#   - Synthesis/writing                → "sonnet" (case_writer)
#
# Override per step via env: FRAUD_INV_MODEL_<STEP_NAME_UPPER>
#   (e.g., FRAUD_INV_MODEL_COUNSEL_DISPOSITION=opus)
# Override all steps via env: FRAUD_INV_MODEL_DEFAULT
#   (applied when no per-step env is set; still overridable per-step)
# Leave both empty to skip --model entirely (inherits from CLI config).
#
# Setting a step's model to the empty string explicitly disables --model for
# that step (useful if env-default is set but you want one step to inherit).
# MODEL ROUTING (2026-07-01, revised). Rule: **Sonnet 5 for querying, Opus for
# reasoning.** Tool-bound EXECUTION (evidence gathering) + final synthesis run on
# Sonnet 5 (`claude-sonnet-5[1m]`); the STRATEGY / adversarial / gating reasoning
# steps — where case quality is made — stay on Opus (`opus[1m]`). `[1m]` selects the
# 1M-context variant where context headroom helps.
# History: briefly moved ALL steps to Sonnet 5 on release day, then reverted the
# reasoning gates to Opus per the query-vs-reason rule (the old Sonnet under-found
# categorical fraud vs Opus on these gates; not yet re-measured on Sonnet 5 — a
# theory_match re-validation would let the gates move to Sonnet 5 too).
# state/model_overrides.json OVERRIDES this map (see _resolve_model_for_step).
# NOTE: `[1m]` requires 1M-context entitlement; without it the CLI falls back to
# the 200K variant of the same model.
MODEL_BY_STEP: dict[str, str] = {
    # Investigator-side (EXECUTION) — tool-bound querying; Sonnet 5, big context.
    "investigator_discover":      "claude-sonnet-5[1m]",
    "investigator":               "claude-sonnet-5[1m]",
    "rebuttal_investigator":      "claude-sonnet-5[1m]",  # Phase B, targeted on weak_links
    # Strategist + adversarial REASONING (STRATEGY) — Opus; the case-shipping gates.
    "counsel_build":              "claude-opus-5[1m]",   # case theory, count framing, follow-up Qs
    "counsel_first_chair_review": "claude-opus-5[1m]",   # Phase A partner gating
    "counsel_rebuttal":           "claude-opus-5[1m]",   # adversarial reasoning + adverse precedent
    "counsel_disposition":        "claude-opus-5[1m]",   # highest-leverage: case_ready vs abandon
    "defense":                    "claude-opus-5[1m]",   # creative motion-to-dismiss
    "foreclosure_check":          "claude-opus-5[1m]",   # Guardrail 3: steelman + foreclose innocent explanations
    "legal_adv_response":         "claude-opus-5",       # per-attack disposition reasoning
    # Synthesis — Sonnet 5 (full case state is large; 1M context keeps it whole).
    "case_writer":                "claude-sonnet-5[1m]",
}


# ── Extended-thinking effort level per step ─────────────────────────────────
# Anthropic models support extended thinking via `claude -p --effort <level>`.
# Levels: low (~1K thinking tokens), medium (~4K), high (~8K), xhigh (~16K),
# max (~32K+ / model cap). Tradeoff: cost + latency scale with thinking budget;
# quality gains concentrate on reasoning-heavy tasks.
#
# Selection principle:
#   - Tool-bound agents (investigator-side) — medium is sufficient; high is wasted budget
#   - Legal reasoning agents (counsel_*, defense, legal_adv_response) — high/xhigh
#     because reasoning quality directly affects case-shipping decisions
#   - The single highest-leverage steps (counsel_disposition, defense) get xhigh —
#     these are where bad reasoning is most expensive (ship a weak case OR abandon
#     a strong one)
#   - Synthesis / mechanical (case_writer, triage) — medium / low
#
# Set per-step via env: FRAUD_INV_EFFORT_<STEP_UPPER>=<level>
# Set global override: FRAUD_INV_EFFORT_DEFAULT=<level>
# Empty string = explicitly skip the --effort flag (inherit claude CLI default)
# PRODUCTION-VALIDATED effort budgets (the levels our filed-case runs used). Most
# steps run `max`; the two gates that are already the most expensive per-call
# (first_chair, disposition) run `high` to keep latency sane. Baked in (was the
# per-machine override) so teammates match our fidelity. The CLI clamps an
# unsupported level to the highest the chosen model supports.
EFFORT_BY_STEP: dict[str, str] = {
    "investigator_discover":      "max",
    "investigator":               "max",
    "rebuttal_investigator":      "max",
    "counsel_build":              "max",
    "counsel_first_chair_review": "high",    # partner gating (high to cap latency)
    "counsel_rebuttal":           "max",
    "counsel_disposition":        "high",    # decision gate (high to cap latency)
    "defense":                    "max",     # creative attacks; depth = better defects
    "foreclosure_check":          "max",     # Guardrail 3 — plausibility test is the heart
    "legal_adv_response":         "max",
    "case_writer":                "high",    # synthesis, not reasoning; cap latency (observed max 55min + a 77min silent hang at "max")
}


# Persistent per-step overrides file. Edit state/model_overrides.json directly
# Provides a per-step knob without env vars or code edits. Schema::
#
#   {
#     "_default": {"model": "sonnet", "effort": "medium"},   # global defaults
#     "<step_name>": {"model": "opus", "effort": "high"},    # per-step
#     ...
#   }
#
# Any field absent/null/empty falls through to the next resolution layer.
_OVERRIDES_PATH = (
    Path(__file__).resolve().parents[3] / "state" / "model_overrides.json"
)


def _load_overrides() -> dict:
    """Load the persistent overrides file. Returns {} on any error.

    Cached for the duration of a single Python process (orchestrator load)
    so concurrent claude_p invocations don't thrash the filesystem. Reset
    by re-importing the module (Streamlit's reload behavior handles this
    for the GUI-side).
    """
    global _OVERRIDES_CACHE
    if _OVERRIDES_CACHE is not None:
        return _OVERRIDES_CACHE
    try:
        if _OVERRIDES_PATH.exists():
            _OVERRIDES_CACHE = json.loads(_OVERRIDES_PATH.read_text())
        else:
            _OVERRIDES_CACHE = {}
    except Exception:
        _OVERRIDES_CACHE = {}
    return _OVERRIDES_CACHE


_OVERRIDES_CACHE: dict | None = None


def _resolve_model_for_step(step_name: str) -> str | None:
    """Return the model name for ``claude -p --model``, or None to skip the flag.

    Resolution order:
      1. FRAUD_INV_MODEL_<STEP_UPPER> env var (per-step override)
      2. state/model_overrides.json[<step>][model] (GUI per-step override)
      3. FRAUD_INV_MODEL_DEFAULT env var (global default)
      4. state/model_overrides.json[_default][model] (GUI global default)
      5. MODEL_BY_STEP hardcoded default
      6. None (don't pass --model, inherit from claude CLI config)

    An empty string at any level means "explicitly inherit" (skip --model).

    PROFILE OVERRIDE (highest precedence): FRAUD_INV_PROFILE=draft forces every
    step to bare `sonnet` (200K) for cheap orchestration smoke-testing — placed
    ABOVE the per-step env / overrides.json layers so it wins regardless of a
    diverged state/model_overrides.json. Unset / "production" → normal defaults.

    FRAUD_INV_PROFILE=battery pins every step to the PRODUCTION MODEL_BY_STEP
    split (Sonnet-5 querying / Opus reasoning gates) at the same highest
    precedence — for calibration-battery runs that must exercise the real
    reasoning gates (incl. the REAL defense red team; see
    Orchestrator.defense_as_mock) regardless of a diverged overrides.json or
    stray env overrides. Differs from draft in WHAT it pins, not how.
    """
    profile = os.environ.get("FRAUD_INV_PROFILE", "").strip().lower()
    if profile == "draft":
        return "sonnet"
    if profile == "battery":
        return MODEL_BY_STEP.get(step_name)
    per_step_env = os.environ.get(f"FRAUD_INV_MODEL_{step_name.upper()}")
    if per_step_env is not None:
        return per_step_env or None
    ov = _load_overrides()
    step_ov = ov.get(step_name, {}).get("model")
    if step_ov:
        return step_ov
    global_env = os.environ.get("FRAUD_INV_MODEL_DEFAULT")
    if global_env is not None:
        return global_env or None
    default_ov = ov.get("_default", {}).get("model")
    if default_ov:
        return default_ov
    return MODEL_BY_STEP.get(step_name)


def _resolve_effort_for_step(step_name: str) -> str | None:
    """Return the ``--effort`` value for a step, or None to skip the flag.

    Resolution order mirrors `_resolve_model_for_step`:
      1. FRAUD_INV_EFFORT_<STEP_UPPER> env var
      2. state/model_overrides.json[<step>][effort]
      3. FRAUD_INV_EFFORT_DEFAULT env var
      4. state/model_overrides.json[_default][effort]
      5. EFFORT_BY_STEP hardcoded default
      6. None (inherit claude CLI default)

    Valid values: ``low``, ``medium``, ``high``, ``xhigh``, ``max``.

    PROFILE OVERRIDE (highest precedence): FRAUD_INV_PROFILE=draft forces every
    step to `medium` effort (cheap orchestration smoke-testing), mirroring the
    draft model override in `_resolve_model_for_step`.

    FRAUD_INV_PROFILE=battery pins every step to the PRODUCTION EFFORT_BY_STEP
    budgets (mirroring the battery model override above).
    """
    profile = os.environ.get("FRAUD_INV_PROFILE", "").strip().lower()
    if profile == "draft":
        return "medium"
    if profile == "battery":
        return EFFORT_BY_STEP.get(step_name)
    per_step_env = os.environ.get(f"FRAUD_INV_EFFORT_{step_name.upper()}")
    if per_step_env is not None:
        return per_step_env or None
    ov = _load_overrides()
    step_ov = ov.get(step_name, {}).get("effort")
    if step_ov:
        return step_ov
    global_env = os.environ.get("FRAUD_INV_EFFORT_DEFAULT")
    if global_env is not None:
        return global_env or None
    default_ov = ov.get("_default", {}).get("effort")
    if default_ov:
        return default_ov
    return EFFORT_BY_STEP.get(step_name)

# Flags every claude -p subprocess needs to run headless with full tool access.
# Without these the subprocess hangs on permission prompts that have no
# interactive UI to answer them.
#
# --dangerously-skip-permissions      : bypass all permission checks (the help
#                                       text recommends this for sandboxes; our
#                                       cleanroom CWD is exactly that)
# --setting-sources user,project,local: load .mcp.json + .env from the CWD so
#                                       CourtListener MCP is registered
# --max-turns 40                      : safety net on runaway tool-call loops.
#                                       Typical agent runs need 10-15 turns;
#                                       40 catches the pathological case where
#                                       a parse-retry-error feedback loops the
#                                       agent without converging.
DEFAULT_HEADLESS_ARGS = [
    "--dangerously-skip-permissions",
    "--setting-sources", "user,project,local",
]

# ── SAFE-PERMS launch mode (FRAUD_INV_SAFE_PERMS=1) ─────────────────────────
# Run every claude -p subprocess under `--permission-mode default` + a broad
# allowlist INSTEAD of `--dangerously-skip-permissions`. The only operational
# reason to do this: the claude CLI's nested-autonomy guard refuses to spawn a
# `--dangerously-skip-permissions` subprocess from INSIDE a Claude Code session,
# so a production run can't be launched from within a session. Default mode does
# NOT trip that guard (verified 2026-06-14: a default-mode `claude -p` spawned
# from inside a session completed with permission_denials=[]), so a SAFE-PERMS
# run is launchable from a session while behaving identically to skip-perms for
# the loop's purposes.
#
# In headless default mode, any tool NOT on the allowlist is auto-DENIED (no
# interactive prompt to answer) — so we must inject a base allowlist covering
# every tool the loop uses. Verified there are no MCP tools in the production
# path (CourtListener is reached via scripts/courtlistener_search.py over Bash),
# so the standard tool set is the whole surface. Bare `Bash` grants all bash
# commands (the scoped `Bash(python3 …:*)` form is only needed to RESTRICT).
#
# An agent that ships its OWN --allowedTools (e.g. counsel_build's narrow
# pull-only set) is respected as-is — we do NOT widen it. Agents that ship a
# --disallowedTools denylist get this base allowlist with their denylist
# subtracting on top (default mode honors both; deny wins). Calibration mode has
# its own tighter flags and is left entirely untouched (it is mutually exclusive
# with SAFE-PERMS).
SAFE_PERMS = os.environ.get("FRAUD_INV_SAFE_PERMS", "0") == "1"
SAFE_PERMS_BASE_ALLOWED = "Bash,Read,Write,Edit,Grep,Glob,WebSearch,WebFetch,LS,TodoWrite"

# ── Execution backend toggle (FRAUD_INV_EXEC_BACKEND) ───────────────────────
# "claude_p" (default): each agent step is a `claude -p` headless subprocess —
# bills the separate Agent-SDK credit (post 2026-06-15 billing split).
# "tmux": each step runs in an interactive `claude` session driven inside a tmux
# pane (see tmux_backend.py) — bills the user's personal interactive limits.
# The toggle is read in invoke_agent; the orchestrator, schemas, caching, and
# event model are identical across both backends.
EXEC_BACKEND = os.environ.get("FRAUD_INV_EXEC_BACKEND", "claude_p").strip().lower()


def _cmd_has_allowlist(cmd: list[str]) -> bool:
    """True if the command already carries an --allowedTools / --allowed-tools
    flag (an agent that scoped its own tool access — don't widen it)."""
    return any(
        isinstance(a, str) and a.split("=", 1)[0] in ("--allowedTools", "--allowed-tools")
        for a in cmd
    )


def _apply_safe_perms(cmd: list[str]) -> list[str]:
    """Convert a skip-permissions command to default-mode + broad allowlist.

    No-op unless FRAUD_INV_SAFE_PERMS=1. Strips --dangerously-skip-permissions,
    forces --permission-mode default, and injects SAFE_PERMS_BASE_ALLOWED unless
    the agent already scoped its own allowlist. Callers must NOT apply this in
    calibration mode (calibration owns its flags)."""
    if not SAFE_PERMS:
        return cmd
    cmd = [a for a in cmd if a != "--dangerously-skip-permissions"]
    if "--permission-mode" not in cmd:
        cmd.extend(["--permission-mode", "default"])
    if not _cmd_has_allowlist(cmd):
        cmd.extend(["--allowedTools", SAFE_PERMS_BASE_ALLOWED])
    return cmd

# Per-step --max-turns budgets. The investigator was hitting 39.7/40 turns in
# the prior abandoned run — running away with tool calls. Tighter budgets force
# the agents to consolidate. Numbers chosen from the prior-run usage telemetry
# (avg turns observed × 1.3 headroom):
#
#   investigator         prior avg 39.7, capped 40 — cap 25 (target 8-12)
#   counsel_build        prior avg 17.8 — cap 18 (consolidation; mostly cite
#                                              verification, much of which is
#                                              now satisfied by the precedent
#                                              cache)
#   counsel_rebuttal     prior avg 2.6 — cap 6
#   counsel_disposition  prior avg 1.0 — cap 3
#   defense              like build — cap 20
#   legal_adv_response   like rebuttal — cap 8
#   rebuttal_investigator like a smaller investigator — cap 15
#   case_writer          synthesis only, no tools — cap 3
TURN_BUDGET_BY_STEP: dict[str, int] = {
    "investigator":               25,
    "counsel_build":              18,
    "counsel_first_chair_review":  5,  # rule application over structured input, no tools
    "counsel_rebuttal":           12,  # was 6 → hit error_max_turns at 7 in CASE-6A0A2598-2026 iter-1
    "counsel_disposition":         5,  # was 3 → defensive bump
    "defense":                    20,
    "legal_adv_response":         10,  # was 8 → defensive bump (similar shape to rebuttal)
    "rebuttal_investigator":      25,  # was 15 → hit error_max_turns at 16 in CASE-6A0CD54F-2026 adv-r1 (Hemlock).
                                       # The agent does parquet lookups + multi-source web verification for
                                       # each of defense's 4-6 attack vectors; 15 is consistently under-budget.
    "case_writer":                 5,  # was 3 → defensive bump
}
DEFAULT_TURN_BUDGET = 25

# Live JSON-output display: when enabled, every successfully-parsed agent
# response is pretty-printed to stdout immediately after validation, so a
# user watching the orchestrator log can track each agent's reasoning as it
# completes. Set FRAUD_INV_DISPLAY_JSON=0 to silence (the parsed.json file
# is always written to disk regardless).
DISPLAY_PARSED_JSON = os.environ.get("FRAUD_INV_DISPLAY_JSON", "1") == "1"

# Module-level CWD for all subprocesses. Set by the orchestrator at startup
# (via set_default_cwd) when --working-dir is provided. None → subprocess
# inherits orchestrator's CWD (default behavior).
_DEFAULT_CWD: "Path | None" = None


def set_default_cwd(path: "Path | None") -> None:
    """Set the CWD for all future invoke_agent() subprocess spawns.

    Used by the orchestrator to scope clean-room runs to an isolated directory
    (e.g., fraud-investigator-v2/cleanroom-larch/). Pass None to revert to the
    orchestrator's own CWD.
    """
    global _DEFAULT_CWD
    _DEFAULT_CWD = Path(path).resolve() if path is not None else None
    if _DEFAULT_CWD:
        LOG.info(f"claude -p subprocesses will run with cwd={_DEFAULT_CWD}")


def get_default_cwd() -> "Path | None":
    return _DEFAULT_CWD


# Module-level event-sink: when set, invoke_agent transparently delegates to
# invoke_agent_streaming so each tool call / text turn / tool result is emitted
# as a STAGE_* event to the configured EventSink. Set by the orchestrator
# (or any caller) via set_event_sink(); pass None to disable streaming.
#
# Type is "Any" to avoid an import-cycle with event_sink.py (which is a small
# stdlib-only module — but cleaner this way).
_EVENT_SINK: object | None = None    # holds an EventSink instance
_EVENT_STAGE: str | None = None      # canonical stage name, e.g., "investigator"


def set_event_sink(sink: object | None, stage_name: str | None = None) -> None:
    """Enable (or disable) per-step streaming events for all subsequent
    ``invoke_agent`` calls.

    When ``sink`` is non-None, invoke_agent transparently delegates to
    ``streaming.invoke_agent_streaming``, which spawns ``claude -p`` with
    ``--output-format stream-json --verbose`` and forwards each event as a
    STAGE_TOOL_USE / STAGE_TEXT_TURN / STAGE_TOOL_RESULT under the given
    ``stage_name`` (defaults to "investigator" for Layer 3/4 agent calls).

    Pass ``sink=None`` to revert to the one-shot ``--output-format json``
    behavior (no streaming, no event forwarding).
    """
    global _EVENT_SINK, _EVENT_STAGE
    _EVENT_SINK = sink
    _EVENT_STAGE = stage_name or "investigator"
    if sink is not None:
        LOG.info(f"claude -p subprocesses will stream events to {sink} as stage={_EVENT_STAGE!r}")


def get_event_sink() -> tuple[object | None, str | None]:
    """Return the current (sink, stage_name) tuple. Both None if no streaming."""
    return (_EVENT_SINK, _EVENT_STAGE)


T = TypeVar("T", bound=BaseModel)


class ClaudePInvocationError(RuntimeError):
    """Raised when ``claude -p`` fails (non-zero exit, timeout, parse failure)."""


class ClaudePHardStopError(ClaudePInvocationError):
    """Non-retryable failure — the org hit its monthly spend limit (billing
    wall). The run is resumable once an admin raises the cap at
    claude.ai/admin; retrying in-process is futile. A subclass so callers that
    catch ClaudePInvocationError still catch it, but the orchestrator can
    special-case it to fail fast with a clear resume hint."""


def _hard_stop_message(step_name: str) -> str:
    """Operator-facing message for an org-spend-limit hard stop."""
    return (
        f"[{step_name}] ORG_SPEND_LIMIT — your org has hit its monthly spend "
        f"limit. This is a hard billing wall, NOT a transient error; retrying "
        f"won't help. Ask an admin to raise it at claude.ai/admin, then RESUME "
        f"the run (make investigate NPI=… RESUME=<case-id>) — every completed "
        f"step is cached, so no work is lost."
    )


def _strip_code_fence(text: str) -> str:
    """Extract the JSON payload from a model response, tolerating:
      a) raw JSON                           → '{"k": "v"}'
      b) fenced-only                        → '```json\\n{...}\\n```'
      c) prose-prefix + fenced JSON         → 'Here is the JSON:\\n```json\\n{...}\\n```'
      d) prose-prefix + raw JSON            → 'Here is the JSON:\\n{...}'
      e) prose-prefix + fenced + trailing   → 'preamble\\n```json\\n{...}\\n```\\nthanks'

    Strategy: prefer the FIRST fenced ```json``` block; otherwise grab the
    first balanced { ... } substring. Validates by attempting to parse.
    """
    text = text.strip()

    # (a) try raw first — fast path
    if (text.startswith("{") and text.endswith("}")) or (text.startswith("[") and text.endswith("]")):
        try:
            json.loads(text)
            return text
        except json.JSONDecodeError:
            pass

    # (b, c, e) look for a fenced block
    fence_match = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, flags=re.DOTALL)
    if fence_match:
        candidate = fence_match.group(1).strip()
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            pass  # fall through to balanced-brace scan

    # (d, fallback) balanced-brace scan for the first object/array
    # Find the first { or [ and match it to its closing partner
    for start_char, end_char in (("{", "}"), ("[", "]")):
        start = text.find(start_char)
        if start == -1:
            continue
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if escape:
                escape = False
                continue
            if ch == "\\" and in_string:
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == start_char:
                depth += 1
            elif ch == end_char:
                depth -= 1
                if depth == 0:
                    candidate = text[start:i + 1]
                    try:
                        json.loads(candidate)
                        return candidate
                    except json.JSONDecodeError:
                        break  # try next start_char
                    break

    # No valid JSON found — return original (caller will hit a validation error)
    return text


def _extract_response_text(wrapper_json: dict) -> str:
    """Extract the assistant's textual response from the claude -p json wrapper.

    ``claude -p --output-format json`` returns a structured object containing
    a ``result`` field with the final assistant text. Some versions return
    the raw text under a different key — we try several.
    """
    for key in ("result", "response", "text", "content"):
        if key in wrapper_json and isinstance(wrapper_json[key], str):
            return wrapper_json[key]
    raise ClaudePInvocationError(
        f"Could not find response text in claude -p wrapper. Keys: {list(wrapper_json.keys())}"
    )


def session_id_for(case_id: str, role: str) -> str:
    """Deterministic UUID per (case_id, agent_role) for claude -p session reuse.

    Stable across orchestrator runs — so a resumed case continues the SAME
    session for each agent role across iterations + Phase A → Phase B (where
    that role appears in both phases). Use ``uuid5`` so the same inputs always
    map to the same UUID. The namespace is arbitrary (URL) — what matters is
    that the mapping is deterministic.

    Examples::

        session_id_for("CASE-X", "counsel_build")
        # iter-1 first call:        claude -p --session-id <uuid>
        # iter-2 / iter-3 / phaseB: claude -p --resume     <uuid>
    """
    import uuid
    return str(uuid.uuid5(uuid.NAMESPACE_URL,
                          f"fraud-investigator/{case_id}/{role}"))


def session_file_exists(session_id: str) -> bool:
    """Return True if a claude -p session file already exists on disk.

    Claude Code stores sessions at::

        ~/.claude/projects/<sanitized-cwd>/<session-uuid>.jsonl

    where ``<sanitized-cwd>`` is the current working directory with ``/``
    replaced by ``-``. We can't always know the sanitized cwd a priori
    (it depends on which directory the parent process was launched from),
    so we scan all project dirs for any file named ``<session_id>.jsonl``.
    Cheap: typically <20 project dirs, single stat per dir.

    Used by callers to decide whether to pass ``--session-id`` (create
    new) or ``--resume`` (continue existing) on the FIRST attempt of a
    call. Without this probe, an orchestrator restart on a case that
    previously created a session would fail with "Session ID is already
    in use" until the retry loop kicked in.
    """
    from pathlib import Path
    projects_root = Path.home() / ".claude" / "projects"
    if not projects_root.exists():
        return False
    target = f"{session_id}.jsonl"
    for proj_dir in projects_root.iterdir():
        if (proj_dir / target).is_file():
            return True
    return False


# ── Shared invoke-path kernel ────────────────────────────────────────────────
# The plain (invoke_agent) and streaming (invoke_agent_streaming) paths share
# these three decision points. They were copy-pasted before — and the #316/#318
# free-text (schema=None) bugs came precisely from that duplication drifting (one
# copy missing the schema=None case). Centralized here so they cannot diverge;
# pinned by tests/test_invoke_agent.py (incl. cross-path parity). Transport-
# specific control flow (cmd build, calibration perms, timeout-resume, tee/event
# replay, repair-vs-retry prompt) stays in each path — it is legitimately
# different, not duplicated.


def _load_cached_parsed(schema: "type[BaseModel] | None", parsed_path: Path) -> "BaseModel | None":
    """Resume primitive: return the cached, schema-validated model for a step, or None.

      - schema=None (free-text steps): NEVER cache-load. The on-disk
        ``parsed.json`` is raw prose, not a model to validate; re-running is
        cheap and a prior (possibly stale/old-format) doc must not be reused.
        (This is the guard the #318 resume crash came from.)
      - missing / invalid / corrupt cache: return None → caller re-runs.

    The CALLER owns any cache-hit side effects (logging, event replay, display)
    and any env-gated bypass (FRAUD_INV_NO_CACHE_LOAD), since those differ per path.
    """
    if schema is None or not parsed_path.exists():
        return None
    try:
        return schema.model_validate_json(parsed_path.read_text())
    except (ValidationError, ValueError) as e:
        LOG.warning(f"cached {parsed_path.name} failed schema validation ({e}); re-running")
        return None


def _coerce_and_persist(clean_text: str, schema: "type[BaseModel] | None",
                        parsed_path: Path) -> "BaseModel | str":
    """Persist + return one agent step's output.

      - schema=None (free-text): the text IS the deliverable — a prose/markdown
        document that must NOT be JSON-wrapped. Write it raw, return the str.
        (The #316 bug: large docs reliably break a ``{"content": ...}`` envelope,
        so free-text steps return the document verbatim.)
      - otherwise: validate ``clean_text`` against ``schema`` (raises
        ValidationError/ValueError on failure — the CALLER owns the parse-retry,
        which differs per path: repair-prompt vs retry-prompt), then write the
        canonical model dump and return the model.

    ``clean_text`` is expected already fence-stripped (the caller keeps the
    stripped text in scope for its repair prompt, so the strip stays there).
    """
    if schema is None:
        parsed_path.write_text(clean_text)
        return clean_text
    model = schema.model_validate_json(clean_text)
    parsed_path.write_text(model.model_dump_json(indent=2))
    return model


def _resolve_continuation(session_id: "str | None", is_continuation: bool,
                          step_name: str) -> bool:
    """Pre-flight: upgrade is_continuation False→True when a session file for
    this deterministic UUID already exists on disk. Handles the orchestrator-
    restart case where the same case_id+role would otherwise re-create a session
    claude-code already knows about (→ "Session ID is already in use")."""
    if session_id and not is_continuation and session_file_exists(session_id):
        LOG.info(
            f"[{step_name}] session file for {session_id[:8]} already exists "
            f"on disk; upgrading is_continuation=False → True so the first "
            f"attempt uses --resume instead of --session-id."
        )
        return True
    return is_continuation


def _strip_tool_permission_flags(args: "list[str] | None") -> list[str]:
    """Drop caller-supplied tool-permission flags (+ their values).

    Agent drivers inject their own ``--allowedTools`` / ``--disallowedTools``.
    The legal agents (counsel_build, defense, legal_adv_response) pass a BARE
    ``Read`` in that list, which OVERRIDES calibration's ``Read(<export>/**)``
    jail and lets the agent read ``gold_set.jsonl`` by absolute path (verified
    leak hole, 2026-06-04: the Read tool returned gold_set's first line while
    Bash/Grep stayed jailed). In calibration mode we strip these so
    ``CalibrationContext.launch_flags_for_agent`` is the single source of truth.
    Handles both ``--allowedTools X`` (space) and ``--allowedTools=X`` (equals),
    case-insensitively.
    """
    drop = {"--allowedtools", "--allowed-tools", "--disallowedtools", "--disallowed-tools"}
    out: list[str] = []
    skip_value = False
    for a in (args or []):
        if skip_value:
            skip_value = False
            continue
        if isinstance(a, str) and a.split("=", 1)[0].lower() in drop:
            skip_value = "=" not in a  # space-separated value follows → skip it too
            continue
        out.append(a)
    return out


def _step_meta_payload(step_name: str, attempts: list[dict]) -> dict:
    """Build the on-disk ``{step}.meta.json`` payload — additive step-level
    duration totals alongside the existing per-attempt records (PR #351's
    per-attempt ``started``/``finished``/``duration_sec`` + ``model``/``effort``
    are unchanged). ``started_at``/``ended_at``/``duration_s`` summarize wall-clock
    elapsed from the FIRST attempt's start to THIS write (so it also captures
    retry backoff/wait time between attempts) — without this, proving a step's
    total cost across retries meant manually diffing every attempt's timestamps.
    Shared by claude_p.invoke_agent, streaming.invoke_agent_streaming, and
    tmux_backend.run_step so the three seams can't drift.
    """
    started_at = attempts[0].get("started") if attempts else None
    ended_at = datetime.utcnow().isoformat()
    duration_s: float | None = None
    if started_at:
        try:
            duration_s = (datetime.fromisoformat(ended_at)
                         - datetime.fromisoformat(started_at)).total_seconds()
        except ValueError:
            duration_s = None
    return {
        "step": step_name,
        "attempts": attempts,
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_s": duration_s,
    }


def invoke_agent(
    *,
    prompt: str,
    schema: type[T] | None,
    out_dir: Path,
    step_name: str,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    claude_bin: str = DEFAULT_CLAUDE_BIN,
    extra_args: list[str] | None = None,
    session_id: str | None = None,
    is_continuation: bool = False,
) -> T | str:
    """Invoke one ``claude -p`` agent step.

    Writes prompt + raw stdout/stderr + parsed JSON to ``out_dir/{step_name}.*``.
    Retries up to ``MAX_PARSE_RETRIES`` on parse/validation errors, appending
    the error message to the prompt each retry.

    If an event sink has been registered via ``set_event_sink``, this call
    transparently delegates to ``streaming.invoke_agent_streaming`` so each
    tool call / text turn / tool result is forwarded to the sink as a
    STAGE_* event. The on-disk artifacts (prompt.md, parsed.json, meta.json)
    remain compatible across both modes.

    Session continuation (NEW):
      - ``session_id`` is a stable UUID per (case_id, agent_role) — when
        provided, the subprocess is spawned with ``--session-id <uuid>`` on
        the first call (``is_continuation=False``) or ``--resume <uuid>`` on
        every subsequent call. This keeps the conversation history alive
        across iterations so the model doesn't have to be re-fed accumulated
        context as part of the user message. Anthropic's 1-hour prompt cache
        + the session-file-on-disk mechanism combine to make the cross-iter
        cost much smaller than restarting fresh each time.
      - ``is_continuation=True`` signals the agent driver to send only a
        delta user message (e.g., "Here's the new investigator evidence for
        iter-N; revise") instead of the full prompt-with-embedded-context.
        The model already has the prior turns in conversation memory.

    Returns the validated Pydantic model.
    """
    # CALIBRATION CLEAN-ROOM injection (must happen BEFORE both the streaming
    # branch and the cache-load branch, so both code paths get the hardening).
    # The orchestrator sets the calibration context via calibration.set_calibration_context()
    # on entry to its main run loop and clears it on exit (try/finally). When
    # active, we:
    #   1. Append per-agent hardened --allowed-tools / --disallowed-tools flags
    #      to extra_args (Read jailed to the calibration run_dir, Bash limited
    #      to date-gated sandbox_browser, web tools denied entirely).
    #   2. Prepend a CALIBRATION MODE banner to the agent's prompt so the
    #      agent understands the date-gating + model-recognition-is-leakage rule.
    #   3. Force FRAUD_INV_NO_CACHE_LOAD=1 so a cached parsed.json from a
    #      previous non-calibration run cannot serve as this run's output
    #      (defense in depth — synthetic case_id should already prevent path
    #      collision, but a bug in case_id generation must NOT silently leak
    #      old data).
    #   4. Extend the per-agent timeout to ctx.timeout_sec (default 1800s).
    #      Sandbox_browser Wayback fetches add real network latency; the
    #      production 600s default is too short for calibration runs (verified
    #      empirically on 2026-06-03: investigator verify pass hit timeout).
    #      We use max() so a caller can still pass a longer timeout explicitly.
    # Lazy import to avoid any potential cycle.
    from . import calibration as _cal
    _cal_ctx = _cal.get_calibration_context()

    if _cal_ctx is not None:
        # Strip caller-supplied tool-permission flags FIRST, then append the
        # calibration flags — otherwise an agent driver's bare `Read` (legal
        # agents) overrides the export Read-jail and leaks gold_set. See
        # _strip_tool_permission_flags.
        extra_args = _strip_tool_permission_flags(extra_args) + _cal_ctx.launch_flags_for_agent(step_name)
        prompt = _cal_ctx.prompt_prefix(step_name) + prompt
        # NOTE: we deliberately do NOT force FRAUD_INV_NO_CACHE_LOAD here. The
        # synthetic CALIBRATION-<uuid> case_id + the isolated state/calibration/
        # root already make it impossible for a cal run to pick up a non-cal run's
        # parsed.json (disjoint paths), so the flag added nothing for leak-safety —
        # but it DID break --case-id resume by forcing already-completed, clean cal
        # steps to re-run (wasting the Sonnet investigator passes). Allowing cache
        # reuse lets resume continue from the failed/interrupted step. A fresh cal
        # run mints a new uuid → empty dir → nothing to reuse, so no behavior change
        # there. To force a specific step to re-run, delete its parsed.json.
        timeout_sec = max(timeout_sec, _cal_ctx.timeout_sec)

    # tmux execution backend: run this step in an interactive `claude` session
    # (personal usage limits) instead of a `claude -p` subprocess (Agent-SDK
    # credit). Dispatched AFTER calibration injection so the pane inherits the
    # calibration prompt-prefix + the per-step --allowed-tools/--disallowed-tools
    # in extra_args — tmux_backend launches each step's pane with those flags +
    # the out-of-repo export cwd, reproducing the clean-room jail at the pane
    # level (default-mode allowlist, not bypassPermissions).
    if EXEC_BACKEND == "tmux":
        from . import tmux_backend
        return tmux_backend.run_step(
            prompt=prompt,
            schema=schema,
            out_dir=out_dir,
            step_name=step_name,
            timeout_sec=timeout_sec,
            claude_bin=claude_bin,
            extra_args=extra_args,
            session_id=session_id,
            is_continuation=is_continuation,
            event_sink=_EVENT_SINK,
            stage_name=_EVENT_STAGE,
            calibration=_cal_ctx is not None,
        )

    # Provider-agnostic backend: run the step as a tool-calling conversation against
    # any LLM provider (user's own API key) instead of the Claude Code CLI. Dispatched
    # AFTER calibration injection so the clean-room prompt prefix + tool jail apply.
    if EXEC_BACKEND == "api":
        from . import api_backend
        return api_backend.run_step(
            prompt=prompt,
            schema=schema,
            out_dir=out_dir,
            step_name=step_name,
            timeout_sec=timeout_sec,
            extra_args=extra_args,
            session_id=session_id,
            is_continuation=is_continuation,
            event_sink=_EVENT_SINK,
            stage_name=_EVENT_STAGE,
            calibration=_cal_ctx is not None,
        )

    # Streaming dispatch: if the orchestrator has registered a sink, use the
    # stream-json subprocess variant so every tool call is visible in the UI.
    if _EVENT_SINK is not None:
        # Lazy import to avoid a cycle: streaming.py imports from claude_p.py
        from .streaming import invoke_agent_streaming
        return invoke_agent_streaming(
            prompt=prompt,
            schema=schema,
            out_dir=out_dir,
            step_name=step_name,
            event_sink=_EVENT_SINK,    # type: ignore[arg-type]
            stage_name=_EVENT_STAGE or "investigator",
            timeout_sec=timeout_sec,
            claude_bin=claude_bin,
            extra_args=extra_args,
            session_id=session_id,
            is_continuation=is_continuation,
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = out_dir / f"{step_name}.prompt.md"
    raw_path = out_dir / f"{step_name}.raw.json"
    stderr_path = out_dir / f"{step_name}.stderr.log"
    parsed_path = out_dir / f"{step_name}.parsed.json"
    meta_path = out_dir / f"{step_name}.meta.json"

    # Cache-load: if a previous run already produced a valid parsed.json for
    # this step in this iteration's directory, reuse it. This is the resume
    # primitive — to force re-run of a specific step, delete its parsed.json.
    # Saves API cost on re-runs after orchestrator/prompt changes that only
    # affect downstream steps. Free-text steps (schema=None) SKIP the cache:
    # their parsed.json is raw text (not a model to validate), re-running is
    # cheap, and a prior (possibly old-format) cache would be unsafe to reuse.
    if os.environ.get("FRAUD_INV_NO_CACHE_LOAD") != "1":
        cached = _load_cached_parsed(schema, parsed_path)
        if cached is not None:
            LOG.info(f"[{step_name}] CACHED — loaded existing {parsed_path.name} "
                     f"(set FRAUD_INV_NO_CACHE_LOAD=1 to force re-run)")
            if DISPLAY_PARSED_JSON:
                _display_parsed_json(step_name, cached, 0.0)
            return cached

    # Pre-flight: if the caller said "fresh session" (is_continuation=False)
    # but a session file already exists on disk for this deterministic UUID,
    # upgrade to is_continuation=True. Handles the orchestrator-restart case
    # where the same case_id + role would otherwise re-create a session that
    # claude-code already knows about (→ "Session ID is already in use").
    is_continuation = _resolve_continuation(session_id, is_continuation, step_name)

    current_prompt = prompt
    attempts: list[dict] = []

    for attempt in range(1, MAX_PARSE_RETRIES + 1):
        # Persist the prompt we're about to send (last write wins; final is
        # the prompt that succeeded — earlier attempts captured in meta).
        prompt_path.write_text(current_prompt)

        cmd = [claude_bin, "-p", "--output-format", "json"] + list(DEFAULT_HEADLESS_ARGS)
        # Per-step turn budget. In calibration export mode the investigator steps
        # get more runway (script-based tools are turn-hungrier than WebSearch);
        # production budgets are untouched.
        max_turns = TURN_BUDGET_BY_STEP.get(step_name, DEFAULT_TURN_BUDGET)
        if _cal_ctx is not None:
            max_turns = _cal_ctx.turn_budget_for(step_name, max_turns)
        cmd.extend(["--max-turns", str(max_turns)])
        # Per-step model selection (None → inherit from CLI config)
        chosen_model = _resolve_model_for_step(step_name)
        if chosen_model:
            cmd.extend(["--model", chosen_model])
        # Per-step extended-thinking effort level (None → inherit CLI default)
        chosen_effort = _resolve_effort_for_step(step_name)
        if chosen_effort:
            cmd.extend(["--effort", chosen_effort])
        # Session continuation — first call uses --session-id <UUID> (creates
        # a new session with that ID); subsequent calls use --resume <UUID>
        # (loads the existing session and appends the new user message). The
        # session file persists on disk between calls; Claude Code's 1-hour
        # prompt cache then automatically covers the prior conversation
        # prefix without re-billing the input tokens.
        # Retry resumption: on attempt 2+, the session was already created
        # during attempt 1 (claude-p registers the session as soon as
        # --session-id is consumed, even if the subprocess later exits 1).
        # If we keep passing --session-id on retries, Anthropic responds
        # "Session ID is already in use" and the retry fails immediately.
        # Switch to --resume on attempt 2+ so the retry actually executes.
        effective_continuation = is_continuation or attempt > 1
        if session_id:
            if effective_continuation:
                cmd.extend(["--resume", session_id])
            else:
                cmd.extend(["--session-id", session_id])
        if extra_args:
            cmd.extend(extra_args)

        # CALIBRATION CLEAN-ROOM enforcement (critical, Phase 1.6, 2026-06-03):
        # The default DEFAULT_HEADLESS_ARGS includes --dangerously-skip-permissions
        # which BYPASSES all --allowed-tools / --disallowed-tools enforcement.
        # In calibration mode we MUST strip that flag and force --permission-mode
        # default so the launch-flag allowlist actually constrains the agent.
        # Verified live 2026-06-03: with --dangerously-skip-permissions in place
        # (production default), the agent successfully Read gold_set.jsonl,
        # docs/calibration/, AND a prior calibration run's state dir, even with
        # tight Read(<run_dir>/**) allowlists. Removing the flag + an out-of-repo
        # cwd (CalibrationContext.sandbox_cwd) closes both leak paths.
        if _cal_ctx is not None:
            cmd = [a for a in cmd if a != "--dangerously-skip-permissions"]
            # Force default mode (claude CLI honors the LAST --permission-mode
            # occurrence if duplicates appear).
            if "--permission-mode" not in cmd:
                cmd.extend(["--permission-mode", "default"])
        else:
            # SAFE-PERMS production mode (default-mode + broad allowlist) so the
            # run is launchable from inside a Claude Code session. No-op unless
            # FRAUD_INV_SAFE_PERMS=1. Mutually exclusive with calibration.
            cmd = _apply_safe_perms(cmd)

        model_tag = f"model={chosen_model}" if chosen_model else "model=<inherit>"
        session_tag = (
            f"resume={session_id[:8]}" if (session_id and effective_continuation)
            else f"new-session={session_id[:8]}" if session_id
            else "no-session"
        )
        LOG.info(f"[{step_name}] attempt {attempt}/{MAX_PARSE_RETRIES} "
                 f"({model_tag}, {session_tag}, max_turns={max_turns}) — "
                 f"spawning: {' '.join(cmd)}")
        started = datetime.utcnow()

        effective_cwd = _DEFAULT_CWD
        try:
            proc = subprocess.run(
                cmd,
                input=current_prompt,
                capture_output=True,
                text=True,
                timeout=timeout_sec,
                cwd=str(effective_cwd) if effective_cwd else None,
            )
        except subprocess.TimeoutExpired as e:
            err = f"timeout after {timeout_sec}s"
            # A wall-clock timeout means the agent was still working (extended
            # thinking + many tool turns) when we killed it — NOT a bad prompt.
            # Treat it like error_max_turns: resume the SAME session once and tell
            # it to stop exploring and emit its final JSON from work-so-far. The
            # resumed call uses --resume (attempt 2+), inheriting the session's
            # prior turns, so the agent finalizes rather than restarts. Bounded by
            # MAX_PARSE_RETRIES; falls through to raise if it still can't finalize.
            # This was the single biggest calibration-queue failure mode
            # (counsel_build / counsel_rebuttal dying at 1800s, previously
            # non-retryable — claude_p raised immediately before any retry branch).
            rec = {"attempt": attempt, "started": started.isoformat(), "error": err}
            if session_id and attempt < MAX_PARSE_RETRIES:
                rec["error"] = f"{err} — resuming to finalize"
                rec["retry_reason"] = "wall_clock_timeout"
                attempts.append(rec)
                meta_path.write_text(json.dumps(_step_meta_payload(step_name, attempts), indent=2))
                LOG.warning(
                    f"[{step_name}] {err}; resuming session to finalize with "
                    f"work-so-far (attempt {attempt} → {attempt + 1})")
                current_prompt = _finalize_prompt()
                continue
            attempts.append(rec)
            meta_path.write_text(json.dumps(_step_meta_payload(step_name, attempts), indent=2))
            raise ClaudePInvocationError(f"[{step_name}] {err}") from e

        finished = datetime.utcnow()
        stderr_path.write_text(proc.stderr or "")
        # ALWAYS write stdout, even on non-zero exit — preserves diagnostic info.
        # Audit (no-loss): also keep each attempt's output under a per-attempt
        # name (raw_path itself is overwritten by retries).
        raw_path.write_text(proc.stdout or "")
        (out_dir / f"{step_name}.raw.attempt{attempt}.json").write_text(proc.stdout or "")

        attempt_record = {
            "attempt": attempt,
            "started": started.isoformat(),
            "finished": finished.isoformat(),
            "duration_sec": (finished - started).total_seconds(),
            "exit_code": proc.returncode,
            "stdout_bytes": len(proc.stdout or ""),
            "stderr_bytes": len(proc.stderr or ""),
            # Resolved routing (None = --model/--effort omitted, CLI inherits) —
            # was absent from meta.json entirely, leaving calibration/battery
            # runs unable to prove which model actually ran a step.
            "model": chosen_model,
            "effort": chosen_effort,
        }

        if proc.returncode != 0:
            attempt_record["error"] = f"exit {proc.returncode}"
            # Hard stop (billing wall) — fail FAST, before any retry branch.
            # Resuming just re-hits the same org spend cap; burning the parse/
            # transient retries on it wastes minutes and muddies the log.
            if is_hard_stop_failure(proc.stderr, proc.stdout):
                attempt_record["error"] = f"exit {proc.returncode} (ORG_SPEND_LIMIT — hard stop)"
                attempts.append(attempt_record)
                meta_path.write_text(json.dumps(_step_meta_payload(step_name, attempts), indent=2))
                LOG.error(_hard_stop_message(step_name))
                raise ClaudePHardStopError(_hard_stop_message(step_name))
            # Graceful turn-budget handling: `error_max_turns` means the agent
            # actually ran but exhausted its exploration budget before emitting
            # the final JSON (it was cut off mid-tool-use). Rather than abort the
            # whole run, resume the SAME session once and tell it to stop exploring
            # and emit its final JSON now, deferring any unfinished investigative
            # threads to follow-up questions — the next iteration picks them up.
            # Bounded by MAX_PARSE_RETRIES; falls through to the raise if it can't
            # finalize. (General across steps; the investigator is the usual hitter.)
            hit_max_turns = False
            try:
                _w = json.loads(proc.stdout or "")
                hit_max_turns = isinstance(_w, dict) and _w.get("subtype") == "error_max_turns"
            except (json.JSONDecodeError, TypeError):
                pass
            if hit_max_turns and session_id and attempt < MAX_PARSE_RETRIES:
                attempt_record["error"] = (
                    f"error_max_turns at {max_turns} turns — resuming to finalize"
                )
                attempts.append(attempt_record)
                meta_path.write_text(json.dumps(_step_meta_payload(step_name, attempts), indent=2))
                LOG.warning(
                    f"[{step_name}] hit error_max_turns ({max_turns} turns); resuming "
                    f"session to finalize with evidence-so-far + defer unfinished "
                    f"threads (attempt {attempt} → {attempt + 1})"
                )
                current_prompt = _finalize_prompt()
                continue
            # Transient API/network failure → resume the session after backoff
            # (shared detector; same policy as the streaming path).
            if (is_transient_api_failure(proc.stderr, proc.stdout, attempt_record.get("error", ""))
                    and attempt < MAX_PARSE_RETRIES):
                wait = transient_backoff_sec(attempt)
                attempt_record["error"] = f"exit {proc.returncode} (transient API/network)"
                attempt_record["retry_reason"] = "transient_api_failure"
                attempts.append(attempt_record)
                meta_path.write_text(json.dumps(_step_meta_payload(step_name, attempts), indent=2))
                LOG.warning(
                    f"[{step_name}] claude -p exited {proc.returncode} on a TRANSIENT "
                    f"API/network failure — resuming in {wait:.0f}s "
                    f"(attempt {attempt+1}/{MAX_PARSE_RETRIES})")
                time.sleep(wait)
                continue

            attempts.append(attempt_record)
            meta_path.write_text(json.dumps(_step_meta_payload(step_name, attempts), indent=2))
            # Include first 500 chars of stdout in the error so the orchestrator
            # log captures a clue without us having to dig through raw.json
            stdout_preview = (proc.stdout or "")[:500]
            raise ClaudePInvocationError(
                f"[{step_name}] claude -p exited {proc.returncode}\n"
                f"stdout ({len(proc.stdout or '')} bytes; preview):\n{stdout_preview}\n"
                f"stderr:\n{(proc.stderr or '')[:2000]}"
            )

        # Parse the outer wrapper
        try:
            wrapper = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            attempt_record["error"] = f"wrapper-json-parse: {e}"
            attempts.append(attempt_record)
            current_prompt = _retry_prompt(
                prompt,
                error_msg=f"Your previous response did not produce a parseable claude -p output: {e}. "
                          f"Return ONLY valid JSON matching the schema. No prose, no fences.",
            )
            continue

        try:
            response_text = _extract_response_text(wrapper)
        except ClaudePInvocationError as e:
            attempt_record["error"] = f"extract-response: {e}"
            attempts.append(attempt_record)
            current_prompt = _retry_prompt(prompt, error_msg=str(e))
            continue

        response_clean = _strip_code_fence(response_text)

        # Coerce + persist. Free-text (schema=None) returns the raw document;
        # otherwise validate against the schema. The schema=None branch lives in
        # _coerce_and_persist so it cannot drift from the streaming path (#316/#318).
        try:
            result = _coerce_and_persist(response_clean, schema, parsed_path)
        except ValidationError as e:
            attempt_record["error"] = f"schema-validation: {type(e).__name__}"
            attempt_record["validation_error"] = str(e)[:2000]
            attempts.append(attempt_record)
            # Surgical repair: the model produced well-formed JSON that missed the
            # schema (e.g., omitted a required `tier`). Re-prompt with its OWN output
            # + the exact errors and ask it to fix ONLY those fields — do NOT re-run
            # the full task (that reproduces the omission). Forces the agent to grade
            # the field rather than us silently defaulting it.
            current_prompt = _repair_prompt(
                response_clean,
                error_msg=(
                    f"Your previous JSON failed Pydantic validation against the "
                    f"{schema.__name__} schema:\n\n{str(e)[:1500]}"
                ),
            )
            continue
        except ValueError as e:
            # NOT JSON at all (prose, a markdown rationale, a refusal…). This must
            # NOT flow into _repair_prompt: that prompt asserts "your previous JSON
            # was well-formed", which is FALSE here — and a fabricated premise makes
            # the model (correctly) refuse the repair. Observed 2026-08-02 on
            # Willow/Magnolia: the workspace TASK.md instructed a plain-text
            # "RISK: ..." format, the model followed it, and three gaslighting
            # repair rounds ended in agent_error. Tell it the truth instead, and
            # explicitly override any output-format instructions it read in
            # workspace files.
            attempt_record["error"] = f"not-json: {type(e).__name__}"
            attempt_record["validation_error"] = str(e)[:2000]
            attempts.append(attempt_record)
            current_prompt = _retry_prompt(
                prompt,
                error_msg=(
                    "Your previous response was NOT JSON — it began:\n"
                    f"    {response_clean[:300]!r}\n"
                    "If a file in your workspace (e.g. TASK.md) describes a different "
                    "output format such as a prose rationale ending in 'RISK: …', "
                    "IGNORE that file's output-format instructions — it describes an "
                    "older harness. Your output contract is THIS prompt's JSON schema "
                    "and nothing else. Re-run the task and return ONLY valid JSON "
                    "matching the schema. No prose, no code fences."
                ),
            )
            continue

        # Success (free-text document or validated model).
        attempt_record["status"] = "ok"
        attempts.append(attempt_record)
        meta_path.write_text(json.dumps(_step_meta_payload(step_name, attempts), indent=2))
        if schema is None:
            LOG.info(f"[{step_name}] attempt {attempt} OK ({attempt_record['duration_sec']:.1f}s · text)")
            return result
        LOG.info(f"[{step_name}] attempt {attempt} OK ({attempt_record['duration_sec']:.1f}s)")
        if DISPLAY_PARSED_JSON:
            _display_parsed_json(step_name, result, attempt_record["duration_sec"])
        return result

    # All retries exhausted
    meta_path.write_text(json.dumps(_step_meta_payload(step_name, attempts), indent=2))
    raise ClaudePInvocationError(
        f"[{step_name}] failed after {MAX_PARSE_RETRIES} attempts. See {meta_path}."
    )


def _display_parsed_json(step_name: str, model: BaseModel, duration_sec: float) -> None:
    """Pretty-print a successfully-parsed agent response to stdout for live
    tracking. Bordered so it's easy to find in a long orchestrator log.

    The full payload is dumped — for InvestigatorOutput with many evidence
    items this can be 200+ lines. That's intentional: the user asked to
    watch each agent's reasoning. Pipe stdout to a file if too verbose, or
    set FRAUD_INV_DISPLAY_JSON=0 to disable.
    """
    payload = model.model_dump_json(indent=2)
    n_lines = payload.count("\n") + 1
    header = f" {step_name} — parsed JSON ({duration_sec:.1f}s, {n_lines} lines) "
    bar = "═" * max(0, 78 - len(header))
    print()
    print(f"╔{header}{bar}╗")
    print(payload)
    print(f"╚{'═' * (len(header) + len(bar))}╝")
    print(flush=True)


def _finalize_prompt() -> str:
    """Wrap-up message sent on --resume after `error_max_turns`.

    The agent has full session context (its tool results so far); it just ran
    out of exploration budget before emitting the final JSON. Tell it to stop
    exploring and finalize, deferring unfinished threads so the next iteration
    continues them — converting a hard turn-budget abort into a clean partial
    result.
    """
    return (
        "You reached your tool/exploration turn budget before returning a final "
        "answer. STOP calling tools now — do not begin any new lookups. Using ONLY "
        "the evidence you have already gathered in this session, immediately return "
        "your final JSON response per the schema you were given. For any "
        "investigative thread you did not finish, add a concise note to your "
        "follow-up-questions / open-questions / notes field so the next iteration "
        "continues it. Return ONLY valid JSON — no prose, no code fences."
    )


def _retry_prompt(original: str, error_msg: str) -> str:
    """Construct a retry prompt with the validation error appended."""
    return (
        original
        + "\n\n---\n"
        + "## ⚠️ RETRY — your previous attempt failed validation\n\n"
        + error_msg
        + "\n\nReturn ONLY the corrected JSON. No prose. No markdown code fences."
    )


def _repair_prompt(prior_output: str, error_msg: str) -> str:
    """Surgical JSON repair (used for schema-validation failures only).

    Hands the model its OWN previous JSON plus the exact Pydantic errors and asks
    it to fix ONLY the flagged fields — instead of re-running the whole task with
    the error appended (``_retry_prompt``), which re-executes the full investigation
    and tends to REPRODUCE the same omission (observed: the investigator dropped the
    required ``tier`` on every evidence item and re-omitted it across 3 task re-runs).

    A focused repair forces the agent to actually supply the missing/invalid values
    (e.g., grade each evidence item's ``tier``) rather than letting it slip through —
    we do NOT silently default required fields, because tier/etc. are real signal.
    """
    return (
        "## ⚠️ REPAIR — your previous JSON was well-formed but failed schema validation.\n\n"
        "Do NOT re-run the task and do NOT re-investigate. Below is YOUR OWN previous output. "
        "Return it again with ONLY the flagged fields corrected — do not add, remove, or reorder "
        "items, and do not touch any field that was not flagged.\n\n"
        "For EACH error below, make that value conform EXACTLY to the schema:\n"
        "  • **Missing required field** → add it with a correct value. A missing evidence `tier` "
        "must be exactly one of `confirmed_fact` | `statistical_signal` | `legal_hypothesis`, chosen "
        "to match the item (verified primary-source record → `confirmed_fact`; percentile/ratio/volume "
        "statistic → `statistical_signal`; unverified inference → `legal_hypothesis`).\n"
        "  • **\"Input should be a valid string\" but you emitted an object/array** → replace that value "
        "with a SINGLE plain string, collapsing the object to one concise sentence (e.g. each "
        "`working_hypotheses` entry must be a string, NOT an `{id, label, ...}` object).\n"
        "  • **\"Input should be '<a>', '<b>', ...\" (an enum)** → emit EXACTLY one of those literal "
        "values with NO extra words, prose, em-dashes, or explanation (e.g. `analysis_unit_chosen` "
        "must be exactly `single_npi`, never `single_npi — address sweep ...`).\n\n"
        "### Validation errors to fix:\n" + error_msg + "\n\n"
        "### Your previous output (fix in place):\n" + prior_output + "\n\n"
        "Return ONLY the corrected JSON. No prose. No markdown code fences."
    )
