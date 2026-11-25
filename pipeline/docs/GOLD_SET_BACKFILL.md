# Historical gold_set backfill — what it does, exactly

A **reconciliation pass**: run the *current* curator + deterministic gate over the
**entire** historical DOJ corpus (~4,753 gated releases, 2009–2026), instead of just
the trailing week the weekly run
processes. Same machinery as the weekly builder — only the **input** differs (the
full warm cache, not the incremental discovery window), plus it runs the curator in
**parallel chunks** across **time-boxed waves that auto-chain around the 6h GitHub
job cap**.

## The wave chain (how it beats the 6h job cap)

A full sweep is ~75–100h serial / ~6–10h at `workers=16` — more than one GitHub
job (capped at 6h). So the run is split into **waves**:

- Each wave processes leads from `offset` until a wall-clock **`deadline_minutes`**
  (default 320, leaving margin under the cap), then computes an exact **`next_offset`**
  checkpoint (the contiguous prefix of chunks that finished).
- It **accumulates** its confident labels into `gold_set_backfill.jsonl` and its
  review rows into `backfill_needs_review.csv`, and **commits both to one chain
  branch**. State carries across the separate 6h jobs via that branch.
- If the corpus isn't fully processed (`done=false`), the wave **re-dispatches the
  workflow itself** with `offset=next_offset` on the same branch. The
  `concurrency` group serializes waves, so each starts only after the prior wave
  pushed its state.
- The **final** wave (`done=true`) emails the digest + CSV and pushes the rebuilt
  file to R2. Guards: the chain stops at `done` OR `wave >= max_waves` (default 15).

So you trigger **one** wave; the chain runs itself overnight and you get **one**
email at the end.

It has two modes; the default is **clean-room**.

## Clean-room (default) vs incremental

| | **Clean-room** (`--clean-room`, default in the workflow) | **Incremental** |
|---|---|---|
| Existing 1,303-row gold_set | treated as **empty** (Step-0 dedupe off) | deduped against; known NPIs SKIP |
| Writes to | a **separate** file `data/extracted/gold_set_backfill.jsonl` | the canonical `gold_set.jsonl` (drift-guarded) |
| Canonical file | **never touched** | appended (new NPIs only) |
| Purpose | a from-scratch, uniformly-provenanced rebuild you **diff against canonical** before adopting | grow/extend the canonical set in place |

**Why clean-room is the default.** The canonical set is mixed-provenance and
mixed-vintage (DOJ + LEIE upgrades + manual year/$ fixes across several pipeline
generations). A clean-room rebuild produces a *uniform* set — every row built the
same auditable way (NPPES-verified NPI, grounded outcome-capped years, billing
surface) — and the **diff vs canonical is the reliability audit**: which canonical
defendants the rebuild couldn't re-derive, which it re-derived with different
years, and which are net-new. Nothing is overwritten, so adopting the result is a
later, deliberate decision (and one that touches the maintainer's ground-truth territory).

## How to trigger it

GitHub → Actions → **goldset-backfill** → *Run workflow*. Inputs:

| Input | Default | Meaning |
|---|---|---|
| `dry_run` | **true** | Classify only — writes nothing, no PR, no email, no R2. **Always dry-run first.** |
| `clean_room` | **true** | Rebuild into the separate file + diff. OFF = incremental merge into canonical. |
| `chunk_size` | `8` | Leads per curator invocation. |
| `workers` | `16` | Concurrent curator subprocesses per wave. |
| `deadline_minutes` | `320` | Per-wave wall-clock budget (margin under the 6h cap). |
| `offset` / `branch` / `wave` | `0` / blank / `1` | Chain state — the chain advances these; leave at defaults to start fresh. |
| `max_waves` | `15` | Runaway backstop — stop chaining after this many waves. |

Locally: `python -m pipeline.scripts.backfill_gold_set --clean-room --dry-run --limit 8`.

## Exactly what happens, step by step

1. **Build leads from the full warm cache** — `cache_leads()`
   ([doj_lead_adapter.py](../scrapers/doj_lead_adapter.py)) reads every release in
   `data/source/doj_api/raw_press_releases.jsonl` (~6,061, tracked in git), strips
   HTML, and re-applies the body gate → **~4,753 gated leads (2009–2026)**. No DOJ
   API re-crawl. (Field mapping — incl. flattening `component` via `_component_str`
   — matches the incremental path so a backfilled Lead is identical to a discovered one.)
2. **Slice to the wave** (`offset`→`limit`), then **chunk** into `chunk_size` groups.
3. **Curate, in parallel** — up to `workers` curator subprocesses run concurrently
   (`claude -p`, I/O-bound, each in its own `run_dir/chunk_<i>/`). Each chunk's
   proposals persist to `chunk_proposals.jsonl` so `--resume` skips completed chunks
   after a timeout. All chunk proposals aggregate into one frame.
4. **Deterministic gate** ([GOLD_SET_GATE.md](./GOLD_SET_GATE.md)) over the
   aggregate — clean-room passes `gold_rows=[]` so every proposal routes from scratch.
   Routes to ACCEPT / HOLD / SKIP / PARK. No LLM.
5. **Build the output**:
   - *Clean-room*: ACCEPTs deduped by NPI (active-years unioned across releases) →
     one row per confident individual → written to `gold_set_backfill.jsonl`.
   - *Incremental*: ACCEPTs drift-guard-merged into canonical (new NPIs auto-append;
     known-NPI updates/holds → review queue).
6. **Two CSVs** —
   - `backfill_diff_vs_canonical.csv`: the **reliability audit** — one row per
     provider that differs, tagged `category` (net_new / year_disagreement /
     not_re_derived) with canonical vs rebuilt years + source URL. Full snapshot
     (recomputed over the whole rebuild each wave).
   - `backfill_needs_review.csv`: the **human worklist** — every HOLD/PARK case,
     accumulated across waves.
7. **Surface the result** — opens a **DRAFT PR** (clean-room: the backfill file;
   incremental: canonical + changelog) and sends **one email**: the digest body
   (headline counts + reliability-audit summary) with **both CSVs attached**.
   Quiet-on-zero, never on a dry-run.
8. **Persist to R2** — `rclone copy` the rebuilt file to
   `r2:needlepoint-data/ground_truth/v1/` (`--s3-no-check-bucket` for the single-file upload).

## Scale / runtime (no silent caps)

~4,753 leads ÷ 8 per chunk ≈ **~594 curator calls** — serial ≈ 75–100h, or ~6–10h
at `workers=16`. The **wave chain** (above) spreads that across as many 6h jobs as
needed (≈2 waves at `deadline_minutes=320` + `workers=16`), so no single job has to
fit it. Notes:
- Bumping `workers` shortens each wave but raises LLM **rate-limit** pressure and
  token spend — tune down if you see throttling (the chain just uses more waves).
- `max_waves` (default 15) is the runaway backstop; raise it if you lower `workers`
  or `deadline_minutes` enough to need more than 15 waves.
- **Transient per-chunk failures are skipped, not retried, in the chain.** A chunk
  whose curator errors (timeout/rate-limit/bad lead) logs a `FAILED` line, contributes
  nothing, and the offset advances past it — those few leads are dropped from this
  pass (not lost data; the source is still in the cache). To recover a specific band,
  re-run a targeted `offset`/`limit` wave. (Within a single fixed-`run_dir` run,
  `--resume` instead retries uncached chunks — but the chain advances by offset.)
- The gate's billing-surface check loads CMS panels for the union of all years
  once at the end — runs after the curator phase, so it doesn't bottleneck parallelism.

## Limitation

Even incremental mode can **add/extend only** — it cannot shrink an over-broad
active-years window or remove a wrongly-included NPI from canonical. Clean-room
sidesteps this by rebuilding from scratch into a separate file; the *downward*
correction of canonical is a deliberate, human-reviewed adoption step driven by
the diff, not something this job does automatically.
