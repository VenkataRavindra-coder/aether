"""Run each script-style suite in its own process; it must exit 0
and actually report results (so a misdetected file can't pass silently)."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from conftest import SCRIPT_TESTS  # noqa: E402


@pytest.mark.parametrize("script", SCRIPT_TESTS)
def test_script_suite(script):
    r = subprocess.run(
        [sys.executable, str(ROOT / script)],
        cwd=(ROOT / script).parent, env=os.environ.copy(),
        capture_output=True, text=True, timeout=300,
    )
    out = r.stdout + r.stderr
    assert r.returncode == 0, f"{script} failed:\n{r.stdout[-3000:]}\n{r.stderr[-2000:]}"
    assert "PASS" in out or "passed" in out.lower(), f"{script} printed no results:\n{out[-1500:]}"
