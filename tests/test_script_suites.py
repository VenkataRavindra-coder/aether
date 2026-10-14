"""Run each script-style suite in its own process, with a clean environment
(no AETHER_/RISK_ settings leaked from other tests), exactly as if run alone.
It must exit 0 and actually report results."""
import os
import subprocess
import tempfile
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from conftest import SCRIPT_TESTS  # noqa: E402

_LEAKY = ("AETHER_", "RISK_")


@pytest.mark.parametrize("script", SCRIPT_TESTS)
def test_script_suite(script):
    env = {k: v for k, v in os.environ.items() if not k.startswith(_LEAKY)}
    # Stripping AETHER_* would let load_dotenv() restore the server's REAL halt path; pin a
    # throwaway one instead (suites that set their own path still override it).
    env["AETHER_HALT_FILE"] = os.path.join(tempfile.mkdtemp(prefix="aether-script-"), "AETHER_HALT")
    r = subprocess.run(
        [sys.executable, str(ROOT / script)],
        cwd=(ROOT / script).parent, env=env,
        capture_output=True, text=True, timeout=300,
    )
    out = r.stdout + r.stderr
    assert r.returncode == 0, f"{script} failed:\n{r.stdout[-3000:]}\n{r.stderr[-2000:]}"
    assert "PASS" in out or "passed" in out.lower(), f"{script} printed no results:\n{out[-1500:]}"
