#!/usr/bin/env python3
"""Mirror the private data bucket to a public Hugging Face dataset that anyone can read.

Copies the bucket layout as is (processed/v1, raw/medicaid, ground_truth/v1,
cache/v1/feature_frames, results/current), so NeedlePoint reads it with one setting:
NP_DATA_URL=hf://datasets/<repo>. Files move in batches (download, upload, delete), so
local disk use stays near --batch-gb. Re-running skips files already uploaded at the same
size, so an interrupted run resumes.

Privacy, applied to every file:
  * excluded: the unscrubbed gold set (replaced with the repo's scrubbed copy), raw case
    IDs, the curation queue, and AMA-copyrighted CPT descriptor text
  * text files (json, txt, md, csv, jsonl): home-directory paths are rewritten to ~, and a
    file that still contains a personal marker is refused

    python3 scripts/publish_hf_dataset.py --repo <owner>/needlepoint-data --dry-run
    hf auth login        # once, with a write token
    python3 scripts/publish_hf_dataset.py --repo <owner>/needlepoint-data
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from fraudlib import r2  # noqa: E402

PREFIXES = ["processed/v1", "raw/medicaid", "ground_truth/v1", "cache/v1/feature_frames", "results/current"]
EXCLUDE = {
    "ground_truth/v1/gold_set.jsonl",            # unscrubbed; replaced below
    "ground_truth/v1/eval_split.json",           # raw case IDs; split is stored in the gold set
    "ground_truth/v1/cpt_descriptors_by_year.parquet",  # AMA-copyrighted CPT text
    "ground_truth/v1/doj_api/curated_ledger.jsonl",
    "ground_truth/v1/doj_api/curator_input.jsonl",
    "ground_truth/v1/doj_api/curator_input.manifest.json",
    "raw/medicaid/provider_spending.duckdb",     # local index of the parquet; remote reads use the parquet
}
REPLACE = {"ground_truth/v1/gold_set.jsonl": REPO / "data/extracted/gold_set.jsonl"}
TEXT_SUFFIXES = {".json", ".jsonl", ".txt", ".md", ".csv"}
HOME_PATH = re.compile(r"/(?:Users|home)/[^/\s\"'\\]+")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Personal names to scrub (comma-separated, e.g. first and last names): NP_PUBLISH_BLOCK.
# Any word containing one is rewritten to "maintainer"; a file where one survives is refused.
BLOCK = [m.strip().lower() for m in os.environ.get("NP_PUBLISH_BLOCK", "").split(",") if m.strip()]


def bucket_files(prefix: str) -> list[tuple[str, int]]:
    out = subprocess.run(["rclone", "lsjson", "-R", "--files-only", f"{r2.R2_REMOTE}/{prefix}"],
                         capture_output=True, text=True, check=True).stdout
    return [(f"{prefix}/{e['Path']}", int(e["Size"])) for e in json.loads(out)]


def clean_text(path: Path, rel: str) -> None:
    """Rewrite home paths to ~, emails to <email>, blocked names to "maintainer"; refuse the
    file if anything personal survives."""
    text = path.read_text(errors="replace")
    text = HOME_PATH.sub("~", text)
    if not rel.startswith("ground_truth/v1/doj_api/"):   # DOJ releases quote public contact emails
        text = EMAIL.sub("<email>", text)
    for m in BLOCK:
        text = re.sub(rf"\w*{re.escape(m)}\w*", "maintainer", text, flags=re.IGNORECASE)
    low = text.lower()
    hits = [m for m in BLOCK if m in low] + (["/users/"] if "/users/" in low else [])
    if hits:
        raise SystemExit(f"refusing {rel}: still contains {hits}")
    path.write_text(text)


def card(repo: str) -> str:
    return f"""---
license: cc-by-4.0
pretty_name: NeedlePoint public data
tags: [medicare, healthcare, fraud-detection, cms, open-data]
---
# NeedlePoint public data

The data behind [NeedlePoint](https://github.com/bentley-14/needlepoint), an open multi-agent
system for investigating Medicare billing from public data. NeedlePoint reads this dataset in
place (DuckDB over `hf://`), so a clone needs no download step:

```bash
NP_DATA_URL=hf://datasets/{repo} python3 needlepoint.py
```

## What is here

| Path | Contents |
|---|---|
| `processed/v1/` | CMS Part B, Part D, DMEPOS, and Open Payments by provider and year; NPPES; LEIE; Medicare revocations; LCD/NCD coverage rules; CLIA; facility files; entity graphs; derived features |
| `raw/medicaid/` | HHS T-MSIS Medicaid provider spending by HCPCS |
| `cache/v1/feature_frames/` | per-provider feature frames used by the ranker |
| `results/current/` | walk-forward ranker scores by year |
| `ground_truth/v1/` | DOJ-charged provider labels (NPI and DOJ URL only), DOJ press releases, legal doctrine and policy texts, HCPCS reference tables |

Source tables are US government data (CMS, HHS-OIG, DOJ, NPPES) restructured into parquet.

## Read this before using the scores

Feature and score columns are statistical measures of how unusual a provider's billing is
compared with peers. They are not findings that anyone committed fraud. Most outliers have
innocent explanations (patient mix, specialty, billing format). Do not publish allegations
about a provider based on this data. Report suspected fraud to HHS-OIG (oig.hhs.gov).
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", required=True, help="Hugging Face dataset id, e.g. someone/needlepoint-data")
    ap.add_argument("--prefixes", nargs="+", default=PREFIXES)
    ap.add_argument("--batch-gb", type=float, default=5.0)
    ap.add_argument("--dry-run", action="store_true", help="list what would be published; no token needed")
    a = ap.parse_args()

    plan = [(rel, size) for p in a.prefixes for rel, size in bucket_files(p)
            if rel not in EXCLUDE and not rel.endswith(".bak")]   # .bak = superseded copies
    plan += [(rel, src.stat().st_size) for rel, src in REPLACE.items()
             if any(rel.startswith(p + "/") for p in a.prefixes)]
    total = sum(s for _, s in plan)
    print(f"{len(plan)} files, {total / 1e9:.1f} GB -> hf://datasets/{a.repo}")
    for p in a.prefixes:
        n = [s for rel, s in plan if rel.startswith(p + "/")]
        print(f"  {p:28s} {len(n):4d} files  {sum(n) / 1e9:6.2f} GB")
    print(f"  excluded: {', '.join(sorted(EXCLUDE - set(REPLACE)))}")
    if a.dry_run:
        return 0

    try:
        from huggingface_hub import CommitOperationAdd, HfApi
    except ImportError:
        raise SystemExit("pip install huggingface_hub, then: hf auth login")
    api = HfApi()
    api.create_repo(a.repo, repo_type="dataset", exist_ok=True)
    have = {f.path: getattr(f, "size", None)
            for f in api.list_repo_tree(a.repo, repo_type="dataset", recursive=True) if hasattr(f, "size")}
    todo = [(rel, size) for rel, size in plan if have.get(rel) != size or rel.endswith(tuple(TEXT_SUFFIXES))]
    print(f"{len(plan) - len(todo)} already uploaded; {len(todo)} to go")

    stage = Path(tempfile.mkdtemp(prefix="np_publish_"))
    try:
        batch, batch_bytes = [], 0
        for i, (rel, size) in enumerate(todo, 1):
            dest = stage / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            if rel in REPLACE:
                shutil.copyfile(REPLACE[rel], dest)
            else:
                subprocess.run(["rclone", "copyto", f"{r2.R2_REMOTE}/{rel}", str(dest)], check=True)
            if dest.suffix in TEXT_SUFFIXES:
                clean_text(dest, rel)
            batch.append(rel)
            batch_bytes += size
            if batch_bytes >= a.batch_gb * 1e9 or i == len(todo):
                api.create_commit(a.repo, repo_type="dataset", commit_message=f"add {len(batch)} files",
                                  operations=[CommitOperationAdd(r, str(stage / r)) for r in batch])
                print(f"  uploaded {i}/{len(todo)} ({batch_bytes / 1e9:.1f} GB batch)")
                for r in batch:
                    (stage / r).unlink()
                batch, batch_bytes = [], 0
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    files = sorted(rel for rel, _ in plan)
    api.create_commit(a.repo, repo_type="dataset", commit_message="index and card", operations=[
        CommitOperationAdd("_index.json", json.dumps({"files": files}).encode()),
        CommitOperationAdd("README.md", card(a.repo).encode()),
    ])
    print(f"done: hf://datasets/{a.repo} (fraudlib/r2.py PUBLIC_DATA_URL is the default readers use)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
