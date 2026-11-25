# Physician Compare — DAC National Downloadable File (group affiliations)

**Scraper:** not included in this repository; the data ships in the public dataset.
**Schema reference (columns + dtypes):** [docs/DATA_SCHEMAS.md §2.7](../../docs/DATA_SCHEMAS.md)
**Files:** `data/registries/physician_compare_YYYY-MM.parquet` (dated snapshots) + `physician_compare.parquet` (symlink → most recent)

> ⚠️ **Multi-row per NPI.** Each row is one clinician *enrollment*
> (NPI × group practice × address). A physician practicing at three groups
> has three rows. `ProviderRecordSchema.npi` is declared `str` but is **not**
> a unique key here — the per-affiliation grain is the whole point (it's
> what lets you see two NPIs sharing one billing entity). Collapse to
> one-row-per-NPI before joining to NPI-keyed sources, the same contract
> `cms_partb_svc` and `cms_dmepos_detailed` live under. See §2.

> ℹ️ **Relationship to `care_compare`.** The ML/ranker side already reads
> this same CMS file via
> [`fraudlib/ingest/providers/care_compare.py`](../../fraudlib/ingest/providers/care_compare.py),
> but only as a **single current snapshot** collapsed to per-NPI features
> (`care_compare.parquet`, `load_care_compare_features()`). This scraper is
> the **pipeline-side producer**: it writes **dated, historical** snapshots
> at the raw per-enrollment grain. The two are intentionally decoupled
> (`pipeline → fraudlib`, never the reverse) — this scraper imports nothing
> from `fraudlib`. Migrating the ranker consumer onto these dated snapshots
> is a separate, future step; it is **not** done here, and
> `care_compare.py` is untouched.

## 1. What this dataset is, in plain English

CMS publishes the **Doctors and Clinicians National Downloadable File**
(`DAC_NationalDownloadableFile.csv`) as part of the Care Compare program
(formerly *Physician Compare*; dataset `mj5m-pzi6`). It is the public
roster of **which clinician belongs to which group practice**.

```
   clinician  ──enrolled at──▶   group practice (billing entity)   ──practices at──▶   address
   (NPI / Ind_PAC_ID)            (Org_PAC_ID = "Group PAC ID")                          (adr_ln_1, …)
```

The field that earns this source its place is **Group PAC ID**
(`Org_PAC_ID`): the group practice's *billing-entity* identity —
effectively the TIN-level grouping. NPPES gives only a practice **address**;
two providers at the same address may bill under different entities, and one
entity may span many addresses. The DAC tells you who actually **shares a
billing entity**, which is exactly what kickback / shared-group analysis
needs:

- **Two NPIs under the same `Org_PAC_ID`** = same billing entity → explains
  correlated volume (a group, legitimately or as a ring).
- **Two NPIs at the same address but *different* `Org_PAC_ID`s** = the
  suspicious pattern (co-located but separately billing — a common
  pill-mill / kickback structuring signal).
- **One `Org_PAC_ID` spanning many addresses** = explains a high-volume
  footprint as a multi-site group rather than one impossible solo provider.

### Acronyms / column meanings

| Column (canonical) | Meaning |
|---|---|
| `npi` | National Provider Identifier (canonical, zero-padded 10-digit string) |
| `Ind_PAC_ID` | Individual PECOS Associate Control ID — a **stable clinician identity** that persists across job changes (NPPES doesn't guarantee this) |
| `Org_PAC_ID` | **Group PAC ID** — the group practice's billing-entity (TIN-level) identity. The headline field |
| `Org_Lgl_Nm` | Group practice name (sourced from `Org_Lgl_Nm` on older snapshots, `Facility Name` on recent ones) |
| `Num_Org_Mem` | Number of clinicians enrolled in the group practice |
| `pri_spec` | Primary specialty (free-text-ish, ~150 distinct values) |
| `Provider_Type` | Provider type, where the snapshot ships it (absent on recent files → NA) |
| `adr_ln_1` / `adr_ln_2` | Practice street address |
| `City` / `state` / `ZIP_Code` | Practice city / 2-letter state / ZIP (zero-padded; read as text) |
| `Hosp_Afl_1`…`Hosp_Afl_5` | Hospital Affiliation **CCNs** (CMS Certification Numbers), up to 5. Present on older snapshots only — NA on recent files |
| `snapshot` | The snapshot date (`YYYY-MM`) this row came from — provenance for cross-snapshot tracking |
| `PAC ID` | PECOS Associate Control ID — PECOS is Medicare's provider enrollment system; PAC IDs are its stable identity keys (individual = `Ind_PAC_ID`, org = `Org_PAC_ID`) |

## 2. Unit of observation

**One row = one clinician enrollment: (NPI × Org_PAC_ID × practice
address).** A solo physician at one practice is one row; a physician
enrolled at three groups (or one group at three addresses) is three rows.
The current file is ~2.5M rows for ~1.3M clinicians.

The scraper **projects** the raw DAC CSV to a curated canonical column set
(the fraud-relevant fields) rather than preserving all ~30 raw columns —
this is deliberate, because the raw DAC layout drifts year-over-year (§6).
Each canonical column is resolved from a list of known source aliases
case-insensitively; columns a given snapshot doesn't ship are NaN-padded.

Declared schema = 3 base stamps (`source`, `fetched_at_utc`,
`schema_version`) + 4 `ProviderRecordSchema` canonicals (`npi`,
`provider_name`, `state`, `data_year`) + the curated affiliation/address/
hospital columns above + `snapshot`. `data_year` is **null** — the DAC is
an evergreen snapshot, not an annual cohort; `snapshot` carries the date.

`provider_name` is built per distinct (last, first, MI) tuple via
`format_provider_name(entity_code="I", …)` (the DAC is individual
clinicians) and broadcast back — avoids a per-row format call at 2.5M rows.

## 3. How CMS publishes it (cadence + suppression)

- **Snapshot cadence**, roughly quarterly. Each file is a full current-state
  roster, **not** an annual cohort — there is no `data_year`.
- **Lag** is short (weeks), unlike the ~18-month CMS PUF lag.
- **The CMS data API exposes only the current distribution.** Historical
  snapshots live on the archived-data page
  (https://data.cms.gov/provider-data/archived-data/doctors-clinicians) and
  are fed to the scraper via `url=` / `local_csv=` with an explicit
  `snapshot="YYYY-MM"`. "Pull whatever historical snapshots are available."
- **No cell suppression.** The DAC is an enrollment roster, not a claims
  aggregate — there is no <11 floor. Missing values are genuine blanks
  (a clinician with no group affiliation has an empty `Org_PAC_ID`).
- **The current-distribution URL rotates** each CMS refresh, so the scraper
  resolves it live at fetch time from the metastore
  (`resolve_current_dac_url()` → `…/metastore/schemas/dataset/items/mj5m-pzi6`)
  rather than pinning a URL — the dataset id is stable, the resource-hash
  path is not.

## 4. Fraud signals — what to look for

The DAC is rarely a standalone signal; its power is as a **structural lens**
on volume found in the claims PUFs.

### 4a. Shared-TIN kickback rings

Two or more NPIs that bill suspiciously correlated volume (Part B / Part D /
DMEPOS) and turn out to share one `Org_PAC_ID` — the billing entity is the
ring. Conversely, co-located NPIs (same `adr_ln_1`/`ZIP_Code`) under
*different* `Org_PAC_ID`s is the classic structuring pattern.

**Column signatures:**
- Group-by `Org_PAC_ID` → set of `npi`s sharing it; cross-reference their
  claims-side volume
- Group-by (`adr_ln_1`, `ZIP_Code`) → NPIs at one address with ≥2 distinct
  `Org_PAC_ID`s

### 4b. Volume that a group explains (false-positive suppression)

A solo-looking provider pulling 95th-percentile volume reads very
differently once you see they're one member of a 40-clinician group
(`Num_Org_Mem` high) billing under a shared entity. Used to *down-weight*
peer-comparison outliers that are really group artifacts.

**Column signatures:** high `Num_Org_Mem`; one `Org_PAC_ID` spanning many
`adr_ln_1` values.

### 4c. Post-settlement provider hopping (needs the dated snapshots)

A provider who leaves a flagged group right after a settlement and
re-enrolls under a fresh `Org_PAC_ID` — invisible in a single current
snapshot, visible by diffing `Org_PAC_ID` for an `npi`/`Ind_PAC_ID` across
`snapshot`s. This is the capability the dated/historical snapshots add over
the existing single-snapshot `care_compare`.

**Column signatures:** same `Ind_PAC_ID`, changed `Org_PAC_ID` across
consecutive `snapshot`s; correlate the change date with a known case
outcome year.

### 4d. Specialty misrepresentation

`pri_spec` claimed here vs. the actual code mix in the claims PUFs (e.g.
"Internal Medicine" on the roster, but a pain-management / J-code code mix
in Part B). The DAC supplies the *claimed* specialty for the comparison.

## 5. Joins to other sources

| Join target | Key | Why |
|---|---|---|
| `cms_partb_YYYY.parquet` | `npi` | Group context for the provider's own billing. Collapse the DAC to one-row-per-NPI first |
| `cms_dmepos_YYYY` / `cms_dmepos_detailed_YYYY` | `npi` | Group/TIN context for DME referrers — does a "solo" mill referrer actually share a billing entity with the supplier-adjacent providers? |
| `nppes_features_*.parquet` | `npi` | NPPES has practice **address**; the DAC adds the **billing entity** NPPES lacks. Use both for the §4a co-location-vs-shared-TIN distinction |
| `care_compare.parquet` | `npi` | The ranker's current-snapshot, per-NPI-collapsed view of this same file. Reconcile, don't duplicate |
| `gold_set.jsonl` | `npi` | Known fraud cases for label join |

Pitfalls when joining:

- **Collapse to one-row-per-NPI before joining to NPI-keyed sources** (e.g.
  `groupby("npi")` taking the largest-group affiliation), or the
  multi-enrollment rows explode the join. `load_care_compare_features()` is
  the existing reference collapse for the ranker.
- **`Org_PAC_ID` is the billing entity, not the address.** Two rows with the
  same address but different `Org_PAC_ID` are different billing entities —
  that distinction is the §4a signal, don't collapse it away by address.
- **NPI must be string-zero-padded.** Use the canonical `npi` column.
- **ZIP / CCN are zero-padded codes.** The scraper reads the whole file as
  text to protect them; don't re-numericize.

## 6. Snapshot coverage and schema drift

The DAC layout has drifted materially across snapshots — this is why the
scraper projects to a curated canonical set rather than pinning the raw
schema:

| Drift | Older snapshots | Recent snapshots | Handling |
|---|---|---|---|
| Group PAC ID casing | `Org_PAC_ID` | `org_pac_id` | alias list, case-insensitive |
| Group name | `Org_Lgl_Nm` | `Facility Name` | alias list → canonical `Org_Lgl_Nm` |
| Group size | `Num_Org_Mem` | `num_org_mem` | alias list |
| Hospital affiliations | `Hosp_Afl_1..5` present | **dropped** | NaN-padded via `_align_to_schema` |
| Provider type | sometimes present | absent | NaN-padded |

Each canonical column maps to a list of known source aliases tried in order
(see `_ALIASES` in the scraper). The first present alias wins; absent
columns are NaN-padded by `BaseScraper._align_to_schema`. Because the
scraper **selects only** the curated columns, the strict `_validate()` never
sees an undeclared raw column — the projection is the drift firewall.

If CMS adds a field worth capturing (e.g. it ever publishes hospital
affiliation again), add it to `_ALIASES` + the schema and bump
`schema_version` to `v1.1`.

### 6a. Dtype contract

The schema is a Pandera `DataFrameModel`. All curated columns are
`nullable=True` (drift means any snapshot may omit some). The one numeric
column, `Num_Org_Mem`, is read as text then `to_numeric`-coerced (blanks →
NA) so Pandera lands it as a nullable int. `snapshot` carries a
`^\d{4}-\d{2}$` check. The whole CSV is read with `dtype=str` (canonical
column names differ from raw headers, so per-column read-dtype pinning
can't apply pre-projection) — this protects zero-padded ZIPs and CCNs, the
same stance `care_compare._csv_to_parquet` takes.

## 7. Known limitations

- **Enrollment roster, not claims.** No dollars, no volume — the DAC tells
  you *structure* (who's in which group), which only becomes a fraud signal
  joined to the claims PUFs.
- **`Org_PAC_ID` ≈ TIN, not exactly TIN.** It's the PECOS billing-entity
  key; it correlates strongly with the tax entity but CMS doesn't publish
  the raw TIN. Good enough for shared-entity grouping.
- **Hospital affiliations are gone from recent snapshots.** The
  `Hosp_Afl_*` CCN columns only populate on older files; recent snapshots
  pad them NA. For current hospital-affiliation analysis use a different
  source.
- **Self-reported specialty.** `pri_spec` is what the clinician declared,
  not adjudicated — that's exactly why §4d compares it to the billed code
  mix.
- **Historical depth depends on what CMS archived.** The API only exposes
  the current file; older snapshots are best-effort from the archive page.

## 8. File contract

After running `PhysicianCompareScraper`:

```
data/registries/physician_compare_YYYY-MM.parquet        # curated cols, multi-row per NPI
data/registries/physician_compare_YYYY-MM.manifest.json  # RunResult: source, schema_version, columns, fetch URL
data/registries/physician_compare.parquet                # symlink → most-recent dated snapshot
```

The `physician_compare.parquet` pointer is a **relative symlink** (target =
filename only) so it survives directory moves and rclone sync to R2 (rclone
copies the link target). Backfilling a snapshot *older* than the current
pointer leaves the pointer alone; the newest snapshot on disk always owns
it. This mirrors the most-recent-pointer convention other rolling sources
use.

Manifest fields are documented on
[`pipeline.base.RunResult`](../../pipeline/base.py).
