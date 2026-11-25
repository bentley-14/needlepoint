# NeedlePoint: convenience targets.
#
# Start here:
#   make setup                  # create .venv and install everything
#   make fetch YEAR=2024        # optional: download one year to work offline (data is read in place otherwise)
#   make case                   # guided run: pick settings, type an NPI, investigate
#   make case NPI=1234567890    # same, straight to a provider (add ARGS="--depth standard ...")
#   make knobs                  # list every setting and its current value
#   make show                   # summarize the latest case (or CASE=CASE-XXXX-2026)
#   make doctor                 # check your setup
#   make test                   # run the test suites
#
# Maintainer targets below (pull-*/push-*/setup-r2, janitor, queue-*, investigate) need your
# own rclone remote and bucket (NP_R2_REMOTE / NP_R2_BUCKET) and are not needed to run cases.

PY       := .venv/bin/python
PIP      := .venv/bin/pip
YEAR     ?= 2024
# R2 bucket names kept as the original v1 prefixes for backward
# compatibility (existing mirrors push/pull against these). Locally, processed
# parquets live in data/processed and the corpus in data/source; a future v2
# rename on R2 can align the names.
R2_REMOTE := r2:needlepoint-data/processed/v1
R2_CORPUS := r2:needlepoint-data/ground_truth/v1
R2_HCPCS_RICH := r2:needlepoint-data/cache/v1/feature_frames
R2_RESULTS := r2:needlepoint-data/results/current   # canonical baseline; results/legacy/ holds archived/superseded runs
R2_MEDICAID := r2:needlepoint-data/raw/medicaid     # HHS T-MSIS Medicaid Provider Spending by HCPCS (2018-2024)
R2_MEDICAID_FEATURES := r2:needlepoint-data/processed/v1  # derived medicaid_features_<year>.parquet (same prefix as LOCAL_PANELS)
R2_ARTIFACTS := r2:needlepoint-data/artifacts       # run artifacts: calibration/cases/investigations/pipeline_runs (PDFs incl.)
LOCAL_PANELS := data/processed
LOCAL_MEDICAID := data/raw/medicaid
LOCAL_CORPUS := data/source
LOCAL_HCPCS_RICH := data/cache/feature_frames
LOCAL_RESULTS := results
# Canonical (3,1,0) baseline panels (per-specialty disabled, PR #117). The glob
# keeps transfers to just those 9 panels — NOT the perspec-True originals or the
# _legacy_/quarantine archives that also live under results/.
RESULTS_GLOB := walk_forward_v4_top5_y20*_extras-True_agentfilter-floor_0.50_perspec-False_es50/**

.PHONY: setup case knobs show fetch doctor janitor janitor-apply venv install install-extras install-hooks test fetch-data setup-r2 pull-data push-data verify-data pull-hcpcs-rich push-hcpcs-rich pull-results push-results pull-medicaid pull-medicaid-features push-medicaid-features pull-entity push-entity clean prompt-budget weekly-ops monthly-ops queue-add queue-status queue-daemon

PYTHON   ?= python3
venv:
	@$(PYTHON) -c 'import sys; sys.exit(sys.version_info < (3, 11))' || { echo "NeedlePoint needs Python 3.11+ ($(PYTHON) is too old). Install a newer Python, then: make setup PYTHON=python3.12"; exit 1; }
	$(PYTHON) -m venv .venv

install: venv
	$(PIP) install -U pip
	$(PIP) install -r requirements.txt
	$(PIP) install -e "fraud-investigator-v2[test]"

setup: install
	@echo "Next: put a model key in .env (cp .env.example .env), then: source .venv/bin/activate && python3 needlepoint.py"

install-extras: install
	$(PIP) install -r requirements-paper-repro.txt

# Activate the committed pre-commit guards (registry / pipeline-doc / gold-set drift /
# triage / schematic). core.hooksPath points git at the version-controlled hook dir, so a
# fresh clone gets all guards with ONE command — no manual copy into .git/hooks (which is
# how they were silently being lost).
install-hooks:
	git config core.hooksPath scripts/git-hooks
	@echo "pre-commit guards active (core.hooksPath -> scripts/git-hooks)"

# PYRUN = the ONE interpreter convention (item-1 of the ops leaning): the
# repo venv when present, else PATH python3 (make doctor flags the fallback).
PYRUN := $(shell test -x .venv/bin/python3 && echo .venv/bin/python3 || echo python3)

doctor:
	@$(PYRUN) needlepoint.py doctor

case:
	$(PYRUN) needlepoint.py run $(if $(NPI),--npi $(NPI),) $(ARGS)

knobs:
	@$(PYRUN) needlepoint.py knobs

show:
	@$(PYRUN) needlepoint.py show $(CASE)

fetch:
	$(PYRUN) needlepoint.py fetch --year $(or $(FETCH_YEAR),$(YEAR)) $(if $(DATASETS),--datasets $(DATASETS),)

janitor:
	$(PYRUN) scripts/r2_janitor.py

janitor-apply:
	$(PYRUN) scripts/r2_janitor.py --apply

prompt-budget:
	$(PYRUN) scripts/prompt_budget_report.py

# ── Operating cadence ─────────────────────────────────────────────────────
weekly-ops:
	@echo "=== doctor ===" && $(PYRUN) scripts/doctor.py || true
	@echo "=== parked-case scan ===" && $(PYRUN) scripts/check_parked_cases.py || true
	@echo "=== battery report ===" && { test -f scripts/calibration_battery.py && $(PYRUN) scripts/calibration_battery.py --report || echo "(pending PR #275)"; }
	@echo "=== janitor (dry-run) ===" && $(PYRUN) scripts/r2_janitor.py || true
	@echo "=== prompt budget ===" && $(PYRUN) scripts/prompt_budget_report.py || true
	@echo "=== merged branches (deletable) ===" && git branch -r --merged origin/main | grep -vE "origin/(main|HEAD)" | head -20 || true

monthly-ops:
	@echo "=== hindsight replay ===" && { test -f scripts/hindsight_replay.py && $(PYRUN) scripts/hindsight_replay.py $(if $(SINCE),--since $(SINCE),) || echo "(pending PR #275)"; }
	@echo "=== hard-negative mine ===" && { test -f scripts/mine_hard_negatives.py && $(PYRUN) scripts/mine_hard_negatives.py || echo "(pending PR #277)"; }
	@echo "=== weight-fit report ===" && { test -f scripts/fit_scoring_weights.py && PYTHONPATH=fraud-investigator-v2/src $(PYRUN) scripts/fit_scoring_weights.py --report || echo "(pending PR #277)"; }
	@echo "=== next: corpus_curator batch + data_auditor (fraud-investigator-v2) ==="


investigate:
	@test -n "$(NPI)" || { echo "usage: make investigate NPI=1234567890 [ROUNDS=4] [RESUME=CASE-ID] [BUDGET=1] [SAFE_PERMS=1] [BACKEND=tmux|claude_p|api] [PROFILE=draft]"; exit 1; }
	$(if $(SAFE_PERMS),FRAUD_INV_SAFE_PERMS=1) $(PYRUN) scripts/np_launch.py investigate $(NPI) $(if $(ROUNDS),--rounds $(ROUNDS),) $(if $(RESUME),--resume $(RESUME),) $(if $(BUDGET),--budget,) $(if $(BACKEND),--backend $(BACKEND),) $(if $(PROFILE),--profile $(PROFILE),)

# Sequential investigation queue + babysitter (auto-resume on stall, notify on
# checkpoint/done). Concurrency is 1 by design — parallel runs drain the shared
# 5h token window faster, not throughput. See scripts/ops/np_queue.py.
queue-add:
	@test -n "$(NPI)" || { echo "usage: make queue-add NPI=1234567890 [RESUME=CASE-ID]"; exit 1; }
	$(PYRUN) scripts/ops/np_queue.py add --npi $(NPI) $(if $(RESUME),--resume $(RESUME),)
queue-status:
	$(PYRUN) scripts/ops/np_queue.py status
queue-daemon:
	$(PYRUN) scripts/ops/np_queue.py daemon --concurrency 1 $(if $(POLL),--poll $(POLL),)

# Run the safeguard suites. R2/data-dependent tests skip gracefully when R2 isn't
# configured or local panels are absent; the hermetic ones (registry integrity, schema
# coercers, DOJ amount, jail-safety) always run.
test:
	PYTHONPATH=.:fraud-investigator-v2/src $(PYRUN) -m pytest tests/ fraud-investigator-v2/tests/ -q

fetch-data:   ## download public CMS data into data/processed (e.g. make fetch-data YEAR=2023 DATASETS="partb partb_svc")
	@test -n "$(YEAR)" || { echo "usage: make fetch-data YEAR=2023 [DATASETS=\"partb partb_svc\"]"; exit 1; }
	$(PYRUN) scripts/fetch_public_data.py --years $(YEAR) $(if $(DATASETS),--datasets $(DATASETS),)

setup-r2:
	bash scripts/setup_r2.sh

# rclone copy is additive: restores/updates from R2 but never deletes local files
# absent from R2 (raw downloads, un-pushed caches) — matches pull-results/-medicaid.
pull-data:
	rclone copy $(R2_REMOTE) $(LOCAL_PANELS) --transfers 4 --progress
	rclone copy $(R2_CORPUS) $(LOCAL_CORPUS) --transfers 4 --progress

# rclone copy (additive) so a thinned-out local can NEVER prune the R2 backup;
# use `make janitor` for intentional R2 cleanup. (sync here could wipe R2.)
push-data:
	rclone copy $(LOCAL_PANELS) $(R2_REMOTE) --transfers 4 --progress
	rclone copy $(LOCAL_CORPUS) $(R2_CORPUS) --transfers 4 --progress

# --one-way: confirm every LOCAL file exists+matches in R2 (the pre-delete check);
# R2-only files (backups, other years) are expected and not flagged.
verify-data:
	rclone check --one-way $(LOCAL_PANELS) $(R2_REMOTE)
	rclone check --one-way $(LOCAL_CORPUS) $(R2_CORPUS)

# Derived HCPCS rich-feature cache (produced by
# scripts/compute_hcpcs_rich_features.py from cms_partb_svc_<year>.parquet).
# ~2.4 GB across 11 years (2013-2023). Filter is *.parquet only so we don't
# upload anything else that may live under data/cache/feature_frames/.
pull-hcpcs-rich:
	mkdir -p $(LOCAL_HCPCS_RICH)
	rclone copy $(R2_HCPCS_RICH) $(LOCAL_HCPCS_RICH) --include 'hcpcs_rich_*.parquet' --transfers 4 --progress

push-hcpcs-rich:
	rclone copy $(LOCAL_HCPCS_RICH) $(R2_HCPCS_RICH) --include 'hcpcs_rich_*.parquet' --transfers 4 --progress

# Canonical (3,1,0) walk-forward baseline panels (2015-2023; per-specialty disabled,
# PR #117). ~2.7 GB. rclone copy is additive (NOT sync) so it can't clobber other
# results/ content on either side.
pull-results:
	mkdir -p $(LOCAL_RESULTS)
	rclone copy $(R2_RESULTS) $(LOCAL_RESULTS) --include '$(RESULTS_GLOB)' --transfers 4 --progress

push-results:
	rclone copy $(LOCAL_RESULTS) $(R2_RESULTS) --include '$(RESULTS_GLOB)' --transfers 4 --progress

# Entity-resolution layer:
# fraudlib/entity/resolve.py's entity_graph_{year}.parquet (+ .megaclusters.json) and
# scripts/compute_entity_features.py's entity_features_{year}.parquet. Both already live
# under data/processed (same R2_REMOTE/LOCAL_PANELS as the other processed/v1 panels) --
# these targets just narrow pull-data/push-data's full-directory transfer to entity outputs
# only, mirroring pull-hcpcs-rich/push-hcpcs-rich's narrower derived-cache convention.
pull-entity:
	rclone copy $(R2_REMOTE) $(LOCAL_PANELS) --include 'entity_graph_*.parquet' --include 'entity_graph_*.megaclusters.json' --include 'entity_features_*.parquet' --transfers 4 --progress

push-entity:
	rclone copy $(LOCAL_PANELS) $(R2_REMOTE) --include 'entity_graph_*.parquet' --include 'entity_graph_*.megaclusters.json' --include 'entity_features_*.parquet' --transfers 4 --progress

# HHS T-MSIS Medicaid Provider Spending by HCPCS (2018-2024, 238M rows, ~3.1 GB parquet),
# read by scripts/medicaid_lookup.py. rclone copy is additive. The .duckdb mirror also lives
# in R2 (faster but storage-version-sensitive); the parquet is the universal copy we pull.
pull-medicaid:
	mkdir -p $(LOCAL_MEDICAID)
	rclone copy $(R2_MEDICAID) $(LOCAL_MEDICAID) --include 'provider_spending_full.parquet' --transfers 4 --multi-thread-streams 4 --progress

# Derived per-(NPI, year) Medicaid feature cache (produced by
# scripts/compute_medicaid_features.py from provider_spending_full.parquet).
# Lives alongside the other processed panels (same R2 prefix as pull-data/push-data);
# --include keeps transfers to just these files, not the whole processed/ corpus.
pull-medicaid-features:
	mkdir -p $(LOCAL_PANELS)
	rclone copy $(R2_MEDICAID_FEATURES) $(LOCAL_PANELS) --include 'medicaid_features_*.parquet' --transfers 4 --progress

push-medicaid-features:
	rclone copy $(LOCAL_PANELS) $(R2_MEDICAID_FEATURES) --include 'medicaid_features_*.parquet' --transfers 4 --progress

# Run artifacts (calibration runs, full case states, curated dossiers, pipeline run
# logs — PDFs included). Canonical home is R2 (r2:needlepoint-data/artifacts/), NEVER Git.
# rclone copy is additive (never deletes local copies). Push mirrors sync_artifacts_r2.sh.
pull-artifacts:
	rclone copy $(R2_ARTIFACTS)/calibration   fraud-investigator-v2/state/calibration --transfers 8 --progress
	rclone copy $(R2_ARTIFACTS)/cases         fraud-investigator-v2/state             --transfers 8 --progress
	rclone copy $(R2_ARTIFACTS)/investigations investigations                         --transfers 8 --progress
	rclone copy $(R2_ARTIFACTS)/pipeline_runs state/runs                              --transfers 8 --progress

push-artifacts:
	bash scripts/sync_artifacts_r2.sh

# Ground-truth reference: HCPCS/CPT code descriptors etc. — the canonical lookup
# data (hcpcs_lookup resolves these from R2 ground_truth/v1 when absent locally).
# CPT descriptors are sourced from CMS MPFS RVU (build_cpt_descriptors_from_rvu.py)
# + canonicalize_hcpcs_reference.py. R2 is the source of truth; rclone copy is additive.
R2_GROUND_TRUTH := r2:needlepoint-data/ground_truth/v1
LOCAL_GROUND_TRUTH := data/ground_truth
pull-ground-truth:
	rclone copy $(R2_GROUND_TRUTH) $(LOCAL_GROUND_TRUTH) --include '*.parquet' --include '*.json' --transfers 4 --progress
push-ground-truth:
	rclone copy $(LOCAL_GROUND_TRUTH) $(R2_GROUND_TRUTH) --include '*.parquet' --include '*.json' --transfers 4 --progress

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

# ── integration seams (2026-07-18) ───────────────────────────────────────────
label-refresh:              ## periodic DOJ label flywheel (extract→…→land→annotate)
	bash scripts/ops/label_refresh.sh

board:                      ## unified target board (org+individual, anchor/MO ranked)
	$(PYRUN) scripts/target_board.py --out results/entity_head/board.jsonl

board-to-queue:             ## queue the top uninvestigated board target
	$(PYRUN) scripts/target_board.py --to-queue 1

outcomes:                   ## refresh the case-outcome ledger from state dirs
	$(PYRUN) scripts/ops/outcome_ledger.py

pull-relationships:         ## ownership + PECOS reassignment + POS (tasks 73-75)
	$(PYRUN) scripts/pull_relationship_data.py --sources ownership,pecos,pos
