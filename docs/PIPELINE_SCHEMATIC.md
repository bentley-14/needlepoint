# Pipeline Schematic (2026-05-05)

**Last updated:** 2026-06-09 (ML-layer entry point corrected to walk_forward_v4.py; deeper refresh pending — feature counts/baselines below reflect 2026-05-05 state)
**Labels:** DOJ-only (LEIE dropped 2026-05-05) — gold_set.jsonl, ~1,000 unique NPIs (current count + changelog: GOLD_SET.md)
**Features (2026-06-09):** ~470–544 columns/year (base + satellites + hcpcs-242 + gnn_pca-16 + centrality + 59 rule__ fires; year-dependent availability)
**Committed baseline (2026-05-31):** mean R@10K = 0.446 across 2015–2023 (maximal-sane sweep, agent filter floor_0.50, blend xgb:3 neural:1 perspec:0)
**Canonical terms:** `scripts/REGISTRY.yaml` → `vocabulary:` is the authoritative "use this, not that" map for terms/artifacts (gold_set, floor_0.50, the 59 rule-features, the NeedlePoint console, the Corpus Curator, DOJ-only labels). Enforced by pre-commit guard #7 (`scripts/verify_vocabulary.py`). Check it before naming an artifact.

---

```
┌─────────────────────────────────────────────────────────────┐
│                        DATA SOURCES                         │
│                                                             │
│  CMS Part B panels (2013–2023)       data/panels/           │
│  CMS Part B by-Service (per HCPCS)   data/panels/           │
│  CMS Part D prescriber               data/processed/        │
│  CMS DMEPOS (referring provider)     data/processed/        │
│  Open Payments (Sunshine Act)        data/panels/           │
│  NPPES provider registry             data/registries/       │
│  HCPCS code distributions            data/panels/           │
│  DOJ case library                    data/source/cases/     │
│                                                             │
│  NOT used for labels:                                       │
│    LEIE (NPI repository for scraping only, NOT a label)     │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────┐
│              FEATURE ENGINEERING                            │
│              fraudlib/ranker/feature_frames.py              │
│                                                             │
│  load_feature_frame(year)                                   │
│    → ~288 columns per provider (248 base + 40 satellite):   │
│                                                             │
│    BASE (248 cols, cached at data/cache/feature_frames/):   │
│      Part B aggregates + derived ratios (47)                │
│      Peer percentiles within specialty+state (19)           │
│      Global percentiles (16)                                │
│      Part D prescribing (31)                                │
│      Bauder entropy/concentration (8)                       │
│      HCPCS code features (35)                               │
│      NPPES provider metadata                                │
│      Graph features (fraud-neighbor, self-subtracted) (7)   │
│                                                             │
│    SATELLITES (40 cols, each with independent cache):        │
│      cl_*  (14) claim-level features                        │
│        Source: data/processed/cms_partb_svc_{year}.parquet   │
│        Cache:  data/cache/claim_level_features/             │
│        Signal: POS patterns (AUC 0.588), J-code (0.569),   │
│                E&M upcoding (0.528)                         │
│      dme_* (14) DMEPOS referring-provider features          │
│        Source: data/processed/cms_dmepos_{year}.parquet      │
│        Cache:  data/cache/dmepos_features/                  │
│        Signal: DME ordering volume (AUC 0.585), HCPCS       │
│                diversity (0.581), supplier count (0.577)     │
│      op_*  (12) Open Payments features                      │
│        Source: data/panels/open_payments_general_{year}.pqt  │
│        Cache:  data/cache/openpayments_features/            │
│        Signal: n_payments (AUC 0.608), n_meals (0.607),     │
│                n_manufacturers (0.607), total_usd (0.598)   │
│                                                             │
│  Coverage: base 2013–2023, cl 2013–2023, dme 2014–2023,    │
│            op 2013–2023 (2013–2014 via Profile ID crosswalk)│
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────┐
│              LABEL STAMPING + RULE FILTER                   │
│              feature_frames.stamp_labels_and_filter()        │
│                                                             │
│  Labels: DOJ ONLY (gold_set.jsonl, high-confidence NPI)     │
│    Source: data/extracted/gold_set.jsonl                  │
│    Loader: fraudlib.eval.archetypes.archetype_lookup(year)   │
│    LEIE is NOT used as a label source (dropped 2026-05-05)   │
│                                                             │
│  5 archetypes:                                              │
│    BILLING_FRAUD | KICKBACK | OPIOID_MILL                   │
│    UPCODING | MEDICALLY_UNNECESSARY                         │
│                                                             │
│  Label counts (DOJ only):                                   │
│    2013: 1,360 | 2015: 1,107 | 2018: 814 | 2023: 456       │
│                                                             │
│  Pre-processing:                                            │
│    → Drop NPI Type-2 (organizational)                       │
│    → 59 v3 rule fires (rule__000 – rule__058)               │
│      Rules: fraudlib/rules/rules_v3.json                    │
│    → Graph features (fraud-neighbor, self-subtracted)       │
│    → Filter: keep only rows where ≥1 rule fires             │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────┐
│       LAYER 1: ML RANKER (3+1 component cascade)            │
│       scripts/experiments/walk_forward_v4.py            │
│                                                             │
│  Walk-forward: train on years < Y, score year Y             │
│  9 rounds: 2015, 2016, 2017, ..., 2023                     │
│                                                             │
│  ┌─────────────────┐  ┌─────────────────┐                  │
│  │ A: Per-archetype │  │ B: Multi-task   │                  │
│  │    XGBoost       │  │    Neural Net   │                  │
│  │ (5 archetype     │  │ (shared encoder │                  │
│  │  heads — no more │  │  128→64→32,     │                  │
│  │  STATE_EXCLUSION)│  │  5 heads)       │                  │
│  └────────┬─────────┘  └────────┬────────┘                  │
│           │                     │                            │
│  ┌────────┴─────────┐  ┌───────┴─────────────┐              │
│  │ C: Cohort        │  │ D: Per-specialty     │              │
│  │    IsolationForest│  │    XGBoost           │              │
│  │ (per specialty+   │  │ (32 buckets defined, │              │
│  │  state, semi-     │  │  ~6-12 materialize   │              │
│  │  supervised)      │  │  per round)          │              │
│  └────────┬─────────┘  └───────┬──────────────┘              │
│           │                     │                            │
│           └──────┬──────────────┘                            │
│                  ▼                                           │
│         RRF Fusion (K=60)                                    │
│         Σ 1/(K + rank_component) for each component          │
│                  │                                           │
│                  ▼                                           │
│         scored_full_{year}.parquet  (gitignored)             │
│         top2000_{year}.csv         (gitignored)              │
│         recall_summary.json        (tracked)                 │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       │  top 1000–2000 candidates
                       ▼
┌─────────────────────────────────────────────────────────────┐
│   LAYER 2: AGENT TRIAGE (15-angle forensic verdict)         │
│   fraud-investigator-v2/.../agents/triage.py                 │
│   prompt: prompts/triage.md  (exact Agent-Triage-v3 rubric)  │
│   norms: data/cache/triage_norms/norms_{year}.json           │
│   (scripts/compute_triage_norms.py — computed per-specialty  │
│    opioid/dual norms → {computed_norms} prompt slot; the     │
│    hand-curated tables remain as labeled fallback)           │
│                                                             │
│  15-angle checklist (specialty-relative; not a flat score): │
│   1 Phantom plausibility   2 Opioid anomaly (norms)         │
│   3 Peer deviation         4 Vulnerability targeting        │
│   5 Billing intensity      6 Code diversity (entropy)       │
│   7 Drug-fraction split    8 Part D prescribing intensity   │
│   9 Single-code dominance 10 DOJ fingerprint match          │
│  11 Cross-year persistence 12 Off-specialty billing         │
│  13 DMEPOS brace-mill rcpt 14 Scheme-code shares            │
│  15 Suppression + industry ties                             │
│                                                             │
│  Signal convergence + archetype → verdict:                  │
│    HIGH_RISK      ←── ≥3 signals + matched DOJ archetype     │
│    MODERATE_RISK  ←── 2 signals (or 1 + ambiguous)          │
│    LOW_RISK       ←── 0–1 weak signals; legit-for-specialty │
│    DISMISS        ←── ML overweighted a benign feature      │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       │  HIGH_RISK + selected MODERATE
                       ▼
┌─────────────────────────────────────────────────────────────┐
│       LAYER 3: INVESTIGATOR ↔ LEGAL LOOP                    │
│       fraud-investigator-v2/.../orchestrator.py              │
│       MAX_ITERATIONS = 10                                    │
│                                                             │
│  Step-0 evidence pack (E1, deterministic, built once/case): │
│    evidence_pack.py pre-computes identity, PECOS (target +  │
│    every co-biller), exclusions, Part B profile, top codes, │
│    co-biller characterization (Step 2B) → injected into the │
│    discover + verify prompts; agent turns go to web/courts. │
│    Calibration: panel-only + year-gated, live sections      │
│    redacted.                                                │
│                                                             │
│    ┌──────────────┐       ┌──────────────┐                  │
│    │ Investigator  │──────▶│ Legal Agent  │                  │
│    │              │       │              │                  │
│    │ Gathers       │       │ Rule 9(b)    │                  │
│    │ evidence via: │       │ slot check,  │                  │
│    │ • describe_   │  ◀────│ damages      │                  │
│    │   panel()     │  gaps │ floor,       │                  │
│    │ • provider_   │       │ caseReady?   │                  │
│    │   context()   │       │              │                  │
│    │ • run_sql()   │       │ Three-tier   │                  │
│    │ • run_python()│       │ evidence:    │                  │
│    │ • v3 rule_    │       │ confirmed_   │                  │
│    │   hits()      │       │ fact /       │                  │
│    └──────────────┘       │ statistical_ │                  │
│                           │ signal /     │                  │
│    Loop until:            │ legal_       │                  │
│    caseReady = TRUE       │ hypothesis   │                  │
│    OR iteration = 10      └──────────────┘                  │
│                                                             │
│  Output: EvidenceBundle + LegalGuidance                      │
│          status = case_ready | not_actionable                │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       │  only if caseReady = TRUE
                       ▼
┌─────────────────────────────────────────────────────────────┐
│       LAYER 4: ADVERSARIAL DEFENSE LOOP                     │
│       fraud-investigator-v2/.../agents/defense.py            │
│       MAX_ADVERSARIAL_ROUNDS = 4                             │
│                                                             │
│    ┌──────────────┐   ┌──────────────┐   ┌──────────────┐  │
│    │ Defense Agent │──▶│ Legal Agent  │──▶│ Investigator  │  │
│    │ (independent  │   │ (disposes    │   │ (rebuttal     │  │
│    │  adverse      │   │  per-count)  │   │  evidence if  │  │
│    │  counsel,     │   │              │   │  PENDING_     │  │
│    │  web search + │   │ SURVIVED     │   │  REBUTTAL)    │  │
│    │  CourtListener│   │ MITIGATED    │   │              │  │
│    │  + URL fetch) │   │ DISPOSITIVE_ │   └──────────────┘  │
│    │              │   │ NOT_SURVIVED │                      │
│    │ Files motion  │   │              │                      │
│    │ to dismiss    │   │ Discount per │                      │
│    └──────────────┘   │ count: det.  │                      │
│                       │ schedule     │                      │
│                       └──────────────┘                      │
│                                                             │
│  Discounts: deterministic schedule (discounts.py) — the     │
│    LLM classifies each MITIGATED attack (SOL window,        │
│    contested factors, severity, …); the code computes the   │
│    0-50% discount. The LLM never picks the number.          │
│                                                             │
│  Terminal when:                                             │
│    • anyAttackSurvives = false → SETTLEMENT_READY           │
│    • all counts abandoned      → NOT_ACTIONABLE             │
│    • round = 4                 → INDEFENSIBLE_AT_CAP        │
│                                                             │
│  Damages = treble × single × (1 − cumulative discount)     │
│            summed over retained counts                       │
│                                                             │
│  Guardrail 2: counsel_build's numeric_predicates are        │
│    re-derived from canonical constants BEFORE defense        │
│    (scripts/audit_case_numerics.py) + injected; a MISMATCH   │
│    is a dispositive integrity defect the defense must raise   │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────┐
│   GUARDRAIL 3: INNOCENT-EXPLANATION FORECLOSURE CHECK       │
│   agents/foreclosure_check.py  (opus / xhigh)               │
│   runs after the adversarial loop, before final scoring     │
│                                                             │
│   Per count:  generate (reusable taxonomy + case-specific)  │
│     → steelman → seek GROUNDED foreclosure (cite-or-LIVE;   │
│       audit the constants behind the foreclosing evidence)  │
│     → classify:  FORECLOSED | PARTIAL | LIVE                │
│                                                             │
│   Deterministic cap (scoring.foreclosure_residual):         │
│     max LIVE plausibility ≥ 0.5  (re-derived from rows,     │
│       NOT the LLM self-report)                              │
│     → strength STRONG→MODERATE, blocks a clean FILE         │
│       (SETTLEMENT_READY → FILE_WITH_DISCOVERY_DEPS)         │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────┐
│   GUARDRAIL 4: HUMAN-IN-THE-LOOP CHECKPOINT                 │
│   checkpoint.py (pure) — fires at the Phase-B disposition   │
│   ABANDON seam + the Phase-C decline seam                   │
│                                                             │
│   Pause (NOT abandon/decline) when a dispositive call rests │
│   on a premise the loop CANNOT verify:                      │
│     • jurisdiction bar (first-to-file / public-disclosure)  │
│       resting on a SEALED prior case asserted as a qui tam  │
│       at confirmed_fact tier  (the Birch/Dogwood failure)         │
│     • foreclosure DECLINE on a LIVE innocent explanation    │
│       closable only by an off-record fact (closure_owner =  │
│       discovery:* | cannot_close_pre_filing)                │
│                                                             │
│   → final_verdict = NEEDS_HUMAN_CHECKPOINT; run() exits 0   │
│     with a ReviewRequest (no dossier). Human answers via    │
│     scripts/answer_checkpoint.py → human_attested fact +    │
│     cache rewind → resume re-decides (--resume-case).       │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────┐
│       LAYER 5: CASE WRITER                                  │
│       fraud-investigator-v2/.../agents/case_writer.py        │
│                                                             │
│  Deliverables per case:                                     │
│    00_README.md              — case summary                 │
│    01_Case_Report.md         — prosecutor brief             │
│    02_Layperson_Summary.md   — plain-language summary       │
│    03_Full_State.json        — machine-readable state       │
│    04+ Adversarial docs      — defense run transcripts      │
│                                                             │
│  Prose docs pass through a deterministic markdown autofix   │
│  (scripts/fix_dossier_markdown.py: \$-escape currency,      │
│  backtick snake_case, ***x***→**x**) before writing;        │
│  scripts/lint_dossier_markdown.py verifies post-write       │
│                                                             │
│  Terminal verdict:                                          │
│    SETTLEMENT_READY — ≥1 retained count, discount <70%      │
│    NOT_ACTIONABLE   — all counts defeated or trivial target  │
└─────────────────────────────────────────────────────────────┘
```

## Queue economics (M2/M3/M5) — between triage (Layer 2) and case-building (Layer 3)

Three tools spend the expensive Layer-3 budget better (added 2026-06-12):

- **Cluster-first investigation** — `scripts/cluster_queue.py` groups queue
  HIGH_RISK/MODERATE_RISK candidates by exact practice address (Part B
  `Rndrng_Prvdr_St1` + `Rndrng_Prvdr_Zip5`) and emits a `suggested_subject`
  per multi-member cluster, so one combined office-suite investigation
  replaces N per-NPI runs that each rediscover the same co-billers.
- **EV ordering** — `scripts/queue_ev_rank.py` re-orders a TriageOutput queue
  by EV = `Tot_Mdcr_Pymt_Amt` × confidence × verdict weight (HIGH 1.0 /
  MODERATE 0.4 / LOW 0.05 / DISMISS 0.0); output is a valid TriageOutput
  (file order = EV desc; each item's `rank` keeps the ML rank).
- **PARKED registry + scanner** — a case abandoned ONLY for an empty floor
  slot 3 (`PASS_NO_SLOT3`: no falsity anchor and no regulatory corroboration)
  is parked by the orchestrator at `<state_root>/parked/<case_id>.json`
  (target + co-biller watch NPIs + resume command; sidecar, never blocks the
  abandon). `scripts/check_parked_cases.py` scans the registry against
  revocation / LEIE / CA-S&I / gold_set refreshes and surfaces the resume
  command when a watch NPI hits — the sunk build spend becomes recoverable.

## Key file paths

| Layer | File | What it does |
|-------|------|--------------|
| Features | `fraudlib/ranker/feature_frames.py` | ~288-col feature builder + DOJ label stamper |
| Claim-level | `fraudlib/ranker/claim_level_features.py` | 14 cl_* features from Part B by-service |
| DMEPOS | `fraudlib/ranker/dmepos_features.py` | 14 dme_* features from referring-provider data |
| Open Payments | `fraudlib/ranker/openpayments_features.py` | 12 op_* features from Sunshine Act |
| Rules | `fraudlib/rules/rules_v3.json` | 59 v3 data-driven rules |
| Labels | `fraudlib/eval/archetypes.py` | DOJ-only archetype lookup (5 archetypes) |
| Cases | `data/extracted/gold_set.jsonl` | DOJ case library — maintained by the **corpus_curator pipeline** (`pipeline/scrapers/gold_set_builder.py`); legacy `doj_api_ingest.py` deprecated |
| Ranker A | `scripts/experiments/train_per_archetype_rankers.py` | 5 archetype XGBoost heads |
| Ranker B | `fraudlib/ranker/multitask.py` | Shared-encoder neural net |
| Ranker C | `scripts/experiments/run_tier1_combined.py` | Cohort IsolationForest |
| Ranker D | `scripts/experiments/run_tier1_combined.py` | Per-specialty XGBoost (~6-12 models) |
| Harness | `scripts/experiments/walk_forward_v4.py` | 9-round walk-forward (canonical worker; walk_forward_full retired 2026-06-09) |
| Agent triage | `fraud-investigator-v2/src/fraud_investigator/agents/triage.py` (`prompts/triage.md`) | 15-angle forensic triage → 4 verdict tiers (HIGH/MODERATE/LOW_RISK, DISMISS); specialty norms computed by `scripts/compute_triage_norms.py` (hand tables = labeled fallback). Angle 2 (opioid) fires on the **within-specialty** opioid / long-acting-opioid percentile (≥0.95), not just the absolute bar — so a `999`-bar specialty (pain/PM&R/anesthesia) no longer disables it (catches intra-specialty diversion outliers). Angle 1 (phantom) is gated by the **unit-semantics guard**: `svcs_per_bene`/`svcs_per_day` count UNITS not encounters, so per-unit drug (J-code mg), skin-substitute (Q41xx per sq cm), and lab billing is NOT phantom volume |
| Launchers | `needlepoint.py`, `scripts/np_launch.py` | `python3 needlepoint.py` (guided run, every knob is a flag) · `make investigate NPI=…` → `orchestrate_pipeline --npi` (strict NPI format: exactly 10 digits starting 1/2; queue auto-discovery → panel subject → bare fallback); agent runs bill the machine-local claude account; non-fatal R2 preflight notice if credentials absent |
| R2 artifact push | `scripts/sync_artifacts_r2.sh` + `scripts/orchestrate_pipeline.py` `_fire_r2_sync` | Completion-time detached push of all run artifacts (cases/investigations/pipeline_runs) to `r2:needlepoint-data/artifacts/`; env-var rclone fallback (R2_ACCOUNT_ID/R2_ACCESS_KEY_ID/R2_SECRET_ACCESS_KEY from env or .env); fail-open (unconfigured machines skip silently) — fired from `finally`, so hard-crash runs push partial artifacts for post-mortems |
| Queue clustering | `scripts/cluster_queue.py` | M2 — groups triage-queue HIGH/MODERATE candidates by exact practice address into office-suite clusters (one combined investigation instead of N) |
| Hindsight replay | `scripts/hindsight_replay.py` | Self-learning loop 1 — monthly DOJ-as-delayed-oracle funnel replay; routes misses to feature_engineer / triage rubric / floor analysis (docs/SELF_LEARNING.md) |
| Calibration battery | `scripts/calibration_battery.py` + `validation/battery/` | Self-learning loop 3 — TP/control quality ratchet; prompt/pipeline changes must pass `--ratchet` vs the committed baseline before merge |
| Weight fitter | `scripts/fit_scoring_weights.py` | Self-learning loop 4 — MAP logistic fit of `scoring.WEIGHTS` vs realized outcomes (hand weights as prior; min-n recommendation floor) |
| Hard-negative miner | `scripts/mine_hard_negatives.py` | Self-learning loop 5 — merits-killed clearances → provisional training negatives (oracle outranks; half vote) |
| EV queue gate | `scripts/queue_ev_rank.py` | M5 — re-orders a TriageOutput queue by EV (payment × confidence × verdict weight); valid TriageOutput out, ML ranks preserved |
| Parked cases | `orchestrator.py::_maybe_park_case` + `scripts/check_parked_cases.py` | M3 — slot-3-only abandons (`PASS_NO_SLOT3`, no FAIL) parked at `<state_root>/parked/`; scanner re-triggers on revocation/LEIE/CA-S&I/gold_set hits for watch NPIs |
| Orchestrator | `fraud-investigator-v2/src/fraud_investigator/orchestrator.py` | Layers 3–5 sequencing (claude -p per step) |
| Investigator | `fraud-investigator-v2/src/fraud_investigator/prompts/investigator.md` | Evidence gathering (iterative passes) |
| Evidence pack | `fraud-investigator-v2/src/fraud_investigator/evidence_pack.py` | Step-0 deterministic pre-computation (identity / PECOS / exclusions / co-biller Step-2B); calibration-aware (year-gated, live-registry sections redacted) |
| Counsel | `fraud-investigator-v2/src/fraud_investigator/prompts/counsel_build.md` (+ first_chair_review / rebuttal / disposition) | count construction · 9(b) · disposition gates |
| Tier lint | `fraud-investigator-v2/src/fraud_investigator/tier_lint.py` | R3 — mechanical modal-verb / government-event flags on `confirmed_fact` items; rides the floor audit to first_chair + goes verbatim to defense |
| Citation audit | `fraud-investigator-v2/src/fraud_investigator/citation_audit.py` | M6 — re-verifies every evidence citation (allowlisted command re-execution, URL, data-path) before case_writer; skipped in calibration |
| Floor audit | `fraud-investigator-v2/src/fraud_investigator/floors.py` | Mechanical convergence-floor arithmetic, injected into first_chair + disposition prompts; slot 3 = falsity_anchor OR regulatory (F1 — regulatory is a bonus, not a gate) |
| Mock defense | `fraud-investigator-v2/src/fraud_investigator/mock_defense.py` | PR7 (flag `--defense-as-mock` / `FRAUD_INV_DEFENSE_AS_MOCK=1`, default OFF): Phase B's mock = REAL defense round 1 fired early into adv-r1/; the adversarial loop replays the cached motion and continues from legal_adv_response. Deterministic rebuttal-shape synthesis + survival heuristic |
| Defense | `fraud-investigator-v2/src/fraud_investigator/prompts/defense.md` (+ legal_adv_response.md) | Adversarial stress test (4 rounds max); **Guardrail 2** numeric-predicate audit (`scripts/audit_case_numerics.py`) injected as dispositive defects |
| Foreclosure check | `fraud-investigator-v2/src/fraud_investigator/agents/foreclosure_check.py` (`prompts/foreclosure_check.md`) | **Guardrail 3** — Innocent-Explanation Foreclosure Ledger (post-defense). LIVE plausibility ≥0.5 caps strength + blocks a clean FILE |
| HITL checkpoint | `fraud-investigator-v2/src/fraud_investigator/checkpoint.py` + `scripts/answer_checkpoint.py` | **Guardrail 4** — pause (`NEEDS_HUMAN_CHECKPOINT` + `ReviewRequest`) instead of killing a case on an unverifiable dispositive premise (sealed-case jurisdiction bar; off-record-closable foreclosure). Human answers → `human_attested` fact + cache rewind → resume. Detectors are pure; wired at the Phase-B disposition + Phase-C decline seams |
| Case writer | `fraud-investigator-v2/src/fraud_investigator/prompts/case_writer.md` | Final deliverables; prose autofixed by `scripts/fix_dossier_markdown.py`, verified by `scripts/lint_dossier_markdown.py` |
| Doctrine | `scripts/precedent_lookup.py` | FCA precedent-corpus lookup (R2-sourced, local fallback); legal agents only (counsel_build's only scoped Bash) — investigator does NOT use it |
| Doctrine (pleading-time) | `scripts/doctrine_lookup.py` + `docs/legal_corpus/` | BM25 free-text retrieval over 13 long-form doctrine cards (Escobar, Schutte/Safeco, Rule 9(b), Twombly, Integra/AseraCare/Winter, first-to-file, public-disclosure, AKS/Greber, SSA 1861(r)(4), Rogan/Bornstein, reasonable-and-necessary, Mackby, Cochise SOL); complements `precedent_lookup.py`'s structured R2 defense-mode x circuit corpus by giving counsel_build/defense the full doctrine narrative at pleading time (counsel_build's scoped Bash includes both) |
| CI guard | `tests/ranker/test_no_label_leakage.py` | max \|corr\| < 0.5 |

## Data path reference

| Data | Path | Years | Notes |
|------|------|-------|-------|
| Part B panels | `data/panels/cms_partb_{year}.parquet` | 2013–2023 | Hardlinked to data/processed/ |
| Part B by-service | `data/panels/cms_partb_svc_{year}.parquet` | 2013–2023 | ~400 MB/yr, 9.5M rows |
| Part D | `data/processed/cms_partd_{year}.parquet` | 2013–2023 | |
| DMEPOS | `data/processed/cms_dmepos_{year}.parquet` | 2014–2023 | 2013 not published by CMS |
| Open Payments | `data/panels/open_payments_general_{year}.parquet` | 2013–2023 | 2013–14 via Profile ID crosswalk |
| Feature cache | `data/cache/feature_frames/features_{year}.parquet` | 2013–2023 | 248 base cols |
| cl_* cache | `data/cache/claim_level_features/claim_{year}.parquet` | 2013–2023 | |
| dme_* cache | `data/cache/dmepos_features/dmepos_{year}.parquet` | 2014–2023 | |
| op_* cache | `data/cache/openpayments_features/op_{year}.parquet` | 2013–2023 | |
| DOJ cases | `data/extracted/gold_set.jsonl` | | ~1,000 unique NPIs (see GOLD_SET.md) |
| LEIE | `data/legacy/leie/leie.parquet` | | NPI repo ONLY, not labels |
| FCA doctrine corpus | `data/ground_truth/doctrine_corpus.jsonl` (R2: `processed/v1/`) | | Precedent by defense-mode × circuit; legal agents only, via `precedent_lookup` |

## Shelf code (not wired)

| File | Status |
|------|--------|
| `fraudlib/ranker/temporal_features.py` | 7 features, tried and reverted (`95f7dc7`) |

## Iteration semantics

| Loop | Max rounds | Exit condition |
|------|-----------|----------------|
| Investigator ↔ Legal | 10 | `caseReady = TRUE` or exhausted |
| Defense ↔ Legal ↔ Rebuttal | 4 | `anyAttackSurvives = false` or all counts abandoned |

## Resume semantics (no run is unrecoverable — added 2026-06-12)

**Invariant: a crash never costs more than the step that was executing.**
Relaunch with `--case-id <id>` (or `--resume-case` via orchestrate_pipeline)
and the run continues from the first step without a completed artifact.

| Layer | Mechanism |
|---|---|
| Every agent step | `parsed.json` cache in `claude_p.invoke_agent` AND `streaming.invoke_agent_streaming` — a valid cached output reloads without spawning `claude -p`. Delete a step's `parsed.json` to force re-run; `FRAUD_INV_NO_CACHE_LOAD=1` forces all. |
| Phase A | Incomplete trailing iter record trimmed; the iter redoes with completed sub-steps reloading from cache. |
| Phase B | Partial `rebuttal_closing` record reused in place; completed sub-steps reload from cache; skipped entirely once disposition exists. |
| Adversarial loop | **Trim-and-replay**: persisted round records are discarded and the loop replays from round 1 via the cache (zero spawns for completed steps), so records/continuation-counts/scoring rebuild consistently. Delete `adv-r*/` dirs to force fresh. |
| Foreclosure check | Skipped when the ledger is already on state (cache covers the agent call regardless). |
| Evidence pack | Built once per case; `evidence_pack.md` presence is the guard. |
| Case writer | Per-document cache (`_cw_*/` dirs); only missing documents regenerate. |
| Status integrity | `_reconcile_status_from_disk` re-derives the true status from disposition `parsed.json` when a crash handler clobbered it to `agent_error`. |

Guarded by `fraud-investigator-v2/tests/test_resume_guards.py` (cache on both
invoke paths, adversarial trim, foreclosure skip) and
`test_event_sink_stage_coverage.py` (every orchestrator stage registered —
the foreclosure_check omission that crashed every completed run pre-#265).

## Calibration clean-room mode (added 2026-06-03)

The fraud-investigator-v2 orchestrator (Layers 3-5 of the pipeline) supports
a toggleable **`--calibration-as-of YYYY-MM-DD`** flag for blind-detection
eval against known DOJ true-positives. When set:

- **Per-agent launch hardening** — `claude_p.invoke_agent` injects per-step
  `--allowed-tools` / `--disallowed-tools` flags so each agent runs sandboxed:
  Read is jailed to the calibration run state dir, `WebSearch`/`WebFetch`/
  `Grep`/`Glob` are denied, web access goes through `scripts/sandbox_browser.py`
  (date-gated Wayback), NPPES through `scripts/sandbox_nppes.py` (post-cutoff
  fields redacted), and CourtListener/doj_pr_lookup are unavailable.
- **Synthetic case_id** — `CALIBRATION-<8-hex-uuid>`, written to
  `state/calibration/<case_id>/` so calibration runs are sibling-isolated
  from production case state.
- **Lifecycle** — orchestrator sets a module-level `CalibrationContext` on
  `.run()` entry, clears it in `try/finally`. Cannot leak into subsequent
  production runs in the same Python process.

The mode is the eval harness for "does the loop independently surface the
fraud DOJ already proved?" Full protocol + threat model in
[`INVESTIGATOR_SANDBOX_EVAL.md`](INVESTIGATOR_SANDBOX_EVAL.md). Smoke runner
+ leak watchdog at `scripts/calibration/run_calibration_smoke.sh`.

This mode does NOT affect the walk-forward training pipeline (Layer 1
ranker) or `fraudlib/pipeline_contract.yaml` — those are independent of
the needlepoint agents.
