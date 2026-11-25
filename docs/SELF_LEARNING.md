# Self-learning loops — how the pipeline improves itself

Three supervision sources exist without any claims-level data. Each powers a
loop; each loop's output lands as a **human-reviewed PR gated by the battery
ratchet** — the system proposes its own improvements, the ratchet proves
them, a human approves them. No unsupervised prompt drift.

## Loop 1 — Hindsight replay (DOJ as a delayed oracle)

`scripts/hindsight_replay.py` · run **monthly**, after a corpus_curator batch
lands (`--since <date>` for new outcomes only).

Every new DOJ defendant was in our panels while the fraud ran. The replay
places each gold_set NPI on the funnel ladder (investigated → parked →
triaged → ranked → in-panel-missed → not-in-panel) and routes every miss to
the agent that owns the fix:

| miss rung | owner |
|---|---|
| TRIAGED_LOW_OR_DISMISS | triage rubric / computed norms |
| RANKED_TOP2K (never triaged) | triage coverage (k-range) |
| IN_PANEL_MISSED | feature_engineer (propose features from the DOJ scheme_summary) |
| INVESTIGATED_DECLINED | floor analysis (which slot failed on a real fraudster?) |
| NOT_IN_PANEL | three causes — pre-2015 years / no Medicare surface / floor_0.50 filter drop; `--check-panels` disambiguates (the filter-drop case is a real miss) |

Output: `results/hindsight/hindsight_<date>.{jsonl,md}`. Time itself is the
training signal; the calibration sandbox keeps any follow-up runs leak-proof.

First full replay (2026-06-12, 1,007 NPIs): 13 TRIAGED_HIGH · 9 MODERATE ·
4 LOW_OR_DISMISS · 78 RANKED_TOP2K · 223 IN_PANEL_MISSED · 680 NOT_IN_PANEL.

## Loop 2 — Self-play mining (the loop grading itself)

The R2 archive accumulates structured adversarial outcomes: grounded
`legitimate_explanation` kills, LIVE foreclosure explanations, SURVIVED
attacks. Mine them periodically (manual for now): a defeat pattern recurring
across ≥3 runs gets promoted UPSTREAM — a deterministic triage screen, a
Step-0 evidence-pack section, a foreclosure-taxonomy entry — so the next
case never spends Opus dollars dying the same death. This is the Guardrail
pattern (recurring LLM discoveries graduate into deterministic code) made
systematic.

## Loop 3 — Calibration battery (the quality ratchet)

`scripts/calibration_battery.py` · manifest `validation/battery/battery.json`
(grow toward ~15 TPs + ~15 controls) · baseline `validation/battery/baseline.json`.

```
--report            score existing calibration runs
--ratchet           exit 1 on regression vs the committed baseline
--update-baseline   accept current metrics (after a verified improvement)
--launch            print run commands for missing/stale subjects (top-level terminal)
```

Metrics: `tp_file_rate` (want 1.0) · `control_file_rate` (want 0.0 — the
anomalous-control firewall) · **`adjudicated_tn_file_rate`** (want 0.0 — the
*defensible* precision metric, see below) · `theory_match_rate` (want high —
did the loop plead the actual DOJ mechanism, `scripts/theory_match.py`, not just
file?) · `evf_separation` (want wide) · `control_dispositive_defeat_rate` (want
1.0 — controls must die on the MERITS, not on EVF economics; the Cypress finding).

**Two flavours of negative subject (kept as DISTINCT metrics):** `negative_control`
= anomalous-but-unprosecuted providers (Baobab, Laurel) — a FILE here *might* be a
true-but-uncharged detection, so `control_file_rate` can't prove a false positive.
`adjudicated_true_negative` = qui tams the govt DECLINED + a court DISMISSED on the
merits (or relator voluntary dismissal) — a FILE here *is* a false positive, so
`adjudicated_tn_file_rate` is the precision number that holds up. Build the cohort
with `scripts/cohort_builder.py` (declined/merits-dismissed NOS-376 dockets via
CourtListener → DRY-RUN candidate list for human keep/exclude review → NPPES-resolve
approved KEEPs → add as `kind: "adjudicated_true_negative"` to `battery.json`).

**Cheap-replay loop for the verdict/scoring layer:** `scripts/replay_battery.py`
harvests completed runs' `summary.json` into frozen fixtures, then replays every
fixture through the CURRENT `compute_verdict`/`build_scorecard` in ms — so changes
to verdict.py / scoring.py thresholds are regression-checked against the whole
battery with NO live re-runs (`--report` / `--check`). Covers the deterministic
post-agent layer; prompt changes still need a live re-run.

**Merge convention: any prompt or pipeline-behavior change re-runs the
affected battery subjects and passes `--ratchet` before merge.** Improvements
update the baseline in the same PR, with the report in the PR body.

Baseline at 2026-06-12 (pre-overhaul runs): tp_file_rate 1.000 ·
control_file_rate **0.333** (Laurel — a control that FILED) · evf_separation
**0.090** (razor-thin) · control_dispositive_defeat_rate 1.000. The overhaul
reruns should move the middle two; that's the first ratchet test.

## Loop 4 — Weight fitting (replacing asserted constants with measured ones)

`scripts/fit_scoring_weights.py` — the formula:

    w* = argmax_w  Σᵢ log σ(yᵢ·(b + w·xᵢ))  −  λ·‖w − w_prior‖²

MAP logistic regression where the CURRENT hand weights (`scoring.WEIGHTS`)
are the prior mean: n=0 returns today's weights exactly; data moves only
what it has evidence for; large n converges to the empirical log-odds each
factor contributes to real FCA outcomes. Labels come from the world
(gold_set TPs, battery controls, provisional hard negatives at half vote).
The legal grounding: features are a quantified elements-of-the-claim
checklist (falsity anchor↔falsity, within-practice+same-district↔scienter,
regulatory↔materiality), labels are outcomes the legal system actually
produced — fitted weights are measured element contributions, the way
litigation-finance underwriters fit docket-outcome models.

Guardrail: below `--min-n 20` (≥5 per class) the tool reports but REFUSES to
recommend (exit 2) — overfit numbers would be worse than vibes. Adoption is
a PR updating `scoring.WEIGHTS` + a passing battery ratchet.

## Loop 5 — Hard-negative mining (the loop's own clearances as labels)

`scripts/mine_hard_negatives.py` → `results/hard_negatives.jsonl`. A
top-ranked provider the loop killed ON THE MERITS (≥1
DISPOSITIVE_NOT_SURVIVED — EVF-economics declines do NOT count) and whom
DOJ has not charged is a provisional negative in exactly the score region
we pay to investigate. Safeguards: half sample-weight in the fitter;
gold_set always outranks (the delayed oracle flips bad labels at the
monthly hindsight replay); rows are tagged `provisional: true`. Future:
weighted negatives for walk-forward ranker retraining.

## A/B convention

Flag-gated behavior changes (e.g. `FRAUD_INV_DEFENSE_AS_MOCK`) are judged on
battery metrics, not vibes.
