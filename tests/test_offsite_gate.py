"""Offsite via the Mac: the gate script only accepts receipts for real archives; the checks read the receipt."""
import os
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "deploy" / "offsite_gate.sh"
NAME = "aether-20261018T031718Z.tar.gz"


def run_gate(tmp_path, cmd):
    (tmp_path / "backups").mkdir(exist_ok=True)
    env = {**os.environ, "AETHER_BACKUP_DIR": str(tmp_path / "backups"), "AETHER_RECEIPT": str(tmp_path / ".offsite_receipt"),
           "SSH_ORIGINAL_COMMAND": cmd}
    return subprocess.run(["bash", str(GATE)], env=env, capture_output=True, text=True)


def test_receipt_accepted_only_for_a_real_archive(tmp_path):
    (tmp_path / "backups").mkdir()
    assert run_gate(tmp_path, f"receipt {NAME}").returncode == 1          # archive does not exist: refused
    assert not (tmp_path / ".offsite_receipt").exists()
    (tmp_path / "backups" / NAME).write_bytes(b"x")
    r = run_gate(tmp_path, f"receipt {NAME}")
    assert r.returncode == 0 and (tmp_path / ".offsite_receipt").read_text().split()[0] == NAME


def test_gate_refuses_everything_else(tmp_path):
    (tmp_path / "backups").mkdir()
    for cmd in ("", "bash", "cat /etc/passwd", "receipt ../../etc/passwd", f"receipt {NAME}; id", "receipt aether-x.tar.gz"):
        assert run_gate(tmp_path, cmd).returncode == 1, cmd


def test_rd3_and_status_use_the_receipt(tmp_path, monkeypatch):
    import aether_daily as d
    import map_status as ms
    (tmp_path / "backups").mkdir()
    arc = tmp_path / "backups" / NAME
    arc.write_bytes(b"x")
    (tmp_path / ".offsite_receipt").write_text(f"{NAME} {int(time.time()) - 3600} icloud\n")
    r = d.check_rd3(home=tmp_path, run=lambda *a, **k: (0, ""))
    assert r["ok"] if "ok" in r else r["status"] in ("pass", "ok", True)
    assert ms._icloud_target(tmp_path, str(arc)) == "iCloud (via Mac)"
    (tmp_path / ".offsite_receipt").write_text("aether-20261017T031718Z.tar.gz %d icloud\n" % (time.time() - 90000))
    assert "behind" in ms._icloud_target(tmp_path, str(arc))
    r2 = d.check_rd3(home=tmp_path, run=lambda *a, **k: (0, ""))
    assert not (r2["ok"] if "ok" in r2 else r2["status"] in ("pass", "ok", True))
    (tmp_path / ".offsite_receipt").unlink()
    assert ms._icloud_target(tmp_path, str(arc)) is None


def test_list_and_get_are_limited_to_real_archives(tmp_path):
    (tmp_path / "backups").mkdir()
    (tmp_path / "backups" / NAME).write_bytes(b"hello")
    (tmp_path / "backups" / "secret.txt").write_bytes(b"nope")
    r = run_gate(tmp_path, "list")
    assert r.returncode == 0 and r.stdout.split() == [NAME, "5"]
    assert run_gate(tmp_path, f"get {NAME}").stdout == "hello"
    for cmd in ("get secret.txt", "get ../backups/" + NAME, f"get {NAME} /etc/passwd", "get "):
        assert run_gate(tmp_path, cmd).returncode == 1, cmd
