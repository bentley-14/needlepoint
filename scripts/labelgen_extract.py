#!/usr/bin/env python3
"""LLM-read label extraction (Haiku first pass; escalate flag for Sonnet).

Reads DOJ press releases (JSONL: uuid,title,url,body) and asks Haiku to read the
actual prose and extract a structured fraud LABEL. Replaces the brittle regex
miner. Resumable (skips uuids already in the output). Parallel workers.

Origin: promoted, byte-for-behavior copy of a scratchpad driver written earlier
in the labelgen-pipeline session (harness scratchpad dir, ephemeral — not part
of the repo). As of 2026-07-16 the LIVE harvest that produces
validation/curator/doj_haiku_extractions_2026-07-14.jsonl is still running the
scratchpad copy directly (a background job — do not touch it while it runs).
This promoted copy is functionally IDENTICAL and is the canonical scripts/
version for the labelgen pipeline (labelgen_escalate.py / labelgen_ground.py
consume its output format); point future invocations at this file.

Usage: labelgen_extract.py <in.jsonl> <out.jsonl> [model] [workers]
"""
import json, subprocess, sys, re, time, threading, datetime
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# CLI args are parsed by _parse_cli() under the __main__ guard — importing this
# module (tests import PROMPT/extract_json/_parse_reset; labelgen siblings copy
# its patterns) must be side-effect-free. Parsing argv at import time crashed
# pytest collection (pytest's own argv leaked into int(sys.argv[4])).
IN: Path | None = None
OUT: Path | None = None
MODEL = "claude-haiku-4-5-20251001"
WORKERS = 8


def _parse_cli() -> None:
    global IN, OUT, MODEL, WORKERS
    IN, OUT = Path(sys.argv[1]), Path(sys.argv[2])
    if len(sys.argv) > 3:
        MODEL = sys.argv[3]
    if len(sys.argv) > 4:
        WORKERS = int(sys.argv[4])

PROMPT = """You extract a healthcare-fraud LABEL from one U.S. DOJ press release. Read the release below and output ONE minified JSON object and NOTHING else (no markdown, no prose).

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
- escalate (bool): true if careful review is warranted — ambiguous which entity actually billed, unclear org-vs-individual, borderline scope, conflicting/unclear disposition, or a multi-defendant/takedown roster where no single biller is identifiable.
- escalate_reason (string): short reason, or "" if escalate=false.

RELEASE:
Title: <<TITLE>>
Body: <<BODY>>
"""

_lock = threading.Lock()
_state = {"n": 0, "ok": 0, "err": 0, "insc": 0, "esc": 0}


def extract_json(text):
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


# Global session-cap gate: when any worker hits "session limit · resets HH:MMam",
# all workers sleep until that reset time instead of hammering failed calls.
_gate = {"resume_at": 0.0}
_gate_lock = threading.Lock()


def _parse_reset(msg):
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


def call(prompt, tries=3):
    err = "empty"
    cap_waits = 0
    k = 0
    while k < tries:
        _honor_gate()
        try:
            p = subprocess.run(["claude", "-p", "--model", MODEL],
                               input=prompt, capture_output=True, text=True, timeout=150)
            if p.returncode == 0 and p.stdout.strip():
                j = extract_json(p.stdout)
                if j is not None:
                    return j, None
            err = (p.stderr or p.stdout or "empty").strip()[:160]
        except subprocess.TimeoutExpired:
            err = "timeout"
        # Session-cap hit: park the global gate at the reset time and retry
        # WITHOUT consuming a try (bounded so a weird message can't loop forever).
        if ("session limit" in err.lower() or "resets" in err.lower()) and cap_waits < 12:
            ra = _parse_reset(err) or (time.time() + 1800)
            with _gate_lock:
                _gate["resume_at"] = max(_gate["resume_at"], ra)
            cap_waits += 1
            continue
        k += 1
        time.sleep(2.0 * k)
    return None, err


def main():
    done = set()
    if OUT.exists():
        for l in OUT.open():
            if l.strip():
                try:
                    rec = json.loads(l)
                    if "extract" in rec:            # errored rows are RETRIED on resume
                        done.add(rec["uuid"])
                except Exception:
                    pass
    rows = [json.loads(l) for l in IN.open() if l.strip()]
    todo = [r for r in rows if r.get("uuid") not in done]
    total = len(todo)
    print(f"{len(rows)} releases | {len(done)} already done | {total} to process | {WORKERS} workers", flush=True)
    t0 = time.time()
    f = OUT.open("a")

    def work(r):
        prompt = PROMPT.replace("<<TITLE>>", r.get("title", "") or "").replace(
            "<<BODY>>", (r.get("body", "") or "")[:6000])
        j, err = call(prompt)
        rec = {"uuid": r.get("uuid"), "title": r.get("title", ""), "url": r.get("url", "")}
        if j is None:
            rec["error"] = err
        else:
            rec["extract"] = j
        with _lock:
            f.write(json.dumps(rec) + "\n")
            f.flush()
            _state["n"] += 1
            if j is None:
                _state["err"] += 1
            else:
                _state["ok"] += 1
                if j.get("in_scope"):
                    _state["insc"] += 1
                if j.get("escalate"):
                    _state["esc"] += 1
            n = _state["n"]
            if n % 25 == 0 or n == total:
                el = time.time() - t0
                rate = n / el if el else 0
                eta = (total - n) / rate / 60 if rate else 0
                print(f"[{n}/{total}] ok={_state['ok']} err={_state['err']} "
                      f"in_scope={_state['insc']} esc={_state['esc']} | "
                      f"{rate:.2f}/s ETA {eta:.0f}m", flush=True)

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        list(ex.map(work, todo))
    f.close()
    print(f"DONE {total} in {(time.time()-t0)/60:.1f}m | ok={_state['ok']} err={_state['err']}", flush=True)


if __name__ == "__main__":
    _parse_cli()
    main()
