"""Aggregate token usage + cost across all claude -p calls in a case dir.

Each agent step's raw.json carries:
  - total_cost_usd               dollar cost of the call
  - usage.{input,output,cache_*}_tokens   raw token counts
  - modelUsage.{model}.costUSD   per-model breakdown
  - duration_ms                  wall-clock
  - num_turns                    number of agent loop turns inside the call

This script walks state/<case>/iter-N/*.raw.json + adv-N/*.raw.json
+ case/*.raw.json and produces:
  1. Per-step row (model, tokens in/out, cost, duration)
  2. Per-iteration subtotal
  3. Grand total + per-model breakdown

Usage:
    python scripts/usage_report.py --case-dir state/_larch_v2/CASE-6A0A1D6D-2026
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

# Shared parsing logic lives in fraud_investigator.usage so every
# report uses the same cost model without duplicating it.
# Fall back to local definitions when the src tree is not on sys.path (e.g. when
# running the script directly from inside the scripts/ directory).
try:
    import sys as _sys
    import os as _os
    _src = _os.path.join(_os.path.dirname(__file__), "..", "src")
    if _src not in _sys.path:
        _sys.path.insert(0, _src)
    from fraud_investigator.usage import _load_raw, _extract_row  # noqa: F401
except ImportError:
    import json as _json

    def _load_raw(p: Path) -> dict | None:  # type: ignore[misc]
        try:
            return _json.loads(p.read_text())
        except Exception:
            return None

    def _extract_row(p: Path, raw: dict) -> dict:  # type: ignore[misc]
        step = p.stem.replace(".raw", "")
        usage = raw.get("usage", {}) or {}
        model_usage = raw.get("modelUsage", {}) or {}
        primary_model = (
            max(
                model_usage.keys(),
                key=lambda k: model_usage[k].get("inputTokens", 0) + model_usage[k].get("outputTokens", 0),
                default="?",
            )
            if model_usage
            else "?"
        )
        return {
            "step": step,
            "phase": p.parent.name,
            "model": primary_model,
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "cache_read_tokens": usage.get("cache_read_input_tokens", 0),
            "cache_create_tokens": usage.get("cache_creation_input_tokens", 0),
            "cost_usd": raw.get("total_cost_usd", 0.0),
            "duration_sec": raw.get("duration_ms", 0) / 1000,
            "num_turns": raw.get("num_turns", 0),
            "model_usage": model_usage,
        }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--case-dir", required=True, type=Path)
    ap.add_argument("--verbose", "-v", action="store_true", help="Show per-step rows")
    args = ap.parse_args(argv)

    if not args.case_dir.exists():
        print(f"Case dir not found: {args.case_dir}", file=sys.stderr)
        return 1

    raw_files = sorted(args.case_dir.rglob("*.raw.json"))
    if not raw_files:
        print(f"No raw.json files found under {args.case_dir}")
        return 0

    rows = []
    for p in raw_files:
        raw = _load_raw(p)
        if raw is None:
            continue
        rows.append(_extract_row(p, raw))

    # ─── Per-step (verbose) ───
    if args.verbose:
        print(f"{'phase':<10} {'step':<25} {'model':<32} {'in_tok':>8} {'out_tok':>7} {'cache_r':>8} {'cache_c':>8} {'cost$':>7} {'sec':>6} {'turns':>5}")
        print("-" * 130)
        for r in rows:
            print(f"{r['phase']:<10} {r['step']:<25} {r['model']:<32} "
                  f"{r['input_tokens']:>8,} {r['output_tokens']:>7,} "
                  f"{r['cache_read_tokens']:>8,} {r['cache_create_tokens']:>8,} "
                  f"${r['cost_usd']:>5.3f} {r['duration_sec']:>6.1f} {r['num_turns']:>5}")
        print()

    # ─── Per-phase subtotal ───
    by_phase: dict[str, dict] = defaultdict(lambda: dict(
        cost=0.0, in_tok=0, out_tok=0, cache_r=0, cache_c=0, dur=0.0, n=0
    ))
    for r in rows:
        b = by_phase[r["phase"]]
        b["cost"] += r["cost_usd"]
        b["in_tok"] += r["input_tokens"]
        b["out_tok"] += r["output_tokens"]
        b["cache_r"] += r["cache_read_tokens"]
        b["cache_c"] += r["cache_create_tokens"]
        b["dur"] += r["duration_sec"]
        b["n"] += 1

    print(f"{'phase':<10} {'#steps':>6} {'in_tok':>10} {'out_tok':>9} {'cache_r':>10} {'cache_c':>10} {'cost$':>8} {'min':>7}")
    print("-" * 78)
    for phase in sorted(by_phase.keys()):
        b = by_phase[phase]
        print(f"{phase:<10} {b['n']:>6} {b['in_tok']:>10,} {b['out_tok']:>9,} "
              f"{b['cache_r']:>10,} {b['cache_c']:>10,} ${b['cost']:>6.3f} {b['dur']/60:>6.1f}")

    # ─── Grand totals ───
    total_cost = sum(r["cost_usd"] for r in rows)
    total_in = sum(r["input_tokens"] for r in rows)
    total_out = sum(r["output_tokens"] for r in rows)
    total_cache_r = sum(r["cache_read_tokens"] for r in rows)
    total_cache_c = sum(r["cache_create_tokens"] for r in rows)
    total_dur = sum(r["duration_sec"] for r in rows)
    print("-" * 78)
    print(f"{'TOTAL':<10} {len(rows):>6} {total_in:>10,} {total_out:>9,} "
          f"{total_cache_r:>10,} {total_cache_c:>10,} ${total_cost:>6.3f} {total_dur/60:>6.1f}")

    # ─── Per-model breakdown ───
    by_model: dict[str, dict] = defaultdict(lambda: dict(in_tok=0, out_tok=0, cost=0.0, calls=0))
    for r in rows:
        for model, mu in r["model_usage"].items():
            b = by_model[model]
            b["in_tok"] += mu.get("inputTokens", 0)
            b["out_tok"] += mu.get("outputTokens", 0)
            b["cost"] += mu.get("costUSD", 0)
            b["calls"] += 1
    if by_model:
        print()
        print(f"{'model':<40} {'calls':>6} {'in_tok':>10} {'out_tok':>9} {'cost$':>8}")
        print("-" * 78)
        for model in sorted(by_model.keys()):
            b = by_model[model]
            print(f"{model:<40} {b['calls']:>6} {b['in_tok']:>10,} {b['out_tok']:>9,} ${b['cost']:>6.3f}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
