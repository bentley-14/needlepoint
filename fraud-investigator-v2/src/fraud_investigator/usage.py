"""Per-case token + cost accounting, backed by *.raw.json / *.raw.jsonl step artifacts.

Each agent step writes a *.raw.json under the case dir with the fields
parsed by usage_report.py's _extract_row():

    total_cost_usd          float — total cost for the claude -p call
    usage.input_tokens      int
    usage.output_tokens     int
    modelUsage.<model>.{inputTokens, outputTokens, cacheRead/CreationInputTokens, costUSD}

This module exposes ``case_cost(case_dir)`` which aggregates those files and
returns a lightweight dict.  ``usage_report.py`` imports this so its own
parsing logic stays in one place.

Usage
-----
    from fraud_investigator.usage import case_cost
    result = case_cost(Path("state/calibration/CALIBRATION-65CFEA7D"))
    # -> {"usd": 16.24, "input_tokens": 10107, "output_tokens": 319365, "by_stage": {...}}
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path


def _load_raw(p: Path) -> dict | None:
    """Load a per-step raw artifact; return None on any error.

    Two formats exist (opus review of PR #283, finding 1):
      *.raw.json   — non-streaming invoke path: ONE json object
      *.raw.jsonl  — STREAMING path (what production/console runs write):
                     stream events, the LAST `type:"result"` line carries
                     total_cost_usd + usage. We parse that final result line.
    """
    try:
        text = p.read_text()
        if p.name.endswith(".raw.jsonl"):
            for line in reversed(text.splitlines()):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if obj.get("type") == "result":
                    return obj
            return None
        return json.loads(text)
    except Exception:
        return None


def _raw_artifacts(case_dir: Path) -> list[Path]:
    """Canonical per-step raw artifacts: *.raw.json + *.raw.jsonl, EXCLUDING
    *.raw.attempt*.jsonl — attempt files duplicate the winning attempt's cost
    (and a FAILED attempt can cost more than the final: $5.73 vs $0.57 seen
    in a real adv-r2). The final <step>.raw.jsonl is the canonical cost
    (streaming.py writes it from the winning attempt). Opus #283 finding 2.
    """
    files = list(case_dir.rglob("*.raw.json")) + [
        f for f in case_dir.rglob("*.raw.jsonl")
        if ".raw.attempt" not in f.name
    ]
    return sorted(files)


def _extract_row(p: Path, raw: dict) -> dict:
    """Mirror of usage_report.py _extract_row — single source of parsing logic."""
    usage = raw.get("usage", {}) or {}
    model_usage = raw.get("modelUsage", {}) or {}
    primary_model = (
        max(
            model_usage.keys(),
            key=lambda k: (
                model_usage[k].get("inputTokens", 0)
                + model_usage[k].get("outputTokens", 0)
            ),
            default="?",
        )
        if model_usage
        else "?"
    )
    return {
        "step": p.stem.replace(".raw", ""),
        "phase": p.parent.name,
        "model": primary_model,
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "cache_read_tokens": usage.get("cache_read_input_tokens", 0),
        "cache_create_tokens": usage.get("cache_creation_input_tokens", 0),
        "cost_usd": raw.get("total_cost_usd", 0.0) or 0.0,
        # duration_sec / num_turns are used by usage_report.py's verbose table
        "duration_sec": raw.get("duration_ms", 0) / 1000,
        "num_turns": raw.get("num_turns", 0),
        "model_usage": model_usage,
    }


def case_cost(
    case_dir: "str | Path",
) -> dict:
    """Aggregate token usage and cost for a single case directory.

    Scans ``case_dir`` recursively for ``*.raw.json`` + final ``*.raw.jsonl``
    files (attempt files excluded — see _raw_artifacts) (the per-step
    artifacts written by ``claude -p``).  Returns a dict:

        {
            "usd":           float | None,   # total cost; None when no files found
            "input_tokens":  int,
            "output_tokens": int,
            "by_stage": {
                "<phase>": {
                    "usd": float, "input_tokens": int, "output_tokens": int
                }
            },
        }

    Tolerant: missing/partial/corrupt files are skipped silently; an empty
    case dir returns ``{"usd": None, "input_tokens": 0, "output_tokens": 0,
    "by_stage": {}}``.
    """
    case_dir = Path(case_dir)
    raw_files = _raw_artifacts(case_dir)

    if not raw_files:
        return {"usd": None, "input_tokens": 0, "output_tokens": 0, "by_stage": {}}

    rows = []
    for p in raw_files:
        raw = _load_raw(p)
        if raw is None:
            continue
        rows.append(_extract_row(p, raw))

    if not rows:
        return {"usd": None, "input_tokens": 0, "output_tokens": 0, "by_stage": {}}

    total_usd = sum(r["cost_usd"] for r in rows)
    total_in = sum(r["input_tokens"] for r in rows)
    total_out = sum(r["output_tokens"] for r in rows)

    by_stage: dict[str, dict] = defaultdict(
        lambda: {"usd": 0.0, "input_tokens": 0, "output_tokens": 0}
    )
    for r in rows:
        s = by_stage[r["phase"]]
        s["usd"] += r["cost_usd"]
        s["input_tokens"] += r["input_tokens"]
        s["output_tokens"] += r["output_tokens"]

    return {
        "usd": total_usd,
        "input_tokens": total_in,
        "output_tokens": total_out,
        "by_stage": dict(by_stage),
    }
