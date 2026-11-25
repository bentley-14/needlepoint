# Point-in-time investigator eval (sandboxed browser)

## Why
Evaluating the investigator loop on a **true-positive** case (a known DOJ defendant) is only
honest if the investigator can't read the answer. The DOJ press release announcing the
charges, the news coverage, the court docket, the exclusion listing — all of that only
exists *after* the fraud was discovered. If the investigator fetches it, the eval measures
answer-reading, not fraud detection.

`scripts/sandbox_browser.py` is a **point-in-time web layer**: given an `--as-of` cutoff, it
only ever returns content that existed **before** that date and BLOCKS anything newer. Set the
cutoff to the case's discovery/charge date and the investigator must build the case from
pre-discovery signals (billing patterns + the contemporaneous web).

**This is a toggle, OFF by default.** Normal investigations keep full live web access
(`WebSearch` / `WebFetch` / `doj_pr_lookup` / `browser_fetch`) — the sandbox does not replace
live search. It is switched on **only** for calibration runs on true-positive NPIs, signalled
per-run via `CALIBRATION MODE as_of=<D>` in the investigator task (or `INVESTIGATOR_AS_OF=<D>`
in the env).

The bar for a calibration run is that the investigator is **blind to anything even suggestive
of the fraud** — not just the literal announcement. That requires the right cutoff (below), a
gate on live NPPES status, and awareness of model-recognition leakage (see Caveats).

## The tool

| Command | What it does |
|---|---|
| `sandbox_browser.py cutoff-for-npi <NPI>` | Suggest a cutoff: prints fraud-active years + disposition date and recommends the **end-of-fraud** cutoff (the disposition date is usually too late — see protocol step 1). |
| `sandbox_browser.py check <url> --as-of <D>` | Does an archived snapshot of the page exist before `<D>`? |
| `sandbox_browser.py fetch <url> --as-of <D> [--text] [--grep RE] [--head N]` | Return the **Wayback** snapshot ≤ `<D>` (the contemporaneous page); BLOCK if none. `--allow-live-if-dated` opts into a weaker live fetch gated on the page's own publish date. |
| `sandbox_browser.py search "<q>" --as-of <D> [--field title\|body]` | DOJ press-release search strictly before `<D>` (delegates to `doj_pr_lookup --date-before`); the announcing release is cut. |

**Cutoff semantics:** `--as-of D` returns content strictly **before** the start of day `D`.
Entries dated on/after `D` are cut (so a release published *on* the discovery date is excluded).

**Exit codes:** `0` content returned · `2` BLOCKED (nothing before the cutoff) · `1` error.

## Eval protocol

1. **Pick the cutoff — the discovery/charge date, NOT the disposition.** `cutoff-for-npi <NPI>`
   prints the fraud-active years, the disposition date, and a recommended cutoff. `outcome.date` is
   usually the plea/sentencing date and is often months-to-years too late — using it leaves the
   *charge announcement* visible. Use the actual charge / indictment / arrest date if known;
   otherwise the recommended **end-of-fraud** cutoff (`{last_active_year + 1}-01-01`, which precedes
   charges).
2. **Sandbox the web.** Run the investigator (see the "Calibration mode" block in
   `prompts/investigator.md`) with native web tools **disabled** — `WebSearch`, `WebFetch`,
   `doj_pr_lookup.py`, `browser_fetch.py`, live CourtListener, and the
   `raw_press_releases.jsonl` / `case_sources` caches all see the present and would leak. The
   **only** open-web access is `sandbox_browser.py`. **Enforce at the harness level** (validated):
   spawn `claude -p` in a **sanitized workspace** containing only `scripts/sandbox_browser.py` + the
   target's pre-cutoff feature files — NOT `data/extracted/gold_set.jsonl` / `proposals/` / `audits/`
   (those are the answer key; an agent *will* `grep` them — observed). Flags:
   `--permission-mode default --allowed-tools "Bash(python3 scripts/sandbox_browser.py:*),Read" --disallowed-tools "WebSearch,WebFetch"` — no general `Bash` (can't `curl` around the sandbox), no
   `bypassPermissions`. If you must run inside the repo, additionally deny `Grep`/`Glob` and block
   `Read(data/extracted/**)`. Stating the cutoff in the prompt is NOT enough on its own (see
   Agent-level validation).
3. **Gate NPPES.** `nppes_lookup.py` is live and has no point-in-time mode. Instruct the run to
   **ignore any `deactivation_date` / status change dated ≥ cutoff** — a post-cutoff exclusion or
   deactivation is the answer leaking in. Use only identity/taxonomy/address as they stood pre-cutoff.
4. **Sandbox the features.** Feed only CMS panels / feature-frame years with `data_year` < the
   cutoff year. (The walk-forward frames are already per-year; just don't pass post-cutoff years.)
5. **Run + score.** The investigator builds the case from pre-discovery evidence only. A
   `BLOCKED` result means "didn't exist yet" — it must not be worked around.

## Verified behavior (2026-05-30, DeNicola, plea 2022-06-29)
- `check <PR> --as-of 2022-06-01` → **BLOCKED** (the announcement didn't exist yet).
- `check <PR> --as-of 2023-06-01` → **AVAILABLE** (archived 2022-08-09).
- `fetch <PR> --as-of 2023-06-01 --text` → returns the full release; `--as-of 2022-06-01` → BLOCKED.
- `search "health care fraud" --as-of 2024-01-01` → newest result 2023-12-21 (2024+ cut).

## Agent-level validation (2026-05-30, 3 obscure TPs via `claude -p`)
Cases: Iken (podiatrist MO, criminal plea 2015), Landry (osteopath ME, civil FCA settlement 2017),
Pruchniewski (podiatrist FL, plea 2014). Each run in two modes.

- **Enforced** (web tools denied; `Bash` limited to `sandbox_browser`; date-gated DOJ search): **all
  three blind** — DOJ search returned 0 pre-cutoff hits → `NO_PRE_CUTOFF_EVIDENCE`. The web/DOJ vector
  is reliably closed.
- **Naive** (live tools available, only the `as_of` in the prompt) exposed three *different non-web*
  leak paths, one per run:
  - **Iken** WebSearched and pulled the entire post-cutoff case (DOJ plea, 2015 news, HHS-OIG,
    restitution, scheme) into context, then self-censored the verdict — *contaminated*.
  - **Landry** ignored the web and **`grep`ped the local `gold_set.jsonl` answer key**, reading the
    case_id, settlement amount, and conduct dates straight off the label file (it excluded them by
    discipline, but the answer was already in context).
  - **Pruchniewski** read this doc, complied, then **admitted it recognized the name from training**
    (self-censored).

**Conclusions:**
1. The web sandbox + tool-denial reliably blinds the **web/DOJ** vector (3/3).
2. It is **not sufficient alone.** The repo itself is an answer key — `gold_set.jsonl`
   (+ `proposals/`, `audits/`) carries the scheme/outcome, and any `Read`/`Grep`/`Glob` access leaks
   it (Landry did exactly this). Calibration MUST also deny repo answer-file access (see step 2/3).
3. **Prompt discipline is unreliable** — sometimes clean (Pruchniewski), sometimes the answer enters
   context first (Iken, Landry). Enforce; don't rely on it.
4. **Model recognition is real** even for fairly obscure names (Pruchniewski). Withhold the
   defendant's name; prefer truly low-buzz TPs. This is the residual the sandbox can't fix.
5. For obscure providers the **web yields almost nothing pre-cutoff** — a blind investigator's real
   signal must be the **pre-cutoff billing features**, which the harness must supply. The web sandbox
   is supplementary, not the evidence source.

## Full-loop calibration mode (added 2026-06-03)

The single-agent protocol above is the foundation; the full-loop mode extends it to the entire
needlepoint loop (orchestrator + every agent: investigator_discover, investigator, counsel_build,
counsel_first_chair_review, counsel_rebuttal, counsel_disposition, defense, legal_adv_response,
rebuttal_investigator, case_writer) so we can score the full pipeline against known DOJ
true-positives, not just the Investigator step.

There is exactly ONE calibration setup: the **fail-closed clean export**. The agent runs the SAME
billing tools as a production run, but inside an out-of-repo export that physically contains only
pre-cutoff data + answer-key-free tools — so the answer can't be reached because it isn't there.
(The earlier empty-sandbox / Wayback-only mode has been removed; `--calibration-as-of` now
*requires* `--calibration-export`, and the wrapper builds it for you.)

### One command — generalizes to any NPI

```
bash scripts/calibration/run_calibration_smoke.sh <NPI> <CUTOFF-YYYY-MM-DD> "<sanitized-subject>"
```

This: (1) derives THIS NPI's leak tokens from gold_set (`scripts/calibration/calibration_leak_tokens.py`, at
setup, outside the sandbox); (2) builds the clean export
(`scripts/calibration/build_calibration_workspace.py --npi <NPI> --as-of <CUTOFF> --with-panels`); (3) runs
`python -m fraud_investigator "<subject>" --calibration-as-of <CUTOFF> --calibration-export <dir>`;
(4) arms the per-NPI leak watchdog.

### The clean export (the agent's cwd)

Built out of repo at `~/.calibration_workspaces/npi_<NPI>_asof_<CUTOFF>/`:
- `data/panels/` — CMS parquet for **years < cutoff only** (APFS zero-copy clone). The build
  hard-fails if any post-cutoff year, `.git`, or answer-key file is present.
- `scripts/` — answer-key-free tools: `dump_hcpcs_breakdown.py`, `provider_trajectory.py`,
  `address_sweep.py`, `hcpcs_lookup.py`, `sandbox_browser.py` (+`browser_fetch.py`), `sandbox_nppes.py`.
- `data/specialty_benchmarks.json` + `data/hcpcs_benchmarks.json` — pre-computed specialty-peer
  percentiles (peer baselines without ad-hoc duckdb, which is denied).
- **NOT present:** gold_set, DOJ press-release cache, `docs/calibration/`, proposals, audits, `.git`,
  any post-cutoff panel year.

`Orchestrator.run()` sets a module-level `CalibrationContext` (`fraud_investigator.calibration`)
that `claude_p.invoke_agent` reads on every spawn to: set cwd = the export, inject the hardened
launch flags + prompt banner, bump the investigator turn budget to 40 (script-based tools are
turn-hungrier than WebSearch), and strip `--dangerously-skip-permissions` (forcing
`--permission-mode default`). Cleared in `try/finally` so it can't leak into a later production
run in the same process (matters for the Streamlit GUI).

### Launch flags (export mode — same for every agent)

| | Value |
|---|---|
| cwd | the clean export (out of repo) |
| `--allowed-tools` | `Bash(python3 scripts/{dump_hcpcs_breakdown,provider_trajectory,address_sweep,hcpcs_lookup,sandbox_browser,sandbox_nppes}.py:*)`, `Read(<export>/**)` |
| `--disallowed-tools` | `WebSearch, WebFetch, Grep, Glob` |
| permission mode | `default` (no `--dangerously-skip-permissions`, so the allowlist actually binds) |
| investigator turn budget | 40 (calibration only; script-based tools are turn-hungrier than WebSearch) |

All NPPES queries route through **`scripts/sandbox_nppes.py --as-of <cutoff>`** which **redacts any
field with a timestamp ≥ cutoff** (`last_updated`, `status` when a sibling timestamp is post-cutoff);
static identity (name, NPI, taxonomy, addresses, license, enumeration_date) is preserved. Web is
pre-cutoff Wayback via `sandbox_browser.py` only. `Agent`, `Write`/`Edit`, bare `duckdb` / `python3 -c`,
`nppes_lookup`, and live courtlistener/doj_pr/pecos are NOT available — the prompt banner says so
explicitly so the agent doesn't burn turns attempting them.

### Synthetic case_id

In calibration mode the orchestrator mints `case_id = CALIBRATION-<8-hex-uuid>` regardless of
subject; the real DOJ case_id (e.g., `DOJ-2025-provider-aspen`) would itself be a leak in the
state-dir path / cache keys / session IDs. State writes go to
`fraud-investigator-v2/state/calibration/<case_id>/` (sibling to production state).

### What the loop is allowed to see — defendant name IS fine

The defendant's NAME is public information (NPPES, state board, practice website) and does NOT
itself reveal a fraud allegation. Pass the name in the SUBJECT string — the agent then uses it
to query pre-cutoff Wayback / pre-cutoff state board normally. The runner script enforces
`SUBJECT` sanitization: rejects subjects containing `settled|indict|plea|qui tam|whistleblow|$NNN`
(post-cutoff outcome tokens). What the loop CAN'T see is anything dated on/after the cutoff or
anything in `data/extracted/gold_set.jsonl`, `docs/calibration/`, `proposals/`, `audits/`,
`fraud-investigator-v2/state/<other-cases>/`, or the git history — all blocked by the Read
allowlist anchored to the run dir.

### Leak watchdog (generalized per-NPI)

`scripts/calibration/calibration_loop_watchdog.sh <run-dir>` polls the run state dir every 3s and greps agent
output against a leak-pattern set **derived per-NPI from gold_set at setup** by
`scripts/calibration/calibration_leak_tokens.py` (emitted to `CALIB_LEAK_TOKENS_FILE`): this case's settlement
amount/date, DOJ-URL slug, docket, defendant/practice name, plus the NPI-independent gold_set field
names (`FAST_SANITY_PASS`, `SOURCE_CONFIRMED`, `defendant_name`, `scheme_summary`,
`_extraction_confidence`, `npi_confidence`, `amount_usd`, …). In export mode (`CALIB_NAME_WITHHELD=0`)
the defendant NAME is ignored — it's in the subject (public) — so only ANSWER tokens fire; a hit
therefore means **model-recall or a sandbox escape**, not normal analysis. Defense-in-depth: the
fail-closed export already makes the answer unreachable; the watchdog is the catch-net.

### Adversarial guarantees (export mode, re-verified 2026-06-03)

Live `claude -p` probes from the real export cwd, with the exact export-mode flags:
- Read tool on absolute `data/extracted/gold_set.jsonl` → **DENIED**
- `Bash` `cat` / `head` of absolute `gold_set.jsonl` → **DENIED** ("only … the allowed working directory")
- Read / `cat` of an arbitrary `$HOME` file → **DENIED**
- `Bash` `head` of a **post-cutoff** panel (`cms_partb_svc_2020.parquet` in the repo) → **DENIED**
- `Bash` `git log` → **DENIED** (no `.git` in the export)
- `Bash` `python3 -c "open('/abs/gold_set').read()"` → **DENIED**
- `python3 scripts/dump_hcpcs_breakdown.py --npi … --years 2014` (in-export tool) → **WORKS** (reproduced the 99215 finding)
- Full run watchdog + post-hoc scan → **CLEAN** (0 answer tokens)

The earlier (pre-Phase-1.6) finding stands as the reason the sandbox must NOT rely on prompt
discipline: with `--dangerously-skip-permissions` or a cwd inside the repo, all of the above
**leaked**. The clean export removes both: out-of-repo cwd + no answer keys in the tree + no
skip-permissions.

### Caveats / limitations
- **NPPES** — now wrapped (`scripts/sandbox_nppes.py`) so identity/taxonomy reach the agent but
  any field with a post-cutoff timestamp is replaced with `REDACTED_POST_CUTOFF`. The
  bare `nppes_lookup.py` is NOT in any agent's allowlist in calibration mode.
- **Model recognition (NOT fixable by any browser):** the investigator is an LLM that may recognize
  a notorious case from training data regardless of what the browser returns. Mitigate by calibrating
  on **less-famous** true positives, and/or **withholding the defendant's name** in the task so the
  agent works from NPI + billing only (the prompt instructs it not to re-identify a withheld name).
- **Cutoff = disposition is the most common mistake.** `outcome.date` (plea/sentencing) post-dates
  the public charge, so it leaves the indictment/arrest coverage visible. Always cut at the
  discovery/charge date or the end-of-fraud fallback.
- **Wayback coverage gaps** cause false BLOCKs (fail-safe: blocks rather than leaks). Lower recall,
  but never post-cutoff leakage.
- **`--allow-live-if-dated`** is weaker than Wayback — a live page can carry post-cutoff edits under
  an old publish date. Off by default; use only when you accept that risk.
- This gates the **open web + DOJ**. State-board / LEIE / court sources should likewise be limited
  to action dates < cutoff (most are date-stamped in their respective feeds).
