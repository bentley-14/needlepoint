"""Human-context inbox — inject an operator note into a LIVE NeedlePoint run.

Appends one JSON line to ``state/{case_id}/human_context.jsonl``. The
orchestrator reads this file every iteration (``fraud_investigator.human_context``)
and folds any not-yet-merged line into the evidence stream as a
``human_attested`` ``confirmed_fact`` — no prompt-template changes, no
special-casing; it flows through the exact same evidence-merge machinery
every agent's findings already use, so every downstream prompt sees it.

Usage:
    python scripts/inject_context.py --case CASE-6A4588BF-2026 \
        --text "Confirmed with the practice: this optometrist sees mostly \
established patients; annual-exam billing is normal for the specialty."
    python scripts/inject_context.py --case CASE-XXXX-2026 \
        --text "..." --source "phone call with compliance officer" --actor human
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "fraud-investigator-v2" / "src"
sys.path.insert(0, str(_SRC))

from fraud_investigator.human_context import HUMAN_CONTEXT_FILENAME  # noqa: E402
from fraud_investigator.state import STATE_ROOT_DEFAULT  # noqa: E402


def inject(
    case_id: str,
    text: str,
    *,
    source: str | None = None,
    actor: str = "human",
    state_root: Path = STATE_ROOT_DEFAULT,
) -> dict:
    """Append one human-context line to the case's inbox.

    Raises ``SystemExit`` if the case directory doesn't already exist (this
    tool injects into a LIVE case; it must not silently create a new one
    from a typo'd --case), or if --text is empty or too short. The 10-char
    floor mirrors EvidenceItem.fact's min_length (schemas.py) — fail loudly
    here at injection time rather than deep in the orchestrator's next
    iteration when the note is converted to evidence.
    """
    text = (text or "").strip()
    if len(text) < 10:
        raise SystemExit("--text must be a substantive note (>= 10 chars).")

    case_path = Path(state_root) / case_id
    if not case_path.is_dir():
        raise SystemExit(f"no such case: {case_id} (expected directory {case_path})")

    entry = {
        "id": f"hc-{uuid.uuid4().hex[:8]}",
        "ts": datetime.now(timezone.utc).isoformat(),
        "actor": actor,
        "text": text,
        "source": source or "operator",
    }
    inbox = case_path / HUMAN_CONTEXT_FILENAME
    with inbox.open("a") as f:
        f.write(json.dumps(entry) + "\n")
    return {"case_id": case_id, "path": str(inbox), **entry}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--case", required=True, dest="case_id", help="Case id, e.g. CASE-6A4588BF-2026")
    ap.add_argument("--text", required=True, help="The note to inject (>=1 char, non-empty).")
    ap.add_argument("--source", default=None, help="Where this note came from (free text; default 'operator').")
    ap.add_argument("--actor", default="human", help="Wbirch is injecting this note (default 'human').")
    ap.add_argument("--state-root", default=str(STATE_ROOT_DEFAULT),
                     help="NeedlePoint state root (default: fraud-investigator-v2/state).")
    args = ap.parse_args(argv)

    info = inject(
        args.case_id, args.text,
        source=args.source, actor=args.actor,
        state_root=Path(args.state_root),
    )
    print(f"✓ appended {info['id']} to {info['path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
