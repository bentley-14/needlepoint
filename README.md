# NeedlePoint

Run your own Medicare fraud investigations from public CMS data with a team of AI agents.

Give it a provider's NPI. An investigator agent pulls the billing record and searches the web, counsel builds a
case theory, a defense agent attacks it, a gate steelmans every innocent explanation, and a verdict engine
applies a legal filing test. You get a written case file explaining what was found, what survived the defense,
and why the verdict came out the way it did. It is built to decline when public data cannot prove a case.

## Run a case

You need Python 3.11+ and either a model API key (Anthropic, OpenAI, Gemini, OpenRouter, Mistral, or a local
model server) or the [Claude Code](https://claude.com/claude-code) CLI.

```bash
git clone https://github.com/bentley-14/needlepoint.git && cd needlepoint
make setup                          # creates .venv and installs everything
cp .env.example .env                # then put your key in .env, e.g. ANTHROPIC_API_KEY=...
source .venv/bin/activate
python3 needlepoint.py              # pick settings, type an NPI, run
```

**Before you run it:** the agents run shell commands (the project's own scripts, plus any command a model
decides to write) and fetch web pages on your machine without asking each time; the `claude` backend starts
Claude Code with permission prompts turned off. Run it in a folder, account, or VM you are comfortable giving
that access to, and do not keep unrelated secrets in the repo folder.

There is nothing to download up front. The agents query the public NeedlePoint dataset in place (about 50 GB of
CMS, Medicaid, NPPES, exclusion, coverage-rule, and DOJ data as parquet), and only the rows a query needs travel.
The one exception is the Medicaid file (about 3 GB), which downloads once the first time a case needs it. Hugging
Face slows anonymous reads when busy; a free read token in `.env` (`HF_TOKEN=...`) raises the limit. To work
offline, download a year instead: `python3 needlepoint.py fetch --year 2024`.

The guided run shows every setting with its default, lets you change any of them, looks up the NPI in the
national registry, runs the case with live progress, asks you if the case pauses for a human decision, and
ends with a one-screen summary and the path to the report.

Prefer flags? Everything in the menu is a flag:

```bash
python3 needlepoint.py run --npi 1234567890 --depth quick --model anthropic/claude-sonnet-5 --yes
```

## The knobs

| Setting | Flag | Default | What it does |
|---|---|---|---|
| Backend | `--backend api\|claude` | `api` if a key is set, else `claude` | `api` runs any provider with your key; `claude` uses the Claude Code CLI |
| Models | `--model` | Sonnet for data gathering, Opus for judgment (or your provider's equivalents) | one model for every step |
| | `--tool-model`, `--reason-model` | | split: data-gathering steps vs judgment steps (counsel, defense, foreclosure, verdict) |
| Effort | `--effort low\|medium\|high\|max` | built-in per step | how hard each step thinks |
| Depth | `--depth quick\|standard\|full` | `quick` | quick: 2 build iterations, 1 defense round (about 2 to 3 h, mostly web searches and remote data reads). full: 4 defense rounds (4 to 8 h) |
| Data year | `--year` | 2024 | which CMS year the case centers on (2013 to 2024) |
| Local models | `--api-base URL` | none | any OpenAI-compatible server (Ollama, vLLM, LM Studio) |
| No prompts | `--yes` | off | for scripts; a paused case is left for you to answer |

`python3 needlepoint.py knobs` prints the current values. Deeper knobs (per-step models, prompts, verdict
thresholds, coverage rules) are listed in `CONTRIBUTING.md`. Cost depends on the models and depth: a quick run
on Sonnet-class models costs a few dollars; a full run on Opus-class models costs more. Small models
(Haiku-class, small local models) finish runs but write noticeably weaker reports: in one test run on Haiku,
20 of 57 cited sources failed the automatic citation check.

## What you get

Each case is a folder in `fraud-investigator-v2/state/<CASE-ID>/`:

| File | |
|---|---|
| `00_README.md` | synopsis: the scheme, the counts, the defense result, and why the verdict |
| `01_Case_Report.md` | full report with legal elements, citations, and a citation audit |
| `02_Layperson_Summary.md` | plain-language version |
| `04_Full_Investigation_Bundle.json` | every piece of evidence with its source command or URL |

`python3 needlepoint.py show` summarizes the latest case. `docs/OUTPUT.md` explains verdicts, evidence tiers,
and human checkpoints. Case folders are git-ignored: they name real people.

## How it works

| Layer | What it does | Where |
|---|---|---|
| Data | public files to per-provider parquet, read in place from the public dataset or a bucket | `pipeline/`, `fraudlib/r2.py`, `scripts/publish_hf_dataset.py` |
| Ranker | walk-forward ML that surfaces unusual providers | `fraudlib/ranker/` |
| Investigator and counsel | evidence gathering, case theory, damages | `fraud-investigator-v2/src/fraud_investigator/` |
| Adversarial defense | motion-to-dismiss rounds; each count survives, is discounted, or is abandoned | `agents/defense.py` |
| Verdict and writer | seven-predicate filing test, human checkpoints, the case file | `verdict.py`, `agents/case_writer.py` |

## Build on it

| Document | For |
|---|---|
| `docs/EVAL.md` | the DOJ label set (1,017 NPIs) and calibration battery, and how to score your changes |
| `docs/OUTPUT.md` | reading a case folder |
| `CONTRIBUTING.md` | where to change models, prompts, tools, gates, and data sources |

## Responsible use

Output is a hypothesis for review, not a finding that anyone committed fraud. Do not publish allegations about
real people based on it. Report suspected fraud to HHS-OIG (oig.hhs.gov) or through counsel. Development cases
in this repo are pseudonymized (tree names such as "Provider Cedar", checksum-invalid NPIs).

## License

Apache-2.0. Copyright 2026 The NeedlePoint Authors.
