"""Mock Layer 1 — stand-in for ``fraudlib.ranker.pipeline.score()``.

Exists so the orchestrator + smoke + backtest paths can run end-to-end
without a trained model artifact on disk or an ``ANTHROPIC_API_KEY``
in the environment. The schema is pinned to
:data:`fraudlib.ranker.pipeline.SCORE_KEEP_COLS` and a dtype test
in ``tests/test_train_test_pipeline.py`` keeps the two in sync.

For a backtest against 2013, the top rows are the seven
``pinned_2013_case_ids`` from ``data/source/cases/eval_split.json``,
resolved to NPIs at call time via ``data/extracted/gold_set.jsonl``
so the mock stays correct if ground truth evolves. Downstream code
can rely on those 7 known-fraud NPIs landing at rank 1-7, which is
how the smoke script verifies the CandidateRow → SQLite → rehydrate
path preserves order.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from fraudlib.ranker.pipeline import SCORE_KEEP_COLS

DEFAULT_EVAL_SPLIT_PATH = Path("data/source/cases/eval_split.json")
DEFAULT_CASES_PATH = Path("data/extracted/gold_set.jsonl")


def _resolve_pinned_npis(
    score_year: int,
    eval_split_path: Path,
    cases_path: Path,
) -> list[dict[str, Any]]:
    """Return pinned-case defendants active in ``score_year`` as dicts with
    ``npi``, ``case_source``, ``specialty``, ``state``, ``name`` keys.
    Only ``npi_confidence == "high"`` defendants are included.
    """
    if not eval_split_path.exists() or not cases_path.exists():
        return []
    split = json.loads(eval_split_path.read_text())
    pinned_ids = split.get("pinned_2013_case_ids", [])
    cases = [json.loads(l) for l in cases_path.read_text().splitlines() if l.strip()]
    by_id = {c["case_id"]: c for c in cases}

    out: list[dict[str, Any]] = []
    for cid in pinned_ids:
        case = by_id.get(cid)
        if case is None:
            continue
        if score_year not in case.get("active_years", []):
            continue
        for d in case.get("defendants", []):
            if d.get("npi_confidence") != "high":
                continue
            npi = d.get("npi")
            if not npi:
                continue
            surname = (d.get("name") or "?").split()[-1]
            spec = (d.get("specialty") or "?").split(",")[0].strip()
            out.append({
                "npi": str(npi),
                "case_source": cid,
                "specialty": spec,
                "state": (d.get("state") or "").strip().upper(),
                "name": f"{surname} ({spec.lower()})",
            })
    return out


def _synthetic_drivers(specialty: str, rank: int) -> list[dict[str, Any]]:
    """Return a plausible driver list matching team's score() shape.

    Each driver dict has: feature, label, value, shap, percentile, fraud_context.
    Values are deterministic functions of rank so the mock is reproducible.
    Percentiles are clamped to [0.0, 99.9] so the mock stays within the
    same value range as real SHAP-driven percentiles at any top_k.
    """
    base = 1.0 / (rank + 1)
    def _pct(start: float) -> float:
        return round(max(0.0, min(99.9, start - 0.1 * rank)), 1)

    drivers: list[dict[str, Any]] = [
        {
            "feature": "partb_svcs_per_bene",
            "label": "services per beneficiary",
            "value": round(120.0 * base + 40.0, 2),
            "shap": round(0.42 * base, 4),
            "percentile": _pct(99.5),
            "fraud_context": "very high services per beneficiary vs specialty peers",
        },
        {
            "feature": "partb_tot_mdcr_pymt_amt",
            "label": "total Medicare paid",
            "value": round(5_000_000.0 * base + 500_000.0, 2),
            "shap": round(0.30 * base, 4),
            "percentile": _pct(99.2),
            "fraud_context": f"{specialty.lower()} provider at national p99 for total paid",
        },
        {
            "feature": "partb_pay_per_bene",
            "label": "payment per beneficiary",
            "value": round(2_500.0 * base + 400.0, 2),
            "shap": round(0.18 * base, 4),
            "percentile": _pct(98.5),
            "fraud_context": "elevated per-beneficiary payment",
        },
        {
            "feature": "partb_drug_frac",
            "label": "drug-service fraction",
            "value": round(0.95 - 0.05 * (rank % 3), 3),
            "shap": round(0.12 * base, 4),
            "percentile": _pct(97.0),
            "fraud_context": "near-100% drug services suggests infusion-clinic pattern",
        },
        {
            "feature": "nppes_taxonomy_bucket",
            "label": "taxonomy bucket",
            "value": specialty,
            "shap": round(0.05 * base, 4),
            "percentile": None,
            "fraud_context": "specialty with historical enforcement activity",
        },
    ]
    return drivers


def _flag_summary_from_drivers(drivers: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for d in drivers:
        direction = "high" if d["shap"] > 0 else "low"
        pct = d.get("percentile")
        pct_str = f" (P{pct:.0f})" if pct is not None else ""
        ctx = f": {d['fraud_context']}" if d.get("fraud_context") else ""
        parts.append(f"{direction} {d['label']}{pct_str}{ctx}")
    return "; ".join(parts)


def mock_score(
    score_year: int,
    top_k: int = 1000,
    eval_split_path: Path | str = DEFAULT_EVAL_SPLIT_PATH,
    cases_path: Path | str = DEFAULT_CASES_PATH,
    seed: int = 0,
) -> pd.DataFrame:
    """Return a top-K DataFrame with the same column + dtype shape as
    :func:`fraudlib.ranker.pipeline.score`.

    Pinned 2013 eval positives — resolved from ``eval_split_path`` and
    ``cases_path`` at call time — occupy ranks 1..N (N<=7). The rest is
    synthetic filler with monotonically decreasing ``risk_score`` so
    ``risk_rank`` is strictly ascending.
    """
    rng = np.random.default_rng(seed)

    pinned = _resolve_pinned_npis(
        score_year,
        Path(eval_split_path),
        Path(cases_path),
    )

    rows: list[dict[str, Any]] = []
    # Pinned NPIs first — label_bucket="case_only", fraud_case=1
    for i, p in enumerate(pinned[:top_k]):
        rank = i + 1
        drivers = _synthetic_drivers(p["specialty"], rank)
        rows.append({
            "npi": p["npi"],
            "risk_score": float(0.99 - 0.005 * i),
            "risk_rank": rank,
            "label_bucket": "case_only",
            "fraud_case": 1,
            "drivers": drivers,
            "flag_summary": _flag_summary_from_drivers(drivers),
        })

    # Synthetic filler
    filler_start = len(rows)
    for i in range(filler_start, top_k):
        rank = i + 1
        # 10-digit pseudo-NPI, deterministic per (seed, rank)
        npi = str(9_000_000_000 + int(rng.integers(0, 999_999_999)))
        drivers = _synthetic_drivers("Internal Medicine", rank)
        rows.append({
            "npi": npi,
            "risk_score": float(max(0.01, 0.95 - 0.0005 * i)),
            "risk_rank": rank,
            "label_bucket": "neither",
            "fraud_case": 0,
            "drivers": drivers,
            "flag_summary": _flag_summary_from_drivers(drivers),
        })

    df = pd.DataFrame(rows, columns=list(SCORE_KEEP_COLS))
    # Pin dtypes explicitly so the parity test against team's score() can
    # rely on exact equality, not pandas' type inference vagaries.
    df["npi"] = df["npi"].astype(str)
    df["risk_score"] = df["risk_score"].astype(float)
    df["risk_rank"] = df["risk_rank"].astype(int)
    df["label_bucket"] = df["label_bucket"].astype(str)
    df["fraud_case"] = df["fraud_case"].astype(int)
    df["flag_summary"] = df["flag_summary"].astype(str)
    return df.reset_index(drop=True)
