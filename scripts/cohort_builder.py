"""Adjudicated true-negative cohort builder — the precision counterpart to gold_set.

The current battery "negative controls" (Baobab, Laurel) are *anomalous-but-
unprosecuted* providers: they cannot distinguish a false positive from a
true-but-uncharged detection (both FILE in calibration; we can't tell if that's
wrong). An *adjudicated* "looked-fileable-but-wasn't" cohort fixes that — qui tam
/ False Claims Act cases the government DECLINED and a court DISMISSED on the
merits (or the relator voluntarily dismissed). Same legal instrument as our FILE
decision, failed on falsity / Rule 9(b). The model SHOULD decline these.

This is a "Curator: negative-cohort mode" (per TODO 2026-06-17). It is **DRY-RUN
only**: it enumerates candidate dockets, classifies each disposition, and writes a
human-reviewable candidate list. It NEVER writes battery.json — a human keeps/
excludes from the list, and only approved candidates get NPPES-resolved + added
(that commit step reuses the Corpus Curator NPPES flow + check_billing_surface).

Pipeline (TODO steps 1-3 here; 4-6 are the post-review commit step):
  1. ENUMERATE  — CourtListener/RECAP search filtered to PACER nature-of-suit
     376 (qui tam), terminated dockets, healthcare-leaning query terms.
  2. DETECT     — declination notice ("Notice of Election to Decline Intervention")
     + a terminating dismissal, from docket-entry descriptions.
  3. CLASSIFY   — the load-bearing disposition-reason filter (classify_disposition):
       KEEP    — declined + merits-dismissed (12(b)(6)/9(b)/SJ on falsity) [strongest]
                 or declined + relator voluntary dismissal [usable]
       EXCLUDE — settlement / consent judgment / judgment-for-govt (POSITIVES),
                 DPA/NPA/criminal, SOL-only, constitutional (Appointments Clause),
                 first-to-file (§3730(b)(5)) / public-disclosure (§3730(e)) bars
                 (jurisdictional, not merits)

Two fetch modes:
  - search-only (default; works UNAUTHENTICATED): disposition-signal phrases ARE
    the search queries, so the matched recap_documents are the dispositive entries.
    Coarser (only query-matched entries), but needs no token.
  - --full-entries (needs COURTLISTENER_TOKEN): fetch every docket entry per
    candidate for complete disposition coverage. Run this on a machine with the
    token set for the authoritative pass.

Usage::

    python scripts/cohort_builder.py --max 40
    python scripts/cohort_builder.py --max 60 --full-entries   # token required
    python scripts/cohort_builder.py --since-year 2015 --out validation/cohort
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

# scripts/ on path so this imports whether run as `python scripts/cohort_builder.py`
# or imported as `scripts.cohort_builder` (the test path). courtlistener_search does
# no network at import (just loads .env + sets TOKEN), so this is side-effect-light.
sys.path.insert(0, str(Path(__file__).resolve().parent))

NOS_QUI_TAM = "376"  # PACER nature-of-suit: qui tam (375 = FCA broadly)

# ── Disposition classifier (the durable, unit-tested core) ───────────────────
# Pure regex/keyword logic over docket-entry descriptions. EXCLUDE signals take
# precedence over KEEP — a case that settled but also had an early MTD is a
# POSITIVE (EXCLUDE), not a clean true negative.

_DECLINE = re.compile(
    r"declin\w*\s+to\s+intervene|notice of election to decline|"
    r"elect\w*\s+(?:not\s+to|to\s+decline)\s+(?:to\s+)?intervene|"
    r"(?:government|united states)['’]?s?\s+(?:notice of\s+)?declination",
    re.I)

# EXCLUDE families (fraud may be real / not adjudicated on the merits) ─────────
_SETTLEMENT = re.compile(
    r"\bsettl(?:e|ed|ement|ing)\b|consent judgment|consent decree|"
    r"stipulation of settlement|settlement agreement|"
    r"joint (?:motion|stipulation)[^.]{0,40}settl",
    re.I)
_GOVT_JUDGMENT = re.compile(
    r"judgment[^.]{0,30}in favor of the united states|"
    r"judgment for the (?:united states|government)", re.I)
_CRIMINAL_DPA = re.compile(
    r"deferred prosecution|non-?prosecution agreement|plea agreement|"
    r"\bpleads? guilty\b|guilty plea|criminal information|\bindictment\b", re.I)
_JURISDICTIONAL = re.compile(
    r"first[- ]to[- ]file|3730\(b\)\(5\)|public[- ]disclosure|"
    r"original source|3730\(e\)", re.I)
_CONSTITUTIONAL = re.compile(
    r"appointments clause|article ii(?:\b| appointments)|"
    r"\bunconstitutional\b|polansky", re.I)
_SOL = re.compile(r"statute of limitations|time[- ]barred|3731\(b\)", re.I)

# KEEP families (court / relator ended it without a recovery) ─────────────────
_DISMISS = re.compile(r"\bdismiss\w*", re.I)
_MERITS_DISMISS = re.compile(
    r"grant\w*[^.]{0,40}motion to dismiss|order\w*\s+dismiss\w*|"
    r"dismiss\w*[^.]{0,30}with prejudice|\b9\(b\)\b|\brule 9\b|"
    r"fail\w*\s+to\s+state a claim|12\(b\)\(6\)|summary judgment|"
    r"failure to plead|particularity", re.I)
_VOLUNTARY_DISMISS = re.compile(
    r"voluntary dismissal|rule 41|notice of dismissal|"
    r"relator['’]?s?[^.]{0,30}dismiss|stipulat\w*[^.]{0,20}dismiss", re.I)

# EXCLUDE labels in precedence order (settlement first — strongest "it was real")
_EXCLUDE_RULES = [
    ("EXCLUDE_SETTLEMENT", [_SETTLEMENT, _GOVT_JUDGMENT]),
    ("EXCLUDE_CRIMINAL_DPA", [_CRIMINAL_DPA]),
    ("EXCLUDE_JURISDICTIONAL", [_JURISDICTIONAL]),
    ("EXCLUDE_CONSTITUTIONAL", [_CONSTITUTIONAL]),
    ("EXCLUDE_SOL", [_SOL]),
]


def _window(text: str, m: re.Match, pad: int = 70) -> str:
    a = max(0, m.start() - pad)
    b = min(len(text), m.end() + pad)
    return ("…" if a else "") + text[a:b].replace("\n", " ").strip() + ("…" if b < len(text) else "")


@dataclass
class Disposition:
    label: str                       # KEEP_MERITS | KEEP_VOLUNTARY | EXCLUDE_* | UNKNOWN*
    keep: bool                       # True only for KEEP_*
    declination: bool                # DOJ declined to intervene (signal present)
    dismissed: bool                  # a terminating dismissal signal present
    evidence: list[str] = field(default_factory=list)


def classify_disposition(entry_texts: list[str], terminated: bool) -> Disposition:
    """Classify a docket's disposition from its entry descriptions.

    ``terminated`` is whether CourtListener has a ``dateTerminated`` for the
    docket — a true negative must be over. EXCLUDE signals win over KEEP.
    """
    text = "\n".join(t for t in entry_texts if t)
    ev: list[str] = []

    decl_m = _DECLINE.search(text)
    declination = bool(decl_m)
    if decl_m:
        ev.append("declination: " + _window(text, decl_m))
    dismissed = bool(_DISMISS.search(text))

    # EXCLUDE takes precedence.
    for label, rules in _EXCLUDE_RULES:
        for rx in rules:
            m = rx.search(text)
            if m:
                ev.append(f"{label}: " + _window(text, m))
                return Disposition(label, False, declination, dismissed, ev)

    # KEEP requires the case to be over with a merits or voluntary ending. A
    # defense SUMMARY JUDGMENT is a merits termination with no "dismiss" token,
    # so test the merits/voluntary patterns independently of the literal-dismiss
    # flag (which only gates the residual "dismissed, reason unclear" bucket).
    if terminated:
        mm = _MERITS_DISMISS.search(text)
        if mm:
            ev.append("merits-dismiss: " + _window(text, mm))
            return Disposition("KEEP_MERITS", True, declination, True, ev)
        vm = _VOLUNTARY_DISMISS.search(text)
        if vm:
            ev.append("voluntary-dismiss: " + _window(text, vm))
            return Disposition("KEEP_VOLUNTARY", True, declination, True, ev)
        if dismissed:
            return Disposition("UNKNOWN_DISMISS", False, declination, True, ev)

    return Disposition("UNKNOWN", False, declination, dismissed, ev)


def propose(disp: Disposition, terminated: bool) -> str:
    """Map a Disposition → a triage recommendation for the human reviewer.

      KEEP   — keep-label AND DOJ declination both detected (strongest TN)
      REVIEW — a keep-label OR a dismissal whose reason is unclear, but the
               declination wasn't captured (search-only mode misses entries) →
               needs eyes / a --full-entries pass
      EXCLUDE — any EXCLUDE_* label (fraud may be real)
      DROP   — not terminated, or no disposition signal at all
    """
    if disp.label.startswith("EXCLUDE"):
        return "EXCLUDE"
    if disp.keep and disp.declination:
        return "KEEP"
    if disp.keep or disp.label == "UNKNOWN_DISMISS" or (terminated and disp.declination):
        return "REVIEW"
    return "DROP"


# ── CourtListener enumeration ────────────────────────────────────────────────
# Disposition-signal phrases double as search queries so the matched
# recap_documents are the dispositive entries (works unauthenticated).
_SIGNAL_QUERIES = [
    '"notice of election to decline intervention"',
    '"decline to intervene" "motion to dismiss"',
    '"order" "granting" "motion to dismiss" "qui tam"',
    '"dismissed with prejudice" "false claims"',
    '"voluntary dismissal" "qui tam"',
    '"failure to plead" "9(b)" "false claims"',
]
# Healthcare-leaning terms appended to bias toward provider defendants.
_HEALTHCARE_HINT = "(medicare OR medicaid OR physician OR clinic OR hospital OR provider OR healthcare OR pharmacy)"


def _cl():
    import courtlistener_search as cl  # noqa: E402  (path injected at module top)
    return cl


def enumerate_candidates(*, max_dockets: int, since_year: int | None,
                         full_entries: bool, log=print) -> list[dict]:
    """Search NOS-376 terminated dockets across the signal queries; dedup by
    docket_id; gather entry texts + parties + dateTerminated for each. Returns
    raw candidate dicts (unclassified)."""
    cl = _cl()
    by_docket: dict[int, dict] = {}
    for q in _SIGNAL_QUERIES:
        params = {"type": "r", "q": f"{q} {_HEALTHCARE_HINT}",
                  "nature_of_suit": NOS_QUI_TAM}
        if since_year:
            params["filed_after"] = f"{since_year}-01-01"
        res = cl._get("search", params)
        if "_error" in res:
            log(f"  ! query {q!r}: {res['_error']}")
            continue
        for r in (res.get("results") or []):
            did = r.get("docket_id")
            if not did:
                continue
            cand = by_docket.setdefault(did, {
                "docket_id": did,
                "case_name": r.get("caseName") or r.get("case_name_full") or "?",
                "docket_number": r.get("docketNumber") or "?",
                "court": r.get("court") or r.get("court_id") or "?",
                "court_id": r.get("court_id") or "",
                "suit_nature": r.get("suitNature") or "?",
                "date_filed": r.get("dateFiled"),
                "date_terminated": r.get("dateTerminated"),
                "defendants": _defendants(r.get("party")),
                "cl_url": "https://www.courtlistener.com" + (r.get("docket_absolute_url") or ""),
                "entry_texts": [],
                "matched_queries": [],
            })
            cand["matched_queries"].append(q)
            for rd in (r.get("recap_documents") or []):
                desc = rd.get("description") or rd.get("short_description") or rd.get("snippet")
                if desc:
                    cand["entry_texts"].append(desc)
        if len(by_docket) >= max_dockets:
            break

    cands = list(by_docket.values())[:max_dockets]
    if full_entries:
        for c in cands:
            c["entry_texts"].extend(_fetch_all_entries(cl, c["docket_id"], log))
    return cands


def _defendants(party) -> list[str]:
    """Best-effort defendant names from the search result's party blob. The
    relator side is 'United States ex rel. <relator>'; defendants are the rest."""
    if not party:
        return []
    if isinstance(party, str):
        names = [party]
    else:
        names = [str(p) for p in party]
    out = []
    for n in names:
        low = n.lower()
        if "united states" in low or "ex rel" in low or "relator" in low:
            continue
        out.append(n.strip())
    return out[:8]


def _fetch_all_entries(cl, docket_id: int, log=print) -> list[str]:
    """Token-mode: pull every docket entry description (auth required)."""
    out: list[str] = []
    res = cl._get("docket-entries", {"docket": docket_id, "page_size": 100})
    if "_error" in res:
        log(f"  ! docket-entries {docket_id}: {res['_error']} "
            f"(set COURTLISTENER_TOKEN for --full-entries)")
        return out
    for e in (res.get("results") or []):
        d = e.get("description")
        if d:
            out.append(d)
    return out


def build(*, max_dockets: int, since_year: int | None, full_entries: bool,
          log=print) -> list[dict]:
    """Enumerate → classify → propose. Returns sorted candidate rows."""
    raw = enumerate_candidates(max_dockets=max_dockets, since_year=since_year,
                               full_entries=full_entries, log=log)
    rows = []
    for c in raw:
        terminated = bool(c.get("date_terminated"))
        disp = classify_disposition(c["entry_texts"], terminated)
        rec = propose(disp, terminated)
        rows.append({
            "proposed": rec,
            "disposition": disp.label,
            "declination_detected": disp.declination,
            "case_name": c["case_name"],
            "docket_number": c["docket_number"],
            "docket_id": c["docket_id"],
            "court_id": c["court_id"],
            "suit_nature": c["suit_nature"],
            "date_filed": c["date_filed"],
            "date_terminated": c["date_terminated"],
            "defendants": c["defendants"],
            "evidence": disp.evidence,
            "cl_url": c["cl_url"],
            "n_entry_texts": len(c["entry_texts"]),
        })
    order = {"KEEP": 0, "REVIEW": 1, "EXCLUDE": 2, "DROP": 3}
    rows.sort(key=lambda r: (order.get(r["proposed"], 9), r["disposition"]))
    return rows


def render_md(rows: list[dict], *, full_entries: bool) -> str:
    n = len(rows)
    by = {}
    for r in rows:
        by[r["proposed"]] = by.get(r["proposed"], 0) + 1
    mode = "full-entries (authenticated)" if full_entries else "search-only (unauthenticated — coarser; re-run with --full-entries + token for full coverage)"
    lines = [
        f"# Adjudicated true-negative cohort — DRY-RUN candidates ({date.today().isoformat()})",
        "",
        f"Fetch mode: **{mode}**.  {n} dockets · "
        + " · ".join(f"{k}={by.get(k, 0)}" for k in ("KEEP", "REVIEW", "EXCLUDE", "DROP")),
        "",
        "**KEEP** = declined + merits/voluntary dismissal (defensible true negative). "
        "**REVIEW** = looks dismissed-on-merits but declination not captured (needs eyes / a token pass). "
        "**EXCLUDE** = settlement / criminal / jurisdictional / SOL (fraud may be real). "
        "**DROP** = not terminated or no signal.",
        "",
        "Next for a KEEP you approve: NPPES-resolve the defendant to an individual NPI "
        "(`scripts/nppes_lookup.py`) + confirm a Medicare surface (`scripts/check_billing_surface.py`), "
        "then add as `kind: \"adjudicated_true_negative\"` to `validation/battery/battery.json` "
        "with `as_of` = pre-conduct cutoff (NOT the dismissal date).",
        "",
        "| proposed | disposition | decl? | case | docket | court | filed | terminated | defendants |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        def_ = "; ".join(r["defendants"])[:60] or "—"
        lines.append(
            f"| {r['proposed']} | {r['disposition']} | "
            f"{'Y' if r['declination_detected'] else '·'} | "
            f"[{r['case_name'][:46]}]({r['cl_url']}) | {r['docket_number']} | "
            f"{r['court_id']} | {r['date_filed'] or '—'} | {r['date_terminated'] or '—'} | {def_} |")
    # evidence appendix for KEEP/REVIEW
    lines += ["", "## Evidence (KEEP / REVIEW)", ""]
    for r in rows:
        if r["proposed"] in ("KEEP", "REVIEW") and r["evidence"]:
            lines.append(f"- **{r['case_name'][:60]}** ({r['docket_number']}) — {r['disposition']}")
            for e in r["evidence"]:
                lines.append(f"  - {e}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Adjudicated true-negative cohort builder (DRY-RUN)")
    ap.add_argument("--max", type=int, default=40, help="max dockets to process")
    ap.add_argument("--since-year", type=int, default=2014, help="only dockets filed on/after this year")
    ap.add_argument("--full-entries", action="store_true",
                    help="fetch every docket entry per candidate (needs COURTLISTENER_TOKEN)")
    ap.add_argument("--out", default="validation/cohort", help="output dir")
    args = ap.parse_args(argv)

    print(f"Enumerating NOS-{NOS_QUI_TAM} qui-tam dockets "
          f"(max={args.max}, since={args.since_year}, "
          f"mode={'full-entries' if args.full_entries else 'search-only'})…")
    rows = build(max_dockets=args.max, since_year=args.since_year,
                 full_entries=args.full_entries)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = date.today().isoformat()
    jsonl_path = out_dir / f"tn_candidates_{stamp}.jsonl"
    md_path = out_dir / f"tn_candidates_{stamp}.md"
    with jsonl_path.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    md_path.write_text(render_md(rows, full_entries=args.full_entries))

    by = {}
    for r in rows:
        by[r["proposed"]] = by.get(r["proposed"], 0) + 1
    print(f"\n{len(rows)} candidates → {jsonl_path}")
    print("  " + "  ".join(f"{k}={by.get(k, 0)}" for k in ("KEEP", "REVIEW", "EXCLUDE", "DROP")))
    print(f"Review: {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
