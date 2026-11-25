# fraud-investigator-v2

The investigation loop: the multi-agent pipeline that `needlepoint.py` (repo root) drives. Most people should
start there; this page is for working on the loop itself.

Each agent step is a one-shot call with a clean context, and the orchestrator holds all state. Steps run on one
of three backends (`FRAUD_INV_EXEC_BACKEND`): `api` (any provider through LiteLLM, with your key), `claude_p`
(the Claude Code CLI), or `tmux` (Claude Code panes). Every prompt, raw response, and tool trace is saved per
case under `state/<CASE-ID>/`.

Per iteration, counsel builds a case theory (`counsel_build`), attacks it (`counsel_rebuttal`), and decides
whether it is ready (`counsel_disposition`). Only a ready case goes to the adversarial defense rounds, then the
foreclosure gate, the verdict, and the case writer. See `../docs/OUTPUT.md` for how to read the result.

## Running the loop directly

```bash
# from the repo root, with the virtualenv active
PYTHONPATH=.:fraud-investigator-v2/src python -m fraud_investigator \
    "Provider Aspen -- Family Practice -- KY -- NPI 1900000007" \
    --max-pre-rebuttal-iters 2 --max-adversarial 1 -v
```

`needlepoint.py run` builds the subject string from the NPI registry and sets these flags and the per-step
model variables for you. `--max-pre-rebuttal-iters` must be at least 2 for the loop to reach a verdict.

### Calibration clean-room mode

To run the full loop on a known DOJ true-positive for calibration eval
(verify the loop independently surfaces the alleged fraud from pre-cutoff
data, without leaking the answer):

```bash
fraud-investigator-v2 "Provider Aspen -- Family Practice -- KY -- NPI 1900000007" \
    --calibration-as-of 2020-01-01
```

In calibration mode every agent runs with hardened launch flags: Read is
jailed to the calibration run's state dir, web access is routed through
date-gated wrappers (`scripts/sandbox_browser.py` for Wayback,
`scripts/sandbox_nppes.py` for NPPES — strips fields with timestamps
≥ cutoff), `WebSearch`/`WebFetch`/`Grep`/`Glob` are denied, the case_id
is synthesized as `CALIBRATION-<uuid>`, and state writes route under
`state/calibration/<case_id>/` (sibling to production state). The
orchestrator clears the calibration context in `try/finally` so the
restriction can NOT leak into subsequent production runs in the same
Python process.

Full protocol, threat model, and adversarial verification in
[`../docs/INVESTIGATOR_SANDBOX_EVAL.md`](../docs/INVESTIGATOR_SANDBOX_EVAL.md)
(§ Full-loop calibration mode). End-to-end smoke runner at
`../scripts/calibration/run_calibration_smoke.sh`.

## Directory layout

```
fraud-investigator-v2/
├── src/fraud_investigator/
│   ├── __main__.py             # CLI entry
│   ├── orchestrator.py         # the loop control
│   ├── claude_p.py             # claude -p subprocess wrapper
│   ├── schemas.py              # Pydantic models for all agent I/O JSON (FinalVerdict, ReviewRequest)
│   ├── checkpoint.py           # Guardrail-4 HITL checkpoint detectors (pure)
│   ├── state.py                # per-case state + transcript writers
│   ├── prompts/                # prompt templates (.md)
│   └── agents/                 # one module per agent step
├── tests/
└── state/{caseId}/             # runtime state (gitignored)
```

## Human-in-the-loop checkpoint (Guardrail-4)

The loop normally runs to a terminal `FinalVerdict` (`schemas.py`):
`SETTLEMENT_READY`, `FILE_WITH_DISCOVERY_DEPS`, `NOT_ACTIONABLE`,
`INDEFENSIBLE_AT_CAP`, `PENDING_REBUTTAL_CAP`, `COUNSEL_ABANDONED`, or
`NEEDS_HUMAN` (the loop got stuck — max-iter / spend-limit / agent error;
terminal, not resumable).

Guardrail-4 adds one more — a **resumable pause**,
`NEEDS_HUMAN_CHECKPOINT`. When the loop is about to make a *dispositive,
case-killing* call on a premise it **cannot verify** (e.g. "this sealed prior
docket is a qui tam" → first-to-file bar → abandon; or a LIVE innocent
explanation it can't rule out), it does not guess. The two pure detectors in
`checkpoint.py` (`detect_jurisdiction_checkpoint` at the Phase-B disposition
seam, `detect_foreclosure_checkpoint` at the Phase-C foreclosure seam) return a
`ReviewRequest`; the orchestrator parks the case at `NEEDS_HUMAN_CHECKPOINT`,
records the `ReviewRequest` on `CaseState.review_request`, and `run()`
exits cleanly (exit 0, no dossier written) instead of finalizing the decline.
The motivating incident was Birch (CASE-6A368C27), where a sealed
`Dogwood v. Agency Myrtle LLC` docket was wrongly asserted to be a qui tam
(it was NOS 442 Employment).

A human resolves the pause with `scripts/answer_checkpoint.py`:

```bash
# from the parent repo
python scripts/answer_checkpoint.py list
python scripts/answer_checkpoint.py answer CASE-XXXX-2026 \
    --answer "Dogwood v. Agency Myrtle (9:99-cv-00001) is NOS 442 Employment, not a qui tam" \
    [--citation "investigations/.../DOGWOOD_SUMMARY.md"] [--resume]
```

`answer` appends the answer as a `human_attested` `EvidenceItem`
(`EvidenceItem.provenance`, vs the default `agent`), clears the stale decision
and its on-disk step cache (rewind), and prints the resume command; `--resume`
also launches it (`FRAUD_INV_SAFE_PERMS=1`). The console surfaces open
checkpoints in a read-only Pending Review panel. One open checkpoint per case
at a time.

## Entry points

`needlepoint.py` (repo root) is the guided front end. `python -m fraud_investigator` is the scriptable entry
underneath it, and `scripts/np_launch.py` (`make investigate`) is the maintainer launcher with queue discovery
and storage sync.
