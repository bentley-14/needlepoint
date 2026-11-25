"""Date-gating wrapper around nppes_lookup.py for calibration runs.

WHY
---
NPPES is the live national provider registry. The bulk of what it returns
(name, taxonomy, addresses, license number, enumeration date) is static
identity info that is not a "fraud allegation" leak — it's the same kind
of public-record data a normal investigator legitimately uses.

The ONE real leak vector is **post-cutoff status changes**: if a provider
was deactivated, excluded, or had their entry materially updated AFTER
the fraud became public (e.g., after the DOJ indictment), the current
NPPES record shows that as a fresh `last_updated` timestamp + a changed
`status`. An investigator querying NPPES today would see "Deactivated
2025-09-15" and immediately know the provider got busted.

This wrapper strips any field whose value parses as a date on or after
the cutoff, so the agent gets the provider's identity as it stood
pre-cutoff but cannot see whether/when anything changed post-cutoff.

USAGE
-----
    python3 scripts/sandbox_nppes.py --as-of YYYY-MM-DD --npi <NPI>
    python3 scripts/sandbox_nppes.py --as-of YYYY-MM-DD --last-name Smith --state CA

Pass-through: every nppes_lookup.py flag (--npi, --first-name, --last-name,
--org-name, --state, --taxonomy, --postal-code, --limit) is forwarded. The
output is always sanitized JSON (we force --raw on the subprocess so we
can re-emit a sanitized copy). The `--text` / formatted-output mode is
intentionally not supported — calibration agents should consume JSON.

EXIT CODES
----------
0 — sanitized result emitted
1 — bad args, lookup failed, or sanitization failure
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
NPPES_LOOKUP = REPO_ROOT / "scripts" / "nppes_lookup.py"

# Regex for ISO-ish date strings appearing in NPPES values. We match
# YYYY-MM-DD anywhere in a string field; if the date >= cutoff, the field
# is redacted. Epoch-milliseconds fields (e.g. last_updated_epoch) are
# handled separately because they're integers, not strings.
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")

# Field-name suffixes that signal an epoch-ms timestamp value. We look at
# the FIELD NAME (not the value) because plain ints don't self-identify
# as timestamps.
_EPOCH_FIELD_SUFFIXES = ("_epoch", "_date_ms", "_timestamp_ms")

REDACTED_MARKER = "REDACTED_POST_CUTOFF"


def _is_epoch_field(key: str) -> bool:
    k = key.lower()
    return any(k.endswith(s) for s in _EPOCH_FIELD_SUFFIXES)


def _epoch_ms_to_date(epoch_ms: int) -> date | None:
    """Convert epoch-ms to date; return None on garbage input."""
    try:
        # Some NPPES epoch fields are stringified ints; normalize.
        v = int(epoch_ms)
        # NPPES epochs look like 1224087200000 (13 digits = ms). Reject
        # values smaller than ~1990 in seconds (754M) — likely garbage.
        if v < 1_000_000_000_000:  # require ms-scale (13 digits)
            return None
        return date.fromtimestamp(v / 1000)
    except (ValueError, OSError, TypeError):
        return None


def _string_max_date(s: str) -> date | None:
    """Find the latest ISO date appearing in string ``s`` (or None)."""
    latest: date | None = None
    for m in _ISO_DATE_RE.finditer(s):
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
        if latest is None or d > latest:
            latest = d
    return latest


def _sanitize(obj, cutoff: date, path: str = ""):
    """Recursively walk ``obj`` and redact fields whose date >= cutoff.

    For dicts: each value is sanitized. For strings: if the string contains
    an ISO date >= cutoff, the WHOLE string is replaced with REDACTED_MARKER.
    For epoch-ms fields (by name): if the epoch date >= cutoff, the value is
    replaced with REDACTED_MARKER. Lists are recursed.
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(v, (int, str)) and _is_epoch_field(k):
                d = _epoch_ms_to_date(v)
                if d is not None and d >= cutoff:
                    out[k] = REDACTED_MARKER
                    continue
            # 'status' redaction: if a sibling 'last_updated' shows a date
            # >= cutoff, the status itself reflects a post-cutoff change.
            # Be conservative: redact status whenever the record's
            # last_updated is post-cutoff.
            if k == "status" and isinstance(obj.get("last_updated"), str):
                d = _string_max_date(obj["last_updated"])
                if d is not None and d >= cutoff:
                    out[k] = REDACTED_MARKER
                    continue
            out[k] = _sanitize(v, cutoff, path + "." + k)
        return out
    if isinstance(obj, list):
        return [_sanitize(x, cutoff, f"{path}[{i}]") for i, x in enumerate(obj)]
    if isinstance(obj, str):
        d = _string_max_date(obj)
        if d is not None and d >= cutoff:
            return REDACTED_MARKER
        return obj
    return obj


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Date-gating NPPES wrapper for calibration runs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--as-of", required=True,
                    help="Cutoff date YYYY-MM-DD; fields with timestamps >= "
                         "this are redacted (post-cutoff knowledge).")
    # Forward nppes_lookup.py args (same surface, minus --raw which is forced).
    ap.add_argument("--npi")
    ap.add_argument("--first-name")
    ap.add_argument("--last-name")
    ap.add_argument("--org-name")
    ap.add_argument("--state")
    ap.add_argument("--taxonomy")
    ap.add_argument("--postal-code")
    ap.add_argument("--limit", type=int, default=20)
    args = ap.parse_args(argv)

    try:
        cutoff = date.fromisoformat(args.as_of)
    except ValueError as e:
        print(f"ERROR: --as-of must be YYYY-MM-DD: {e}", file=sys.stderr)
        return 1

    # Build subprocess invocation for nppes_lookup.py with --raw (we need
    # the structured JSON to sanitize). The --raw flag forces JSON output.
    cmd = [sys.executable, str(NPPES_LOOKUP), "--raw", "--limit", str(args.limit)]
    for arg_name, val in [
        ("--npi", args.npi),
        ("--first-name", args.first_name),
        ("--last-name", args.last_name),
        ("--org-name", args.org_name),
        ("--state", args.state),
        ("--taxonomy", args.taxonomy),
        ("--postal-code", args.postal_code),
    ]:
        if val:
            cmd.extend([arg_name, val])

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        print("ERROR: nppes_lookup timed out", file=sys.stderr)
        return 1
    if proc.returncode != 0:
        print(f"ERROR: nppes_lookup exited {proc.returncode}: {proc.stderr}",
              file=sys.stderr)
        return 1

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        print(f"ERROR: nppes_lookup output was not JSON: {e}", file=sys.stderr)
        print(f"stdout (first 500): {proc.stdout[:500]}", file=sys.stderr)
        return 1

    sanitized = _sanitize(data, cutoff)
    # Add a banner so the agent sees this is a date-gated response.
    if isinstance(sanitized, dict):
        sanitized["_sandbox"] = {
            "as_of": cutoff.isoformat(),
            "note": (
                "This NPPES response has been date-gated. Any field with a "
                "timestamp on or after " + cutoff.isoformat() + " has been "
                "replaced with " + REDACTED_MARKER + " to prevent post-cutoff "
                "knowledge leakage (e.g., a deactivation following the fraud "
                "becoming public). Static identity fields (name, NPI, taxonomy, "
                "enumeration date, addresses) are preserved as-is."
            ),
        }
    print(json.dumps(sanitized, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
