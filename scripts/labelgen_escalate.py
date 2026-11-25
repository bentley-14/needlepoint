#!/usr/bin/env python3
"""Sonnet re-read of hard cases flagged by labelgen_extract.py's Haiku pass.

Selection (needs_escalation): rows where extract.escalate is true, OR
extract.confidence == "low". Rows with no `extract` at all (a prior parse
failure) are NOT selected — that is labelgen_extract.py's own resume/retry
job, a different failure mode from "extracted fine but the read is shaky."

Each selected release is re-prompted with its FULL body (HTML->text, no
truncation — the Haiku pass caps bodies at 6000 chars; some escalate cases are
exactly the multi-defendant takedown releases that run long), joined by uuid
against the local DOJ press-release cache (data/source/doj_api/raw_press_releases.jsonl,
see doj_pr_lookup.LOCAL_CACHE). Same output schema as the Haiku prompt so
downstream (labelgen_ground.py) can treat either provenance identically.
ONE release per call — batching degrades entity extraction (validated).

Model: claude-sonnet-5 via `claude -p`, 2 workers by default. Cap-aware:
reuses the _parse_reset/_honor_gate session-window-backoff pattern from the
Haiku driver (scripts/labelgen_extract.py) — a "session limit ... resets
H:MMam (tz)" reply parks ALL workers until that reset (honoring the quoted
timezone, not the machine's local tz) instead of hammering failed calls.
Resumable by uuid (skips uuids already present with an `extract` in the
output file; errored rows are retried, matching the Haiku driver's convention).

Output: `<extractions-stem>.escalated.jsonl` (sibling of the input file,
--out overridable) with rows `{uuid, title, url, extract: {...}, provenance:
"sonnet_escalation"}`. Downstream, an escalated row REPLACES the Haiku row
for that uuid (see labelgen_ground.py's overlay logic).

Usage:
    python scripts/labelgen_escalate.py validation/curator/doj_haiku_extractions_2026-07-14.jsonl
    python scripts/labelgen_escalate.py <extractions.jsonl> --out <custom.jsonl>
    python scripts/labelgen_escalate.py <extractions.jsonl> --dry-run --limit 5
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

import doj_pr_lookup  # noqa: E402  reused: LOCAL_CACHE, _html_to_text

DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_WORKERS = 2
CALL_TIMEOUT_SEC = 180  # full-body prompts run longer than the Haiku 6000-char cap

# Same field spec as the Haiku prompt (scripts/labelgen_extract.py) — schema
# compatibility is the point of escalation, only the read gets deeper.
PROMPT = """You extract a healthcare-fraud LABEL from one U.S. DOJ press release. This release was flagged for careful re-reading (ambiguous entity, low first-pass confidence, or a multi-defendant roster) — read the FULL body below closely and output ONE minified JSON object and NOTHING else (no markdown, no prose).

Fields:
- in_scope (bool): true ONLY if this is an enforcement action for fraud against a GOVERNMENT healthcare program (Medicare, Medicaid, TRICARE, CHIP) committed by a healthcare provider/supplier/entity. false for: non-healthcare fraud, purely private-insurer-only fraud, personnel/appointment/award news, policy announcements, or releases with no identifiable healthcare defendant.
- primary_entity_name (string): the defendant whose PROVIDER IDENTITY (an NPI) would carry the fraud label. Prefer the billing ORGANIZATION if one is clearly the biller (e.g. "Maxim Healthcare Services"); otherwise the individual provider. "" if none.
- primary_entity_type ("individual"|"organization"|"unclear").
- all_defendants (array of {"name":..., "type":"individual"|"organization"}).
- payer ("medicare"|"medicaid"|"tricare"|"multiple"|"private"|"other"|"unclear").
- disposition ("charged"|"indicted"|"pleaded_guilty"|"convicted"|"settled_civil"|"sentenced"|"dismissed"|"declined"|"other").
- is_resolved (bool): true if pleaded_guilty/convicted/settled_civil/sentenced (a FINAL adjudication or admission of liability); false if only charged/indicted/accused (allegations NOT yet proven).
- amount_usd (number|null): fraud/loss/settlement dollar amount if stated, else null.
- label_class ("org"|"medicaid"|"facility"|"individual"|"none"): facility = hospice/home-health/SNF/hospital ENTITY; org = other billing organization (lab/pharmacy/clinic/DME company); medicaid = a Medicaid-billing provider (individual or org) where Medicaid is the program; individual = a natural person biller; none = out of scope.
- scheme_summary (string, <=200 chars): what the fraud was.
- confidence ("high"|"medium"|"low"): your confidence in this extraction.
- escalate (bool): true if careful review is STILL warranted after this deeper read.
- escalate_reason (string): short reason, or "" if escalate=false.

RELEASE:
Title: <<TITLE>>
Body: <<BODY>>
"""


def needs_escalation(row: dict) -> bool:
    """Selection predicate — see module docstring."""
    extract = row.get("extract")
    if not isinstance(extract, dict):
        return False
    return bool(extract.get("escalate")) or extract.get("confidence") == "low"


def extract_json(text: str):
    """Parse one JSON object out of a claude -p reply (markdown-fence and
    leading/trailing-prose tolerant). Duplicated from labelgen_extract.py by
    design — see module docstring on why the cap-aware pattern is copied,
    not shared, across each LLM-calling script."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    return None


# ---------------------------------------------------------------------------
# Cap-aware session-window gate — identical pattern to labelgen_extract.py.
# ---------------------------------------------------------------------------
_gate = {"resume_at": 0.0}
_gate_lock = threading.Lock()


def _parse_reset(msg: str):
    # e.g. "resets 3:30am (America/Los_Angeles)" — honor the quoted tz, since the
    # machine's local tz may differ from the account's reset tz.
    m = re.search(r"resets\s+(\d{1,2}):(\d{2})\s*([ap]m)(?:\s*\(([^)]+)\))?", msg, re.I)
    if not m:
        return None
    h, mm, ap = int(m.group(1)), int(m.group(2)), m.group(3).lower()
    if ap == "pm" and h != 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    tz = None
    if m.group(4):
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(m.group(4).strip())
        except Exception:
            tz = None
    now = datetime.datetime.now(tz)          # aware if tz known; else naive-local
    tgt = now.replace(hour=h, minute=mm, second=0, microsecond=0)
    if tgt <= now:
        tgt += datetime.timedelta(days=1)
    return tgt.timestamp() + 120  # 2-min buffer past the reset


def _honor_gate():
    while True:
        with _gate_lock:
            wait = _gate["resume_at"] - time.time()
        if wait <= 0:
            return
        print(f"  [cap] sleeping {wait/60:.0f}m until session window resets", flush=True)
        time.sleep(min(wait, 300))


def call(prompt: str, model: str, tries: int = 3):
    err = "empty"
    cap_waits = 0
    k = 0
    while k < tries:
        _honor_gate()
        try:
            p = subprocess.run(["claude", "-p", "--model", model],
                               input=prompt, capture_output=True, text=True,
                               timeout=CALL_TIMEOUT_SEC)
            if p.returncode == 0 and p.stdout.strip():
                j = extract_json(p.stdout)
                if j is not None:
                    return j, None
            err = (p.stderr or p.stdout or "empty").strip()[:160]
        except subprocess.TimeoutExpired:
            err = "timeout"
        if ("session limit" in err.lower() or "resets" in err.lower()) and cap_waits < 12:
            ra = _parse_reset(err) or (time.time() + 1800)
            with _gate_lock:
                _gate["resume_at"] = max(_gate["resume_at"], ra)
            cap_waits += 1
            continue
        k += 1
        time.sleep(2.0 * k)
    return None, err


# ---------------------------------------------------------------------------
# Data plumbing
# ---------------------------------------------------------------------------

def load_raw_cache(path: Path) -> dict:
    """uuid -> raw release dict (data/source/doj_api/raw_press_releases.jsonl),
    for the full-body join."""
    cache: dict = {}
    if not path.exists():
        return cache
    with path.open() as f:
        for line in f:
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("uuid"):
                cache[d["uuid"]] = d
    return cache


def build_prompt(title: str, body_text: str) -> str:
    return PROMPT.replace("<<TITLE>>", title or "").replace("<<BODY>>", body_text or "")


def default_out_path(in_path: Path) -> Path:
    """`<extractions>.escalated.jsonl` — sibling of the input, stem-derived."""
    name = in_path.name
    stem = name[: -len(".jsonl")] if name.endswith(".jsonl") else name
    return in_path.with_name(f"{stem}.escalated.jsonl")


def select_todo(rows: list[dict], raw_cache: dict, done_uuids: set) -> tuple[list[tuple[dict, dict]], int]:
    """Rows needing escalation, joined to their full-body raw release. Returns
    (todo, n_selected_but_no_raw_body)."""
    todo = []
    skipped_no_body = 0
    for r in rows:
        u = r.get("uuid")
        if u in done_uuids or not needs_escalation(r):
            continue
        raw = raw_cache.get(u)
        if not raw:
            skipped_no_body += 1
            continue
        todo.append((r, raw))
    return todo, skipped_no_body


def load_done_uuids(out_path: Path) -> set:
    done = set()
    if not out_path.exists():
        return done
    for line in out_path.open():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "extract" in rec:            # errored rows are RETRIED on resume
            done.add(rec.get("uuid"))
    return done


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

_lock = threading.Lock()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("extractions", type=Path, help="Haiku extractions JSONL (labelgen_extract.py output)")
    ap.add_argument("--out", type=Path, default=None, help="default: <extractions-stem>.escalated.jsonl")
    ap.add_argument("--raw-cache", type=Path, default=doj_pr_lookup.LOCAL_CACHE)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    ap.add_argument("--limit", type=int, default=None, help="cap rows to escalate (testing)")
    ap.add_argument("--dry-run", action="store_true", help="print planned calls; invoke nothing")
    args = ap.parse_args(argv)

    out_path = args.out or default_out_path(args.extractions)
    rows = [json.loads(l) for l in args.extractions.open() if l.strip()]
    raw_cache = load_raw_cache(args.raw_cache)
    done = load_done_uuids(out_path)

    todo, skipped_no_body = select_todo(rows, raw_cache, done)
    if args.limit is not None:
        todo = todo[: args.limit]
    total = len(todo)

    print(f"{len(rows)} rows | {len(done)} already escalated | {total} to escalate "
          f"| {skipped_no_body} selected-but-no-raw-body | {args.workers} workers -> {out_path}",
          flush=True)

    if args.dry_run:
        print(f"[dry-run] would call `claude -p --model {args.model}` for {total} release(s), "
              f"1 release/call, {args.workers} workers")
        for r, _raw in todo[:10]:
            print(f"  - {r.get('uuid')}  {r.get('title', '')[:80]!r}")
        if total > 10:
            print(f"  ... and {total - 10} more")
        return 0

    t0 = time.time()
    f = out_path.open("a")
    state = {"n": 0, "ok": 0, "err": 0}

    def work(item):
        r, raw = item
        body_text = doj_pr_lookup._html_to_text(raw.get("body") or "")
        prompt = build_prompt(r.get("title", ""), body_text)
        j, err = call(prompt, args.model)
        rec = {"uuid": r.get("uuid"), "title": r.get("title", ""), "url": r.get("url", "")}
        if j is None:
            rec["error"] = err
        else:
            rec["extract"] = j
            rec["provenance"] = "sonnet_escalation"
        with _lock:
            f.write(json.dumps(rec) + "\n")
            f.flush()
            state["n"] += 1
            state["ok" if j is not None else "err"] += 1
            n = state["n"]
            if n % 10 == 0 or n == total:
                el = time.time() - t0
                rate = n / el if el else 0
                eta = (total - n) / rate / 60 if rate else 0
                print(f"[{n}/{total}] ok={state['ok']} err={state['err']} | "
                      f"{rate:.2f}/s ETA {eta:.0f}m", flush=True)

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        list(ex.map(work, todo))
    f.close()
    print(f"DONE {total} in {(time.time()-t0)/60:.1f}m | ok={state['ok']} err={state['err']}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
