"""Calibration clean-room mode for the needlepoint loop.

WHY
---
The needlepoint loop is the production case-building pipeline. For calibration
runs against known DOJ true-positives (TPs), every agent must be blind to the
verified-fraud allegation: no live web that would surface the post-cutoff DOJ
press release, no live CourtListener that would surface a sealed-then-unsealed
case, no NPPES fields with post-cutoff dates, and no reads of the repo's
answer-key files (``data/extracted/gold_set.jsonl``, ``docs/calibration/``,
``proposals/``, ``audits/``).

The defendant's name is NOT a leak when the web + repo are sandboxed: names
are public info; the fraud allegation is post-cutoff knowledge. The agent
can use the name to search pre-cutoff public sources (Wayback, pre-cutoff
practice website, pre-cutoff state board), and those don't mention the
sealed qui tam case.

DESIGN
------
A module-level global ``_CONTEXT`` is set by the orchestrator on entry to
``run()`` and cleared on exit (try/finally). When non-None, ``invoke_agent()``
in ``claude_p.py`` injects per-agent hardened launch flags + a prompt prefix
banner, and forces cache-bypass. Mirrors the existing ``_EVENT_SINK`` and
``_DEFAULT_CWD`` patterns to keep the surface consistent.

Threat-model + adversarial review documented in
``docs/calibration/ASPEN_BASELINE_TRUTH.md`` and the
``experiment/calibration-clean-room-loop`` branch description.

ACTIVATING
----------
The orchestrator's ``--calibration-as-of YYYY-MM-DD`` CLI flag is the only
way to activate calibration mode. The CLI flag becomes the cutoff date; the
synthetic case_id becomes ``CALIBRATION-<uuid>`` so cache lookups + session
IDs don't collide with prior production runs.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

# Agents that touch external tools (web / NPPES / CourtListener / doj_pr_lookup).
# These get the strictest sandbox: only date-gated sandbox_browser for web.
# All other agents (counsel_*, defense, case_writer) operate on pre-gathered
# evidence and their input is the investigator's (verified-clean) output, so
# they produce clean output by induction. They still get the Read allowlist
# (jails them to the state dir) but their Bash allowlist permits
# scripts/precedent_lookup.py instead of sandbox_browser.
#
# Step-name source-of-truth (verified 2026-06-03 by grepping
# `step_name=` in fraud-investigator-v2/src/fraud_investigator/agents/):
#   investigator.py uses "investigator_discover" AND "investigator"
#   rebuttal_investigator.py uses "rebuttal_investigator"
# Missing a step name here = silent demotion to the no-web allowlist = the
# Investigator's discover pass would fail to use sandbox_browser. So this
# set MUST include every step-name variant produced by the investigator
# driver modules. When a new investigator-style agent is added, update here.
TOOL_USING_AGENTS = frozenset({
    "investigator",
    "investigator_discover",
    "rebuttal_investigator",
})

# EXPORT MODE (--calibration-export): instead of jailing the agent to an empty
# dir with only the sandboxed browser, we point its cwd at a fail-closed *clean
# export* (built by scripts/calibration/build_calibration_workspace.py --with-panels) that
# physically contains ONLY pre-cutoff CMS panels + answer-key-free analysis tools
# + pre-computed peer benchmarks — no gold_set, no DOJ press-release cache, no
# docs/calibration answer keys, no .git, no post-cutoff panel years. The agent
# runs the SAME tools as a production run ("go back to what we had before") but
# can't reach the answer because it isn't in the tree. These are the relative
# `Bash(python3 scripts/<tool>:*)` invocations allowed in export mode (cwd=export).
EXPORT_BASH_TOOLS = (
    "dump_hcpcs_breakdown.py",   # subject per-(HCPCS) line-item breakdown
    "dump_dme_breakdown.py",     # subject per-(HCPCS) DMEPOS-referral breakdown (code-level)
    "provider_trajectory.py",    # cross-program Part B/D/DMEPOS trajectory by year
    "address_sweep.py",          # co-located billers at the practice address
    "hcpcs_lookup.py",           # HCPCS descriptions / predicate checks
    "sandbox_browser.py",        # date-gated pre-cutoff Wayback (the ONLY web)
    "sandbox_nppes.py",          # date-gated NPPES identity
)


@dataclass(frozen=True)
class CalibrationContext:
    """Per-run calibration config. Immutable to prevent mid-run tampering.

    ``as_of`` is the cutoff date; anything dated on/after this is post-cutoff
    knowledge and must not appear in the agent's reasoning.
    ``run_dir`` is the absolute path to the calibration run's state dir; the
    Read allowlist is anchored to this dir so the agent cannot reach the
    repo's gold_set / docs/calibration / other case states.
    ``repo_root`` is the absolute path to the repository root, used to
    construct absolute Bash allowlist paths (e.g. for sandbox_browser.py).
    Absolute paths are required because the orchestrator may change the
    agent's cwd via ``set_default_cwd()`` — a relative-path Bash pattern
    would silently fail to match.
    ``timeout_sec`` is the per-agent subprocess timeout in seconds. The
    production default (CLAUDE_TIMEOUT_SEC=600, ~10 min) is too short for
    calibration runs: the agent's Wayback fetches via sandbox_browser add
    real network latency, and an agent that keeps retrying a denied
    Read (e.g., the prompt-instructed gold_set lookup) will burn the
    budget. Default 1800 (30 min) gives realistic runway. invoke_agent
    uses ``max(timeout_sec, ctx.timeout_sec)`` so the caller can request
    even more.
    """

    as_of: date
    run_dir: Path
    repo_root: Path
    sandbox_cwd: Path                 # absolute, OUTSIDE the repo (see below)
    timeout_sec: int = 1800
    export_root: "Path | None" = None  # absolute clean-export dir (export mode); see EXPORT_BASH_TOOLS

    def __post_init__(self) -> None:
        # Defense in depth: paths must be absolute. A relative path here
        # would produce a relative --allowed-tools pattern that wouldn't
        # match the agent's actual cwd-resolved invocations.
        if not self.run_dir.is_absolute():
            raise ValueError(
                f"CalibrationContext.run_dir must be absolute, got {self.run_dir!r}"
            )
        if not self.repo_root.is_absolute():
            raise ValueError(
                f"CalibrationContext.repo_root must be absolute, got {self.repo_root!r}"
            )
        if not self.sandbox_cwd.is_absolute():
            raise ValueError(
                f"CalibrationContext.sandbox_cwd must be absolute, got {self.sandbox_cwd!r}"
            )
        # CRITICAL: sandbox_cwd MUST be outside the repo. Verified empirically
        # on 2026-06-03: when the spawned claude -p subprocess has cwd inside
        # the repo, the cwd-implicit Read scope grants the agent access to
        # the entire repo tree (gold_set.jsonl, docs/calibration/, sibling
        # case-state dirs), regardless of --allowed-tools restrictions.
        # Only an out-of-repo cwd actually isolates the agent.
        try:
            self.sandbox_cwd.relative_to(self.repo_root)
        except ValueError:
            pass  # NOT relative to repo_root → outside the repo → good
        else:
            raise ValueError(
                f"CalibrationContext.sandbox_cwd ({self.sandbox_cwd}) must NOT be "
                f"inside the repo ({self.repo_root}); the cwd-implicit Read scope "
                f"would grant the agent access to the entire repo, defeating the "
                f"sandbox."
            )
        if self.timeout_sec <= 0:
            raise ValueError(
                f"CalibrationContext.timeout_sec must be positive, got {self.timeout_sec!r}"
            )
        if self.export_root is not None:
            if not self.export_root.is_absolute():
                raise ValueError(
                    f"CalibrationContext.export_root must be absolute, got {self.export_root!r}"
                )
            # Same fail-closed rationale as sandbox_cwd: the export MUST be outside
            # the repo, or the agent's cwd-implicit scope (+ git) reaches the answer keys.
            try:
                self.export_root.relative_to(self.repo_root)
            except ValueError:
                pass  # outside the repo → good
            else:
                raise ValueError(
                    f"CalibrationContext.export_root ({self.export_root}) must NOT be inside "
                    f"the repo ({self.repo_root}); a clean export inside the repo can reach "
                    f"gold_set / docs/calibration / .git history."
                )

    def turn_budget_for(self, step_name: str, production_default: int) -> int:
        """Turn budget for a step in calibration mode (production budgets untouched).

        Calibration export mode gives the agent SCRIPT-based tools (one subprocess
        per query: dump_hcpcs_breakdown, provider_trajectory, address_sweep, ...)
        instead of WebSearch. That's far more turn-hungry than the production
        tuning (which targets a few rich WebSearch calls), so the investigator
        passes need more runway — observed: investigator_discover hit the
        production cap of 25 at turn 26. Bump ONLY the investigator-side steps
        here; every other step keeps its production budget.
        """
        if step_name in ("investigator_discover", "investigator", "rebuttal_investigator"):
            # 75, not the production 25: discover completes ~26-30 turns; the VERIFY
            # pass is heavier (verifies each discover hypothesis + reads benchmarks).
            # Bumped 50 → 75 (2026-06-08): a complex high-$ practice with co-billers
            # and 10 years of J-code data (Cypress, CALIBRATION-1940FE5D) hit 50 at
            # turn 51. error_max_turns is now ALSO handled gracefully in claude_p
            # (auto-finalize + defer unfinished threads to the next round), so this
            # is headroom, not a hard wall.
            return max(production_default, 75)
        return production_default

    def launch_flags_for_agent(self, agent_name: str) -> list[str]:
        """Hardened --allowed-tools + --disallowed-tools args for the agent.

        Per-agent Bash allowlist (absolute paths so cwd changes don't break them):
          - Tool-using agents (investigator, rebuttal_investigator):
            * scripts/sandbox_browser.py — date-gated Wayback web access
            * scripts/sandbox_nppes.py — date-gated NPPES wrapper that
              redacts any field with a timestamp >= cutoff (post-cutoff
              status changes, deactivation dates etc.); static identity
              fields (name, NPI, taxonomy, addresses, enumeration date)
              are preserved.
          - All other agents (counsel_*, defense, case_writer):
            * scripts/precedent_lookup.py — read-only FCA precedent lookup
              from a pre-cached file (no live CourtListener).

        Both agent classes get:
          - Read access ANCHORED to the calibration run_dir (no other repo
            paths reachable, no relative-path escapes — verified live in
            the Aspen audit on 2026-06-03).
          - Web tools (WebSearch, WebFetch) denied entirely.
          - Grep/Glob denied (can't enumerate filesystem to discover paths).
        """
        # EXPORT MODE: cwd is the clean export; allow the answer-key-free analysis
        # tools (relative `scripts/<tool>`, since cwd=export) + Read anchored to the
        # export. Web tools + Grep/Glob still denied (pre-cutoff Wayback only, via the
        # allow-listed sandbox_browser). Applies to every agent — the investigator
        # uses the billing tools; downstream agents simply don't invoke them.
        if self.export_root is not None:
            ws = str(self.export_root)
            tools = EXPORT_BASH_TOOLS
            # Data-driven: the claim-level tool is offered ONLY when a claims layer was
            # built into this export (scripts/calibration/build_synth_claims.py +
            # builder --with-claims). Keeps claims-free exports (Arm A) byte-for-byte
            # unchanged; an export with synth_claims.parquet is the Arm-B superset.
            if (self.export_root / "data" / "synth_claims.parquet").exists():
                tools = (*EXPORT_BASH_TOOLS, "claims_lookup.py")
            bash_allow = ",".join(f"Bash(python3 scripts/{t}:*)" for t in tools)
            return [
                "--allowed-tools", f"{bash_allow},Read({ws}/**)",
                "--disallowed-tools", "WebSearch,WebFetch,Grep,Glob",
            ]

        ws = str(self.run_dir)  # already absolute by __post_init__ guarantee
        sandbox_web = str(self.repo_root / "scripts" / "sandbox_browser.py")
        sandbox_nppes = str(self.repo_root / "scripts" / "sandbox_nppes.py")
        precedent = str(self.repo_root / "scripts" / "precedent_lookup.py")
        if agent_name in TOOL_USING_AGENTS:
            bash_allow = (
                f"Bash(python3 {sandbox_web}:*),"
                f"Bash(python3 {sandbox_nppes}:*)"
            )
        else:
            bash_allow = f"Bash(python3 {precedent}:*)"
        return [
            "--allowed-tools", f"{bash_allow},Read({ws}/**)",
            "--disallowed-tools", "WebSearch,WebFetch,Grep,Glob",
        ]

    def prompt_prefix(self, agent_name: str) -> str:
        """Banner prepended to the agent's user prompt in calibration mode.

        The banner is a hard instruction to:
          1. Treat the cutoff as authoritative.
          2. Recognize model-recognition (recall from training) as leakage.
          3. Override any prompt instruction to read repo answer-key paths
             (``data/extracted/gold_set.jsonl``, ``docs/calibration/``,
             ``proposals/``, ``audits/``, sibling case states under
             ``fraud-investigator-v2/state/``). The base agent prompts (e.g.,
             investigator.md) tell the agent to use gold_set.jsonl as the
             "canonical peer-case corpus" — that is correct in production
             but a contradiction in calibration mode, where the Read
             allowlist blocks those paths. Without this override, the agent
             will keep retrying the denied Read and burn its turn budget
             (observed empirically on 2026-06-03: 10-min timeout reached on
             investigator verify pass).

        Per the validated protocol (docs/INVESTIGATOR_SANDBOX_EVAL.md),
        prompt discipline is NOT sufficient on its own — the launch-flag +
        Bash allowlist + Read allowlist are the enforcement. This banner is
        the polite explanation of what the sandbox is doing AND the override
        of any contradictory in-prompt instructions.
        """
        cutoff = self.as_of.isoformat()
        banner = (
            f"--- CALIBRATION MODE — point-in-time as of {cutoff} ---\n"
            f"You are running inside a sandboxed calibration of the needlepoint "
            f"loop. Anything dated on or after {cutoff} is post-cutoff knowledge "
            f"and must not appear in your reasoning or output. The runtime "
            f"enforces date-gating: live WebSearch/WebFetch are denied, NPPES "
            f"goes through `python3 scripts/sandbox_nppes.py --as-of {cutoff}` "
            f"(post-cutoff fields redacted), CourtListener/doj_pr_lookup are "
            f"unavailable, and web access is `python3 scripts/sandbox_browser.py "
            f"{{check,fetch,search}} --as-of {cutoff} ...` (Wayback snapshots "
            f"strictly before {cutoff}; a BLOCKED result means the page did not "
            f"exist yet — do not work around it).\n\n"
            f"OVERRIDE OF PROMPT INSTRUCTIONS (calibration-mode only):\n"
            f"  Your base agent prompt may instruct you to read repo answer-key "
            f"files: `data/extracted/gold_set.jsonl`, `docs/calibration/`, "
            f"`proposals/`, `audits/`, sibling case-state dirs under "
            f"`fraud-investigator-v2/state/`. **All of these are blocked by the "
            f"Read allowlist by design** — the Read tool is anchored to your "
            f"calibration run dir only. Do NOT attempt to Read those paths "
            f"(it will fail with permission-denied; retrying will waste your "
            f"turn budget). The base prompts were written for production runs "
            f"that DO use gold_set as the peer-case corpus; in calibration "
            f"mode you build the case from in-workspace data + sandboxed web/"
            f"NPPES only.\n\n"
            f"If you recognize a case by training-data recall, treat that "
            f"recognition as leakage and exclude it from your analysis. "
            f"Ground every finding in evidence you can fetch pre-{cutoff} or "
            f"that appears in the in-workspace billing data.\n\n"
            f"CALIBRATION OBJECTIVE — RUN TO COMPLETION: this run tests whether the loop can "
            f"CONSTRUCT the case, not whether a firm would file it, so the filing-economics "
            f"floors are OFF. If you are a counsel / strategy step (counsel_first_chair_review, "
            f"counsel_rebuttal, counsel_disposition): do NOT ABANDON_CASE or downgrade below "
            f"case-ready because aggregate damages are below any dollar threshold, because a "
            f"LEIE / state-board / prior-enforcement 'regulatory anchor' is missing, or because "
            f"an expected-value / strength score is low. Develop the strongest case the evidence "
            f"supports across the available build iterations (CONTINUE_BUILD), then "
            f"PROCEED_TO_REBUTTAL and DECLARE_CASE_READY so the full pipeline (mock-defense → "
            f"case writer) runs to completion. Still report your honest strength assessment and "
            f"flag weaknesses for the record — but COMPLETION, not the filing decision, is the goal.\n\n"
            f"INHERENT-LIMITATION DEFENSES (defense + legal-adversarial steps): two attack families are "
            f"STRUCTURAL limits of this sandboxed, public-data-only evaluation, not case-specific defects: "
            f"(a) the PUBLIC-DISCLOSURE BAR grounded in the fact that the factual basis comes from public "
            f"CMS data with no original-source relator, and (b) the Rule 9(b) REPRESENTATIVE-CLAIM "
            f"requirement (pleading one specific false claim with particularity), which needs claim-level "
            f"data unavailable in the sandbox. Both are assumed CURED downstream by an original-source "
            f"relator + DOJ pre-suit production. Do NOT raise them as attacks (defense); if present, "
            f"classify them MITIGATED at 0% discount — NEVER dispositive — and do not abandon a count on "
            f"their account (legal-adversarial). They must NOT factor into case strength. Do still fully "
            f"scrutinize CASE-SPECIFIC defects — a genuine prior qui tam / first-to-file conflict on THIS "
            f"defendant, falsity / scienter / materiality gaps, safe-harbor, SOL, damages overstatement — "
            f"those remain fair game and should be weighed normally.\n\n"
        )
        if self.export_root is not None:
            last_pre = self.as_of.year - 1
            banner += (
                f"WORKSPACE (export mode): your cwd is a self-contained clean export. The base prompt "
                f"below describes a production toolset — IGNORE the parts of it that conflict with this "
                f"list. Here is EXACTLY what is available, and what is not:\n\n"
                f"AVAILABLE (run with cwd-relative paths, ONE command per Bash call):\n"
                f"  python3 scripts/dump_hcpcs_breakdown.py --npi <NPI> --years 2013-{last_pre}\n"
                f"  python3 scripts/dump_dme_breakdown.py --npi <NPI> --years 2014-{last_pre}\n"
                f"  python3 scripts/provider_trajectory.py --npi <NPI>\n"
                f"  python3 scripts/address_sweep.py --npi <NPI> --year <YR>\n"
                f"  python3 scripts/hcpcs_lookup.py <args>\n"
                f"  python3 scripts/sandbox_nppes.py --as-of {self.as_of.isoformat()} --npi <NPI>\n"
                f"  python3 scripts/sandbox_browser.py {{search,fetch,check}} --as-of {self.as_of.isoformat()} ...\n"
                f"  Read on any file under this workspace (data/, scripts/). The panels in `data/panels/` "
                f"hold only years < {self.as_of.year} — there is no post-cutoff data to find. Pre-computed "
                f"specialty-peer baselines are in `data/specialty_benchmarks.json` + `data/hcpcs_benchmarks.json` "
                f"(use these for peer percentiles).\n\n"
                f"NOT AVAILABLE — do NOT call these; they are denied and each attempt wastes a turn:\n"
                f"  - WebSearch / WebFetch / Grep / Glob / the Agent (sub-investigation) tool\n"
                f"  - Write / Edit (keep working notes in your response text, not in files)\n"
                f"  - `nppes_lookup` (use `sandbox_nppes.py`), live courtlistener / doj_pr_lookup / pecos\n"
                f"  - bare `duckdb ...` and `python3 -c ...` (use the scripts above; they cover the analysis)\n"
                f"  - Do NOT chain or parallelize Bash (no `&&`, `;`, or multiple tool calls in one turn): "
                f"if any part needs approval the whole batch is cancelled. One command, one Bash call.\n\n"
                f"GUIDANCE (avoid wasting turns):\n"
                f"  - Read JSON files (benchmarks, provider_context) with the Read tool or `cat` — never `python3 -c`.\n"
                f"  - Pre-cutoff Wayback usually has NO snapshot. A BLOCKED result is FINAL — do not retry the same\n"
                f"    or alternate URLs. Spend at most 1-2 turns on web; the billing data + benchmarks are primary.\n"
                f"  - Legal/strategy agents: `precedent_lookup`, courtlistener, and doj_pr_lookup are unavailable —\n"
                f"    cite FCA precedent from your own knowledge; do not call lookup tools or WebSearch.\n\n"
            )
            # Claim-level layer (only present when this export was built --with-claims).
            if (self.export_root / "data" / "synth_claims.parquet").exists():
                banner += (
                    f"CLAIM-LEVEL DATA (available — this export includes a claim-level extract the "
                    f"public PUFs lack):\n"
                    f"  python3 scripts/claims_lookup.py --scan --year <YR>        # rank billing NPIs by share of\n"
                    f"      services RENDERED BY SOMEONE ELSE (who actually performed the billed service)\n"
                    f"  python3 scripts/claims_lookup.py --billed-under <NPI> --year <YR>  # break a provider's\n"
                    f"      billed volume down by who actually rendered each service\n"
                    f"  python3 scripts/claims_lookup.py --rendered-by <NPI>       # whose NPI a provider's work was billed under\n"
                    f"  python3 scripts/claims_lookup.py --exemplars <NPI> --n 5   # individual claims (bene+date+code+renderer)\n"
                    f"  Each claim carries: beneficiary, service date, HCPCS, the RENDERING provider vs the "
                    f"BILLED-UNDER provider, and amounts. 'rendering != billed-under' is LEGAL for compliant "
                    f"incident-to on ESTABLISHED patients, but NOT for NEW patients — weigh that distinction. "
                    f"There is NO fraud label; draw your own conclusions and cite specific claims for any "
                    f"representative-claim (Rule 9(b)) pleading.\n\n"
                )
        return banner


# Module-level global. Set by orchestrator on enter of run(), cleared on exit
# via try/finally. Mirrors _EVENT_SINK / _DEFAULT_CWD in claude_p.py — keeps
# the agent-invocation surface (claude_p.invoke_agent) free of new parameters
# that would have to thread through 10+ agent driver modules.
_CONTEXT: "CalibrationContext | None" = None


def set_calibration_context(ctx: "CalibrationContext | None") -> None:
    """Set or clear the per-run calibration context.

    The orchestrator MUST clear (set to None) on exit via try/finally so a
    calibration run cannot leak its restrictions into a subsequent production
    run in the same Python process.
    """
    global _CONTEXT
    _CONTEXT = ctx


def get_calibration_context() -> "CalibrationContext | None":
    """Return the current calibration context, or None if not in calibration mode."""
    return _CONTEXT


def is_active() -> bool:
    """True iff a calibration context is currently set."""
    return _CONTEXT is not None
