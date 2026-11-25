# Stage 2: LLM Extraction with Verification

Stage 1 (`scripts/scrape/`) produces an immutable raw archive of bytes. Stage 2 (this directory) extracts structured case records from those bytes with multi-pass verification.

This directory now contains the executable LLM-first rebuild scaffold. Python
is responsible for inventory, deterministic batching, local public-data joins,
schema validation, and strict label export. Python is **not** the narrative
parser; authoritative case extraction and NOT_CASE decisions happen in the
local Codex/model session with required source quotes. No hosted model API is
called by this pipeline.

## Current runnable flow

```bash
# 1. Inventory the immutable raw archive. No case facts are parsed here.
python -m scripts.extract.cli manifest \
  --raw-root data/raw \
  --output data/extracted/manifest.jsonl

# 2. Select a deterministic 300-500 document pilot.
python -m scripts.extract.cli pilot \
  --manifest data/extracted/manifest.jsonl \
  --output data/extracted/pilot_manifest.jsonl \
  --size 400

# 3. Prepare local-model extraction prompts. Process these in the local
# Codex/model session and write JSON responses to local_extract_results.jsonl.
python -m scripts.extract.cli prepare-extract \
  --manifest data/extracted/pilot_manifest.jsonl \
  --prompts data/extracted/local_extract_prompts.jsonl

# 4. Import local-model extraction responses.
python -m scripts.extract.cli import-extract \
  --results data/extracted/local_extract_results.jsonl \
  --output data/extracted/pilot_extracted.jsonl

# 5. Deduplicate LLM-extracted cases while retaining every source reference.
python -m scripts.extract.cli dedupe \
  --extracted data/extracted/pilot_extracted.jsonl \
  --output data/extracted/pilot_deduped.jsonl

# 6. Prepare local-model validation prompts with local CMS/NPPES facts.
python -m scripts.extract.cli prepare-validate \
  --extracted data/extracted/pilot_deduped.jsonl \
  --prompts data/extracted/local_validate_prompts.jsonl

# 7. Import local-model validation responses.
python -m scripts.extract.cli import-validate \
  --results data/extracted/local_validate_results.jsonl \
  --output data/extracted/pilot_validated.jsonl

# 8. Export strict provider-year model labels.
python -m scripts.extract.cli labels \
  --validated data/extracted/pilot_validated.jsonl \
  --output data/extracted/model_labels.jsonl
```

## Pipeline

```
data/raw/{source}/{YYYY}/{slug}.html    ← Stage 1 output (immutable)
                ↓
        Pass 1: Extraction
           ↓ JSON with required source_quote per field
data/extracted/{source}/{YYYY}/{slug}.json
                ↓
        Pass 2: Cross-source verification
           ↓ same case in DOJ + USAO + OIG → match by date/defendants
data/extracted/_verified/{case_id}.json
                ↓
        Pass 3: NPI / public-data validation
           ↓ local NPPES/PECOS/CMS facts → local model confirms or flags
data/extracted/_npi_verified/{case_id}.json
                ↓
        Pass 4: Adversarial error-finding
           ↓ "find errors in this extraction" pass with same source bytes
data/extracted/_adversarial/{case_id}.json
                ↓
        Pass 5: Schema validation (Pydantic)
           ↓ typed validation; controlled-vocab charges
data/cases/v2/{case_id}.json    ← Final output (Stage 2 result)
data/cases/_review/{case_id}.json   ← Failed validation (human review)
data/cases/_rejected/{case_id}.json ← Manifestly not health-care fraud
```

In the current local-model workflow, the intermediate files are JSONL prompt
and result files rather than API job IDs:

- `local_extract_prompts.jsonl` → processed by the local model session
- `local_extract_results.jsonl` → imported to `pilot_extracted.jsonl`
- `local_validate_prompts.jsonl` → processed by the local model session
- `local_validate_results.jsonl` → imported to `pilot_validated.jsonl`

## Pass 1: Structured extraction

Required fields per case, each with `confidence` and `source_quote`:

| Field | Type | Required | Notes |
|---|---|---|---|
| `case_name` | string | Y | Free text caption |
| `case_id` | string | Y | Generated stable ID: `{source}-{date}-{first-defendant-slug}` |
| `defendants` | array of objects | Y | Each: `{name, role, sentence?, restitution?}` |
| `charges` | array of strings | Y | Controlled vocab: FCA / AKS / Stark / wire-fraud / mail-fraud / etc. |
| `fraud_types` | array of strings | Y | Controlled vocab from existing taxonomy |
| `restitution_total` | number (USD) | N | Sum of all defendants' restitution |
| `forfeiture_total` | number | N | |
| `prison_terms` | array of `{defendant, months}` | N | |
| `entities` | array of objects | N | Lab/clinic/practice names involved |
| `medicare_codes` | array of strings | N | CPT/HCPCS codes mentioned |
| `payer_lines` | array of strings | N | medicare-partb, medicaid, tricare, partd, dmepos |
| `geographic_jurisdiction` | object | Y | `{usao, district, court}` |
| `enforcement_action_date` | ISO date | Y | When the action occurred (sentencing, settlement, indictment) |
| `press_release_date` | ISO date | Y | |
| `scheme_summary` | string | Y | 1-3 sentence narrative |
| `active_years` | object | Y | `{start, end}` for the underlying conduct |
| `tier` | A/B/C | Y | A: convicted/sentenced; B: indicted/charged; C: civil settlement only |

Each field carries `_confidence ∈ {high, medium, low}` and `_source_quote` (verbatim excerpt from press release supporting the extraction).

LLM model: Sonnet for cost. ~3K input tokens (truncated press release) + ~2K output (JSON). $0.018/case at $3/M-input + $15/M-output. ~13K cases × $0.018 = ~$235.

## Pass 2: Cross-source verification

The same case often appears in DOJ HQ + USAO district + OIG enforcement releases. Match candidates by:
- `enforcement_action_date` within ±3 days
- ≥1 shared defendant name (Levenshtein distance ≤ 2)
- Same district / state

When matched: merge fields, take the most-detailed version, boost `confidence` to high for cross-source-confirmed fields, flag disagreements for review.

When no match: case is single-sourced, `cross_source_verified: false`.

## Pass 3: NPI resolution

For every `defendant` with role ∈ {doctor, physician, nurse-practitioner, dentist, pharmacist, etc.}:
- Query local NPPES registry data by explicit NPI, or by name + state when NPI is absent
- If unique match found: attach NPI, primary taxonomy, license state
- If multiple matches: attempt disambiguation via specialty/credential
- If no match: flag as "name only — NPI not resolved" (could be excluded provider, retired, or name mismatch)

NPI resolution gates **Tier A** quality. Cases without NPI go to Tier B.

## Pass 4: Adversarial error-finding

Separate LLM call with prompt: "Given the press release text and this extraction, identify any errors: hallucinated facts the press release does not contain; fabricated dollar amounts; confused defendant roles; misstated charges; conflated separate cases."

This catches the most common v1 corpus pollution modes. Output: list of issues per case, each with severity. Any "high" severity issue → routes case to `_review/`.

## Pass 5: Schema validation

Pydantic model with controlled vocabularies for charges, fraud_types, payer_lines. Hard validation of date formats, currency parsing, etc.

Cases failing validation route to `_review/` with the validation error attached.

## Quality tiers

- **Tier A** (the new gold standard): cross-source verified + NPI resolved + adversarial check passed + schema valid. Estimated 30-50% of input cases.
- **Tier B**: schema valid + (cross-source verified OR adversarial check passed) + (NPI resolved OR no medical-professional defendants). 30-40% of input.
- **Tier C**: single-source + adversarial check passed + schema valid. 10-20% of input.
- **Rejected**: no medical professional defendants OR clearly not healthcare-fraud OR adversarial check found high-severity errors.

## Estimated outputs (after running on all archived raw files)

Given Stage 1 archive size at completion:
- ~2,100 DOJ press releases (HQ + USAO from v1 seed)
- ~11,000 OIG enforcement actions
- ~5,000-10,000 additional USAO press releases (from discovery)

Total raw input: ~18,000-23,000 source files.

After dedup + classification:
- Unique cases: ~10,000-15,000 (substantial overlap between sources)
- Tier A: ~3,000-5,000
- Tier B: ~3,000-5,000
- Tier C: ~2,000-4,000
- Rejected: ~2,000-4,000

This is a 5-10x improvement over v1's 2,337 polluted cases.

## Cost estimate

| Pass | Tokens per case | Cases | Total | Sonnet | Opus |
|---|---|---|---|---|---|
| 1 (extraction) | ~5K in + 2K out | 18K | 90M in + 36M out | $810 | $4,050 |
| 4 (adversarial) | ~6K in + 1K out | 18K | 108M in + 18M out | $594 | $2,970 |
| **Total LLM** | | | | **~$1,400** | **~$7,000** |

Pass 2 (cross-source) and Pass 3 (NPI) are pure Python (~free). Pass 5 (validation) is also pure Python.

For comparison, real-world FCA case-research subscriptions cost $50K-$200K/year. We're building a comparable corpus for low-thousand dollars.

## Why not run Stage 2 yet

Stage 1 is still running. Stage 2 should not start until at least the v1 seed scrape completes — otherwise we'll re-extract the same cases when later raw files arrive. Defer to a separate PR after the archive is stable.

The architecture above is the contract. Implementation files (`pass1_extract.py`, `pass2_verify.py`, etc.) land when Stage 1 archive is settled.
