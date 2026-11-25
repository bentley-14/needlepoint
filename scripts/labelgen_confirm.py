#!/usr/bin/env python3
"""Opus 3-gate chunked confirm stage — reviews labelgen_ground.py's grounded
candidates and decides keep / reject / needs_human.

Candidates are split into chunks of ~50; each chunk is ONE `claude -p
--model claude-opus-4-8` call whose prompt contains the chunk's rows as JSON
plus a 3-gate rubric:

  G1 disposition RESOLVED on the public record (plea/conviction/sentencing/
     civil settlement — mere charge/indictment fails).
  G2 right-entity (the NPI is THE defendant org — reject NPPES name-twins in
     the wrong state/type; uses identity_candidates + venue_match).
  G3 scope = billing fraud against a government healthcare program by this
     entity (kickback-based FCA counts; pure bank/tax/immigration fraud fails).

The model must return a strict minified JSON ARRAY, one verdict object per
candidate: {"org_npi", "confirmed_label": "keep"|"reject"|"needs_human",
"confidence": "high"|"medium"|"low", "basis": "<1-2 sentences>"}. Parsing is
robust: a chunk that fails to parse (or parses but is missing/garbles some
members) is retried whole-chunk a few times; candidates still unresolved
after that get a per-candidate needs_human fallback rather than failing the
whole chunk or the whole run.

Cap-aware: reuses the _parse_reset/_honor_gate session-window-backoff pattern
(see scripts/labelgen_extract.py). Resumable per chunk — chunk_id is a
deterministic, order-independent hash of the chunk's member uuids (used for
logging); the actual skip/resume check is "does every org_npi in this chunk
already have a confirmed row in --out", so a re-run after a mid-run death
does not re-call already-confirmed chunks.

Each confirmed row on disk = the grounded candidate's own fields, with
`confidence` OVERWRITTEN by this stage's verdict confidence (the grounded
row's own NPPES-match confidence was only ever an intermediate/audit value)
and `confirmed_label` + `basis` added — this matches the existing
entity_labels_CONFIRMED_2026-07-14.jsonl schema exactly (verified by reading
it: basis, confidence, confirmed_label, dedupe_checked, defendant_name,
disposition, disposition_finality, identity_candidates, identity_confidence,
identity_flag, label_class, nppes_org_name, org_npi, scheme_summary, source,
source_url, state, surface_confirmed, venue_match — plus this pipeline's own
`uuid`, needed for chunk_id).

Output: `entity_labels_CONFIRMED_<date>.jsonl` + a review .md mirroring the
existing entity_labels_CONFIRMED_2026-07-14.md format (counts by label_class
+ keep/reject/needs_human breakdown + grouped bullet lists).

Usage:
    python scripts/labelgen_confirm.py validation/curator/grounded_candidates_2026-07-16.jsonl
    python scripts/labelgen_confirm.py <grounded.jsonl> --dry-run
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_MODEL = "claude-opus-4-8"
CHUNK_SIZE = 50
CALL_TIMEOUT_SEC = 240

RUBRIC = """You are auditing candidate healthcare-fraud ORG/Medicaid/facility labels for a qui-tam investigation pipeline. Each candidate below already passed automated NPPES-identity resolution and a billing-surface confirm; your job is a 3-gate HUMAN-GRADE review using ONLY the information given (defendant_name, org_npi, state, scheme_summary, source_url, disposition, venue_match, identity_confidence, identity_flag, identity_candidates, surface_confirmed).

Apply exactly these 3 gates to EACH candidate:
- G1 (disposition RESOLVED): the public record shows a FINAL disposition — a guilty plea, conviction, sentencing, or a civil FCA settlement. A mere charge, indictment, or complaint (no proven/admitted liability yet) FAILS this gate.
- G2 (right entity): org_npi is THE defendant organization named in the scheme, not an NPPES same-name "twin" in the wrong state or of the wrong entity type. Use identity_candidates + venue_match — a venue_match of "mismatch" or an identity_flag of "ambiguous_multi_candidate" is a strong signal this gate may fail; a venue_match of "match" with no identity_flag is a strong signal it passes.
- G3 (scope): the fraud is BILLING fraud against a government healthcare program (Medicare/Medicaid/TRICARE/CHIP) committed by this entity — false claims, medically-unnecessary billing, phantom billing, or kickback-driven referrals (kickback-based False Claims Act counts PASS this gate). Pure bank fraud, tax fraud, or immigration fraud with no healthcare billing nexus FAILS this gate.

confirmed_label:
- "keep" — all 3 gates pass.
- "reject" — any gate clearly fails.
- "needs_human" — you cannot confidently apply a gate from the given information (genuinely ambiguous, not just "less than perfect" evidence).

Output a single minified JSON ARRAY, one object per candidate IN THE SAME ORDER given, and NOTHING else (no markdown, no prose, no trailing commentary):
[{"org_npi": "<the candidate's org_npi, copied exactly>", "confirmed_label": "keep"|"reject"|"needs_human", "confidence": "high"|"medium"|"low", "basis": "<1-2 sentences citing what drove the verdict>"}, ...]

CANDIDATES:
<<CANDIDATES_JSON>>
"""


def make_chunks(rows: list[dict], size: int = CHUNK_SIZE) -> list[list[dict]]:
    return [rows[i:i + size] for i in range(0, len(rows), size)]


def chunk_id(rows: list[dict]) -> str:
    """Deterministic, order-independent identifier for a chunk: a hash of its
    member uuids. Same MEMBERSHIP (any order) -> same id; different
    membership -> a different id. Falls back to org_npi if a row has no uuid
    (defensive — grounded rows always carry one)."""
    keys = sorted(str(r.get("uuid") or r.get("org_npi") or "") for r in rows)
    return hashlib.sha256("|".join(keys).encode("utf-8")).hexdigest()[:12]


def build_prompt(chunk: list[dict]) -> str:
    payload = [
        {
            "org_npi": c.get("org_npi"),
            "defendant_name": c.get("defendant_name"),
            "label_class": c.get("label_class"),
            "state": c.get("state"),
            "scheme_summary": c.get("scheme_summary"),
            "source_url": c.get("source_url"),
            "disposition": c.get("disposition"),
            "venue_match": c.get("venue_match"),
            "identity_confidence": c.get("identity_confidence"),
            "identity_flag": c.get("identity_flag"),
            "identity_candidates": c.get("identity_candidates"),
            "surface_confirmed": c.get("surface_confirmed"),
        }
        for c in chunk
    ]
    return RUBRIC.replace("<<CANDIDATES_JSON>>", json.dumps(payload))


def extract_json_array(text: str):
    """Parse a JSON array out of a claude -p reply (markdown-fence and
    leading/trailing-prose tolerant)."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        j = json.loads(text)
        if isinstance(j, list):
            return j
    except Exception:
        pass
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if m:
        try:
            j = json.loads(m.group(0))
            if isinstance(j, list):
                return j
        except Exception:
            return None
    return None


def reconcile_chunk(chunk: list[dict], parsed: list | None) -> list[dict]:
    """Match parsed verdicts back to chunk candidates by org_npi. Any
    candidate missing from `parsed` (or a wholesale parse failure, parsed is
    None) gets an individual needs_human fallback instead of failing the
    whole chunk."""
    by_npi: dict[str, dict] = {}
    if parsed:
        for item in parsed:
            if isinstance(item, dict) and item.get("org_npi"):
                by_npi[str(item["org_npi"])] = item
    out = []
    for c in chunk:
        v = by_npi.get(str(c.get("org_npi")))
        if v and v.get("confirmed_label") in ("keep", "reject", "needs_human"):
            out.append({
                "org_npi": c.get("org_npi"),
                "confirmed_label": v["confirmed_label"],
                "confidence": v.get("confidence") if v.get("confidence") in ("high", "medium", "low") else "low",
                "basis": v.get("basis", ""),
            })
        else:
            out.append({
                "org_npi": c.get("org_npi"),
                "confirmed_label": "needs_human",
                "confidence": "low",
                "basis": "chunk response parse/reconcile failed for this candidate",
            })
    return out


# ---------------------------------------------------------------------------
# Cap-aware session-window gate — identical pattern to labelgen_extract.py /
# labelgen_escalate.py.
# ---------------------------------------------------------------------------
_gate = {"resume_at": 0.0}


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
        wait = _gate["resume_at"] - time.time()
        if wait <= 0:
            return
        print(f"  [cap] sleeping {wait/60:.0f}m until session window resets", flush=True)
        time.sleep(min(wait, 300))


def call_chunk(prompt: str, model: str, tries: int = 3):
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
                arr = extract_json_array(p.stdout)
                if arr is not None:
                    return arr, None
            err = (p.stderr or p.stdout or "empty").strip()[:160]
        except subprocess.TimeoutExpired:
            err = "timeout"
        if ("session limit" in err.lower() or "resets" in err.lower()) and cap_waits < 12:
            ra = _parse_reset(err) or (time.time() + 1800)
            _gate["resume_at"] = max(_gate["resume_at"], ra)
            cap_waits += 1
            continue
        k += 1
        time.sleep(2.0 * k)
    return None, err


# ---------------------------------------------------------------------------
# Resumability + I/O
# ---------------------------------------------------------------------------

def load_done_npis(out_path: Path) -> set[str]:
    done = set()
    if not out_path.exists():
        return done
    for line in out_path.open():
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("org_npi"):
            done.add(str(d["org_npi"]))
    return done


def chunk_is_done(chunk: list[dict], done_npis: set[str]) -> bool:
    return bool(chunk) and all(str(c.get("org_npi")) in done_npis for c in chunk)


def render_confirm_md(rows: list[dict], *, generated: str, out_name: str) -> str:
    n_keep = sum(1 for r in rows if r.get("confirmed_label") == "keep")
    n_reject = sum(1 for r in rows if r.get("confirmed_label") == "reject")
    n_needs_human = sum(1 for r in rows if r.get("confirmed_label") == "needs_human")
    lines = [
        f"# Entity/org fraud labels — CONFIRMED audit pass ({generated})",
        "",
        "Opus 3-gate chunked confirm of grounded candidates (G1 disposition resolved, "
        f"G2 right-entity, G3 billing-fraud scope). Output: `{out_name}`.",
        "",
        "> **These are CONFIRMED candidate labels for the maintainer to review and "
        "land via labelgen_land.py.** This is NOT a gold_set / entity_labels write.",
        "",
        f"## Counts — keep **{n_keep}** / reject **{n_reject}** / needs_human "
        f"**{n_needs_human}** (of {len(rows)})",
    ]
    by_class: dict[str, dict[str, int]] = {}
    for r in rows:
        lc = r.get("label_class", "?")
        counts = by_class.setdefault(lc, {"keep": 0, "reject": 0, "needs_human": 0})
        counts[r.get("confirmed_label", "needs_human")] = counts.get(r.get("confirmed_label", "needs_human"), 0) + 1
    for lc, counts in sorted(by_class.items()):
        lines.append(f"- **{lc}**: keep {counts['keep']}, reject {counts['reject']}, "
                      f"needs_human {counts['needs_human']}")
    lines.append("")

    for label, title in (("keep", "CONFIRMED KEEPS"), ("reject", "REJECTS"),
                          ("needs_human", "NEEDS HUMAN")):
        subset = [r for r in rows if r.get("confirmed_label") == label]
        if not subset:
            continue
        lines.append(f"## {title} ({len(subset)})")
        lines.append("")
        for r in subset:
            lines.append(
                f"- `{r.get('org_npi')}` **{r.get('defendant_name')}** "
                f"({r.get('label_class')}, {r.get('state') or '?'}, conf={r.get('confidence')}) "
                f"— {r.get('basis', '')}"
            )
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("grounded", type=Path, help="labelgen_ground.py output JSONL")
    ap.add_argument("--out", type=Path, default=None,
                    help="default: validation/curator/entity_labels_CONFIRMED_<today>.jsonl")
    ap.add_argument("--md-out", type=Path, default=None, help="default: --out with .md extension")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--chunk-size", type=int, default=CHUNK_SIZE)
    ap.add_argument("--dry-run", action="store_true", help="print planned chunks; invoke nothing")
    args = ap.parse_args(argv)

    today = datetime.date.today().isoformat()
    out_path = args.out or (REPO_ROOT / "validation" / "curator" / f"entity_labels_CONFIRMED_{today}.jsonl")
    md_out = args.md_out or out_path.with_suffix(".md")

    rows = [json.loads(l) for l in args.grounded.open() if l.strip()]
    chunks = make_chunks(rows, args.chunk_size)
    done_npis = load_done_npis(out_path)
    todo_chunks = [c for c in chunks if not chunk_is_done(c, done_npis)]

    print(f"{len(rows)} candidates | {len(chunks)} chunks (size {args.chunk_size}) | "
          f"{len(chunks) - len(todo_chunks)} chunks already done | {len(todo_chunks)} to confirm "
          f"-> {out_path}", flush=True)

    if args.dry_run:
        print(f"[dry-run] would make {len(todo_chunks)} `claude -p --model {args.model}` "
              f"call(s), 1 chunk/call")
        for c in todo_chunks[:5]:
            npis = [r.get("org_npi") for r in c]
            print(f"  - chunk {chunk_id(c)}  n={len(c)}  npis={npis[:5]}"
                  f"{'...' if len(npis) > 5 else ''}")
        if len(todo_chunks) > 5:
            print(f"  ... and {len(todo_chunks) - 5} more chunks")
        return 0

    f = out_path.open("a")
    n_keep = n_reject = n_needs_human = 0
    for i, chunk in enumerate(todo_chunks, 1):
        cid = chunk_id(chunk)
        arr, err = call_chunk(build_prompt(chunk), args.model)
        verdicts = reconcile_chunk(chunk, arr)
        by_npi = {str(c.get("org_npi")): c for c in chunk}
        for v in verdicts:
            grounded_row = by_npi.get(str(v["org_npi"]), {})
            merged = {**grounded_row, "confirmed_label": v["confirmed_label"],
                      "confidence": v["confidence"], "basis": v["basis"]}
            f.write(json.dumps(merged) + "\n")
            f.flush()
            if v["confirmed_label"] == "keep":
                n_keep += 1
            elif v["confirmed_label"] == "reject":
                n_reject += 1
            else:
                n_needs_human += 1
        print(f"[{i}/{len(todo_chunks)}] chunk {cid} n={len(chunk)} "
              f"{'OK' if err is None else f'FALLBACK ({err})'}", flush=True)
    f.close()

    # Rebuild the review .md from the FULL output file (not just this run's
    # todo_chunks) so a resumed run's .md still reflects everything landed so far.
    all_rows = [json.loads(l) for l in out_path.open() if l.strip()]
    md_out.write_text(render_confirm_md(all_rows, generated=today, out_name=out_path.name))
    print(f"DONE | keep={n_keep} reject={n_reject} needs_human={n_needs_human} -> {out_path}")
    print(f"review .md -> {md_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
