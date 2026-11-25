# Building on NeedlePoint

The platform is meant to be modified. Every common change is listed below with the files it touches and how
to check that it worked. `docs/OUTPUT.md` explains the gates and verdicts; `docs/EVAL.md` explains how to measure a change.

## Setup

```bash
make setup && source .venv/bin/activate
make test          # or: PYTHONPATH=.:fraud-investigator-v2/src python -m pytest tests fraud-investigator-v2/tests -q
```

Tests that need the non-distributed case corpus or a storage bucket skip themselves.

## Common changes

| Goal | Where | Check with |
|---|---|---|
| Use a different model (any provider) | `FRAUD_INV_EXEC_BACKEND=api`, `FRAUD_INV_API_MODEL` or `FRAUD_INV_API_MODEL_<STEP>` (see `.env.example`) | a quick run, then the battery (`docs/EVAL.md`) |
| Change a step's default model or effort | `MODEL_BY_STEP`, `EFFORT_BY_STEP` in `fraud-investigator-v2/src/fraud_investigator/claude_p.py` | `fraud-investigator-v2/tests/test_profile_resolver.py` |
| Edit an agent's instructions | `fraud-investigator-v2/src/fraud_investigator/prompts/<step>.md` | `tests/test_prompt_budget.py` (size ceiling), the battery |
| Change what an agent may do | the `extra_args` tool flags in `agents/<step>.py` (Claude-style `--allowedTools` / `--disallowedTools`; the api backend enforces the same policy) | `fraud-investigator-v2/tests/test_api_backend.py` |
| Add an investigator tool | a script in `scripts/`, registered in `scripts/REGISTRY.yaml`, mentioned in `prompts/investigator.md` | `tests/test_registry_integrity.py` |
| Add an agent tool for non-Claude models | `fraud-investigator-v2/src/fraud_investigator/llm_tools.py` (`tool_specs` + a `_t_<name>` method) | `test_api_backend.py` |
| Add a coverage rule (falsity anchor) | `data/reference/coverage_anchors.yaml` (cite the rule text, not guidance) | `tests/test_anchors.py` |
| Change verdict logic | `verdict.py` (filing predicates), `scoring.py` (strength and EVF bands), `discounts.py` | `fraud-investigator-v2/tests/test_verdict*.py`, the battery |
| Change loop depth | `needlepoint.py --depth`, or `DEPTHS` in `needlepoint.py` (build iterations, at least 2, and defense rounds); `TURN_BUDGET_BY_STEP` for per-step turns | a quick run |
| Add a data source | a `BaseScraper` subclass in `pipeline/scrapers/` and an entry in `scripts/fetch_public_data.py`; read it through `fraudlib.r2.DataPath` so it works locally, from a bucket, or from the public dataset | `tests/pipeline/`, `tests/test_r2_public_mirror.py` |
| Use your own copy of the data | point `NP_DATA_URL` at any mirror (an `hf://` dataset, an `https` URL, or `file://` path) with the same layout | `tests/test_r2_public_mirror.py` |
| Change the ranker | `fraudlib/ranker/`, `fraudlib/pipeline_contract.yaml` | walk-forward recall (`docs/EVAL.md`) |

## Rules

1. **No real names.** Do not commit case files, dossiers, or anything naming a provider who has not been
   charged. Test fixtures use synthetic NPIs (checksum-invalid) and pseudonyms.
2. **Claims need receipts.** A falsity predicate cites rule text (Federal Register / CFR / LCD), not program
   guidance. A statistic cites the query that produced it. Before calling a pattern impossible, establish from
   CMS manuals how the claims system could have paid it (date-span lines, unit conventions, bundled codes).
3. **Report evaluations with run health.** Say how many runs errored or were excluded.
4. **Leak safety is not optional.** Calibration runs must use the export mode and date-gated browser.

## Agent design rules

- **Required behavior is a scored output field**, enforced by a guardrail; prose instructions alone lose to
  turn pressure. Check that no scoring layer penalizes it.
- **Repair prompts state the actual error** (not-JSON and wrong-shape JSON get different prompts) and return
  the model's own output with the exact validation errors.
- **On a turn or time budget, finalize**: resume the same session and ask for the answer from the evidence so
  far; unfinished threads go to the next iteration.
- **Unverifiable case-killing premises pause** at a human checkpoint (`checkpoint.py`) instead of deciding.
- **Route models by step.** Tool-heavy steps tolerate cheaper models; judgment steps (counsel, defense,
  foreclosure, disposition) set case quality, so measure them on the production profile.

## Pull requests

One logical change per pull request, with tests. For changes to prompts, models, or gates, include battery
results before and after (`python3 scripts/calibration_battery.py --report --format json`).
