# The gold_set gate (link 3) — how a proposal becomes a label

The spec for `GoldSetBuilder.transform()` (link 3 of
the gold_set builder). Self-contained; no external prerequisites to read it.

## What the gate is, in one sentence

The curator (link 2) is an **LLM that *proposes*** cases (fuzzy, can be wrong).
The gate is **deterministic, no-LLM code that *decides*** which proposals
become `y=1` labels — and is the auditable record of *why*. Nothing it does
uses an LLM; it re-runs fixed tools (`nppes_lookup`, `check_billing_surface`)
and applies fixed rules.

## The four outcomes (plain English)

Every proposal lands in exactly one bucket:

| Bucket | Plain meaning | What happens to it |
|---|---|---|
| **ACCEPT** | "Confirmed and scoreable — make it a label." | → the merge (link 4) writes it to gold_set |
| **HOLD** | "Probably real, but the NPI is *shaky* — a human must look." | → human-review queue (link 5); never auto-labeled |
| **SKIP** | "Valid fraud, but our model can *never* score it." | → dropped + logged (with the reason) |
| **PARK** | "Can't score it *yet* — the CMS data isn't published." | → parked file; re-checked when data lands |

The difference that trips people up is **HOLD vs SKIP** and **SKIP vs PARK** —
both pairs are explained below.

## The decision flow

The load-bearing insight (learned by running the gate on real data): **a surface
verdict is only meaningful if we checked the *right* NPI.** A `NO_SURFACE`
result on a *shaky* NPI tells you nothing — it might just be the wrong NPI. So
**NPI-confidence is checked *before* we trust the surface verdict.** Only the
two *definitive* structural skips (no NPI at all, or an organization NPI) decide
without it.

```
                         ┌──────────────────────────────────────────────┐
   proposal ───────────▶ │ 1. Definitively unscoreable?                  │
                         │    (no NPI in NPPES, or NPI-2 organization)   │
                         └──────────────────────────────────────────────┘
                              │ yes                       │ no
                              ▼                           ▼
                          ┌──────┐        ┌──────────────────────────────────┐
                          │ SKIP │        │ 2. CONFIDENT? — NPI (1 candidate, │
                          └──────┘        │    name exact/prefix) AND years   │
                                          │    GROUNDED (source/web, not       │
                                          │    inferred/fallback)             │
                                          └──────────────────────────────────┘
                                              │ no, shaky          │ yes
                                              ▼                     ▼
                                          ┌──────┐    ┌────────────────────────────┐
                                          │ HOLD │    │ 3. Surface verdict          │
                                          └──────┘    │    (now trustworthy)        │
                                                       └────────────────────────────┘
                                                         │ FOUND   │ NO_PANELS │ NO_SURFACE
                                                         ▼         ▼           ▼
                                                     ┌────────┐ ┌──────┐   ┌──────┐
                                                     │ ACCEPT │ │ PARK │   │ SKIP │
                                                     └────────┘ └──────┘   └──────┘
```

So the order is: **(1) definitive structural skip → (2) NPI confidence (a shaky
NPI → HOLD *regardless of surface*) → (3) surface verdict (trusted only now)**.
The earlier "scoreability-first" framing was wrong: it would SKIP a shaky-NPI
case on a `NO_SURFACE` that was really just the *wrong* NPI (exactly what
happened to Banyan — caught by the integration test).

### Step 0 — already-labeled dedupe (runs *before* everything above)

The curator re-discovers DOJ releases week to week, so the same defendant keeps
coming back. Before any confidence/surface logic, the gate checks whether the
proposal's NPI is **already a confirmed gold_set label**:

- **already labeled, no NEW active years** → **SKIP** (`already_labeled`). A pure
  duplicate; nothing reaches the human. The NPI is already trusted, so this short
  -circuits even the NPPES-name and grounding holds — a re-found label with a
  fuzzy name (gold_set "Provider Banyan" vs curator "Provider Banyan") or
  ungrounded years is *still* just a duplicate, not review-worthy.
- **already labeled, but brings NEW active years** → **ACCEPT** → the merge
  classifies it as an UPDATE and defers it to the review queue. New years are a
  potential **conduct-window extension** (the curator found earlier/later fraud
  years than the existing row carries) and are worth a human look.

"New years" is the *only* signal that re-surfaces a known label — a different
`source_url` or changed `fraud_types`/`covered_years` with the same active years
is treated as a duplicate (SKIP). This is what stops the review email filling up
with already-handled cases (Kapok + Banyan were both same-year re-finds of the
one Utah release already in gold_set).

---

## Concept 1 — Scoreability (the SKIP vs PARK split)

Our model's features come from CMS **Part B / Part D / DMEPOS** billing panels,
keyed by **individual provider (NPI-1) × year**. A proposal is *scoreable* only
if its NPI has a billing **surface** (appears in ≥1 of those panels) in its
fraud-active years. (Note: Open Payments is **not** a billing surface — it's
industry payments *to* doctors, a kickback signal, not Medicare billing *by*
the provider; it's used as `op_` feature columns but never to decide
scoreability.) For a *confident* NPI, the billing-surface check returns one of
three verdicts that map onto the buckets:

| `check_billing_surface` verdict | Meaning | Bucket |
|---|---|---|
| `SURFACE_FOUND` (in ≥1 capped active year) | the NPI billed Medicare in a fraud year → we can score it | **ACCEPT** |
| `NO_SURFACE` (panels for those years **exist**, NPI absent) | the data is published and the NPI simply isn't in it → **never scoreable** | **SKIP** (structural) |
| `UNVALIDATABLE_NO_PANELS` (no panel published for those years yet) | we just can't check *yet* | **PARK** (temporal) |

(These outcomes apply **only after** the NPI clears the confidence gate — see
the flow above; an uncertain NPI is HELD before the surface verdict is trusted.)

**SKIP vs PARK is "never" vs "not yet."**
- **SKIP (structural):** the absence is permanent. Wrong entity type (an *org*
  NPI, not an individual), no individual NPI at all, or the provider bills a
  program we don't ingest (e.g. Part A home-health). Re-running next year
  changes nothing.
- **PARK (temporal):** the absence is just timing. The fraud is recent
  (e.g. 2025), CMS hasn't published those panels yet (~12–18-month lag). This
  is the **freshest, most-wanted** fraud — so we **don't throw it away**, we
  park it and re-check when the data lands. A flat "no surface → skip" would
  silently discard the newest cases; that's the whole reason PARK exists.

> Important (the Provider Kapok lesson, PR #134): the surface check sets
> `covered_years` (*which years we can score*), it does **not** set
> `all_active_years` (*which years fraud occurred*). Active-years come from the
> curator's reading of the documented conduct window — never from billing
> presence. A provider can keep billing after the fraud ended; those later
> years are not labels.

---

## Concept 2 — confidence: NPI *and* years (the ACCEPT vs HOLD split)

Driving principle: **a wrong label is worse than a missing case.** A wrong NPI
puts `y=1` on an *innocent* provider; wrong *years* put `y=1` in the wrong
score-years. Both mislead the model, so the bar for auto-accepting is high —
anything shaky on **either** axis goes to a human.

A proposal is **ACCEPT** only if **both** hold:

- **NPI confident** — exactly **one** candidate, it is **NPI-1 (individual)**,
  and the NPPES name matches **exactly or by prefix** (a *fuzzy* match → HOLD).
- **Years grounded** — `active_years_source` is **`source_stated`** or
  **`web_corroborated`** (the conduct window came from the release or a
  corroborating source). **`inferred`** / **`outcome_fallback`** (and a missing
  `unknown`) → HOLD — we never auto-mint a label with *guessed* years (the
  Provider Kapok failure: a dateless release → billing-derived `[2017–2022]` vs the
  true `[2013–2019]`).

Either axis weak → **HOLD** (a human confirms/supplies before it can become a label).

**"fuzzy → HOLD" specifically:** our NPPES lookup now does fuzzy first-name
matching (so DOJ "Emily" finds NPPES "Emilee"). But a fuzzy match is a *maybe*,
not a *yes* — the name didn't actually match. So any proposal whose NPI was
found via a fuzzy (non-exact) name match is **never auto-ACCEPTed**; it goes to
HOLD for a human to confirm it's the right person. (Exact and prefix matches —
e.g. "Rob"→"Robert" — are trusted.)

---

## Worked examples — our 4 real curator proposals

From the 15-lead batch (`state/runs/goldset_batch/aggregate.json`):

Run live through the gate (live NPPES + local panels):

| Proposal | 1. definitive skip? | 2. NPI confident? | 3. surface | Bucket | Why |
|---|---|---|---|---|---|
| **Kapok** (podiatrist, UT, NPI 1900000015) | no (NPI-1) | ✅ single, name **exact** | `SURFACE_FOUND` (Part B) | **ACCEPT** | clean — becomes a label |
| **Banyan** (NP, UT, NPI 1900000025) | no (NPI-1) | ❌ **fuzzy** name (Emily≈Emilee) | (not trusted) | **HOLD** | uncertain NPI ⇒ its `NO_SURFACE` is unreliable (likely the wrong/late NPI); a human finds the right one |
| **Boxelder** (RN, UT, NPI 1900000063) | no (NPI-1) | ✅ single, name **exact** | `NO_SURFACE`, panels exist | **SKIP** | confident it's him; he bills *under Kapok's* NPI → genuinely no surface; structural |
| **Ruby Scott** (HHA owner, MI, NPI 1900000012) | ✅ **NPI-2 org** | — | — | **SKIP** | org NPI, home-health = Part A → not an individual provider; structural |

The Banyan vs Boxelder contrast is the whole point of step 2: **both are
`NO_SURFACE`, but Banyan's NPI is *uncertain* (fuzzy name) so we don't trust that
verdict → HOLD, while Boxelder's NPI is *confident* (exact name, single
candidate) so the `NO_SURFACE` is believable → SKIP.**

*(No PARK example in this batch — a PARK case would be e.g. a 2025 indictment
whose 2025 Part B panel CMS hasn't published yet.)*

---

## What the gate actually runs (deterministic, per proposal)

1. **NPI-verify** — `nppes_lookup --npi <candidate>`: confirm it exists, is
   **NPI-1**, name-match strength (exact/prefix/fuzzy), specialty/state.
   *(Verify the candidate the curator gave — do NOT re-resolve from the name;
   re-resolving "Emily" would reject the correct "Emilee".)*
2. **Outcome-cap** — cap `all_active_years` to ≤ `outcome.year` (port of
   `cap_active_years_by_outcome.py`); preserve the original under
   `_original_all_active_years`.
3. **Billing-surface** — `check_billing_surface` over the capped active years →
   `covered_years` + the verdict that drives Q1.
4. **Route** — apply Q1 then Q2 → tag the proposal with `gate_decision`
   (accept/hold/skip/park) + `gate_reason`.

**Where the decision comes from.** The curator (link 2) outputs **only
proposals** (`ProposedCase`: NPI candidates, active years, outcome, …) — it has
**no** accept/hold/skip/park concept. The four buckets are **derived entirely
here**, deterministically, by the steps above. The gate **never trusts the
curator's claimed verdicts** (it's an LLM) — it re-runs `nppes_lookup` +
`check_billing_surface` itself, so the gate's output is the reproducible record.
Order note: **step 2 (outcome-cap of active years) runs first**, before the NPI
lookup — and active-year *values* come from the curator's reading of the
documented conduct window; the gate only caps them (it does not re-derive them
from billing; see **Known limitations**).

## Output

`transform()` returns the proposals frame with added columns: `npi_verified`,
`nppes_entity_type`, `name_match` (exact/prefix/fuzzy/none),
`outcome_capped_years`, `covered_years`, `surface_verdict`, `gate_decision`,
`gate_reason`. **ACCEPT** rows feed the merge (link 4); **HOLD**/**PARK** rows
feed the review/parked queues (link 5); **SKIP** rows are logged and dropped.

## Active-years grounding (how wrong years are caught)

The gate can only *outcome-cap* `all_active_years`; it can't independently know
the true conduct window (that's in the source text). The Provider Kapok case showed
the failure mode: a dateless FCA release → the curator **billing-derived**
`[2017–2022]` (wrong) and the outcome-cap was a no-op (2024 settlement ≫ 2018
conduct end). Two fixes close this:

1. **Curator prompt** — when the release omits the fraud period, the curator
   **WebSearches for the conduct window** (local news / court filing / qui tam
   complaint) and **never billing-derives** the years. *(Empirically: with this,
   the curator found Kapok's `[2013–2019]` via the same local-paper article a
   human used — vs `[2017–2022]` without it.)*
2. **Grounding signal → HOLD** — the curator stamps `active_years_source`
   (`source_stated` / `web_corroborated` / `inferred` / `outcome_fallback`), and
   the gate routes `inferred` / `outcome_fallback` (and a missing `unknown`) to
   **HOLD** — a human supplies/confirms the window rather than auto-accepting
   guessed years.

**Residual limitation:** if no source anywhere states the window, the years
still can't be grounded — but that case now lands in **HOLD** (human-supplied),
not a silently-wrong ACCEPT.

## Merge (link 4) — identity resolution + dry-run

`_merge_into_gold_set` takes the gate's **ACCEPT** rows and decides how each
lands in `gold_set.jsonl`. Key fact: a **`source_url` is per-*release*, not
per-defendant** — one release routinely names many billing NPIs (148 gold_set
URLs are shared across multiple NPIs; the largest has 10 physicians). So
`source_url` alone never implies "same row"; it must be paired with the NPI.
Identity rule (`_classify_merge`):

```
known NPI already in gold_set:
    + overlapping years OR same source_url → UPDATE   (same defendant / follow-up)
    + neither                              → HOLD     (distinct case? never auto-
                                                        append a 2nd row for a known
                                                        NPI — would've stopped the
                                                        Sassafras triplication)
new NPI:
    + source_url matches an UNRESOLVED (npi=null) row → UPDATE  (resolves Kapok:
                                                        same release, NPI was null)
    + otherwise                                       → APPEND  (new defendant —
                                                        incl. another NPI from a
                                                        release already partly curated)
```

UPDATE changes **derived** fields only (`npi`, `all_active_years`,
`covered_years`, `fraud_types`, `outcome`, …) and **never** the `_`-prefixed
manual overrides; an update whose target row carries overrides (e.g. the Kapok
#134 fix) is **flagged** in the dry-run. The curator's `active_years_source`
maps to the gold_set provenance field `_active_years_source`.

**Dry-run first (mandatory).** `_merge_into_gold_set(dry_run=True)` reports
append/update/hold counts + a per-row diff and writes **nothing**; `dry_run=False`
raises until the drift-guarded writer (backup + this changelog + atomic rename)
lands in a follow-up PR, reviewed against the dry-run.
