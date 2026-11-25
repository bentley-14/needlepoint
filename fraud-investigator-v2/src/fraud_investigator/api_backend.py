"""Provider-agnostic agent backend (``FRAUD_INV_EXEC_BACKEND=api``).

Runs each agent step as a tool-calling conversation against ANY LLM provider through
LiteLLM (Anthropic, OpenAI, Google Gemini, OpenRouter, Mistral, local Ollama, ...), using
the caller's own API key from the environment (ANTHROPIC_API_KEY, OPENAI_API_KEY,
GEMINI_API_KEY, OPENROUTER_API_KEY, ...).

It is a drop-in replacement for the ``claude -p`` subprocess path: same inputs, same
on-disk artifacts (prompt.md, raw.json, parsed.json, meta.json), same parse/repair/retry
policy (shared helpers from ``claude_p``), same per-agent tool jails (``llm_tools``
parses the existing ``--allowedTools`` / ``--disallowedTools`` flags).

Model selection, highest precedence first:
  1. FRAUD_INV_API_MODEL_<STEP>        e.g. FRAUD_INV_API_MODEL_DEFENSE=openai/gpt-5
  2. FRAUD_INV_API_MODEL               one model for every step, e.g. gemini/gemini-2.5-pro
  3. the shared resolver: FRAUD_INV_PROFILE, FRAUD_INV_MODEL_<STEP>,
     state/model_overrides.json, FRAUD_INV_MODEL_DEFAULT, then the built-in per-step map
Any LiteLLM model string works ("provider/model"). Bare Claude IDs and the aliases
opus / sonnet / haiku are mapped to Anthropic automatically.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, TypeVar

from pydantic import BaseModel, ValidationError

from . import claude_p as cp
from .llm_tools import ToolExecutor, policy_from_flags

LOG = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

MAX_TOKENS = int(os.environ.get("FRAUD_INV_API_MAX_TOKENS", "16000"))
# Opt-in: forward the step's effort as LiteLLM ``reasoning_effort``. Off by default because
# providers disagree on how (or whether) they accept it; turning it on for a model that
# rejects it fails the step loudly rather than silently.
PASS_REASONING_EFFORT = os.environ.get("FRAUD_INV_API_REASONING_EFFORT", "0") == "1"
SESSION_DIR = Path(os.environ.get(
    "FRAUD_INV_API_SESSION_DIR",
    str(Path(__file__).resolve().parents[2] / "state" / "_api_sessions")))

_ALIASES = {"opus": "anthropic/claude-opus-5", "sonnet": "anthropic/claude-sonnet-5",
            "haiku": "anthropic/claude-haiku-4-5", "fable": "anthropic/claude-fable-5-1"}

SYSTEM_PROMPT = (
    "You are an autonomous agent step inside a healthcare-fraud investigation pipeline. "
    "Use the provided tools to gather evidence (run the project's scripts with the bash tool, "
    "read files, search and fetch the web when permitted). Work from the repository working "
    "directory. When you are done, reply with ONLY the final answer in exactly the format the "
    "user's instructions require (usually a single JSON object) and no other text."
)

CompletionFn = Callable[..., Any]


def resolve_api_model(step_name: str) -> str:
    """Map the step's configured model onto a LiteLLM model string."""
    raw = (os.environ.get(f"FRAUD_INV_API_MODEL_{step_name.upper()}")
           or os.environ.get("FRAUD_INV_API_MODEL")
           or cp._resolve_model_for_step(step_name)
           or "sonnet")
    model = re.sub(r"\[[^\]]*\]$", "", raw.strip())  # drop Claude Code context suffixes like [1m]
    if model in _ALIASES:
        return _ALIASES[model]
    if "/" in model:
        return model
    if model.startswith("claude-"):
        return f"anthropic/{model}"
    return model


def _default_completion() -> CompletionFn:
    try:
        import litellm  # noqa: WPS433 (optional dependency, imported lazily)
    except ImportError as e:  # pragma: no cover - exercised only without the extra
        raise cp.ClaudePInvocationError(
            "FRAUD_INV_EXEC_BACKEND=api needs LiteLLM: pip install 'litellm>=1.40'") from e
    litellm.drop_params = True  # tolerate params a given provider does not support
    litellm.suppress_debug_info = True
    return litellm.completion


def _session_path(session_id: str) -> Path:
    return SESSION_DIR / f"{session_id}.json"


def _load_session(session_id: str | None) -> list[dict]:
    if not session_id:
        return []
    p = _session_path(session_id)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            LOG.warning(f"[api] corrupt session file {p}; starting fresh")
    return []


def _save_session(session_id: str | None, messages: list[dict]) -> None:
    if not session_id:
        return
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    _session_path(session_id).write_text(json.dumps(messages))


def _msg_to_dict(msg: Any) -> dict:
    """Normalise a LiteLLM/OpenAI message object into a plain dict we can persist."""
    if isinstance(msg, dict):
        d = dict(msg)
    elif hasattr(msg, "model_dump"):
        d = msg.model_dump()
    else:
        d = {"role": getattr(msg, "role", "assistant"), "content": getattr(msg, "content", None),
             "tool_calls": getattr(msg, "tool_calls", None)}
    out = {"role": d.get("role") or "assistant", "content": d.get("content")}
    calls = d.get("tool_calls") or []
    norm = []
    for c in calls:
        c = c if isinstance(c, dict) else (c.model_dump() if hasattr(c, "model_dump") else vars(c))
        fn = c.get("function") or {}
        fn = fn if isinstance(fn, dict) else (fn.model_dump() if hasattr(fn, "model_dump") else vars(fn))
        norm.append({"id": c.get("id"), "type": "function",
                     "function": {"name": fn.get("name"), "arguments": fn.get("arguments") or "{}"}})
    if norm:
        out["tool_calls"] = norm
    return out


def _emit(sink: Any, stage: str | None, kind: str, payload: dict) -> None:
    if sink is None:
        return
    try:
        from .event_sink import EVENT_STAGE_TEXT_TURN, EVENT_STAGE_TOOL_RESULT, EVENT_STAGE_TOOL_USE
        ev = {"tool_use": EVENT_STAGE_TOOL_USE, "tool_result": EVENT_STAGE_TOOL_RESULT,
              "text": EVENT_STAGE_TEXT_TURN}[kind]
        sink.emit(stage or "investigator", ev, payload)
    except Exception:  # the UI feed must never break a run
        LOG.debug("[api] event emit failed", exc_info=True)


def run_conversation(*, messages: list[dict], model: str, executor: ToolExecutor, max_turns: int,
                     deadline: float, completion: CompletionFn, effort: str | None,
                     sink: Any = None, stage: str | None = None, finalize: bool = False) -> dict:
    """Drive one tool-calling exchange until the model answers without tool calls.

    Mutates ``messages`` in place. Returns {"text", "turns", "usage", "cost", "hit_limit"}.
    ``hit_limit`` is "max_turns" or "timeout" when the model was cut off before answering.
    """
    tools = executor.tool_specs()
    usage = {"input_tokens": 0, "output_tokens": 0}
    cost = 0.0
    turns = 0
    while True:
        if turns >= max_turns:
            return {"text": None, "turns": turns, "usage": usage, "cost": cost, "hit_limit": "max_turns"}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"text": None, "turns": turns, "usage": usage, "cost": cost, "hit_limit": "timeout"}
        kwargs: dict[str, Any] = {"model": model, "messages": messages, "max_tokens": MAX_TOKENS,
                                  "timeout": max(30, int(remaining))}
        if tools:
            kwargs["tools"] = tools
            if finalize:
                kwargs["tool_choice"] = "none"  # answer from gathered evidence only
        if os.environ.get("FRAUD_INV_API_BASE"):
            kwargs["api_base"] = os.environ["FRAUD_INV_API_BASE"]  # local / self-hosted endpoints
        if effort and PASS_REASONING_EFFORT:
            kwargs["reasoning_effort"] = effort
        resp = completion(**kwargs)
        turns += 1
        u = getattr(resp, "usage", None) or (resp.get("usage") if isinstance(resp, dict) else None) or {}
        u = u if isinstance(u, dict) else (u.model_dump() if hasattr(u, "model_dump") else vars(u))
        usage["input_tokens"] += int(u.get("prompt_tokens") or u.get("input_tokens") or 0)
        usage["output_tokens"] += int(u.get("completion_tokens") or u.get("output_tokens") or 0)
        try:
            import litellm
            cost += float(litellm.completion_cost(completion_response=resp) or 0.0)
        except Exception:
            pass
        choice = (resp["choices"] if isinstance(resp, dict) else resp.choices)[0]
        msg = _msg_to_dict(choice["message"] if isinstance(choice, dict) else choice.message)
        messages.append(msg)
        if msg.get("content"):
            _emit(sink, stage, "text", {"text": msg["content"]})
        calls = msg.get("tool_calls") or []
        if not calls:
            return {"text": msg.get("content") or "", "turns": turns, "usage": usage, "cost": cost,
                    "hit_limit": None}
        for call in calls:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}
            _emit(sink, stage, "tool_use", {"name": name, "input": args, "id": call.get("id", "")})
            result = executor.run(name, args) if isinstance(args, dict) else "ERROR: arguments must be an object"
            _emit(sink, stage, "tool_result", {"tool_use_id": call.get("id", ""), "content": result[:4096]})
            messages.append({"role": "tool", "tool_call_id": call.get("id"), "name": name, "content": result})


def run_step(*, prompt: str, schema: "type[T] | None", out_dir: Path, step_name: str, timeout_sec: int,
             extra_args: list[str] | None = None, session_id: str | None = None,
             is_continuation: bool = False, event_sink: Any = None, stage_name: str | None = None,
             calibration: bool = False, completion: CompletionFn | None = None) -> "T | str":
    """Run one agent step on the provider-agnostic backend. Mirrors ``claude_p.invoke_agent``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = out_dir / f"{step_name}.prompt.md"
    raw_path = out_dir / f"{step_name}.raw.json"
    parsed_path = out_dir / f"{step_name}.parsed.json"
    meta_path = out_dir / f"{step_name}.meta.json"

    if os.environ.get("FRAUD_INV_NO_CACHE_LOAD") != "1":
        cached = cp._load_cached_parsed(schema, parsed_path)
        if cached is not None:
            LOG.info(f"[{step_name}] CACHED - loaded existing {parsed_path.name}")
            return cached

    completion = completion or _default_completion()
    model = resolve_api_model(step_name)
    effort = cp._resolve_effort_for_step(step_name)
    max_turns = cp.TURN_BUDGET_BY_STEP.get(step_name, cp.DEFAULT_TURN_BUDGET)
    if calibration:
        from . import calibration as _cal
        ctx = _cal.get_calibration_context()
        if ctx is not None:
            max_turns = ctx.turn_budget_for(step_name, max_turns)
    executor = ToolExecutor(cp.get_default_cwd(), policy_from_flags(extra_args))

    messages = _load_session(session_id) if (session_id and is_continuation) else []
    if not messages or messages[0].get("role") != "system":
        messages.insert(0, {"role": "system", "content": SYSTEM_PROMPT})
    current_prompt = prompt
    finalize = False
    attempts: list[dict] = []
    totals = {"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "turns": 0}

    for attempt in range(1, cp.MAX_PARSE_RETRIES + 1):
        prompt_path.write_text(current_prompt)
        messages.append({"role": "user", "content": current_prompt})
        started = datetime.utcnow()
        rec: dict[str, Any] = {"attempt": attempt, "started": started.isoformat(), "model": model,
                               "effort": effort, "backend": "api"}
        LOG.info(f"[{step_name}] attempt {attempt}/{cp.MAX_PARSE_RETRIES} (api backend, model={model}, "
                 f"max_turns={max_turns}, tools={len(executor.tool_specs())})")
        try:
            res = run_conversation(messages=messages, model=model, executor=executor,
                                   max_turns=3 if finalize else max_turns,
                                   deadline=time.monotonic() + timeout_sec, completion=completion,
                                   effort=effort, sink=event_sink, stage=stage_name, finalize=finalize)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            rec.update(finished=datetime.utcnow().isoformat(), error=err[:2000])
            # Drop the dangling user turn so a retry resends a clean conversation.
            if messages and messages[-1].get("role") == "user":
                messages.pop()
            if cp.is_transient_api_failure(err) and attempt < cp.MAX_PARSE_RETRIES:
                rec["retry_reason"] = "transient_api_failure"
                attempts.append(rec)
                meta_path.write_text(json.dumps(cp._step_meta_payload(step_name, attempts), indent=2))
                wait = cp.transient_backoff_sec(attempt)
                LOG.warning(f"[{step_name}] transient provider error; retrying in {wait:.0f}s: {err[:200]}")
                time.sleep(wait)
                continue
            attempts.append(rec)
            meta_path.write_text(json.dumps(cp._step_meta_payload(step_name, attempts), indent=2))
            raise cp.ClaudePInvocationError(f"[{step_name}] api backend failed: {err[:2000]}") from e
        finally:
            _save_session(session_id, messages)

        for k in ("input_tokens", "output_tokens"):
            totals[k] += res["usage"][k]
        totals["cost"] += res["cost"]
        totals["turns"] += res["turns"]
        finished = datetime.utcnow()
        rec.update(finished=finished.isoformat(), duration_sec=(finished - started).total_seconds(),
                   turns=res["turns"], usage=res["usage"], cost_usd=round(res["cost"], 6))

        if res["hit_limit"]:
            rec["error"] = f"{res['hit_limit']} - resuming to finalize"
            rec["retry_reason"] = "wall_clock_timeout" if res["hit_limit"] == "timeout" else "max_turns"
            attempts.append(rec)
            meta_path.write_text(json.dumps(cp._step_meta_payload(step_name, attempts), indent=2))
            if attempt < cp.MAX_PARSE_RETRIES:
                current_prompt = cp._finalize_prompt()
                finalize = True
                continue
            raise cp.ClaudePInvocationError(f"[{step_name}] {res['hit_limit']} on final attempt")

        text = res["text"] or ""
        raw_path.write_text(json.dumps({
            "type": "result", "backend": "api", "result": text, "num_turns": totals["turns"],
            "total_cost_usd": round(totals["cost"], 6),
            "usage": {"input_tokens": totals["input_tokens"], "output_tokens": totals["output_tokens"]},
            "modelUsage": {model: {"inputTokens": totals["input_tokens"],
                                   "outputTokens": totals["output_tokens"],
                                   "costUSD": round(totals["cost"], 6)}},
        }, indent=2))
        (out_dir / f"{step_name}.raw.attempt{attempt}.json").write_text(text)
        clean = cp._strip_code_fence(text)
        try:
            result = cp._coerce_and_persist(clean, schema, parsed_path)
        except ValidationError as e:
            rec["error"] = f"schema-validation: {type(e).__name__}"
            rec["validation_error"] = str(e)[:2000]
            attempts.append(rec)
            current_prompt = cp._repair_prompt(clean, error_msg=(
                f"Your previous JSON failed Pydantic validation against the "
                f"{schema.__name__} schema:\n\n{str(e)[:1500]}"))
            continue
        except ValueError as e:
            rec["error"] = f"not-json: {type(e).__name__}"
            rec["validation_error"] = str(e)[:2000]
            attempts.append(rec)
            current_prompt = (
                "Your previous response was NOT valid JSON. It began:\n"
                f"    {clean[:300]!r}\n"
                "Return ONLY valid JSON matching the schema from the original instructions. "
                "No prose, no code fences.")
            continue

        rec["status"] = "ok"
        attempts.append(rec)
        meta_path.write_text(json.dumps(cp._step_meta_payload(step_name, attempts), indent=2))
        LOG.info(f"[{step_name}] attempt {attempt} OK via api backend ({rec['duration_sec']:.1f}s, "
                 f"{res['turns']} turns, ${res['cost']:.4f})")
        if schema is not None and cp.DISPLAY_PARSED_JSON:
            cp._display_parsed_json(step_name, result, rec["duration_sec"])
        return result

    meta_path.write_text(json.dumps(cp._step_meta_payload(step_name, attempts), indent=2))
    raise cp.ClaudePInvocationError(
        f"[{step_name}] failed after {cp.MAX_PARSE_RETRIES} attempts (api backend). See {meta_path}.")
