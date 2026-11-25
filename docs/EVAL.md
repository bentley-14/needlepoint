# Evaluating NeedlePoint (and your changes to it)

Two evaluation layers ship with the repo. Both use public data and name no one.

## 1. Ranker evaluation (does the ML layer surface real fraud?)

**Labels:** `data/extracted/gold_set.jsonl`, 1,313 DOJ case records covering 1,017 NPIs. Each record carries
the NPI, the DOJ press-release URL (the provenance), the outcome type (`indictment`, `plea`, `conviction`,
`sentence`, `settlement`, ...), the outcome date, fraud types, active years, specialty, and state. Names and
case narratives are not included. `split` (train/val/test) is fixed per case, so results are comparable
across contributors.

Outcome types matter. About 82% of records are indictments: charged, not convicted, and presumed innocent.
Filter on `outcome.type` if your experiment needs adjudicated outcomes only.

**Walk-forward protocol:** train only on years before the scoring year, score the scoring year, report recall
at K against that year's positives (`fraudlib/pipeline_contract.yaml` holds the canonical settings; the
reference result is mean recall@10k of 0.446 across 2015 to 2023). Fetch the panel years you need first:

```bash
python3 scripts/fetch_public_data.py --years 2021 2022 2023 --datasets partb partb_svc partd
```

## 2. Investigation-loop calibration (does the agent loop reach the right verdict?)

**Battery:** `validation/battery/battery.json`, 10 DOJ-charged true positives keyed by NPI, each with a leak-safe
cutoff (`as_of` = discovery or charge date, not the disposition date). The agent sees only data from before the
cutoff (pre-cutoff panels in an out-of-repo export, a date-gated Wayback browser, and a leak watchdog).

**Expected answers:** a true positive should reach a FILE-class verdict (`SETTLEMENT_READY`,
`FILE_WITH_DISCOVERY_DEPS`); a negative control should be declined. `validation/battery/doj_theory.json`
gives DOJ's actual mechanism for scoreable subjects, used for theory-match recall.

**Add negative controls yourself, by construction, never by hand-picking anomalous providers:**

```bash
# needs a scored panel from the ranker (scripts/experiments/walk_forward_v4.py writes scored_full_<year>.parquet)
python3 scripts/calibration/select_tier1_controls.py --scored 'results/**/scored_full_2021.parquet' \
    --lo 0.40 --hi 0.60 --as-of 2022-01-01      # statistically typical peers per specialty
```

**Run it:**

```bash
python3 scripts/fetch_public_data.py --years 2016 2017 2018 2019 2020 2021 --datasets partb partb_svc
python3 scripts/calibration_battery.py --launch --manifest validation/battery/battery.json   # prints commands
bash scripts/calibration/run_calibration_smoke.sh <NPI> <AS_OF> "<SUBJECT_HINT>"          # one subject (hours)
python3 scripts/calibration_battery.py --report --manifest validation/battery/battery.json
python3 scripts/calibration_battery.py --ratchet --manifest validation/battery/battery.json # fail on regression
```

Run subjects one at a time. A production-profile subject takes roughly 2 to 4 hours.

**Reference baseline** (`validation/battery/baseline.json`, August 2026): 2 of 7 scored true positives filed and
no negative control filed. Every decline was a scheme public data cannot prove (see Structural limits).

**Structural limits.** Score recall on the subset public data can prove, and record which declines are
structural. Aggregate public files cannot establish: phantom billing (services never rendered), chart-level
medical necessity, entity and network schemes run by non-billing owners or marketers, cash-paid pill mills,
or coverage text that is no longer retrievable for older years.

## 3. Reporting a result

When you change a model, prompt, threshold, or gate, report:

- the config (backend, `FRAUD_INV_API_MODEL*` or `MODEL_BY_STEP`, profile, loop depth, changed knobs);
- per-subject verdicts and the battery metrics (`--report --format json`);
- run health: how many runs errored or were excluded (a crashed run can make precision look perfect).

## Rules

- **Leak safety.** Calibration runs use the export mode and the date-gated browser; `as_of` is the discovery or
  charge date, never the disposition date.
- **Clean controls.** Negative controls are built by construction (typical peers or adjudicated declines),
  never anomalous-but-uncharged providers.
- **Exclude errored runs** from metrics, and gate stale runs before reporting.
- **Replays and schema changes.** A run that predates a scored field records it as empty; check when each
  field was introduced before replaying old runs against a new scorer.

## Caveats

- **Model recognition is not solved by the sandbox.** A model may already know a famous case. Before adding a
  subject, ask a fresh model what it knows about the provider and drop subjects it recognizes.
- **The api backend in calibration mode** reuses the same tool jail, but the reference baseline was measured
  on the Claude Code backend. Re-baseline before comparing across backends.
