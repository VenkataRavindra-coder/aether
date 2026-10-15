"""deploy/aether-health.sh must restart the engine only when the API gives no HTTP response.
2026-10-15: the first version treated the login middleware's 401 as dead and restarted the
engine every ~90 s. Runs the real script with fake curl/systemctl on PATH."""
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "aether-health.sh"


def run_with(tmp_path, http_code, started_secs_ago=600):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    log = tmp_path / "systemctl.log"
    uptime = 100000
    started_us = (uptime - started_secs_ago) * 1_000_000
    # Behaves like real curl: with -f, an HTTP status >= 400 exits 22; no connection exits 7.
    (bin_ / "curl").write_text(
        "#!/bin/sh\n"
        f"code='{http_code}'\n"
        "case \" $* \" in *' -w '*) printf '%s' \"${code:-000}\";; esac\n"
        "if [ -z \"$code\" ] || [ \"$code\" = 000 ]; then exit 7; fi\n"
        "case \" $* \" in *' -sf '*|*' -f '*) [ \"$code\" -ge 400 ] && exit 22;; esac\n"
        "exit 0\n")
    (bin_ / "systemctl").write_text(
        "#!/bin/sh\n"
        f"if [ \"$1\" = show ]; then echo {started_us}; exit 0; fi\n"
        f"echo \"$@\" >> {log}\n")
    (bin_ / "logger").write_text("#!/bin/sh\nexit 0\n")
    (bin_ / "cut").write_text(f"#!/bin/sh\necho {uptime}\n")
    for f in bin_.iterdir():
        f.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}", "AETHER_SERVICE": "ratelark.service"}
    subprocess.run(["bash", str(SCRIPT)], env=env, check=True)
    return log.read_text() if log.exists() else ""


@pytest.mark.parametrize("code", ["200", "303", "401", "403", "500"])
def test_any_http_response_is_alive(tmp_path, code):
    assert run_with(tmp_path, code) == ""


@pytest.mark.parametrize("code", ["000", ""])
def test_no_response_restarts_the_configured_service(tmp_path, code):
    assert run_with(tmp_path, code).strip() == "restart ratelark.service"


def test_grace_period_after_start(tmp_path):
    assert run_with(tmp_path, "000", started_secs_ago=30) == ""
