# Calibration clean-room suite

Blind, **fail-closed** evaluation of the needlepoint loop against known DOJ true-positives.
It builds an out-of-repo *clean export* (pre-cutoff CMS panels + answer-key-free tools — no
`gold_set`, no DOJ caches, no `.git`, no post-cutoff data), runs the full loop inside it, and
monitors for leaks. The agent runs the **real** billing tools but cannot reach the answer
because it isn't in the tree.

## Scripts (this directory)

| Script | Role |
|---|---|
| `run_calibration_smoke.sh <NPI> <YYYY-MM-DD> "<SUBJECT>"` | **Entry point.** Builds the export, derives per-NPI leak tokens, runs the loop, arms the watchdog. |
| `build_calibration_workspace.py --npi N --as-of DATE --with-panels` | Builds the fail-closed clean export (zero-copy clone of pre-cutoff panels + answer-key-free tools; self-audits — fails on any post-cutoff year / `.git` / answer-key file). |
| `calibration_leak_tokens.py --npi N` | Derives this NPI's answer-fact leak tokens from `gold_set` (at setup, **outside** the sandbox). |
| `calibration_loop_watchdog.sh <run-dir>` | Polls agent output for those tokens every 3 s; writes a marker on any hit. |
| `verify_calibration_cleanroom.sh [NPI] [DATE]` | **Verification suite** — static/logic checks + builder fail-closed audit + watchdog fire/no-FP + dump_hcpcs OOM regression + live adversarial leak probes. |

## Quick start

```bash
# Verify the clean-room is leak-tight + correct (static checks + a live probe):
bash scripts/calibration/verify_calibration_cleanroom.sh

# Run a full calibration on a true-positive NPI (run detached for long runs):
bash scripts/calibration/run_calibration_smoke.sh 1900000007 2020-01-01 "<sanitized subject>"
```

## Shared tools (stay in `scripts/` — used by production too)

`sandbox_browser.py` (+ `browser_fetch.py`), `sandbox_nppes.py` (date-gated web / identity),
`dump_hcpcs_breakdown.py`, `provider_trajectory.py`, `address_sweep.py`, `hcpcs_lookup.py`,
`precedent_lookup.py`. The builder copies the answer-key-free subset into each export's own
`scripts/` directory.

## How it stays leak-tight

Out-of-repo cwd + `--permission-mode default` (no `--dangerously-skip-permissions`) + a per-agent
`Read(<export>/**)` jail + agent-supplied tool-permission flags stripped + web denied (pre-cutoff
Wayback only) + the build-time self-audit + the runtime watchdog. Full threat model:
`docs/INVESTIGATOR_SANDBOX_EVAL.md`.

## Calibration-mode evaluation choices

- **Floors off** — the filing-economics floors (>$1M damages, mandatory regulatory anchor,
  expected-value threshold) gate *whether a firm would file*, not whether the loop can *build*
  the case, so they no-op in calibration → the loop runs end-to-end. Process floors (iteration
  cap, min-iterations, venue) stay.
- **Inherent-limitation defenses mitigated** — the public-disclosure bar (public CMS data, no
  original-source relator) and the Rule 9(b) representative-claim requirement (no claim-level
  data) are structural limits of our data access; they're dropped before scoring so they don't
  drag every case to indefensible. `first_to_file` (a real prior qui tam) + all merits attacks stay.
