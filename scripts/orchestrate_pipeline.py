"""Single entry point for the operationalized 4-stage pipeline.

Three invocation modes:

  full
    Chain stages 1 -> 2 -> 3 -> 4 end-to-end. Initial-validation workflow.
    Picks the top-HIGH_RISK NPI from the resulting triage queue (or accepts --new-npi).

  rank-only
    Stages 1 -> 2 -> 3 only. Used to amortize the ranking/triage pipeline across
    years / K-ranges before investigating any specific NPI.

  investigate
    Stage 4 only. Reads an NPI from a previously-built triage queue and
    runs the investigator loop. Day-to-day workflow.

Outputs:
  state/runs/{run_id}/events.jsonl    — append-only event stream
  state/runs/{run_id}/manifest.json   — run metadata (mode, year, status)
  state/runs/{run_id}/stage1_rederive.json     (if Stage 1 ran)
  state/runs/{run_id}/stage3_triage_top*.json  (if Stage 3 ran)
  validation/triage/{year}_top{lo}-{hi}.json   — canonical triage queue
  fraud-investigator-v2/state/CASE-*/           — investigator state
  investigations/<NewSubject>-YYYY-MM-DD/       — final case file (post Stage 4)

Usage:
  python -m scripts.orchestrate_pipeline --invocation-mode full --year 2023 --new-npi 1234567890
  python -m scripts.orchestrate_pipeline --invocation-mode rank-only --year 2022 --k-range 1-500
  python -m scripts.orchestrate_pipeline --invocation-mode investigate \\
      --queue-entry validation/triage/2023_top1-500.json#1234567890
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "fraud-investigator-v2" / "src"))

from fraud_investigator.agents import rule_derivation as rule_derivation_agent
from fraud_investigator.agents import triage as triage_agent
from fraud_investigator.event_sink import (
    EventSink,
    EVENT_STAGE_COMPLETE,
    EVENT_STAGE_FAILED,
    EVENT_STAGE_LOG_LINE,
    EVENT_STAGE_START,
    STAGE_ML_TRAINING,
    STAGE_TRIAGE,
)
from fraud_investigator.orchestrator import Orchestrator
from fraud_investigator.pipeline_schemas import TriageOutput

# Stage-4 subject auto-pick: most-suspicious verdict, then highest confidence.
_VERDICT_PRIORITY = {"HIGH_RISK": 0, "MODERATE_RISK": 1, "LOW_RISK": 2, "DISMISS": 3}

LOG = logging.getLogger("fraud_investigator.orchestrate_pipeline")

STATE_ROOT = REPO_ROOT / "state" / "runs"

# Wall-clock cap on the ranker subprocess. A full walk-forward is long but finite; a hung
# ranker (e.g. an OpenMP deadlock) must not block the investigation stage forever.
RANKER_TIMEOUT_SEC = 12 * 3600


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_run_id() -> str:
    return f"run_{datetime.now(timezone.utc):%Y%m%dT%H%M%S}_{secrets.token_hex(4)}"


# ─────────────────────────────────────────────────────────────────────────────
# Stage 2 — walk-forward ML training (shells out to walk_forward_v4.py)
# ─────────────────────────────────────────────────────────────────────────────


def run_stage2_ml(
    *,
    run_dir: Path,
    event_sink: EventSink,
    score_year: int,
    filter_path: Path | None,
) -> Path:
    """Invoke walk_forward_v4.py with --score-year + --filter-from.

    Returns path to the produced scored_full_{score_year}.parquet.
    """
    event_sink.emit(STAGE_ML_TRAINING, EVENT_STAGE_START, {"score_year": score_year})

    cmd = [
        sys.executable, "scripts/experiments/walk_forward_v4.py",
        "--score-year", str(score_year),
        "--events-jsonl", str(run_dir / "events.jsonl"),
    ]
    if filter_path is not None:
        cmd.extend(["--filter-from", str(filter_path)])

    log_path = run_dir / "stage2_walk_forward.log"
    event_sink.emit(STAGE_ML_TRAINING, EVENT_STAGE_LOG_LINE, {
        "level": "info", "msg": f"spawn: {' '.join(cmd)}",
    })

    with log_path.open("w") as log_fh:
        try:
            proc = subprocess.run(cmd, stdout=log_fh, stderr=subprocess.STDOUT, cwd=str(REPO_ROOT),
                                  timeout=RANKER_TIMEOUT_SEC)
        except subprocess.TimeoutExpired:
            event_sink.emit(STAGE_ML_TRAINING, EVENT_STAGE_FAILED, {
                "timeout_sec": RANKER_TIMEOUT_SEC, "log_path": str(log_path),
            })
            raise RuntimeError(f"walk_forward_v4.py timed out after {RANKER_TIMEOUT_SEC}s; see {log_path}")

    if proc.returncode != 0:
        event_sink.emit(STAGE_ML_TRAINING, EVENT_STAGE_FAILED, {
            "exit_code": proc.returncode, "log_path": str(log_path),
        })
        raise RuntimeError(f"walk_forward_v4.py exited {proc.returncode}; see {log_path}")

    # Find the produced scored panel. The walk_forward script writes into
    # results/walk_forward_v4_top{N}_*_es{ES}/scored_full_{year}.parquet
    # We let the script tell us where via the events.jsonl STAGE_COMPLETE
    # payload (added in walk_forward in a later commit). For now, glob.
    candidates = sorted(
        REPO_ROOT.glob(f"results/walk_forward_v4_top*/scored_full_{score_year}.parquet"),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    if not candidates:
        raise RuntimeError(f"walk_forward succeeded but no scored_full_{score_year}.parquet found")
    scored_panel = candidates[0]

    event_sink.emit(STAGE_ML_TRAINING, EVENT_STAGE_COMPLETE, {
        "scored_path": str(scored_panel.relative_to(REPO_ROOT)),
        "log_path": str(log_path.relative_to(REPO_ROOT)),
    })
    LOG.info(f"Stage 2 complete: {scored_panel}")
    return scored_panel


# ─────────────────────────────────────────────────────────────────────────────
# Stage 4 — investigator (existing pipeline) with event_sink
# ─────────────────────────────────────────────────────────────────────────────


def run_stage4_investigator(
    *,
    subject: str,
    event_sink: EventSink,
    max_iterations: int,
    max_pre_rebuttal_iters: int,
    max_adversarial: int,
    resume_case_id: str | None = None,
) -> int:
    """Spin up the Stage 4 investigator with event_sink threaded through.

    When ``resume_case_id`` is given, the orchestrator picks up the existing
    case from disk. Combined with the file-level checkpoint in streaming.py
    (skip claude -p when <step>.parsed.json already exists), this preserves
    all completed iter-N / adv-rN agent work and only re-invokes the steps
    that never finished.
    """
    orch = Orchestrator(
        subject=subject,
        max_iterations=max_iterations,
        max_pre_rebuttal_iters=max_pre_rebuttal_iters,
        max_adversarial=max_adversarial,
        event_sink=event_sink,
        case_id=resume_case_id,
    )
    return orch.run()


# ─────────────────────────────────────────────────────────────────────────────
# Subject string construction (helper for full mode)
# ─────────────────────────────────────────────────────────────────────────────


def _build_subject_from_scored_row(scored_panel_path: Path, npi: str) -> str:
    """Construct a one-line subject string from a scored panel row.

    Example output:
      "Provider Larch -- General Practice -- Anytown, CA -- NPI 1900000085
       -- score_rrf=0.0247 -- partb_pay=$540K, 207 benes, 88.6 svcs/bene"
    """
    import pandas as pd
    df = pd.read_parquet(scored_panel_path)
    df["npi"] = df["npi"].astype(str).str.zfill(10)
    row = df[df["npi"] == str(npi).zfill(10)]
    if len(row) == 0:
        raise ValueError(f"NPI {npi} not found in {scored_panel_path}")
    r = row.iloc[0]
    name_parts = [
        str(r.get("partb_rndrng_prvdr_first_name", "")).strip(),
        str(r.get("partb_rndrng_prvdr_mi", "")).strip(),
        str(r.get("partb_rndrng_prvdr_last_org_name", "")).strip(),
    ]
    name = " ".join(p for p in name_parts
                    if p and p.lower() not in ("nan", "none", "0.0", "0"))
    credentials = str(r.get("partb_credentials", "")).strip()
    if credentials and credentials != "nan":
        name = f"{name}, {credentials}"
    specialty = str(r.get("partb_provider_type", "")).strip() or "Unknown specialty"
    city = str(r.get("partb_rndrng_prvdr_city", "")).strip()
    state = str(r.get("partb_state", "")).strip()
    loc = f"{city}, {state}".strip(", ")

    pay = float(r.get("partb_total_medicare_payment", 0) or 0)
    benes = int(r.get("partb_n_beneficiaries", 0) or 0)
    svcs_per_bene = float(r.get("partb_svcs_per_bene", 0) or 0)
    score_rrf = float(r.get("score_rrf", 0) or 0)

    return (
        f"{name} -- {specialty} -- {loc} -- NPI {npi} -- "
        f"score_rrf={score_rrf:.4f} -- partb_pay=${pay/1000:.0f}K, "
        f"{benes} benes, {svcs_per_bene:.1f} svcs/bene"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Modes
# ─────────────────────────────────────────────────────────────────────────────


def _run_ranking(args, run_dir: Path, event_sink: EventSink, manifest: dict) -> tuple[Path, Path]:
    """Stages 1 -> 2 -> 3: rule derivation, ranker, triage. Returns (scored panel, triage queue)."""
    rule_derivation_agent.run(run_dir=run_dir, event_sink=event_sink)
    filter_path = run_dir / "stage1_rederive.json"
    manifest["stage1"] = {"filter_path": str(filter_path)}

    scored_panel = run_stage2_ml(
        run_dir=run_dir, event_sink=event_sink,
        score_year=args.year, filter_path=filter_path,
    )
    manifest["stage2"] = {"scored_panel": str(scored_panel)}

    triage_path = triage_agent.run(
        run_dir=run_dir, event_sink=event_sink,
        scored_panel_path=scored_panel,
        score_year=args.year,
        k_range=args.k_range,
    )
    manifest["stage3"] = {"triage_path": str(triage_path)}
    return scored_panel, triage_path


def run_full(args, run_dir: Path, event_sink: EventSink, manifest: dict) -> int:
    """Stages 1 -> 2 -> 3 -> 4.

    Ranking is isolated from the investigation: if any ranking stage fails or times out and
    --new-npi names the subject, the investigation still runs (subject built as for an ad-hoc
    NPI). Without --new-npi there is nothing to investigate, so the failure propagates.
    """
    try:
        scored_panel, triage_path = _run_ranking(args, run_dir, event_sink, manifest)
    except Exception as e:
        manifest["ranking_error"] = f"{type(e).__name__}: {e}"
        if not args.new_npi:
            raise
        LOG.warning(f"Ranking failed ({manifest['ranking_error']}); "
                    f"investigating NPI {args.new_npi} without it")
        subject, _ = _build_subject_for_npi(args.new_npi)
        manifest["stage4"] = {"subject": subject, "chosen_npi": args.new_npi}
        return run_stage4_investigator(
            subject=subject, event_sink=event_sink,
            max_iterations=args.max_iterations,
            max_pre_rebuttal_iters=args.max_pre_rebuttal_iters,
            max_adversarial=args.max_adversarial,
            resume_case_id=args.resume_case,
        )

    # Stage 4 — needs a chosen NPI
    if args.new_npi:
        chosen_npi = args.new_npi
    else:
        # Default: take the most-suspicious triage verdict (HIGH_RISK, highest confidence)
        merged: TriageOutput = TriageOutput.model_validate_json(triage_path.read_text())
        top1 = min(merged.items,
                   key=lambda i: (_VERDICT_PRIORITY.get(i.verdict, 9), -i.confidence))
        chosen_npi = top1.npi
        event_sink.emit(STAGE_TRIAGE, EVENT_STAGE_LOG_LINE, {
            "level": "info",
            "msg": f"Auto-selected NPI {chosen_npi} ({top1.verdict}, confidence={top1.confidence})",
        })

    subject = _build_subject_from_scored_row(scored_panel, chosen_npi)
    manifest["stage4"] = {"subject": subject, "chosen_npi": chosen_npi}
    return run_stage4_investigator(
        subject=subject, event_sink=event_sink,
        max_iterations=args.max_iterations,
        max_pre_rebuttal_iters=args.max_pre_rebuttal_iters,
        max_adversarial=args.max_adversarial,
        resume_case_id=args.resume_case,
    )


def run_rank_only(args, run_dir: Path, event_sink: EventSink, manifest: dict) -> int:
    """Stages 1 -> 2 -> 3. No investigator."""
    _, triage_path = _run_ranking(args, run_dir, event_sink, manifest)
    LOG.info(f"Rank-only mode complete. Triage queue at {triage_path}")
    return 0


def _find_npi_in_queues(npi: str, repo_root: Path = REPO_ROOT) -> "Path | None":
    """Newest triage queue (validation/triage/**/*.json) containing the NPI."""
    import json as _json
    npi = str(npi).zfill(10)
    queues = sorted(repo_root.glob("validation/triage/**/*.json"),
                    key=lambda q: q.stat().st_mtime, reverse=True)
    for q in queues:
        try:
            data = _json.loads(q.read_text())
        except Exception:
            continue
        if any(str(i.get("npi", "")).zfill(10) == npi
               for i in data.get("items", [])):
            return q
    return None


def _build_subject_for_npi(npi: str, repo_root: Path = REPO_ROOT) -> tuple[str, str]:
    """Best-available subject string for an AD-HOC NPI (no queue entry).

    Fallback chain — each layer is optional so the command works on a
    teammate's machine with partial (or no) local data:
      1. freshest scored panel containing the NPI  → rich subject
      2. freshest cms_partb_{year} panel           → identity + billing line
      3. bare NPI                                  → the Step-0 evidence pack
         + investigator resolve identity live at run time
    Returns (subject, source_tag).
    """
    npi = str(npi).zfill(10)
    scored = sorted(repo_root.glob("results/walk_forward_v4_top*/scored_full_*.parquet"),
                    key=lambda c: c.stat().st_mtime, reverse=True)
    for panel in scored:
        try:
            return _build_subject_from_scored_row(panel, npi), f"scored_panel:{panel.name}"
        except Exception:
            continue
    partb = sorted({pp for sub_dir in ("processed", "panels")
                    for pp in (repo_root / "data" / sub_dir).glob("cms_partb_[0-9]*.parquet")},
                   key=lambda c: c.name, reverse=True)
    for panel in partb:
        try:
            import duckdb
            df = duckdb.sql(
                "SELECT Rndrng_Prvdr_First_Name AS first, Rndrng_Prvdr_Last_Org_Name AS last, "
                "Rndrng_Prvdr_Crdntls AS cred, Rndrng_Prvdr_Type AS type, "
                "Rndrng_Prvdr_City AS city, Rndrng_Prvdr_State_Abrvtn AS state, "
                "Tot_Benes, Tot_Srvcs, Tot_Mdcr_Pymt_Amt "
                f"FROM read_parquet('{panel}') WHERE Rndrng_NPI = ?", params=[npi]).df()
            if not len(df):
                continue
            r = df.iloc[0]
            year = panel.stem.rsplit("_", 1)[-1]
            name = " ".join(s for s in (str(r["first"]).strip(), str(r["last"]).strip())
                            if s and s != "nan")
            cred = str(r["cred"]).strip().strip(".,")
            if cred and cred != "nan":
                name = f"{name}, {cred}"
            return (
                f"{name} — {r['type']} — {r['city']}, {r['state']} — NPI {npi} — "
                f"ad-hoc investigation ({year} Part B: "
                f"${float(r['Tot_Mdcr_Pymt_Amt']):,.0f} paid, "
                f"{int(float(r['Tot_Benes']))} benes, "
                f"{int(float(r['Tot_Srvcs']))} services)",
                f"partb_panel:{panel.name}",
            )
        except Exception:
            continue
    return (
        f"NPI {npi} — ad-hoc investigation (no local panels matched; resolve "
        f"identity via the Step-0 evidence pack + NPPES at run time)",
        "bare",
    )


def run_investigate_npi(args, run_dir: Path, event_sink: EventSink, manifest: dict) -> int:
    """Stage 4 by bare NPI (`--npi`) — the team-friendly entry.

    Prefers a triage-queue entry when one exists (same provenance as the
    console path); otherwise builds the best-available subject and launches
    the investigator loop directly. Either way the run is event-sinked.
    """
    npi = str(args.npi).zfill(10)
    queue = _find_npi_in_queues(npi)
    if queue is not None:
        args.queue_entry = f"{queue.relative_to(REPO_ROOT)}#{npi}"
        LOG.info(f"--npi {npi}: found in triage queue {args.queue_entry} — "
                 f"using the standard queue path")
        try:
            return run_investigate(args, run_dir, event_sink, manifest)
        except Exception as e:
            LOG.warning(f"--npi {npi}: queue path failed ({e}); "
                        f"falling back to ad-hoc subject construction")
    subject, source = _build_subject_for_npi(npi)
    LOG.info(f"--npi {npi}: subject via {source}: {subject[:120]}")
    manifest["stage4"] = {
        "subject": subject, "chosen_npi": npi, "queue_path": None,
        "subject_source": source, "resume_case_id": args.resume_case,
    }
    return run_stage4_investigator(
        subject=subject, event_sink=event_sink,
        max_iterations=args.max_iterations,
        max_pre_rebuttal_iters=args.max_pre_rebuttal_iters,
        max_adversarial=args.max_adversarial,
        resume_case_id=args.resume_case,
    )


def run_investigate(args, run_dir: Path, event_sink: EventSink, manifest: dict) -> int:
    """Stage 4 only. Reads a queue entry."""
    # Parse "validation/triage/2023_top1-500.json#1234567890"
    if "#" not in args.queue_entry:
        raise ValueError(
            "queue-entry must be of the form 'path/to/queue.json#NPI', "
            f"got: {args.queue_entry!r}"
        )
    queue_path_str, npi = args.queue_entry.rsplit("#", 1)
    queue_path = REPO_ROOT / queue_path_str
    if not queue_path.exists():
        raise FileNotFoundError(f"queue file not found: {queue_path}")
    merged = TriageOutput.model_validate_json(queue_path.read_text())
    match = next((i for i in merged.items if i.npi == npi.zfill(10)), None)
    if match is None:
        raise ValueError(f"NPI {npi} not in queue {queue_path}")

    # Need a scored panel for the subject string. Use the freshest one for the
    # queue's score_year.
    candidates = sorted(
        REPO_ROOT.glob(f"results/walk_forward_v4_top*/scored_full_{merged.score_year}.parquet"),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    if not candidates:
        raise RuntimeError(f"No scored_full_{merged.score_year}.parquet to construct subject string")
    subject = _build_subject_from_scored_row(candidates[0], npi)

    manifest["stage4"] = {
        "subject": subject, "chosen_npi": npi,
        "queue_path": str(queue_path), "triage_verdict": match.verdict,
        "triage_confidence": match.confidence, "triage_reasoning": match.reasoning,
        "resume_case_id": args.resume_case,
    }
    return run_stage4_investigator(
        subject=subject, event_sink=event_sink,
        max_iterations=args.max_iterations,
        max_pre_rebuttal_iters=args.max_pre_rebuttal_iters,
        max_adversarial=args.max_adversarial,
        resume_case_id=args.resume_case,
    )


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────


def _parse_k_range(s: str) -> tuple[int, int]:
    """Parse '1-2000' or '1,2000' into (1, 2000)."""
    if "-" in s:
        lo, hi = s.split("-", 1)
    elif "," in s:
        lo, hi = s.split(",", 1)
    else:
        raise argparse.ArgumentTypeError(f"k-range must be 'lo-hi' or 'lo,hi', got {s!r}")
    return (int(lo), int(hi))


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    ap = argparse.ArgumentParser(
        description="Operationalized 4-stage healthcare-fraud-detection pipeline.",
    )
    ap.add_argument("--invocation-mode", choices=("full", "rank-only", "investigate"),
                    default="full")
    ap.add_argument("--year", type=int, default=2023,
                    help="Score year (default 2023)")
    ap.add_argument("--new-npi", default=None,
                    help="(full mode) NPI to investigate; if omitted, picks the top HIGH_RISK from triage output")
    ap.add_argument("--queue-entry", default=None,
                    help="(investigate mode) e.g., 'validation/triage/2023_top1-500.json#1234567890'")
    ap.add_argument("--npi", default=None,
                    help="(investigate mode) bare 10-digit NPI — auto-finds a triage-queue "
                         "entry, else builds the subject from local panels, else launches "
                         "bare (identity resolved at run time). The team-friendly path: "
                         "make investigate NPI=<npi>.")
    ap.add_argument("--k-range", type=_parse_k_range, default=(1, 2000),
                    help="Rank range to triage (default 1-2000)")
    ap.add_argument("--max-iterations", type=int, default=6,
                    help="(vestigial in two-phase flow) — Phase A cap is "
                         "--max-pre-rebuttal-iters; kept for back-compat")
    ap.add_argument("--max-pre-rebuttal-iters", type=int, default=3,
                    help="Phase A build-loop cap (default 3). First-chair must "
                         "PROCEED_TO_REBUTTAL or ABANDON_CASE at this iter.")
    ap.add_argument("--max-adversarial", type=int, default=2,
                    help="Stage 4 adversarial rounds (budget-mode default 2)")
    ap.add_argument("--budget-mode", action="store_true",
                    help="Use budget-friendly defaults (max-iter=6, max-adv=2). "
                         "Same as explicitly passing those flags.")
    ap.add_argument("--resume-case", default=None,
                    help="(full + investigate modes) Resume an existing case_id "
                         "(e.g., CASE-6A0CD54F-2026). Existing iter-N + adv-rN "
                         "agent outputs are reused via file-level checkpoint; "
                         "only steps that never finished get re-invoked.")
    args = ap.parse_args()

    if args.budget_mode:
        # Already the defaults — flag is for self-documentation
        pass

    # Validation
    if args.invocation_mode == "investigate" and not (args.queue_entry or args.npi):
        ap.error("--invocation-mode investigate requires --queue-entry or --npi")
    if args.npi is not None and not (args.npi.isdigit() and len(args.npi) == 10
                                     and args.npi[0] in "12"):
        ap.error(f"--npi must be exactly 10 digits starting with 1 or 2 "
                 f"(NPI format), got {args.npi!r}")

    # Setup
    run_id = _new_run_id()
    run_dir = STATE_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    event_sink = EventSink(run_dir / "events.jsonl")
    manifest = {
        "run_id": run_id,
        "invocation_mode": args.invocation_mode,
        "year": args.year,
        "k_range": list(args.k_range),
        "max_iterations": args.max_iterations,
        "max_pre_rebuttal_iters": args.max_pre_rebuttal_iters,
        "max_adversarial": args.max_adversarial,
        "started_at": _iso_now(),
        "status": "running",
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    LOG.info(f"=== Run {run_id} ({args.invocation_mode}) ===")

    # Dispatch
    pipeline_failed = False
    try:
        if args.invocation_mode == "full":
            exit_code = run_full(args, run_dir, event_sink, manifest)
        elif args.invocation_mode == "rank-only":
            exit_code = run_rank_only(args, run_dir, event_sink, manifest)
        elif args.invocation_mode == "investigate":
            if args.queue_entry:
                exit_code = run_investigate(args, run_dir, event_sink, manifest)
            else:
                exit_code = run_investigate_npi(args, run_dir, event_sink, manifest)
        else:
            ap.error(f"unknown invocation-mode: {args.invocation_mode}")
        manifest["status"] = "complete" if exit_code == 0 else "failed"
        manifest["finished_at"] = _iso_now()
        manifest["exit_code"] = exit_code
        pipeline_failed = exit_code != 0
    except Exception as e:
        LOG.exception("Pipeline failed")
        # Emit a top-level STAGE_FAILED so events.jsonl is self-describing
        # (otherwise the UI can't tell from events alone whether the run
        # is still in-flight or crashed — the engineer flagged this as a
        # follow-up after the Hemlock case_writer crash). Best-effort —
        # don't let an EventSink failure mask the original exception.
        try:
            from fraud_investigator.event_sink import EVENT_STAGE_FAILED
            event_sink.emit("investigator", EVENT_STAGE_FAILED, {
                "error": str(e),
                "kind": type(e).__name__,
                "where": "orchestrate_pipeline top-level dispatch",
            })
        except Exception:
            pass
        manifest["status"] = "failed"
        manifest["finished_at"] = _iso_now()
        manifest["error"] = str(e)
        pipeline_failed = True
        (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
        # Auto-spawn pipeline_engineer below the raise — we still want
        # diagnosis even when the pipeline raised an uncaught exception.
        _spawn_pipeline_engineer(run_id, manifest)
        raise
    finally:
        # Push run artifacts to R2 from `finally` so HARD-CRASH paths (the
        # raise above) are covered too — a crashed run's partial artifacts
        # are exactly what the post-mortem needs (opus review of PR #273,
        # finding 1). Detached + fail-open; ordered before the manifest
        # write so r2_sync_started is persisted.
        _fire_r2_sync(run_dir, manifest)
        (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
        LOG.info(f"=== Manifest: {run_dir/'manifest.json'} ===")

    # On clean-but-non-zero exit (e.g., agent error caught at the top level),
    # auto-spawn the pipeline_engineer to diagnose the failed run. Detached
    # subprocess so it doesn't block this script's exit.
    if pipeline_failed:
        _spawn_pipeline_engineer(run_id, manifest)

    return exit_code


def _fire_r2_sync(run_dir: Path, manifest: dict) -> None:
    """Detached, non-blocking R2 artifact push fired at run completion.

    Uses subprocess.Popen (start_new_session=True) so it never blocks the
    orchestrator exit and can never affect the exit code.  Wrapped in a broad
    try/except for the same reason — an R2 misconfiguration must not fail a run.
    Sets manifest["r2_sync_started"] = True/False so the console can surface it.
    Gate: NP_R2_DISABLE=1 suppresses (calibration / offline).
    """
    if os.environ.get("NP_R2_DISABLE") == "1":
        manifest["r2_sync_started"] = False
        return
    sync_script = REPO_ROOT / "scripts" / "sync_artifacts_r2.sh"
    if not sync_script.exists():
        manifest["r2_sync_started"] = False
        return
    log_path = run_dir / "r2_sync.log"
    try:
        log_fh = log_path.open("a")
        try:
            proc = subprocess.Popen(
                ["bash", str(sync_script)],
                cwd=str(REPO_ROOT),
                stdout=log_fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
            LOG.info(f"R2 sync started (pid={proc.pid}); log: {log_path}")
            manifest["r2_sync_started"] = True
        finally:
            log_fh.close()
    except Exception as exc:
        LOG.warning(f"R2 sync could not be started: {exc}")
        manifest["r2_sync_started"] = False


def _spawn_pipeline_engineer(failed_run_id: str, manifest: dict) -> None:
    """Detached subprocess spawn of the pipeline_engineer support agent.

    Idempotent: skips if a previous engineer diagnosis already exists for
    this failed run (so a manual relaunch doesn't double-fire).
    """
    import subprocess
    # Check for an existing engineer diagnosis for THIS failed_run_id.
    # The CLI dispatcher puts the engineer's run under
    # state/runs/agent_pipeline_engineer_<ts>_<hex>/ — we mark each one
    # with the failed_run_id it diagnosed via a small marker file so we
    # can find it on re-runs.
    state_runs = REPO_ROOT / "state" / "runs"
    if state_runs.exists():
        for d in state_runs.iterdir():
            if not d.is_dir():
                continue
            if not d.name.startswith("agent_pipeline_engineer_"):
                continue
            marker = d / "diagnosed_run.txt"
            if marker.exists() and marker.read_text().strip() == failed_run_id:
                LOG.info(
                    f"pipeline_engineer already diagnosed {failed_run_id!r} "
                    f"(see {d.relative_to(REPO_ROOT)}); skipping auto-spawn."
                )
                return

    cmd = [
        sys.executable, str(REPO_ROOT / "scripts" / "run_support_agent.py"),
        "--agent", "pipeline_engineer",
        "--failed-run-id", failed_run_id,
    ]
    log_path = state_runs / f"_agent_launcher_pipeline_engineer_auto_{failed_run_id}.log"
    log_fh = log_path.open("a")
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(REPO_ROOT),
            stdout=log_fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            start_new_session=True,  # detach so we don't block orchestrator exit
        )
        LOG.info(
            f"=== Pipeline failed → auto-spawned pipeline_engineer "
            f"(pid={proc.pid}) to diagnose run {failed_run_id} ===\n"
            f"    Engineer log: {log_path}"
        )
    finally:
        log_fh.close()


if __name__ == "__main__":
    raise SystemExit(main() or 0)
