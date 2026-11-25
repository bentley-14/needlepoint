# What a run produces, and how to read it

Every investigation writes one folder: `fraud-investigator-v2/state/<CASE-ID>/` (git-ignored; it names real
people). Start with the one-screen summary:

```bash
python3 needlepoint.py show            # latest case, or: show <CASE-ID>
```

It prints the verdict, the filing test, strength, damages, the live innocent explanations, any open checkpoint,
evidence counts by tier, and the report files.

## The verdict

The loop applies a filing test of seven predicates. All must pass for a FILE-class verdict; the default is to
decline.

| Predicate | Passes when |
|---|---|
| `confirmed_false_claim` | at least one count rests on a confirmed false-claim anchor (a rule, not a statistic) |
| `innocent_foreclosed` | no innocent explanation is at least as plausible as fraud |
| `materiality` | the falsity is material to payment |
| `scienter` | knowing or reckless conduct is pleaded |
| `confirmed_damages` | a confirmed loss exists |
| `jurisdiction_clear` | no surviving public-disclosure or first-to-file bar |
| `bookkeeping_coherent` | retained counts and the settlement verdict agree |

Final verdicts: `SETTLEMENT_READY` and `FILE_WITH_DISCOVERY_DEPS` (file-class), `NOT_ACTIONABLE` (decline),
`INDEFENSIBLE_AT_CAP` (adversarial rounds ran out before convergence), `NEEDS_HUMAN_CHECKPOINT` (paused for you).

**Strength** (STRONG / MODERATE / WEAK) and the **expected-value factor** are reporting scores. They do not
override the filing test: a STRONG case can still be declined if, for example, its falsity rests only on
statistics.

## Human checkpoints

When a case-killing premise cannot be verified from the record (for example, whether an innocent explanation
can be ruled out, or whether a sealed docket is the same scheme), the run pauses rather than guessing. In a
guided run, `needlepoint.py` asks you the question and resumes the case itself. If you ran with `--yes`, or
stopped the run, answer and resume by hand:

```bash
python3 scripts/answer_checkpoint.py list
python3 scripts/answer_checkpoint.py answer <CASE-ID> --resolution affirm_decline|foreclose_explanation \
    --answer "what you verified" --citation "source"
python3 needlepoint.py run --resume <CASE-ID>     # finished steps load from cache
```

Answer only with what you can verify. A human answer resolves the pause; it does not override the
foreclosure gate's own judgment.

## The report files

| File | What it is |
|---|---|
| `00_README.md` | Synopsis: subject, scheme, counts, adversarial result, why the verdict, key evidence, next steps |
| `01_Case_Report.md` | Full report: counts with Rule 9(b) elements, the adversarial summary, failed predicates, citations, citation audit |
| `02_Layperson_Summary.md` | Plain-language version: what looked suspicious, why the answer is what it is, what would change it |
| `03_Full_State.json` | Machine-readable case state (same content as `summary.json` at completion) |
| `04_Full_Investigation_Bundle.json` | Every evidence item with tier, citation, and rationale, plus all agent outputs |
| `05_Counsel_Memorandum.pdf`, `06_Qui_Tam_Complaint.pdf` | Draft legal documents rendered from the case. They are drafts for review, not filings, and must be read together with the verdict |

## Evidence tiers

Every fact carries a tier: `confirmed_fact` (a primary record, reproducible from its citation),
`statistical_signal` (a percentile, ratio, or volume statistic), or `legal_hypothesis` (an unverified
inference). Each citation is a runnable command or a URL; `numeric_audit.json` and the citation audit
re-check them.

## The working files

`iter-01/`, `iter-02/`, ... hold each build iteration and `adv-r1/`, ... each adversarial round. Every agent
step leaves `<step>.prompt.md` (what it was asked), `<step>.parsed.json` (its validated answer),
`<step>.raw.json` (raw output with token usage and cost), and `<step>.meta.json` (attempts, retries, model).
To re-run one step, delete its `parsed.json` and resume.

## Example (draft profile, about 23 minutes)

A smoke run on a publicly indicted provider found a one-year jump in billing to $18.3M, driven by a single
skin-substitute code with no prior billing, and built two counts. The defense round defeated the kickback count;
the retained count survived at a 25% discount. The verdict was still NOT_ACTIONABLE: falsity rested on
statistics, and the foreclosure gate held that real wound care on real patients was at least as plausible on
public data. That is the system working as designed: it surfaced the anomaly, and it declined to claim more
than public data can prove.
