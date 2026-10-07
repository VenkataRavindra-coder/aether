"""Tests for aether_daily.py (plan 11a/11b). Fakes only: no network, no broker, no Telegram, no systemd.

Run from the repo root:  venv/bin/python3 -m pytest -q tests/test_daily_runner.py
"""
import datetime
import json
import os
import pathlib
import random
import sqlite3
import subprocess
import sys
import tarfile
import time
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import pytest

import aether_daily as ad

ROOT = pathlib.Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------- helpers
def make_archive(home, ts="20261007T031500Z", age_h=1.0, state=None, extra=None, drop=(), corrupt_db=False,
                 evidence=False, traversal=False):
    home = pathlib.Path(home)
    work = home / "stage" / ts
    work.mkdir(parents=True, exist_ok=True)
    for name in ("aether_decisions.sqlite", "research.sqlite"):
        con = sqlite3.connect(work / name)
        con.execute("create table t (a)")
        con.execute("insert into t values (1)")
        con.commit()
        con.close()
    if corrupt_db:
        (work / "research.sqlite").write_bytes(b"this is not a sqlite database" * 50)
    (work / "aether_state.json").write_text(json.dumps(state if state is not None else {"us_paper": {}, "us_live": {}}))
    (work / ".aether_risk_state.json").write_text("{}")
    (work / "git_commit.txt").write_text("abc1234\n")
    for name, body in (extra or {}).items():
        (work / name).write_text(body)
    if evidence:
        (work / "evidence" / "daily").mkdir(parents=True, exist_ok=True)
        (work / "evidence" / "daily" / "x.json").write_text("{}")
    for name in drop:
        (work / name).unlink()
    out = home / "backups"
    out.mkdir(exist_ok=True)
    path = out / f"aether-{ts}.tar.gz"
    with tarfile.open(path, "w:gz") as tf:
        tf.add(work, arcname=ts)
        if traversal:
            evil = home / "evil.txt"
            evil.write_text("x")
            tf.add(evil, arcname="../evil.txt")
    t = time.time() - age_h * 3600
    os.utime(path, (t, t))
    return path


def fake_run(table):
    """table: list of (substring of the command, (rc, output))."""
    def _run(cmd, timeout=900, cwd=None):
        line = " ".join(str(c) for c in cmd)
        for key, val in table:
            if key in line:
                return val
        return 0, ""
    return _run


# ---------------------------------------------------------------- RD1
def test_rd1_fresh_archive_passes(tmp_path):
    make_archive(tmp_path)
    assert ad.check_rd1(tmp_path)["ok"] is True


def test_rd1_no_archives_fails(tmp_path):
    assert ad.check_rd1(tmp_path)["ok"] is False


def test_rd1_stale_archive_fails(tmp_path):
    make_archive(tmp_path, age_h=30)
    r = ad.check_rd1(tmp_path)
    assert r["ok"] is False and "old" in r["detail"]


def test_rd1_size_anomaly_fails(tmp_path):
    for i in range(4):
        rng = random.Random(i)  # one generator per archive: incompressible padding
        make_archive(tmp_path, ts=f"2026100{i}T031500Z", age_h=30 + i,
                     extra={"pad.txt": "".join(rng.choice("abcdefghij") for _ in range(30000))})
    make_archive(tmp_path, ts="20261007T031500Z", age_h=1)  # much smaller than the others
    r = ad.check_rd1(tmp_path)
    assert r["ok"] is False and "median" in r["detail"]


def test_rd1_too_many_archives_fails(tmp_path):
    for i in range(15):
        make_archive(tmp_path, ts=f"202610{i + 10:02d}T031500Z", age_h=1 + i)
    assert ad.check_rd1(tmp_path)["ok"] is False


# ---------------------------------------------------------------- RD2
def test_rd2_valid_archive_restores(tmp_path):
    make_archive(tmp_path)
    assert ad.check_rd2(tmp_path)["ok"] is True


def test_rd2_corrupt_sqlite_fails(tmp_path):
    make_archive(tmp_path, corrupt_db=True)
    r = ad.check_rd2(tmp_path)
    assert r["ok"] is False and "research.sqlite" in r["detail"]


def test_rd2_state_without_us_keys_fails(tmp_path):
    make_archive(tmp_path, state={"positions": {}})
    assert ad.check_rd2(tmp_path)["ok"] is False


def test_rd2_unparseable_state_fails(tmp_path):
    p = make_archive(tmp_path)
    # rebuild with garbage state
    make_archive(tmp_path, ts="20261008T031500Z", state=None)
    work = tmp_path / "stage" / "20261008T031500Z"
    (work / "aether_state.json").write_text("{not json")
    with tarfile.open(tmp_path / "backups" / "aether-20261008T031500Z.tar.gz", "w:gz") as tf:
        tf.add(work, arcname="20261008T031500Z")
    assert ad.check_rd2(tmp_path)["ok"] is False


def test_rd2_missing_git_commit_fails(tmp_path):
    make_archive(tmp_path, drop=("git_commit.txt",))
    assert ad.check_rd2(tmp_path)["ok"] is False


def test_rd2_path_traversal_member_is_rejected_not_extracted(tmp_path):
    make_archive(tmp_path, traversal=True)
    r = ad.check_rd2(tmp_path)
    assert r["ok"] is False and "unsafe" in r["detail"]
    assert not (tmp_path.parent / "evil.txt").exists()


def test_rd2_leaves_nothing_behind(tmp_path):
    make_archive(tmp_path)
    before = sorted(p.name for p in tmp_path.iterdir())
    ad.check_rd2(tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == before


# ---------------------------------------------------------------- RD3
def test_rd3_upload_line_present(tmp_path):
    p = make_archive(tmp_path)
    run = fake_run([("journalctl", (0, f"x backup.sh[1]: uploaded {p.name} offsite\nbackup ok"))])
    assert ad.check_rd3(tmp_path, run)["ok"] is True


def test_rd3_upload_line_missing(tmp_path):
    make_archive(tmp_path)
    run = fake_run([("journalctl", (0, "backup ok: something else"))])
    assert ad.check_rd3(tmp_path, run)["ok"] is False


def test_rd3_upload_of_an_older_archive_does_not_count(tmp_path):
    make_archive(tmp_path)
    run = fake_run([("journalctl", (0, "uploaded aether-20250101T000000Z.tar.gz offsite"))])
    assert ad.check_rd3(tmp_path, run)["ok"] is False


# ---------------------------------------------------------------- RD4
GOOD_SHOW = (0, "Restart=always\nWatchdogUSec=3min\n")


def rd4_run(show=GOOD_SHOW, active="active"):
    return fake_run([("systemctl show", show), ("is-active", (0, active + "\n"))])


def test_rd4_all_good(tmp_path):
    (tmp_path / ".env").write_text("X=1\nHEALTHCHECK_URL=https://hc.example/abc\n")
    assert ad.check_rd4(tmp_path, rd4_run())["ok"] is True


@pytest.mark.parametrize("show,env,active,word", [
    ((0, "Restart=on-failure\nWatchdogUSec=3min\n"), "HEALTHCHECK_URL=u\n", "active", "Restart"),
    ((0, "Restart=always\nWatchdogUSec=0\n"), "HEALTHCHECK_URL=u\n", "active", "Watchdog"),
    (GOOD_SHOW, "X=1\n", "active", "HEALTHCHECK_URL"),
    (GOOD_SHOW, "HEALTHCHECK_URL=\n", "active", "HEALTHCHECK_URL"),
    (GOOD_SHOW, "HEALTHCHECK_URL=u\n", "inactive", "timer"),
])
def test_rd4_each_problem_is_caught(tmp_path, show, env, active, word):
    (tmp_path / ".env").write_text(env)
    r = ad.check_rd4(tmp_path, rd4_run(show, active))
    assert r["ok"] is False and word in r["detail"]


# ---------------------------------------------------------------- RD5
def test_rd5_all_files_present(tmp_path):
    make_archive(tmp_path)
    assert ad.check_rd5(tmp_path)["ok"] is True


def test_rd5_missing_file_fails(tmp_path):
    make_archive(tmp_path, drop=(".aether_risk_state.json",))
    r = ad.check_rd5(tmp_path)
    assert r["ok"] is False and ".aether_risk_state.json" in r["detail"]


def test_rd5_live_ledger_required_once_it_exists(tmp_path):
    make_archive(tmp_path)
    (tmp_path / "live_risk_ledger.json").write_text("{}")
    r = ad.check_rd5(tmp_path)
    assert r["ok"] is False and "live_risk_ledger.json" in r["detail"]


def test_rd5_ledger_in_archive_passes(tmp_path):
    make_archive(tmp_path, extra={"live_risk_ledger.json": "{}"})
    (tmp_path / "live_risk_ledger.json").write_text("{}")
    assert ad.check_rd5(tmp_path)["ok"] is True


def test_rd5_evidence_required_once_the_folder_exists(tmp_path):
    make_archive(tmp_path)
    (tmp_path / "tests" / "evidence").mkdir(parents=True)
    assert ad.check_rd5(tmp_path)["ok"] is False
    make_archive(tmp_path, ts="20261008T031500Z", evidence=True, age_h=0.5)
    assert ad.check_rd5(tmp_path)["ok"] is True


# ---------------------------------------------------------------- A1
def test_a1_all_suites_pass(tmp_path):
    run = fake_run([("pytest", (0, "75 passed in 2s")), ("test_uslive", (0, "ALL PASSED")), ("halt", (0, "8/8 passed"))])
    assert ad.check_a1(tmp_path, run)["ok"] is True


def test_a1_names_the_failing_suite(tmp_path):
    run = fake_run([("test_uslive", (1, "TOTAL FAIL: 1")), ("pytest", (0, "ok"))])
    r = ad.check_a1(tmp_path, run)
    assert r["ok"] is False and "test_uslive: rc=1" in r["detail"]


# ---------------------------------------------------------------- A2 (uses the real us_strategy; engine functions from the repo or a stub)
def weekday_dates(end, n=420):
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= datetime.timedelta(days=1)
    return list(reversed(out))


def universe(end):
    import us_strategy as us
    data = {}
    for i, s in enumerate(us.UNIVERSE):
        rng, c, bars = random.Random(i + 1), 100.0, []
        for d in weekday_dates(end):
            c *= 1 + rng.gauss(0.0003, 0.01)
            bars.append({"d": d.isoformat(), "c": c})
        data[s] = bars
    return data


def test_a2_good_data(tmp_path):
    data = universe(datetime.date(2026, 6, 30))
    r = ad.check_a2(fetch=lambda syms: data, today="2026-07-15")
    assert r["ok"] is True and "signal_date=2026-06-30" in r["detail"]


def test_a2_stale_data_raises(tmp_path):
    data = universe(datetime.date(2026, 6, 12))
    with pytest.raises(ValueError):
        ad.check_a2(fetch=lambda syms: data, today="2026-07-15")


# ---------------------------------------------------------------- A3
def test_compare_holdings():
    uni = {"SPY", "QQQ"}
    assert ad.compare_holdings({"SPY": 2.0}, {"SPY": 2.0}, uni) == []
    assert ad.compare_holdings({"SPY": 2.0}, {"SPY": 2.5}, uni)
    assert ad.compare_holdings({}, {"QQQ": 1.0}, uni)
    assert ad.compare_holdings({"SPY": 1.0}, {"SPY": 1.0, "AAPL": 9.0}, uni) == []  # not a strategy ETF
    assert ad.compare_holdings({"SPY": 1.0}, {"SPY": 1.0000000001}, uni) == []


class FakeBroker:
    def __init__(self, pos, open_syms):
        self.pos, self.open = pos, open_syms

    def get_positions_map(self):
        return self.pos

    def open_order_symbols(self):
        return self.open


def a3_home(tmp_path, holdings):
    import aether_engine
    (tmp_path / "aether_state.json").write_text(json.dumps({aether_engine._us_state_key(): {"holdings": holdings}}))
    return tmp_path


def test_a3_match(tmp_path):
    home = a3_home(tmp_path, {"SPY": 2.0})
    assert ad.check_a3(home, FakeBroker({"SPY": 2.0, "AAPL": 5.0}, set()))["ok"] is True


def test_a3_mismatch(tmp_path):
    home = a3_home(tmp_path, {"SPY": 2.0})
    r = ad.check_a3(home, FakeBroker({"SPY": 1.0}, set()))
    assert r["ok"] is False and "SPY" in r["detail"]


def test_a3_open_orders_on_strategy_etfs_fail(tmp_path):
    home = a3_home(tmp_path, {"SPY": 2.0})
    assert ad.check_a3(home, FakeBroker({"SPY": 2.0}, {"SPY"}))["ok"] is False


def test_a3_unreadable_broker_fails(tmp_path):
    home = a3_home(tmp_path, {})
    assert ad.check_a3(home, FakeBroker(None, set()))["ok"] is False
    assert ad.check_a3(home, FakeBroker({}, None))["ok"] is False


# ---------------------------------------------------------------- A4 (a real throwaway git repo)
def git(repo, *args):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", *args], cwd=repo, env=env, capture_output=True, text=True, check=True).stdout.strip()


def commit(repo, files, msg):
    for name, body in files.items():
        p = pathlib.Path(repo) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg)
    return git(repo, "rev-parse", "--short", "HEAD")


def a4_repo(tmp_path, code_version=None):
    git(tmp_path, "init", "-q")
    cv = commit(tmp_path, {"engine.py": "x = 1\n", "docs/readme.md": "a\n"}, "base")
    db = tmp_path / "aether_decisions.sqlite"
    con = sqlite3.connect(db)
    con.execute("create table decisions (id integer primary key, engine text, code_version text)")
    con.execute("insert into decisions (engine, code_version) values ('us', ?)", (code_version or cv,))
    con.commit()
    con.close()
    (tmp_path / ".gitignore").write_text("*.sqlite\n")
    commit(tmp_path, {".gitignore": "*.sqlite\n"}, "ignore db")  # a non-code commit after the running version
    return cv


def test_a4_clean_and_matching(tmp_path):
    a4_repo(tmp_path)
    assert ad.check_a4(tmp_path)["ok"] is True


@pytest.mark.parametrize("files", [
    {"docs/readme.md": "changed\n"},
    {"tests/test_x.py": "pass\n"},
    {"aether_daily.py": "pass\n"},
    {"stress_test.py": "pass\n"},
    {"test_something.py": "pass\n"},
    {"aether-research/r.py": "pass\n"},
])
def test_a4_non_engine_commits_are_not_a_mismatch(tmp_path, files):
    a4_repo(tmp_path)
    commit(tmp_path, files, "non-engine change")
    assert ad.check_a4(tmp_path)["ok"] is True


def test_a4_engine_code_change_is_a_mismatch(tmp_path):
    a4_repo(tmp_path)
    commit(tmp_path, {"engine.py": "x = 2\n"}, "engine change")
    r = ad.check_a4(tmp_path)
    assert r["ok"] is False and "differs" in r["detail"]


def test_a4_dirty_tree_fails(tmp_path):
    a4_repo(tmp_path)
    (tmp_path / "engine.py").write_text("x = 99\n")
    r = ad.check_a4(tmp_path)
    assert r["ok"] is False and "not clean" in r["detail"]


def test_a4_untracked_file_fails(tmp_path):
    a4_repo(tmp_path)
    (tmp_path / "new_module.py").write_text("pass\n")
    assert ad.check_a4(tmp_path)["ok"] is False


def test_a4_no_code_version_fails(tmp_path):
    a4_repo(tmp_path)
    con = sqlite3.connect(tmp_path / "aether_decisions.sqlite")
    con.execute("delete from decisions")
    con.commit()
    con.close()
    assert ad.check_a4(tmp_path)["ok"] is False


# ---------------------------------------------------------------- A5 (reads the stress timer's record)
NOW = datetime.datetime(2026, 10, 7, 12, 0, 0, tzinfo=datetime.timezone.utc)


def stress_record(home, rc, hours_ago, raw=None):
    when = (NOW - datetime.timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    (pathlib.Path(home) / ".stress_last_run.json").write_text(raw if raw is not None else json.dumps({"rc": rc, "utc": when}))


def test_a5_no_record_is_neither_pass_nor_fail(tmp_path):
    r = ad.check_a5(tmp_path, NOW)
    assert r["ok"] is None and "NO STRESS RUN" in r["detail"]


def test_a5_recent_success_passes(tmp_path):
    stress_record(tmp_path, 0, 20)
    assert ad.check_a5(tmp_path, NOW)["ok"] is True


def test_a5_failed_run_fails(tmp_path):
    stress_record(tmp_path, 1, 2)
    r = ad.check_a5(tmp_path, NOW)
    assert r["ok"] is False and "failed" in r["detail"]


def test_a5_old_success_fails_but_a_long_weekend_does_not(tmp_path):
    stress_record(tmp_path, 0, 90)   # Friday run seen on Tuesday morning
    assert ad.check_a5(tmp_path, NOW)["ok"] is True
    stress_record(tmp_path, 0, 100)
    r = ad.check_a5(tmp_path, NOW)
    assert r["ok"] is False and "limit" in r["detail"]


@pytest.mark.parametrize("raw", ["{not json", "{}", '{"rc": "x", "utc": "2026-10-07T00:00:00Z"}',
                                 '{"rc": 0, "utc": "yesterday"}'])
def test_a5_unreadable_record_fails(tmp_path, raw):
    stress_record(tmp_path, 0, 1, raw=raw)
    assert ad.check_a5(tmp_path, NOW)["ok"] is False


def test_a5_timestamp_in_the_future_fails(tmp_path):
    stress_record(tmp_path, 0, -5)
    assert ad.check_a5(tmp_path, NOW)["ok"] is False


# ---------------------------------------------------------------- orchestration
def ok(cid):
    return lambda: ad.res(cid, True, "fine")


def test_a_crashing_check_is_recorded_and_the_rest_still_run():
    def boom():
        raise RuntimeError("kaput")
    results = ad.run_all([("X1", boom), ("X2", ok("X2"))])
    assert results[0]["ok"] is False and "kaput" in results[0]["detail"]
    assert results[1]["ok"] is True


def test_summary_text_variants():
    alert, text = ad.summary_text([ad.res("A1", True, "x")], "abcdef123", "2026-10-07")
    assert alert is False and "all checks green" in text
    alert, text = ad.summary_text([ad.res("A1", True, "x"), ad.res("A5", None, "n")], "abcdef123", "2026-10-07")
    assert alert is False and "not enabled: A5" in text
    alert, text = ad.summary_text([ad.res("A3", False, "SPY: state 1 vs broker 2")], "abcdef123", "2026-10-07")
    assert alert is True and "A3" in text and "SPY" in text


def test_evidence_file_and_second_run_naming(tmp_path):
    now = datetime.datetime(2026, 10, 7, 4, 30, 0, tzinfo=datetime.timezone.utc)
    r = [ad.res("A1", True, "x")]
    p1 = ad.write_evidence(r, "abc", tmp_path, now)
    p2 = ad.write_evidence(r, "abc", tmp_path, now)
    assert p1.name == "2026-10-07.json" and p2.name == "2026-10-07-043000.json" and p1 != p2
    body = json.loads(p1.read_text())
    assert body["head"] == "abc" and body["all_green"] is True and len(body["sha256"]) == 64
    p3 = ad.write_evidence([ad.res("A5", None, "off")], "abc", tmp_path, now.replace(day=8))
    assert json.loads(p3.read_text())["all_green"] is False  # not enabled is not green


def test_ping_variants():
    assert ad.ping(None, False) is None
    seen = []

    class R:
        def read(self):
            return b"OK"
    assert ad.ping("https://hc/x/", False, opener=lambda u, timeout: (seen.append(u), R())[1]) is True
    assert ad.ping("https://hc/x", True, opener=lambda u, timeout: (seen.append(u), R())[1]) is True
    assert seen == ["https://hc/x", "https://hc/x/fail"]

    def raiser(u, timeout):
        raise OSError("down")
    assert ad.ping("https://hc/x", False, opener=raiser) is False


def run_main(tmp_path, checks, monkeypatch, send=None, ping_fn=None, url="https://hc/daily"):
    if url:
        monkeypatch.setenv("DAILY_HEALTHCHECK_URL", url)
    else:
        monkeypatch.delenv("DAILY_HEALTHCHECK_URL", raising=False)
    calls = []
    send = send or (lambda text, alert: calls.append((alert, text)))
    code = ad.main(home=tmp_path, checks=checks, send=send, ping_fn=ping_fn or (lambda u, f: True))
    return code, calls


def test_main_all_green_returns_zero_and_writes_evidence(tmp_path, monkeypatch):
    code, calls = run_main(tmp_path, [("A1", ok("A1"))], monkeypatch)
    assert code == 0 and calls[0][0] is False
    files = list((tmp_path / "tests" / "evidence" / "daily").glob("*.json"))
    assert len(files) == 1
    body = json.loads(files[0].read_text())
    assert [r["id"] for r in body["results"]] == ["A1", "A6"] and body["all_green"] is True


def test_main_failure_returns_one_and_alerts(tmp_path, monkeypatch):
    code, calls = run_main(tmp_path, [("A1", lambda: ad.res("A1", False, "bad"))], monkeypatch)
    assert code == 1 and calls[0][0] is True


def test_main_not_enabled_check_does_not_fail_the_run(tmp_path, monkeypatch):
    code, _ = run_main(tmp_path, [("A5", lambda: ad.res("A5", None, "off"))], monkeypatch)
    assert code == 0


def test_main_telegram_failure_fails_a6(tmp_path, monkeypatch):
    def boom(text, alert):
        raise OSError("telegram down")
    code, _ = run_main(tmp_path, [("A1", ok("A1"))], monkeypatch, send=boom)
    assert code == 1


def test_main_failed_ping_fails_a6(tmp_path, monkeypatch):
    code, _ = run_main(tmp_path, [("A1", ok("A1"))], monkeypatch, ping_fn=lambda u, f: False)
    assert code == 1


def test_main_without_a_daily_url_is_ok_but_says_so(tmp_path, monkeypatch):
    code, _ = run_main(tmp_path, [("A1", ok("A1"))], monkeypatch, ping_fn=lambda u, f: None, url=None)
    assert code == 0
    body = json.loads(next((tmp_path / "tests" / "evidence" / "daily").glob("*.json")).read_text())
    assert "DAILY_HEALTHCHECK_URL" in body["results"][-1]["detail"]


def test_main_sends_the_failure_ping_when_something_failed(tmp_path, monkeypatch):
    seen = []
    run_main(tmp_path, [("A1", lambda: ad.res("A1", False, "bad"))], monkeypatch,
             ping_fn=lambda u, failed: seen.append(failed) or True)
    assert seen == [True]


# ---------------------------------------------------------------- the runner must stay read-only
def test_runner_source_has_no_forbidden_actions():
    src = (ROOT / "aether_daily.py").read_text()
    for forbidden in ("AETHER_HALT", "systemctl restart", "systemctl stop", "systemctl start", '"restart"',
                      '"stop"', "APCA_LIVE", "submit_order", "submit_market_order", "execute_order",
                      "cancel_order", "shutil.rmtree", "os.remove", "unlink("):
        assert forbidden not in src, forbidden


def test_all_green_requires_every_check_to_be_green(tmp_path):
    now = datetime.datetime(2026, 10, 9, 4, 30, 0, tzinfo=datetime.timezone.utc)
    mixed_fail = [ad.res("A1", True, "x"), ad.res("A2", False, "y")]
    mixed_off = [ad.res("A1", True, "x"), ad.res("A5", None, "off")]
    assert json.loads(ad.write_evidence(mixed_fail, "abc", tmp_path, now).read_text())["all_green"] is False
    assert json.loads(ad.write_evidence(mixed_off, "abc", tmp_path, now.replace(day=10)).read_text())["all_green"] is False


# ---------------------------------------------------------------- Telegram delivery (A6 must not claim what it cannot prove)
import io
import urllib.error as _uerr

TOKEN, CHAT = "123456:SECRET-TOKEN-VALUE", "987654321"


class FakeReply:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return self.payload if isinstance(self.payload, bytes) else json.dumps(self.payload).encode()


def opener_returning(payload, seen=None):
    def _open(req, timeout=None):
        if seen is not None:
            seen.append((req.full_url, req.data, timeout))
        return FakeReply(payload)
    return _open


def test_telegram_send_returns_the_message_id_only_when_telegram_says_ok():
    seen = []
    mid = ad.telegram_send("hello", TOKEN, CHAT, opener_returning({"ok": True, "result": {"message_id": 77}}, seen))
    assert mid == 77
    url, data, timeout = seen[0]
    assert url == f"https://api.telegram.org/bot{TOKEN}/sendMessage" and timeout == 15
    assert b"chat_id=987654321" in data and b"text=hello" in data


@pytest.mark.parametrize("payload", [{"ok": False, "description": "Forbidden: bot was blocked"}, {}, [], b"not json", b""])
def test_a_reply_that_is_not_ok_is_a_failure(payload):
    with pytest.raises(RuntimeError):
        ad.telegram_send("hello", TOKEN, CHAT, opener_returning(payload))


def test_network_and_http_errors_are_failures_and_never_leak_the_token():
    def refuse(req, timeout=None):
        raise _uerr.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b""))

    def down(req, timeout=None):
        raise OSError(f"cannot reach {req.full_url}")
    for opener in (refuse, down):
        with pytest.raises(RuntimeError) as e:
            ad.telegram_send("hello", TOKEN, CHAT, opener)
        assert "SECRET-TOKEN-VALUE" not in str(e.value) and "SECRET-TOKEN-VALUE" not in repr(e.value)
        assert e.value.__cause__ is None and e.value.__suppress_context__ is True
    with pytest.raises(RuntimeError, match="HTTP 401"):
        ad.telegram_send("hello", TOKEN, CHAT, refuse)


@pytest.mark.parametrize("token,chat", [("", CHAT), (TOKEN, ""), (None, None)])
def test_missing_credentials_fail_loudly(token, chat):
    with pytest.raises(RuntimeError, match="not set"):
        ad.telegram_send("hello", token, chat, opener_returning({"ok": True}))


def test_long_messages_are_cut_to_telegrams_limit():
    seen = []
    ad.telegram_send("Q" * 10000, TOKEN, CHAT, opener_returning({"ok": True, "result": {"message_id": 1}}, seen))
    assert seen[0][1].count(b"Q") == ad.TELEGRAM_MAX


def test_default_send_reads_the_credentials_from_the_environment(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT)
    captured = []
    monkeypatch.setattr(ad, "telegram_send", lambda text, token, chat, **k: (captured.append((text, token, chat)), 5)[1])
    assert ad.default_send("hi", True) == 5 and captured == [("hi", TOKEN, CHAT)]
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    monkeypatch.undo()                                    # use the real telegram_send: no token must fail before any network
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="not set"):
        ad.default_send("hi", False)


def test_a6_names_the_delivered_message_and_fails_when_delivery_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("DAILY_HEALTHCHECK_URL", "https://hc/daily")
    code = ad.main(home=tmp_path, checks=[("A1", lambda: ad.res("A1", True, "fine"))],
                   send=lambda text, alert: 4242, ping_fn=lambda u, f: True)
    body = json.loads(next((tmp_path / "tests" / "evidence" / "daily").glob("*.json")).read_text())
    assert code == 0 and "delivered (Telegram message 4242)" in body["results"][-1]["detail"]

    def undeliverable(text, alert):
        raise RuntimeError("Telegram replied not-ok")
    code = ad.main(home=tmp_path, checks=[("A1", lambda: ad.res("A1", True, "fine"))],
                   send=undeliverable, ping_fn=lambda u, f: True)
    assert code == 1
