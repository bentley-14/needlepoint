"""Load-time pipeline-contract verification + per-run provenance manifest.

The canonical pipeline config lives in ``pipeline_contract.yaml`` (next to this
file). It is the single source of truth that both the walk-forward runner and
``scripts/preflight_walk_forward.py`` check the live pipeline against, so silent
config drift can't cost hours of compute. Human companion: ``docs/WALK_FORWARD_PIPELINE.md``.

Drift policy (chosen 2026-05-28):
  * ``verify_pipeline_contract`` WARNS loudly on any deviation from non-invariant
    values and writes a per-run provenance manifest into the run output dir.
  * It HARD-ERRORS (``ContractViolation``) only on ``invariants`` — the
    never-deviate entries (label source, GNN form/dims, no-leakage).
  * If pyyaml is unavailable the check degrades to a warning and SKIPS rather
    than crashing the runner — the checker must never be the thing that breaks a
    run.

The runner passes a flat ``live`` dict built from its own module constants::

    from fraudlib.pipeline_contract import verify_pipeline_contract
    warns = verify_pipeline_contract({
        "label_source":        str(CASES_PATH),
        "gnn_form":            "pca",
        "gnn_pca_dims":        16,
        "blend_weights":       {"xgb": 1.0, "neural": 1.0, "perspec": 1.0},
        "rrf_k":               RRF_K,
        "heads_train_full":    list(SCHEMES_TRAIN_FULL),
        "scale_pos_weight":    "(n_neg / n_pos) * 2.0",
        "early_stopping_rounds": EARLY_STOPPING_ROUNDS,
        "agent_filter":        AGENT_FILTER_NAME,
    }, run_dir=OUT_DIR)
    for w in warns:
        _log(f"  [contract] {w}")
"""
from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Any

_CONTRACT_PATH = Path(__file__).with_name("pipeline_contract.yaml")


class ContractViolation(RuntimeError):
    """Raised when a never-deviate invariant is breached."""


def load_contract(path: Path = _CONTRACT_PATH) -> dict[str, Any]:
    """Parse the YAML contract. Returns ``{}`` if pyyaml is unavailable (the
    caller treats an empty contract as "check skipped")."""
    try:
        import yaml  # noqa: PLC0415 — optional dependency, guarded on purpose
    except ImportError:
        return {}
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def verify_pipeline_contract(
    live: dict[str, Any],
    run_dir: Path | str | None = None,
    *,
    strict_invariants: bool = True,
) -> list[str]:
    """Check ``live`` runner config against the contract.

    Returns a list of human-readable warning strings (empty == clean).
    Raises :class:`ContractViolation` if a never-deviate invariant is breached
    and ``strict_invariants`` is True. Always writes a provenance manifest into
    ``run_dir`` (if given) recording the exact ``live`` config that ran.
    """
    contract = load_contract()
    warnings: list[str] = []

    if not contract:
        warnings.append(
            "CONTRACT CHECK SKIPPED — pyyaml not installed or contract missing "
            "(`pip install pyyaml` to enable drift checks)."
        )
        _write_manifest(run_dir, live, contract_checked=False)
        return warnings

    # ── invariants: HARD ERROR ────────────────────────────────────────────
    inv = contract.get("invariants", {}) or {}
    errs: list[str] = []
    if "gnn_form" in live and live["gnn_form"] != inv.get("gnn_form"):
        errs.append(f"gnn_form={live['gnn_form']!r} != invariant {inv.get('gnn_form')!r}")
    if "gnn_pca_dims" in live and live["gnn_pca_dims"] != inv.get("gnn_pca_dims"):
        errs.append(f"gnn_pca_dims={live['gnn_pca_dims']} != invariant {inv.get('gnn_pca_dims')}")
    if "label_source" in live:
        want = (inv.get("label_source") or {}).get("path")
        if want and Path(str(live["label_source"])).name != Path(want).name:
            errs.append(f"label_source={live['label_source']!r} != invariant {want!r}")

    if errs and strict_invariants:
        _write_manifest(run_dir, live, contract_checked=True, invariant_errors=errs)
        raise ContractViolation(
            "Pipeline INVARIANT breach (never-deviate) — refusing to run:\n  - "
            + "\n  - ".join(errs)
            + f"\nFix the runner, or update {_CONTRACT_PATH.name} if this change is intended."
        )
    warnings += [f"INVARIANT (non-strict): {e}" for e in errs]

    # ── runner_config: WARN ───────────────────────────────────────────────
    rc = contract.get("runner_config", {}) or {}
    blend = rc.get("blend", {}) or {}
    heads = rc.get("heads", {}) or {}
    xgb = rc.get("xgb", {}) or {}
    af = rc.get("agent_filter", {}) or {}

    def _warn_if(name: str, live_val: Any, want_val: Any) -> None:
        if live_val is not None and want_val is not None and live_val != want_val:
            warnings.append(f"{name}: live={live_val!r} != contract={want_val!r}")

    _warn_if("blend.weights", live.get("blend_weights"), blend.get("weights"))
    _warn_if("blend.rrf_k", live.get("rrf_k"), blend.get("rrf_k"))
    _warn_if("heads.train_full", live.get("heads_train_full"), heads.get("train_full"))
    _warn_if("xgb.scale_pos_weight", live.get("scale_pos_weight"), xgb.get("scale_pos_weight"))
    _warn_if("xgb.early_stopping_rounds", live.get("early_stopping_rounds"), xgb.get("early_stopping_rounds"))
    _warn_if("agent_filter.name", live.get("agent_filter"), af.get("name"))
    nn = rc.get("neural", {}) or {}
    _warn_if("neural.device", live.get("neural_device"), nn.get("device"))
    _warn_if("neural.seed", live.get("neural_seed"), nn.get("seed"))

    _write_manifest(run_dir, live, contract_checked=True, warnings=warnings,
                    contract_version=contract.get("version"))
    return warnings


def _write_manifest(run_dir: Path | str | None, live: dict[str, Any], **meta: Any) -> None:
    """Write the exact live config (+ check metadata) to a per-run manifest.

    This is the durable provenance record — even when the check warns or skips,
    the manifest captures what actually ran, so a result is never orphaned from
    the config that produced it."""
    if run_dir is None:
        return
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "written": _dt.datetime.now().isoformat(timespec="seconds"),
        "live_config": live,
        **meta,
    }
    (run_dir / "pipeline_run_manifest.json").write_text(
        json.dumps(payload, indent=2, default=str)
    )


if __name__ == "__main__":
    # Sanity: load + summarize the contract.
    c = load_contract()
    if not c:
        raise SystemExit("contract unavailable (pyyaml missing or file absent)")
    print(f"pipeline_contract.yaml v{c.get('version')} ({c.get('updated')})")
    print("  invariants:", list((c.get("invariants") or {}).keys()))
    print("  blend:", (c.get("runner_config", {}).get("blend", {})).get("weights"))
    print("  heads:", (c.get("runner_config", {}).get("heads", {})).get("train_full"))
    # Self-test: a clean live config should produce zero warnings.
    clean = {
        "label_source": "data/extracted/gold_set.jsonl",
        "gnn_form": "pca", "gnn_pca_dims": 16,
        "blend_weights": {"xgb": 1.0, "neural": 1.0, "perspec": 1.0},
        "rrf_k": 60,
        "heads_train_full": ["PHANTOM_BILLING", "MED_UNNECESSARY", "KICKBACK", "OPIOID", "UPCODING"],
        "scale_pos_weight": "(n_neg / n_pos) * 2.0",
        "early_stopping_rounds": 50,
        "agent_filter": "floor_0.50",
    }
    w = verify_pipeline_contract(clean, run_dir=None)
    print("  self-test (clean config) warnings:", w if w else "none ✓")
