"""aether_ops.load_env must find the repo's .env/.env.live without AETHER_HOME set (interactive shell)."""
import os
from pathlib import Path

import aether_ops
import safety_guard


def test_load_env_reads_repo_folder_first(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".env").write_text("AETHER_HOME=/srv/aether\nX_ONLY_IN_ENV=1\n")
    (repo / ".env.live").write_text("X_LIVE_KEY=abc\n")
    monkeypatch.setattr(aether_ops, "__file__", str(repo / "aether_ops.py"))
    for k in ("AETHER_HOME", "X_ONLY_IN_ENV", "X_LIVE_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "nowhere")
    aether_ops.load_env()
    assert os.environ["X_LIVE_KEY"] == "abc" and os.environ["X_ONLY_IN_ENV"] == "1"
    assert str(safety_guard.home()) == "/srv/aether"  # ledger path now matches the engine's


def test_existing_environment_wins(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".env").write_text("X_KEEP=from_file\n")
    monkeypatch.setattr(aether_ops, "__file__", str(repo / "aether_ops.py"))
    monkeypatch.setenv("X_KEEP", "from_env")
    aether_ops.load_env()
    assert os.environ["X_KEEP"] == "from_env"
