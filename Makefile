# NeedlePoint: convenience targets.
#
#   make setup                  # create .venv and install everything
#   make case                   # guided run: pick settings, type an NPI, investigate
#   make case NPI=1234567890    # same, straight to a provider (add ARGS="--depth standard ...")
#   make knobs                  # list every setting and its current value
#   make show                   # summarize the latest case (or CASE=CASE-XXXX-2026)
#   make doctor                 # check your setup
#   make fetch YEAR=2024        # optional: download one year to work offline (data is read in place otherwise)
#   make test                   # run the test suites
#   make install-ranker         # add PyTorch etc. to train or rescore the ranker
#
# The ranking pipeline (XGBoost + neural ranker, triage, investigation) runs through
# scripts/orchestrate_pipeline.py; see docs/WALK_FORWARD_PIPELINE.md.

YEAR     ?= 2024
PYTHON   ?= python3
PIP      := .venv/bin/pip
PYRUN    := $(shell test -x .venv/bin/python3 && echo .venv/bin/python3 || echo python3)

.PHONY: setup venv install install-ranker case knobs show doctor fetch investigate test clean

venv:
	@$(PYTHON) -c 'import sys; sys.exit(sys.version_info < (3, 11))' || { echo "NeedlePoint needs Python 3.11+ ($(PYTHON) is too old). Install a newer Python, then: make setup PYTHON=python3.12"; exit 1; }
	$(PYTHON) -m venv .venv

install: venv
	$(PIP) install -U pip
	$(PIP) install -r requirements.txt
	$(PIP) install -e "fraud-investigator-v2[test]"

install-ranker: install
	$(PIP) install -r requirements-ranker.txt

setup: install
	@echo "Next: put a model key in .env (cp .env.example .env), then: source .venv/bin/activate && python3 needlepoint.py"

case:
	$(PYRUN) needlepoint.py run $(if $(NPI),--npi $(NPI),) $(ARGS)

knobs:
	@$(PYRUN) needlepoint.py knobs

show:
	@$(PYRUN) needlepoint.py show $(CASE)

doctor:
	@$(PYRUN) needlepoint.py doctor

fetch:
	$(PYRUN) needlepoint.py fetch --year $(YEAR) $(if $(DATASETS),--datasets $(DATASETS),)

# Engine launcher with the low-level flags (rounds, backend, budget profile).
investigate:
	@test -n "$(NPI)" || { echo "usage: make investigate NPI=1234567890 [ROUNDS=4] [RESUME=CASE-ID] [BUDGET=1] [SAFE_PERMS=1] [BACKEND=tmux|claude_p|api] [PROFILE=draft]"; exit 1; }
	$(if $(SAFE_PERMS),FRAUD_INV_SAFE_PERMS=1) $(PYRUN) scripts/np_launch.py investigate $(NPI) $(if $(ROUNDS),--rounds $(ROUNDS),) $(if $(RESUME),--resume $(RESUME),) $(if $(BUDGET),--budget,) $(if $(BACKEND),--backend $(BACKEND),) $(if $(PROFILE),--profile $(PROFILE),)

test:
	PYTHONPATH=.:fraud-investigator-v2/src $(PYRUN) -m pytest tests/ fraud-investigator-v2/tests/ -q

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
