"""Shared helpers for agent modules.

Every agent module exposes a single ``run(...)`` function that:
  1. Loads its prompt template from ``prompts/{name}.md``.
  2. Renders the template with the call's inputs.
  3. Calls ``claude_p.invoke_agent`` with the relevant Pydantic schema.
  4. Returns the validated model.
"""
from __future__ import annotations

from pathlib import Path

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"


def load_prompt(name: str) -> str:
    """Load a prompt template by name (e.g., 'investigator', 'counsel_build')."""
    path = PROMPT_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(
            f"Prompt template not found: {path}\n"
            f"Expected one of: {sorted(p.stem for p in PROMPT_DIR.glob('*.md'))}"
        )
    return path.read_text()


def render(template: str, **fields: object) -> str:
    """Fill {{placeholders}} in the template. Missing keys raise KeyError to
    catch typos early."""
    out = template
    for key, val in fields.items():
        token = "{{" + key + "}}"
        if token not in out:
            # Allow templates to omit fields explicitly; just log to stderr.
            continue
        out = out.replace(token, str(val))
    # Sanity: any remaining {{...}} placeholders are a bug
    import re
    leftover = re.findall(r"\{\{[a-zA-Z0-9_]+\}\}", out)
    if leftover:
        raise KeyError(f"Unfilled placeholders in prompt: {leftover}")
    return out
