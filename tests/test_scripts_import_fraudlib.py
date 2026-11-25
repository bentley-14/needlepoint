"""Agent tools run as `python3 scripts/<tool>.py`, which puts scripts/ (not the repo root) on
sys.path. A tool that imports fraudlib without adding the repo root silently falls back to
local-only data and never reads the bucket or the public dataset."""
import re
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
IMPORTS_FRAUDLIB = re.compile(r"^\s*(from fraudlib\b|import fraudlib\b)", re.M)


def test_top_level_scripts_that_import_fraudlib_add_the_repo_root():
    missing = [p.name for p in sorted(SCRIPTS.glob("*.py"))
               if IMPORTS_FRAUDLIB.search(p.read_text()) and "sys.path.insert" not in p.read_text()]
    assert not missing, f"add the repo root to sys.path before importing fraudlib: {missing}"
