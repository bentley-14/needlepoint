# Synth-claims configs

A synth-claims config is a JSON file that fully specifies a calibration case's synthetic claim-level layer: the subject NPI, the cutoff window, the peer cohort, the scheme to inject, and the compliant-background controls.

`scripts/calibration/build_synth_claims.py --config <path-to-config>` reads one of these and produces the corresponding `synth_claims.parquet`. The engine is NPI-agnostic; cases differ only in their config + (for new fraud types) a scheme module.

## Why config-driven

Before generalization, `build_synth_claims.py` was hardcoded to Aspen (NPI, scheme renderer NPIs, co-located peers, year window, intensity schedule — all module-level constants). Generalization unlocks:

1. **Running the calibration loop in Arm B mode (with claim-level data) for any case** — not just Aspen.
2. **Adding a new fraud scheme** without touching the engine — write a new module in `synth_schemes/` + a new config here.
3. **Preserving reproducibility of pinned runs** — the original Aspen v3 (`CALIBRATION-5B2E14D2`) values live in `aspen.json` verbatim, so re-running on that config yields equivalent output.

## Schema (top-level keys)

| Key | Type | Purpose |
|---|---|---|
| `case_id` | string | Short slug used in claim_id prefixes + the report header. |
| `subject_npi` | string (10 digits) | The TP NPI being calibrated against. |
| `cutoff_year_start`, `cutoff_year_end` | int | Inclusive pre-cutoff window. Claims outside this range are not generated. |
| `synthea_dir` | path | Where to read the Synthea CSV fabric (beneficiary IDs + service dates) for realistic (bene, date) tuples. |
| `seed` | int | Deterministic random seed. Pin this and the same config yields the same parquet. |
| `peer_cohort` | object | The real-NPI peer pool to sample for cross-provider contrast. Keys: `state`, `specialty`, `count`, `claims_cap_per_year_per_peer`, `em_only`. |
| `scheme` | object | The fraud-pattern injector. Must have `type` (which dispatches to a scheme module). Other keys depend on the scheme. |
| `compliant_background` | object | Optional. Adds a "normal" version of the scheme to non-subject peers so the scheme's mechanic isn't itself a fraud label. |

The scheme module reads `scheme.*` keys it understands; unknown keys raise.

## Existing schemes

### `scheme.type: "incident_to"`

NP-rendered E&M billed under a physician's NPI at 100% (without 42 CFR 410.26 conditions met). See `aspen.json` for the full schema example.

Required `scheme.*` keys:
- `renderer_npis`: `{npi: {name, enrollment}, ...}` — the NPs whose rendering gets attributed under the subject. Enrollment dates date-gate pre-enrollment attributions (prevents synthetic artifacts).
- `co_located_npis`: list of real NPIs at the subject's practice (used as a within-building control; get full-disaggregation real billing, NO scheme injection).
- `intensity_by_year`: per-year fraction of subject's E&M that is NP-rendered. Split into `established` and `new_patient` (ancillary codes are always subject-rendered).

## Adding a new case / scheme

1. **Same scheme, new case** (e.g., another incident-to TP): copy `aspen.json` → `<newcase>.json`, swap `subject_npi`, `renderer_npis`, `co_located_npis`, `cutoff_year_*`, `peer_cohort.state` to the new case's values.
2. **New scheme**: add a module in `scripts/calibration/synth_schemes/<scheme>.py` (TBD when the second scheme lands) implementing the engine's scheme protocol, then a config here that uses it.

## Backward compatibility

Provider Aspen v3 (`CALIBRATION-5B2E14D2`) was built when the constants were hardcoded. `aspen.json` reproduces those exact values. With the same seed, the refactored engine produces a parquet whose row count, NPI distribution, year coverage, and per-NPI roll-up match the original (deterministic).
