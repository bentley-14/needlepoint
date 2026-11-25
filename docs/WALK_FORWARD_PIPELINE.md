# Walk-Forward Ranker — Pipeline Contract & Tunable Surface

**Scope.** The single authoritative reference for the **walk-forward training/scoring
ranker** (`scripts/experiments/walk_forward_v4.py`) — flow, every tunable knob,
and the drift guard that keeps it honest. This doc + its machine-readable companion
[`fraudlib/pipeline_contract.yaml`](../fraudlib/pipeline_contract.yaml) are the
source of truth the runner and pre-flight check against.

> **Not** the investigation loop ([`PIPELINE_SCHEMATIC.md`](PIPELINE_SCHEMATIC.md) covers that);
> this doc is the ML ranker.

> **Canonical numbers live in the YAML, not this prose.** When a value here disagrees
> with `pipeline_contract.yaml`, **the YAML wins** — fix this doc.

---

## 0. Keeping this doc in sync (read before changing the pipeline)

**Rule: any commit that changes pipeline behavior MUST update this doc *and*
`pipeline_contract.yaml` in the same commit.** "Pipeline files" =
`scripts/experiments/walk_forward_v4.py`, `fraudlib/ranker/multitask.py`,
`fraudlib/ranker/feature_frames.py`, `fraudlib/ranker/feature_select.py`, the agent
filter, the GNN/centrality builders, `fraudlib/labels/*`, `fraudlib/rules/rules_v3.json`.

**Why:** so the preflight is *robust* — when you run the pipeline, the contract check
diffs live config against the canonical values and warns on **every** deviation. If the
doc/contract are current, the only warnings you see are the knob(s) you *intended* to
change. Stale doc → noisy diff → drift hides in the noise (exactly how we lost
`(1,3,0.5)→(1,1,1)` and the GNN form silently).

**Workflow for an experiment / pipeline change:**
1. Change the one knob you mean to change.
2. Update its value in `pipeline_contract.yaml` (+ a `history:` line if canonical) and this doc.
3. Run; confirm `verify_pipeline_contract` warns about **only** that knob.
4. Commit code + YAML + doc together.

The drift guard (§1) checks this at run time.

---

## 1. The drift guard

| Piece | File | Role |
|---|---|---|
| Contract | `fraudlib/pipeline_contract.yaml` | Single source of truth for canonical config. |
| Runtime check | `fraudlib/pipeline_contract.py` → `verify_pipeline_contract(live, run_dir)` | Runner calls on startup; diffs live constants vs contract. |
| Feature-cache check | `scripts/preflight_walk_forward.py` | Validates `features_full_{year}.parquet` vs the `feature_cache` section before a sweep. |

**Drift policy:** WARN + per-run `pipeline_run_manifest.json` on any non-invariant
deviation; HARD-ERROR (`ContractViolation`) only on `invariants` (label source, GNN
form/dims, no-leakage). Degrades to a warning if pyyaml is missing — the checker must
never break a run.

**Wiring status:** `verify_pipeline_contract()` is wired into `walk_forward_v4.py`
`main()` (2026-05-29) — it runs at startup, logs drift warnings, and writes a per-run
`pipeline_run_manifest.json` into the run dir.

---

## 2. Highest-leverage tuning levers (ranked)

1. **Top-level RRF blend weights** — canonical `(3,1,0)` (perspec dropped 2026-05-30). ±2.5pp on baseline panels.
2. **Head combination** — unweighted `Σ predict_proba`; calibration-sensitive (ablation: task #4).
3. **Agent filter** — `floor_0.50`; the dead `_rederived` variant measured better recall.
4. **`scale_pos_weight ×2.0`** — buried magic multiplier on every head.
5. **5 vs 6 heads**, then **XGB depth/n_estimators** (one config for all heads), **GNN dims**, **neural capacity**.

---

## 3. Latent issues & decisions (2026-05-29)

Status reflects owner decisions:

| # | Issue | Decision |
|---|---|---|
| 1 | Neural net non-deterministic (no `torch.manual_seed`) | **DONE** (2026-05-29) — `MultitaskConfig.seed=0` seeds torch/numpy/DataLoader; CPU exactly reproducible, MPS reproducible-ish. |
| 2 | Active filter `floor_0.50` < `_rederived` on recall | **DEFER** — switch after optimizing current setup. |
| 3 | Neural & XGB use different positive-weighting | **KEEP** — intentional; the two arms surface different fraud types. |
| 4 | Neural hardcoded to CPU (`device="cpu"`) | **DONE/VERIFIED** (2026-05-29) — `device="auto"`→MPS; verified not degrading (MPS seed-0 R@10k 0.338 > CPU seed-0 0.257 on 2015). NB: neural is high-variance + **device-dependent** (CPU≠MPS) → reproducible *per device*. `MULTITASK_DEVICE` env overrides. |
| 5 | Agent filter drops rows from the **score** panel (`walk_forward:449`) | **KEEP** (2026-05-29) — score panel stays filtered; preserves the established recall methodology + comparability with prior runs. |
| 6 | `graph_*` fraud-neighbor features consume `fraud_npis` (leakage) | **DONE** (2026-05-29) — call removed from `feature_frames.py`; `graph_features.py` deleted. HCPCS centrality kept (structural, no leak). |
| 7 | Percentile builders disagree (peer-pct type×state/`keep`; HCPCS-rich type-only/`bottom`) | **FIX** — standardize on **type × state**; needs feature rebuild. |
| 8 | `SPECIALTY_BUCKETS` overlap → providers scored by multiple specialty models, RRF-summed | **MOOT** (2026-05-30) — per-specialty arm DISABLED (`perspec=0` beats `(3,1,0.3)` at every k; the 4-bucket restriction flooded top-100 with cardiology). See §6/§9.5. |
| 9 | "Walk-forward" is single-round, expanding window from 2013 | **OPEN** — deciding (see §9). |
| 10 | `enable_categorical=True` is dead (`CAT_COLS` cast then dropped) | **OPEN** — deciding (use them vs remove dead flag, see §9). |
| 11 | GNN training code is off-`main` (branch only) | **Task #25** — per-year retrain merges it to `main`. |

---

## 4. Walk-forward split

For score year `Y`: `train = years <= Y-2`, `val = Y-1`, `score = Y`. Score-year
positives are removed from train/val (`walk_forward:398`). Multi-fold CV = the sweep
running each `Y` separately; per-invocation is single-round, expanding window from a
hardcoded 2013 start.

## 5. Pipeline stages (`run_round`)

```
load_stamped(Y) → build agent filter from TRAIN positives → apply to train/val/score
→ feature matrix → train 5 XGB heads + multitask neural  (per-specialty arm OFF, 2026-05-30)
→ SCORE: combine heads → rank → RRF blend (xgb + neural) → recall@K → save panel
```

## 6. The blend (most error-prone knob)

```
xgb_s = Σ_head predict_proba[:,1]              # level 1: unweighted sum (calibration-sensitive)
rrf   = w_xgb·1/(K+rank(xgb_s)) + w_neural·1/(K+rank(mt_s)) + w_perspec·1/(K+rank(spec_s))
```
Canonical `(w_xgb,w_neural,w_perspec) = (3,1,0)`, `RRF_K=60` — **locked 2026-05-30**
(XGB-heavy + neural; **per-specialty dropped**). Weights live in `BLEND_W`
(walk_forward_v4.py); `USE_PERSPECIALTY=False`, so the perspec arm is off and
`score_perspec` persists as NaN (§9.5).

**History (task #3).** Baseline runner used `(1,3,0.5)` ("proven +2.7pp"); on the clean
gold set that does NOT reproduce (ranks 9/12, loses to `(1,1,1)`). `(3,1,0.3)` on the
restricted 4-bucket perspec was adopted 2026-05-29 — but a 2026-05-30 `perspec=0` sweep
across 2015-2023 (post-hoc re-blend on persisted component scores) **superseded it**:
dropping perspec beats `(3,1,0.3)` at every k (mean R@10k 0.446 vs 0.432, R@5k +3.4pp),
and XGB+neural at ~3:1 beats pure XGB by **+5.5pp** R@10k (0.446 vs 0.390) — neural is
*not* redundant; the weak perspec arm (standalone R@10k 0.147) was masking it. The earlier
"`(3,1,0.3)` > `(3,1,0)`" win was an artifact of the restricted-bucket perspec on the
perspec4 panels. `(2,1,0)` edges `(3,1,0)` at R@1k (0.116 vs 0.110). Don't re-bet on
neural-heavy or perspec-heavy weights.

## 7. Persisted columns

`scored_full_{Y}.parquet` carries, for post-hoc reweighting without a re-run:
`score_rrf`, `score_xgb_sum`, `score_neural_sum`, `score_perspec`, and one per-head
proba `head__{SCHEME}`. A sidecar `components_meta_{Y}.json` holds per-head
`{best_iter, best_score (val-AUPRC), n_pos}` for the val-AUPRC / n_pos head-weighting
schemes. (Added 2026-05-29, task #2; `score_neural_sum`/`score_perspec` are NaN when
that source is off.)

## 8. Running a sweep

```bash
python3 scripts/preflight_walk_forward.py --years 2015-2023 \
    --baseline-dir baselines/r10k_315_v4_top5_floor050_es50/ablations/hcpcs_graph_gnnpca/_run/results
bash scripts/run_maximal_sane_sweep.sh   # serial, memory-safe, resumable via YEARS=...
```

### 8b. Ablation toggles (added 2026-06-09, env-gated — default off, canonical behavior unchanged)

- `NO_RULE_FEATURES=1` — exclude the 59 `rule__*` feature columns from the training
  matrix (labels + agent filter untouched). OUT_DIR is auto-suffixed `_norules` so
  committed baselines are never overwritten.
- `WF_OUT_SUFFIX=<tag>` — append an arbitrary suffix to OUT_DIR (e.g. `_ablon` for a
  same-env rules-ON re-run in a both-arm ablation).
- history: 2026-06-09 rule-feature ablation (both arms, same env, 2015–2023) —
  results under `results/*_ablon` / `*_norules`; decision recorded in TODO/memory.

**R2 ephemeral staging** (opt-in):
- `NP_R2_STAGING=1` — stage the working-set parquets from R2 at the start of the walk,
  train, then purge them on completion (success or failure). Default off = read local as today.
- `NP_KEEP_CACHE=1` — skip the purge (retain staged panels for the next run; iterative dev).
- `NP_CACHE_DIR=<path>` — override the scratch/cache location (default `data/cache/feature_frames`).

---

## 9. Full knob inventory

Audit 2026-05-28/29. Line numbers audit-time; verify before editing. GNN §9.8 is
**off-`main`** (branch `maintainer/gnn-per-year-retrain-20260528`). Dormant paths flagged.

### 9.1 Walk-forward control & windowing
| Knob | file:line | Current | Note |
|---|---|---|---|
| `--score-year` | walk_forward:683 | 2022 | Drives all windows; single round |
| train window | :693 | `range(2013, Y-1)` | Expanding from 2013 (no lookback cap) |
| `ACTIVE_ONLY` | :89 | True | Active providers only |
| `ATTACH_EXTRAS` | :88 | True | cl/dme/dmer/op/xprog/gnn_pca/centrality satellites ("maximal-sane") |
| `USE_COHORT` | :90 | False | Isolation forests OFF (no-op) |
| `USE_PERSPECIALTY` | :91 | True | 3rd RRF source |
| `USE_AGENT_FILTER` | :92 | True | Pre-train filter |
| `MAX_NEG_PER_YEAR` | :117 | 500_000 | Per-year neg cap (seed 42) |
| `EXCLUDED_NPIS` | :166-173 | bottom-5-only NPIs dropped | — |
| Recall@K grid | :631 | 100…10000 | Eval cutoffs |
| top-N CSV | :374,668 | 2000 | `top2000_*.csv` magic |

### 9.2 XGB scheme heads
| Knob | file:line | Current | Note |
|---|---|---|---|
| `SCHEMES_TRAIN_FULL` | :65-71 | 5 heads | PHANTOM, MED_UNNEC, KICKBACK, OPIOID, UPCODING |
| `SCHEMES_TRAIN_SMALL` | :72 | `()` | Disabled |
| `SCHEMES_DROPPED` | :74-80 | HHA, DME, TELEHEALTH, LAB_FRAUD, NPI_THEFT | docstring "bottom-4" stale (5) |
| `n_estimators` / `max_depth` / `learning_rate` | :501 | 300 / 8 / 0.1 | Same for all heads; no per-head tuning |
| `min_child_weight` / `subsample` / `colsample_bytree` | :502 | 1 / 0.8 / 0.8 | — |
| `scale_pos_weight` | :499 | `(n_neg/n_pos)*2.0` | **`*2.0` buried magic** |
| `eval_metric` / `early_stopping_rounds` | :505/:101 | aucpr / 50 | Drives best_iter; bumped from 20 |
| `tree_method` / `random_state` / `n_jobs` | :503/:505 | hist / 0 / -1 | seed 0 (neg sampling uses 42 — inconsistent) |
| min-pos-to-train | :476 | `<5` skip | Magic `5` (reused in filter) |

### 9.3 Head combination & RRF blend
| Knob | file:line | Current | Note |
|---|---|---|---|
| head combine | :575-580 | unweighted `Σ proba` | Ablation target #4 |
| RRF source weights | `BLEND_W` wf | **`(3,1,0)`** (locked 2026-05-30) | XGB-heavy + neural; perspec dropped — `perspec=0` beats `(3,1,0.3)` at every k (§6) |
| `RRF_K` | :115 | 60 | Untuned |
| neural aggregation | :586 | `"sum"` | `sum`/`max` only ("mean" raises) |

### 9.4 Multitask neural (`fraudlib/ranker/multitask.py`)
| Knob | file:line | Current | Note |
|---|---|---|---|
| `hidden_dims` | mt:45 / runner :533 | `(128,64,32)` (override) | last entry = embed dim |
| `dropout` / `lr` / `weight_decay` | mt:46-48 | 0.3 / 1e-3 / 1e-4 | AdamW |
| `batch_size` / `n_epochs` / `patience` | mt:49-51 / :534 | 4096 / 50 / 8 | `drop_last=True` risk under imbalance |
| `device` | mt:52 | **auto** (mps/cuda/cpu) | `_resolve_device`; `MULTITASK_DEVICE` env overrides. Results device-dependent (float ops) — reproducible per-device, not across |
| seed | mt:53 | **0** | seeds torch/numpy/DataLoader (`_seed_everything`); `MULTITASK_SEED` env overrides. NB: neural arm is high-variance — single-run R@K carries ~±6pp (small-n years); seeding fixes the draw + makes weighting ablations valid (neural held constant) |
| activation/BN/optimizer/loss | mt:63-172 | ReLU/BN1d/AdamW/BCE | Hardcoded; no focal-loss |
| LR scheduler | mt:141-143 | ReduceLROnPlateau (patience 3, factor 0.5) | magic, not in config |
| `pos_weight` cap | mt:135 | `min(n_neg/n_pos, 50000)` | buried; differs from XGB **by design (#3)** |
| feature scaling | mt:146 | none (`fillna(0)`) | BN only |
| num tasks | mt:88-91 | =5 (runtime) | matches XGB heads |
| `NEURAL_SUBSAMPLE` | runner :116 | 300_000 | neural neg cap |

### 9.5 Per-specialty model (`run_tier1_combined.py`, `discover_multisource_rules_v2.py`)
| Knob | file:line | Current | Note |
|---|---|---|---|
| `SPECIALTY_BUCKETS` | discover:51-97 | **inert — perspec OFF** (2026-05-30) | arm disabled (`USE_PERSPECIALTY=False`); full import, unused. Was restricted to 4 buckets, which flooded top-100 with cardiology (§6) |
| `_bucket_mask` | discover:206-212 | case-insensitive substring | over-match risk |
| min positives / min rows | tier1:68 | `<5` / `<100` skip | narrow specialties dropped |
| XGB params | tier1:79-80 | 300/8/0.1/0.8 | identical to main heads |
| `early_stopping_rounds` | tier1:90 | 20 | **differs from main (50)**; disabled if no val positives |

### 9.6 Agent filter (`walk_forward_v4.py`)
| Knob | file:line | Current | Note |
|---|---|---|---|
| `AGENT_FILTER_NAME` | :93 | floor_0.50 | enum (5 variants) |
| applies to | :447-449 | train + val + score | all three filtered (kept #5, 2026-05-29) |
| `spec_le0` / `state_le2` | :192-196 | 0 / ≤2 train pos → drop | train→score leakage |
| floor percentile | :205 | **P10** | hardcoded basis; only `0.5` mult is "named" |
| floor_0.50 R1/R4/R5/R6 | :237-244 | partd<500 / `0.5*floor` / svcs<1.5 / hcpcs≤5&opi==0 | active rules |
| _rederived / dmeexempt / floor_0.65 | :225-282 | raw-value floors | **dead variants** (#2: revisit later) |
| from_json | :294-313 | feature/direction/threshold | `--filter-from`; skips bad cols silently |

### 9.7 Feature construction
| Knob | file:line | Current | Note |
|---|---|---|---|
| peer-pct groupby / na_option | discover:198-201 | type × state / "keep" | — |
| HCPCS-rich groupby / na_option | compute_hcpcs_rich:415 | type only / "bottom" | **FIX #7 → type × state** (rebuild) |
| ratio denom clip | feature_frames:32-40 | `clip(lower=1)` | 9 ratios |
| xprog clips | feature_frames:256-262 | upper 1000 / 10 | magic |
| brace_mill thresholds | feature_frames:272 | DME>$25k & PartB<$10k | magic |
| family fillna | feature_frames (many) | 0 | universal zero-fill |
| org-NPI drop | feature_frames:399 | drop entity_code "O" | Type-2 removal |
| `CAT_COLS` | walk_forward:119,458 | 2 cols cast then **dropped** | enable_categorical dead (#10) |

**2024 coverage (added 2026-05-29):** `features_full_2024` = **453 cols** = full set minus the 51 DMEPOS-derived cols (`dme` 14 / `dmer` 32 / `xprog` 5) — CMS has not released 2024 DMEPOS. Otherwise family-parity with 2023 (incl. base-`hcpcs` 35, `op` 12, `gnn_pca` 16, centrality 2). The 51 absent cols are reindex-filled at score time. See `pipeline_contract.yaml availability_gaps.dmepos_2024_absent`.

### 9.8 GNN encoder + PCA (on `main` 2026-05-29; per-year export is deterministic — a re-run reproduces embeddings to ~1e-6)
| Knob | file:line | Current | Note |
|---|---|---|---|
| raw embed dim / layers / dropout | gnn.py:39-41 | 64 / 2 / 0.1 | masked-code GraphSAGE AE (not node2vec) |
| epochs / lr / weight_decay / grad clip | pretrain_gnn:313-314, train() | 30 / 1e-3 / 1e-5 / 1.0 | wd+clip hardcoded |
| mask fraction | pretrain_gnn:315 | 0.15 | self-supervised strength |
| device | pretrain_gnn:317 | mps | — |
| graph / top_k_codes / drop_top_codes_k | gnn_data:122-123 | bipartite NPI×HCPCS (Part B) / 500 / 15 | — |
| inductive (no NPI table) | gnn.py:121 | True | anti-leakage by design; gold NPIs NOT force-included |
| **PCA n_components** | pca_analyze_gnn | **16** | **INVARIANT** |
| OOV fill | export | 0.0 + `gnn_in_vocab=0` | — |

### 9.9 Graph centrality (`build_hcpcs_centrality_fast.py`)
| Knob | file:line | Current | Note |
|---|---|---|---|
| PageRank alpha / max_iter / tol | :90-92 | 0.85 / 50 / 1e-9 | legacy graph.py uses max_iter 100 (divergent) |
| top_k_codes / drop_top_codes_k | :160-161 | 500 / 15 | drop ubiquitous E&M |
| clustering variant | :127-189 | bipartite-projected, weighted-degree denom | differs from networkx binary |
| min co-occurrence | — | none | no edge pruning |
| cols kept | contract | pagerank+clustering; x3 forbidden | x3 intermediate |

### 9.10 Feature selection (DORMANT — `SCHEMES_TRAIN_SMALL=()`)
| Knob | file:line | Current | Note |
|---|---|---|---|
| method / `SMALL_HEAD_K` | feature_select:137 / walk_forward:84 | f_classif / 30 | dead unless small heads enabled |
| `SMALL_HEAD_SEEDS` | feature_select:126 | DME/TELEHEALTH | both dropped → unused |
| variance/correlation filter | — | none | no collinearity pruning anywhere |

### 9.11 Labels (`labels_v2.py`, `v4_gold.py`)
| Knob | file:line | Current | Note |
|---|---|---|---|
| `min_confidence` (labels_v2) | labels_v2:69 | high | rank {high:0,med:1,low:2}, `<=` |
| active_years intersection | labels_v2:128 | exact membership | — |
| `augment_leie_lookahead` | labels_v2:70 | None | **DEPRECATED dead block (150-206)** |
| `min_confidence` (v4_gold) | v4_gold:70 | **IGNORED (no-op)** | all gold rows "high" |
| year filter (v4_gold) | v4_gold:110 | exact membership | `window` removed 2026-05-22 |
| NPI-centric gold_set | labels_v2:41-62,134-137 | `.get` + empty-`eligible` guard | tolerates rows with no `defendants[]`/`tier`/`outcome` (2026-05-29); robustness only |
| join-key column (v4_gold) | v4_gold:113,149 | `npi` (fallback `v4_npi`) | renamed `v4_npi`→`npi` 2026-05-31 to match feature sources; see `docs/GOLD_SET.md` changelog |

### 9.12 Rule features (`fraudlib/rules/rules_v3.json`)
These 59 rules are used **only as FEATURE columns** (`rule__000–058`). They are **NOT the panel filter** — the **canonical** panel pre-filter is the agent filter `floor_0.50` (`walk_forward_v4.py::_agent_filter_drop_mask`, data-fit from train-positive specialty/state/payment stats), independent of `rules_v3`. (The `rule_derivation` agent is an experimental tool to propose *new* candidate rules from new features — not the canonical filter.)
| Knob | file:line | Current | Note |
|---|---|---|---|
| rule-feature count | rules_v3.json | 59 (37 single_hi, 7 single_lo, 15 pair) | coverage 0.5489; feature columns only |
| per-rule threshold / volume_floor | rules_v3.json | percentiles / {25,50,100} | hand-editable (feature generation) |
| panel filter (canonical) | walk_forward_v4:_agent_filter_drop_mask | agent filter `floor_0.50` | data-fit; NOT rules_v3 |

### 9.13 Train-matrix precision (`walk_forward_v4.py`)
| Knob | file:line | Current | Note |
|---|---|---|---|
| feature dtype | `_load_stamped` (float64→float32 at load) + reindex `.astype(np.float32)` | **float32** | memory: a full 500k-cap train pool (~3M rows × 544 cols) is ~13.6GB at float64 (OOMs 16GB) vs ~6.8GB at float32 |

**Why it's safe.** XGBoost `tree_method=hist` bins each feature into 256 buckets before splitting, and the multitask neural net trains in float32, so float32 inputs are *lossless* for both learners and sub-noise for ranking. A 500k-cap A/B on the 2024 rederived-filter panel moved positive ranks by less than the binning resolution; the only R@10k difference was one borderline positive crossing the 10k line (8,499 ↔ 11,573) — and that A/B was confounded by training-pool size (500k vs 250k), not dtype.

**Baseline note.** `results/current` was built **float64** (pre-2026-05-31) and was NOT rebuilt — it is a float64-era baseline. The float32 difference is below the recall noise floor, so the committed panels remain the canonical reference. Adopted 2026-05-31. Mirrored in `pipeline_contract.yaml` (`caps.train_matrix_dtype`, `history.precision`).
