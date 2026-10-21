"""Guard (2026-10-21): the repo's .gitignore is a whitelist, so a new package, page or deploy unit
can silently never reach GitHub (and the server). Every untracked code/page/unit file must not be
ignored (files already tracked, e.g. force-added older units, are fine)."""
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SKIP = {".venv", "__pycache__", ".pytest_cache", ".hypothesis", "backups", "node_modules", ".git"}


def _candidates():
    for p in ROOT.rglob("*"):
        if p.is_file() and not (set(p.relative_to(ROOT).parts) & SKIP) and (
                p.suffix in (".py", ".html") or (p.parent.name == "deploy" and p.suffix in (".service", ".timer"))):
            yield p


def test_no_code_page_or_unit_is_gitignored():
    if not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    files = [str(p.relative_to(ROOT)) for p in _candidates()]
    r = subprocess.run(["git", "check-ignore", *files], cwd=ROOT, capture_output=True, text=True)
    ignored = [x for x in r.stdout.splitlines() if x.strip()]
    assert not ignored, f"ignored by the .gitignore whitelist (add a ! rule): {ignored}"
