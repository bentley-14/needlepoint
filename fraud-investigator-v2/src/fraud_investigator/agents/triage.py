"""Stage 3 — Triage Agent driver (canonical; supersedes the reorder-only `rerank`).

Takes the ML top-K from scored_full_{year}.parquet and assigns each provider a
forensic **verdict** (HIGH_RISK / MODERATE_RISK / LOW_RISK / DISMISS) with a matched
DOJ archetype, confidence, and named signals — the exact behavior of Agent Triage v3,
re-homed into the v2 batched-streaming pipeline.

Input record per NPI = the UNION (de-duplicated) of v3's `build_record` fields and the
rerank agent's HCPCS-signature / DMEPOS / cross-program / Open-Payments columns. See
`prompts/triage.md` for the rubric and the full field list.

**NO WEB SEARCH** at this layer (tool whitelist excludes WebSearch/WebFetch). External
lookups happen in Stage 4 (the investigator).

Amortization: processed in batches of ~50 NPIs; each batch is one `claude -p` call that
emits a partial TriageOutput; the driver merges and writes the canonical queue file at
``validation/triage/{year}_top{lo}-{hi}.json``.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pandas as pd

from ..claude_p import DEFAULT_CLAUDE_BIN
from ..event_sink import (
    EventSink,
    STAGE_TRIAGE,
    EVENT_STAGE_COMPLETE,
    EVENT_STAGE_FAILED,
    EVENT_STAGE_LOG_LINE,
    EVENT_STAGE_START,
)
from ..pipeline_schemas import TriageItem, TriageOutput
from ..streaming import invoke_agent_streaming

LOG = logging.getLogger("fraud_investigator.triage")

REPO_ROOT = Path(__file__).resolve().parents[4]
PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "triage.md"

# Outcome columns that MUST never reach the agent (label leakage).
OUTCOME_COLS = {"y_eval", "archetype", "fraud_npi"}


# ── per-row helpers (ported from prepare_agent_triage_batches.py) ──────────────
def _get(row, col, default=None):
    if col not in row.index:
        return default
    v = row[col]
    try:
        if pd.isna(v):
            return default
    except (TypeError, ValueError):
        pass
    return v


def _f(row, col, digits=2):
    v = _get(row, col)
    try:
        return round(float(v), digits) if v is not None else None
    except (ValueError, TypeError):
        return None


def _i(row, col):
    v = _get(row, col)
    try:
        return int(v) if v is not None else None
    except (ValueError, TypeError):
        return None


def _pct(row, col):
    return _f(row, col, digits=3)


def _build_record(row, rank, cross_year_ranks, prior_year):
    """Per-NPI record handed to the agent. Union of v3 build_record + rerank signals."""
    npi = str(row["npi"]).zfill(10)
    first = _get(row, "partb_rndrng_prvdr_first_name", "")
    last = _get(row, "partb_rndrng_prvdr_last_org_name", "")
    name = f"{first} {last}".strip() if (first or last) else "(unknown)"

    benes = _i(row, "partb_n_beneficiaries") or 0
    services = _f(row, "partb_n_services", 1) or 0
    pymt = _f(row, "partb_total_medicare_payment", 0) or 0
    sbmtd = _f(row, "partb_tot_sbmtd_chrg", 0) or 0
    allowed = _f(row, "partb_total_medicare_allowed", 0) or 0
    drug_pymt = _f(row, "partb_drug_mdcr_pymt_amt", 0) or 0
    med_pymt = _f(row, "partb_med_mdcr_pymt_amt", 0) or 0

    bcount = max(benes, 1)
    dual_cnt = _get(row, "partb_bene_dual_cnt", 0) or 0
    age_lt_65 = _get(row, "partb_bene_age_lt_65_cnt", 0) or 0
    age_gt_84 = _get(row, "partb_bene_age_gt_84_cnt", 0) or 0

    # Derived ratios (match agent_triage_2023_v3 build_record)
    svcs_per_day = services / 250.0
    patients_per_day = benes / 250.0
    chrg_to_allowed = sbmtd / max(allowed, 1)
    drug_frac = drug_pymt / max(pymt, 1)

    rank_prior = cross_year_ranks.get(npi)

    return {
        "npi": npi,
        "rank": int(rank),
        "score_rrf": _f(row, "score_rrf", 5),
        "provider": {
            "name": name,
            "specialty": _get(row, "partb_provider_type", "Unknown"),
            "state": _get(row, "partb_state", "??"),
        },
        "volume": {
            "n_beneficiaries": benes,
            "n_services": round(services, 1),
            "n_hcpcs_codes": _i(row, "partb_n_hcpcs_codes"),
            "tot_medicare_payment": round(pymt, 0),
            "tot_submitted_charges": round(sbmtd, 0),
            "tot_medicare_allowed": round(allowed, 0),
            "drug_medicare_payment": round(drug_pymt, 0),
            "medical_medicare_payment": round(med_pymt, 0),
        },
        "derived": {
            "svcs_per_bene": round(services / bcount, 2),
            "pay_per_bene": round(pymt / bcount, 0),
            "pay_per_svc": round(pymt / max(services, 1), 2),
            "svcs_per_day": round(svcs_per_day, 1),
            "patients_per_day": round(patients_per_day, 2),
            "chrg_to_allowed_ratio": round(chrg_to_allowed, 2),
            "drug_frac_of_payment": round(drug_frac, 3),
        },
        "demographics": {
            "avg_age": _f(row, "partb_bene_avg_age", 1),
            "dual_share": round(dual_cnt / bcount, 3),
            "age_lt_65_share": round(age_lt_65 / bcount, 3),
            "age_gt_84_share": round(age_gt_84 / bcount, 3),
        },
        "hcpcs_concentration": {
            "bauder_full_entropy": _f(row, "bauder_full_hcpcs_entropy", 3),
            "bauder_top3_code_share": _f(row, "bauder_top3_code_share", 3),
            "bauder_n_distinct_hcpcs": _i(row, "bauder_n_distinct_hcpcs"),
            "hcpcs_em_distribution_entropy": _f(row, "hcpcs_em_distribution_entropy", 3),
            "hcpcs_top3_code_concentration": _f(row, "hcpcs_top3_code_concentration", 3),
        },
        # NEW (from rerank): single-code dominance, off-specialty, scheme-code shares
        "hcpcs_signatures": {
            "top1_code_share": _f(row, "hcpcs_top1_code_share", 3),
            "revenue_hhi": _f(row, "hcpcs_revenue_hhi", 3),
            "tele_pos_share": _f(row, "hcpcs_tele_pos_share", 3),
            "off_specialty_dollar_share": _f(row, "hcpcs_off_specialty_dollar_share", 3),
            "lab_genetic_share": _f(row, "hcpcs_lab_genetic_share", 3),
            "sentinel_dollar_share": _f(row, "hcpcs_sentinel_dollar_share", 3),
        },
        "partd": {
            "has_partd": bool(_get(row, "has_partd", 0)),
            "opioid_clm_share": _f(row, "partd_opioid_clm_share", 3),
            "opioid_la_clm_share": _f(row, "partd_opioid_la_clm_share", 3),
            "antipsychotic_ge65_clm_share": _f(row, "partd_antipsychotic_ge65_clm_share", 3),
            "brand_clm_share": _f(row, "partd_brand_clm_share", 3),
            "clms_per_bene": _f(row, "partd_clms_per_bene", 2),
            "cost_per_clm": _f(row, "partd_cost_per_clm", 2),
            "tot_drug_cst": _f(row, "partd_tot_drug_cst", 0),
            "tot_clms": _i(row, "partd_tot_clms"),  # NEW (from rerank)
        },
        # NEW (from rerank): DMEPOS brace-mill recipient signals (absent for 2024 → null)
        "dmepos": {
            "total_pay_pct_spec": _pct(row, "dmer_total_pay_pct_spec"),
            "pay_per_bene_pct_spec": _pct(row, "dmer_pay_per_bene_pct_spec"),
            "dme_pay_share": _f(row, "dmer_dme_pay_share", 3),
            "few_suppliers_flag": _i(row, "dmer_few_suppliers_flag"),
            "any_sprsn": _i(row, "dmer_any_sprsn"),
            "dual_share": _f(row, "dmer_dual_share", 3),
        },
        # NEW (from rerank): cross-program brace-mill indicators
        "cross_program": {
            "brace_mill_indicator": _i(row, "xprog_brace_mill_indicator"),
            "dme_to_partb_ratio": _f(row, "xprog_dme_to_partb_ratio", 3),
            "dme_pay_dominance": _f(row, "xprog_dme_pay_dominance", 3),
        },
        # NEW (from rerank): industry-relationship proxy
        "open_payments": {
            "total_received_usd": _f(row, "op_total_received_usd", 0),
            "n_manufacturers": _i(row, "op_n_manufacturers"),
        },
        "within_specialty_percentiles": {
            "n_beneficiaries_pct": _pct(row, "partb_n_beneficiaries_pct"),
            "n_services_pct": _pct(row, "partb_n_services_pct"),
            "tot_payment_pct": _pct(row, "partb_total_medicare_payment_pct"),
            "n_hcpcs_codes_pct": _pct(row, "partb_n_hcpcs_codes_pct"),
            "pay_per_bene_pct": _pct(row, "partb_pay_per_bene_pct"),
            "svcs_per_bene_pct": _pct(row, "partb_svcs_per_bene_pct"),
            "drug_frac_pct": _pct(row, "partb_drug_frac_pct"),
            "chrg_to_allowed_pct": _pct(row, "partb_chrg_to_allowed_pct"),
            "dual_share_pct": _pct(row, "partb_dual_pct_pct"),
            "under65_share_pct": _pct(row, "partb_under65_pct_pct"),
            "over84_share_pct": _pct(row, "partb_over84_pct_pct"),
            "hcpcs_diversity_pct": _pct(row, "partb_hcpcs_diversity_pct"),
            "risk_score_pct": _pct(row, "partb_risk_score_pct"),
            "partd_opioid_clm_share_pct": _pct(row, "partd_opioid_clm_share_pct"),
            # Long-acting-opioid within-specialty percentile — the cleaner
            # intra-specialty diversion signal (a pain/PM&R provider can be
            # specialty-typical on total opioid share yet an extreme LA outlier
            # among peers). Surfaced so angle 2 can fire on it even when the
            # absolute OPIOID_NORMS bar is 999.
            "partd_opioid_la_clm_share_pct": _pct(row, "partd_opioid_la_clm_share_pct"),
        },
        "ranker_signals": {
            "score_rrf": _f(row, "score_rrf", 5),
            "score_xgb_sum": _f(row, "score_xgb_sum", 4),
            "score_neural_sum": _f(row, "score_neural_sum", 4),
            "score_perspec": _f(row, "score_perspec", 4),
        },
        "cross_year": {
            f"in_{prior_year}_top_2k": rank_prior is not None,
            f"rank_{prior_year}": rank_prior,
        },
    }


def _load_cross_year_ranks(path: Path | None, top_n: int = 2000) -> dict[str, int]:
    if not path or not Path(path).exists():
        return {}
    cy = pd.read_parquet(path, columns=["npi", "score_rrf"])
    cy = cy.sort_values("score_rrf", ascending=False).reset_index(drop=True).head(top_n)
    cy["r"] = cy.index + 1
    return dict(zip(cy["npi"].astype(str).str.zfill(10), cy["r"].astype(int)))


_NO_NORMS_FALLBACK = (
    "(no computed norms file for this year — fall back to the reference tables below, "
    "and flag in batch_summary that norms were uncomputed)"
)


def _norms_path(score_year: int) -> Path:
    """Computed-norms file for the score year (scripts/compute_triage_norms.py output).
    NP_TRIAGE_NORMS_DIR overrides the default repo location (tests; exports)."""
    norms_dir = os.environ.get("NP_TRIAGE_NORMS_DIR")
    base = Path(norms_dir) if norms_dir else REPO_ROOT / "data" / "cache" / "triage_norms"
    return base / f"norms_{score_year}.json"


def _render_computed_norms(score_year, norms_path=None):
    """Render norms_{year}.json as a compact markdown table for the {computed_norms}
    prompt slot; the fallback string when the file is absent/unreadable."""
    path = Path(norms_path) if norms_path else _norms_path(score_year)
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return _NO_NORMS_FALLBACK
    specs = data.get("specialties") or {}
    if not specs:
        return _NO_NORMS_FALLBACK
    meta = data.get("_meta", {})

    def _c(v):
        return f"{v:.4f}" if isinstance(v, (int, float)) else "—"

    lines = [
        f"Computed by `scripts/compute_triage_norms.py` from `{meta.get('source', 'the feature panel')}` "
        f"(year {meta.get('year', score_year)}; specialties with ≥{meta.get('min_providers', '?')} providers; "
        f"opioid basis: {meta.get('opioid_basis', 'unspecified')}).",
        "",
        "| Specialty | n_providers | opioid_share median | opioid_share p95 | dual_share median |",
        "|---|--:|--:|--:|--:|",
    ]
    for name, s in sorted(specs.items(), key=lambda kv: -(kv[1].get("n_providers") or 0)):
        lines.append(
            f"| {name} | {s.get('n_providers', '—')} | {_c(s.get('opioid_share_median'))} "
            f"| {_c(s.get('opioid_share_p95'))} | {_c(s.get('dual_share_median'))} |"
        )
    return "\n".join(lines)


def _build_batch_prompt(base_prompt, records, scored_panel_path, score_year, k_lo, k_hi):
    # Year placeholders keep the prompt panel-agnostic: the cross_year input
    # keys are built as in_{prior_year}_top_2k by _build_record, and the
    # anti-leak knowledge cutoff is always Dec 31 of the year BEFORE the
    # score year (the same boundary the walk-forward trains against).
    rendered = (
        base_prompt
        .replace("{scored_panel_path}", str(scored_panel_path))
        .replace("{score_year}", str(score_year))
        .replace("{prior_year}", str(score_year - 1))
        .replace("{knowledge_cutoff}", f"{score_year - 1}-12-31")
        .replace("{computed_norms}", _render_computed_norms(score_year))
    )
    rendered += (f"\n\n## Batch input (year={score_year}, rank range {k_lo}-{k_hi}, "
                 f"{len(records)} records — one JSON object per line)\n\n```jsonl\n")
    rendered += "\n".join(json.dumps(r) for r in records)
    rendered += "\n```\n"
    return rendered


def run_batch(*, records, scored_panel_path, score_year, k_lo, k_hi, out_dir,
              event_sink, batch_idx, timeout_sec=600, claude_bin=DEFAULT_CLAUDE_BIN) -> TriageOutput:
    if not PROMPT_PATH.exists():
        raise FileNotFoundError(f"triage prompt not found: {PROMPT_PATH}")
    prompt = _build_batch_prompt(PROMPT_PATH.read_text(), records, scored_panel_path,
                                 score_year, k_lo, k_hi)
    return invoke_agent_streaming(
        prompt=prompt,
        schema=TriageOutput,
        out_dir=out_dir,
        step_name=f"triage_batch_{batch_idx:03d}",
        event_sink=event_sink,
        stage_name=STAGE_TRIAGE,
        timeout_sec=timeout_sec,
        claude_bin=claude_bin,
        extra_args=["--disallowedTools", "WebSearch,WebFetch"],
    )


def run(*, run_dir: Path, event_sink: EventSink, scored_panel_path: Path, score_year: int,
        k_range: tuple[int, int] = (1, 500), batch_size: int = 50,
        cross_year_panel_path: Path | None = None,
        timeout_sec: int = 600, claude_bin: str = DEFAULT_CLAUDE_BIN) -> Path:
    """Invoke the Stage-3 triage agent over a top-K range, in batches.

    cross_year_panel_path: prior-year scored_full parquet (for cross_year persistence);
        if None, cross_year flags are False.
    Returns the merged canonical queue file: validation/triage/{year}_top{lo}-{hi}.json
    """
    out_dir = run_dir / "stage3"
    out_dir.mkdir(parents=True, exist_ok=True)
    k_lo, k_hi = k_range
    event_sink.emit(STAGE_TRIAGE, EVENT_STAGE_START,
                    {"score_year": score_year, "k_range": [k_lo, k_hi], "batch_size": batch_size})

    df = pd.read_parquet(scored_panel_path)
    if "score_rrf" not in df.columns:
        raise ValueError(f"Expected 'score_rrf' column in {scored_panel_path}")
    df = df.drop(columns=[c for c in OUTCOME_COLS if c in df.columns], errors="ignore")
    df = df.sort_values("score_rrf", ascending=False).reset_index(drop=True)
    df["__rank"] = df.index + 1
    df["npi"] = df["npi"].astype(str).str.zfill(10)
    top_k = df[(df["__rank"] >= k_lo) & (df["__rank"] <= k_hi)].copy()
    if top_k.empty:
        raise ValueError(f"No NPIs in rank range {k_range}; check scored panel")

    prior_year = score_year - 1
    cross_year_ranks = _load_cross_year_ranks(cross_year_panel_path)
    LOG.info(f"Triaging {len(top_k)} NPIs (ranks {k_lo}-{k_hi}) in batches of {batch_size}; "
             f"{len(cross_year_ranks)} cross-year top-2k")

    all_items: list[TriageItem] = []
    n_batches = (len(top_k) + batch_size - 1) // batch_size
    for batch_idx in range(n_batches):
        chunk = top_k.iloc[batch_idx * batch_size:(batch_idx + 1) * batch_size]
        b_lo, b_hi = int(chunk["__rank"].iloc[0]), int(chunk["__rank"].iloc[-1])
        records = [_build_record(row, row["__rank"], cross_year_ranks, prior_year)
                   for _, row in chunk.iterrows()]
        event_sink.emit(STAGE_TRIAGE, EVENT_STAGE_LOG_LINE, {
            "level": "info", "msg": f"Batch {batch_idx+1}/{n_batches}: ranks {b_lo}-{b_hi}",
            "batch": batch_idx + 1, "n_batches": n_batches})
        try:
            batch_out = run_batch(
                records=records, scored_panel_path=scored_panel_path, score_year=score_year,
                k_lo=b_lo, k_hi=b_hi, out_dir=out_dir, event_sink=event_sink,
                batch_idx=batch_idx, timeout_sec=timeout_sec, claude_bin=claude_bin)
        except Exception as e:
            event_sink.emit(STAGE_TRIAGE, EVENT_STAGE_FAILED,
                            {"error": str(e), "batch": batch_idx + 1, "completed_batches": batch_idx})
            raise
        all_items.extend(batch_out.items)
        event_sink.emit_progress(STAGE_TRIAGE, pct=100 * (batch_idx + 1) / n_batches,
                                 msg=f"Completed batch {batch_idx + 1}/{n_batches}")

    merged = TriageOutput(
        score_year=score_year, k_range=(k_lo, k_hi), items=all_items,
        batch_summary=f"Merged {n_batches} batches × {batch_size} NPIs across rank range "
                      f"{k_lo}-{k_hi} for score year {score_year}.")

    queue_dir = REPO_ROOT / "validation" / "triage"
    queue_dir.mkdir(parents=True, exist_ok=True)
    canonical_path = queue_dir / f"{score_year}_top{k_lo}-{k_hi}.json"
    canonical_path.write_text(merged.model_dump_json(indent=2))
    (run_dir / f"stage3_triage_top{k_lo}-{k_hi}.json").write_text(merged.model_dump_json(indent=2))

    event_sink.emit(STAGE_TRIAGE, EVENT_STAGE_COMPLETE,
                    {"triage_path": str(canonical_path), "n_items": len(all_items), "n_batches": n_batches})
    LOG.info(f"Stage 3 (triage) complete: {len(all_items)} verdicts → {canonical_path}")
    return canonical_path
