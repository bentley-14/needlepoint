"""Smoke tests — verify imports + dry-run flow without spawning claude -p."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_package_imports():
    """All modules can import — catches typo/circular-import bugs."""
    here = Path(__file__).resolve().parents[1] / "src"
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{here}{os.pathsep}{env.get('PYTHONPATH', '')}"

    code = (
        "import fraud_investigator; "
        "from fraud_investigator import orchestrator, schemas, state, claude_p; "
        "from fraud_investigator.agents import ("
        "investigator, counsel_build, counsel_first_chair_review, "
        "counsel_rebuttal, counsel_disposition, "
        "defense, legal_adv_response, rebuttal_investigator, case_writer); "
        "print('OK')"
    )
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert r.returncode == 0, f"import failed: stderr={r.stderr}"
    assert "OK" in r.stdout


def test_prompts_all_present():
    """Every agent has a corresponding prompt file."""
    prompts = Path(__file__).resolve().parents[1] / "src/fraud_investigator/prompts"
    expected = {
        "investigator.md",
        "counsel_build.md",
        "counsel_first_chair_review.md",
        "counsel_rebuttal.md",
        "counsel_disposition.md",
        "defense.md",
        "legal_adv_response.md",
        "rebuttal_investigator.md",
        "case_writer.md",
    }
    actual = {p.name for p in prompts.glob("*.md")}
    assert expected <= actual, f"missing prompts: {expected - actual}"


def test_schemas_validate_known_shapes():
    """Pydantic models accept minimally-valid JSON."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from fraud_investigator.schemas import (
        InvestigatorOutput, CounselBuildOutput, CounselRebuttalOutput,
        CounselFirstChairReviewOutput,
        CounselDispositionOutput, DefenseMotion, LegalAdversarialResponse,
        CaseWriterOutput,
    )

    InvestigatorOutput.model_validate({
        "case_id": "CASE-X-2026", "iteration": "1",
        "evidence": [], "notes_for_counsel": "",
    })
    CounselBuildOutput.model_validate({
        "case_id": "CASE-X-2026", "iteration": 1,
        "case_theory": "A subject is alleged to have done X to extract Y.",
        "counts_pled": [],
        "rule_9b_elements": {"who": [], "what": [], "when": [], "where": [], "how": []},
        "precedents": [], "public_disclosure_concerns": [],
        "first_to_file_concerns": [], "estimated_aggregate_damages_usd": None,
        "current_strength": "weak", "reasoning": "",
    })
    CounselRebuttalOutput.model_validate({
        "case_id": "CASE-X-2026", "iteration": 1,
        "weak_links": [],
        "overall_assessment": "Weak as drafted.",
        "estimated_survival_likelihood": 0.4,
    })
    CounselFirstChairReviewOutput.model_validate({
        "case_id": "CASE-X-2026", "iteration": 1,
        "action": "CONTINUE_BUILD",
        "reasoning": "iter-1 needs venue-axis precedent search before red-team.",
        "counts_to_drop": [],
        "follow_up_questions_for_investigator": ["Run Step 8b venue search."],
        "gaps_to_fill": [],
    })
    CounselDispositionOutput.model_validate({
        "case_id": "CASE-X-2026", "iteration": 1,
        "action": "NEED_MORE_INVESTIGATION",
        "reasoning": "More peer-cohort data needed.",
        "counts_to_drop": [],
        "follow_up_questions_for_investigator": ["Verify NPI specialty."],
        "gaps_to_fill": [],
    })
    DefenseMotion.model_validate({
        "case_id": "CASE-X-2026", "round": 1,
        "attacks": [], "any_attack_survives": False,
        "counts_with_live_attack": [], "counts_with_no_live_attack": [],
    })
    LegalAdversarialResponse.model_validate({
        "case_id": "CASE-X-2026", "round": 1,
        "attack_dispositions": [],
        "counts_retained_this_round": [], "counts_abandoned_this_round": [],
        "counts_pending_rebuttal": [], "follow_up_questions": [],
        "cumulative_case_discount_pct": 0,
        "settlement_target_range_usd": "$0",
        "settlement_verdict": "NOT_ACTIONABLE",
        "post_defense_reasoning": "all counts abandoned",
    })
    CaseWriterOutput.model_validate({
        "case_id": "CASE-X-2026", "case_summary": "...",
        "full_case_report": "...", "layperson_summary": "...",
    })


def test_dry_run_executes():
    """End-to-end with --dry-run — no claude -p calls, but the orchestrator's
    state file should be written and reach 'complete'."""
    here = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{here / 'src'}{os.pathsep}{env.get('PYTHONPATH', '')}"
    state_dir = here / "state" / "_test"
    if state_dir.exists():
        import shutil
        shutil.rmtree(state_dir)
    state_dir.mkdir(parents=True)

    r = subprocess.run(
        [
            sys.executable, "-m", "fraud_investigator",
            "Dr. Test Subject smoke check",
            "--state-dir", str(state_dir),
            "--max-iterations", "2",
            "--max-adversarial", "1",
            "--dry-run",
            "-vv",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert r.returncode == 0, f"dry-run failed:\nstdout:\n{r.stdout}\nstderr:\n{r.stderr}"
    # One summary.json should exist
    summaries = list(state_dir.glob("CASE-*/summary.json"))
    assert len(summaries) == 1, f"expected 1 summary.json, got {len(summaries)}"
