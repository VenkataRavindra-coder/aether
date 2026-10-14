#!/usr/bin/env python3
"""AETHER daily automation: plan sections 11a (A1-A6) and 11b (RD1-RD5).

Read-only by design. It never restarts, stops or starts a service, never touches the halt file, never
uses the live keys, and never places an order. Restores go into a temporary folder. Evidence is written
to tests/evidence/daily/<date>.json. Exit code 1 if any check FAILED; a check that is not enabled is
listed as such and does not fail the run, but it is not green either.

Run:  venv/bin/python3 aether_daily.py
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import pathlib
import sqlite3
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

HOME = pathlib.Path(os.environ.get("AETHER_HOME", str(pathlib.Path.home())))
MAX_BACKUP_AGE_H = 26
MAX_ARCHIVES = 14
REQUIRED_IN_ARCHIVE = ["aether_state.json", "aether_decisions.sqlite", "research.sqlite", "git_commit.txt"]
# Created on first use (first live buy / first canary buy). Required in the archive once they exist.
OPTIONAL_STATE_FILES = (".aether_risk_state.json", ".aether_canary_risk_state.json", "live_risk_ledger.json")


def engine_service() -> str:
    """The engine's systemd unit. On Hetzner it is ratelark.service (set AETHER_SERVICE in .env)."""
    return os.environ.get("AETHER_SERVICE", "aether.service").strip() or "aether.service"
REQUIRED_STATE_KEYS = ("us_paper", "us_live")
TIMERS = ("aether-backup.timer", "aether-watchdog.timer", "aether-health.timer")
# Code the engine runs. Tests, the stress script, this runner, patches and research do not count, so a
# commit that only adds them (or only edits docs) does not look like a deployment mismatch.
CODE_PATHSPEC = ["*.py", ":(exclude)test_*.py", ":(exclude)stress_test*.py", ":(exclude)aether_daily.py",
                 ":(exclude)patch_*.py", ":(exclude)tests", ":(exclude)aether-research",
                 ":(exclude)*backups*", ":(exclude)aether-test", ":(exclude)venv"]


def run(cmd, timeout=900, cwd=None):
    try:
        p = subprocess.run([str(c) for c in cmd], cwd=str(cwd or HOME), capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"timeout after {timeout}s"
    except Exception as e:  # noqa: BLE001
        return 127, f"{type(e).__name__}: {e}"


def res(cid, ok, detail):
    return {"id": cid, "ok": ok, "detail": str(detail)[:600]}


def load_env(home=HOME):
    path = pathlib.Path(home) / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# ------------------------------------------------------------------ 11a: daily automation
def check_a1(home=HOME, run=run):
    # The server venv is .venv (Hetzner); older hosts used venv. Fall back to this interpreter.
    py = next((p for p in (pathlib.Path(home) / ".venv" / "bin" / "python3",
                           pathlib.Path(home) / "venv" / "bin" / "python3") if p.exists()),
              pathlib.Path(sys.executable))
    suites = [("pytest", [py, "-m", "pytest", "-q", "tests/", "test_risk_engine_keys.py"]),
              ("test_uslive", [py, "test_uslive.py"]),
              ("test_halt_allows_exit", [py, "test_halt_allows_exit.py"])]
    bad, notes = [], []
    for name, cmd in suites:
        rc, out = run(cmd, timeout=900, cwd=home)
        tail = [ln for ln in out.strip().splitlines() if ln.strip()][-1:] or [""]
        notes.append(f"{name}: rc={rc} {tail[0][:70]}")
        if rc != 0:
            bad.append(name)
    return res("A1", not bad, "; ".join(notes))


def check_a2(fetch=None, today=None):
    import zoneinfo
    import aether_engine
    import us_strategy
    fetch = fetch or aether_engine.fetch_us_daily
    now = datetime.datetime.now(zoneinfo.ZoneInfo("America/New_York"))
    today = today or now.strftime("%Y-%m-%d")
    daily = fetch(us_strategy.UNIVERSE)
    sig = aether_engine.us_signals(lambda symbols: daily, today)  # validates bars, freshness, history
    n = min(len(us_strategy.bars_before_month(daily.get(s, []), today)) for s in us_strategy.UNIVERSE)
    w = ", ".join(f"{k} {v * 100:.0f}%" for k, v in sorted(sig["weights"].items()))
    return res("A2", True, f"signal_date={sig['signal_date']} min_bars={n} target: {w}")


def compare_holdings(state_h, broker_h, universe, tol=1e-6):
    diffs = []
    for s in sorted(set(state_h) | set(broker_h)):
        if s not in universe:
            continue
        a, b = float(state_h.get(s, 0) or 0), float(broker_h.get(s, 0) or 0)
        if abs(a - b) > tol:
            diffs.append(f"{s}: state {a:g} vs broker {b:g}")
    return diffs


def check_a3(home=HOME, broker=None):
    import aether_engine
    import us_strategy
    broker = broker or aether_engine.us_broker
    state = json.loads((pathlib.Path(home) / "aether_state.json").read_text())
    us = state[aether_engine._us_state_key()]
    pos = broker.get_positions_map()
    if pos is None:
        return res("A3", False, "broker positions unreadable")
    open_syms = broker.open_order_symbols()
    if open_syms is None:
        return res("A3", False, "broker open orders unreadable")
    universe = set(us_strategy.UNIVERSE)
    diffs = compare_holdings(us.get("holdings") or {}, {s: q for s, q in pos.items() if q > 1e-9}, universe)
    stray = sorted(set(open_syms) & universe)
    if stray:
        diffs.append(f"open orders on {stray}")
    return res("A3", not diffs, "; ".join(diffs) if diffs else "state matches broker; no open orders")


def newest_code_version(home=HOME):
    db = pathlib.Path(home) / "aether_decisions.sqlite"
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        row = con.execute("select code_version from decisions where engine='us' order by id desc limit 1").fetchone()
    finally:
        con.close()
    return row[0] if row and row[0] else None


def check_a4(home=HOME, run=run):
    rc, head = run(["git", "rev-parse", "HEAD"], cwd=home)
    if rc != 0:
        return res("A4", False, f"git rev-parse failed: {head[:80]}")
    head = head.strip()
    rc, status = run(["git", "status", "--porcelain"], cwd=home)
    cv = newest_code_version(home)
    if not cv:
        return res("A4", False, "no code_version in the newest decision-log row")
    rc_d, out = run(["git", "diff", "--quiet", cv, "HEAD", "--"] + CODE_PATHSPEC, cwd=home)
    problems = []
    if status.strip():
        problems.append(f"working tree not clean ({len(status.strip().splitlines())} entries)")
    if rc_d != 0:
        problems.append(f"engine code at running version {cv} differs from HEAD {head[:7]}")
    return res("A4", not problems, "; ".join(problems) if problems else
               f"clean; running code {cv} matches HEAD {head[:7]}")


STRESS_MAX_AGE_H = 96  # covers a long weekend: the stress timer runs on weekdays


def check_a5(home=HOME, now=None):
    """The stress test is run by aether-stress.timer (weekdays, US market hours) through run_stress.sh, which
    records .stress_last_run.json. This check only reads that record; it places no order itself."""
    path = pathlib.Path(home) / ".stress_last_run.json"
    if not path.exists():
        return res("A5", None, "NO STRESS RUN RECORDED YET (enable aether-stress.timer)")
    try:
        body = json.loads(path.read_text())
        rc = int(body["rc"])
        when = datetime.datetime.strptime(body["utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    except Exception as e:  # noqa: BLE001
        return res("A5", False, f"stress status file unreadable: {type(e).__name__}")
    now = now or datetime.datetime.now(datetime.timezone.utc)
    age_h = (now - when).total_seconds() / 3600
    if age_h < -1:
        return res("A5", False, "stress status timestamp is in the future (clock problem?)")
    if rc != 0:
        return res("A5", False, f"last stress run failed (rc={rc}) {age_h:.0f} h ago")
    if age_h > STRESS_MAX_AGE_H:
        return res("A5", False, f"last stress run was {age_h:.0f} h ago (limit {STRESS_MAX_AGE_H})")
    return res("A5", True, f"last stress run passed {age_h:.0f} h ago")


# ------------------------------------------------------------------ 11b: daily recovery checks
def list_archives(home=HOME):
    return sorted((pathlib.Path(home) / "backups").glob("aether-*.tar.gz"),
                  key=lambda p: p.stat().st_mtime, reverse=True)


def check_rd1(home=HOME, now=None):
    arcs = list_archives(home)
    if not arcs:
        return res("RD1", False, "no backup archives found")
    now = now or time.time()
    newest = arcs[0]
    age_h = (now - newest.stat().st_mtime) / 3600
    problems = []
    if age_h > MAX_BACKUP_AGE_H:
        problems.append(f"newest archive is {age_h:.1f} h old (limit {MAX_BACKUP_AGE_H})")
    sizes = [a.stat().st_size for a in arcs[:7]]
    if len(sizes) >= 3:
        med = statistics.median(sizes)
        if med and abs(newest.stat().st_size - med) > 0.5 * med:
            problems.append(f"size {newest.stat().st_size} is not within 50% of the median {med:.0f}")
    if len(arcs) > MAX_ARCHIVES:
        problems.append(f"{len(arcs)} archives kept (rotation limit {MAX_ARCHIVES})")
    return res("RD1", not problems, "; ".join(problems) if problems else
               f"{newest.name} {age_h:.1f} h old, {newest.stat().st_size} bytes, {len(arcs)} kept")


def _safe_members(tf):
    for m in tf.getmembers():
        parts = pathlib.PurePosixPath(m.name).parts
        if m.name.startswith("/") or ".." in parts or m.issym() or m.islnk() or m.isdev():
            raise ValueError(f"unsafe archive member: {m.name}")
    return tf.getmembers()


def check_rd2(home=HOME):
    arcs = list_archives(home)
    if not arcs:
        return res("RD2", False, "no backup archives found")
    newest = arcs[0]
    problems = []
    with tempfile.TemporaryDirectory(prefix="aether-restore-") as td:
        with tarfile.open(newest) as tf:
            try:
                members = _safe_members(tf)
            except ValueError as e:
                return res("RD2", False, str(e))
            kw = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
            tf.extractall(td, members=members, **kw)
        files = [p for p in pathlib.Path(td).rglob("*") if p.is_file()]
        for db in [p for p in files if p.suffix == ".sqlite"]:
            try:
                con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
                ok = con.execute("pragma integrity_check").fetchone()[0]
                con.close()
            except Exception as e:  # noqa: BLE001
                ok = f"{type(e).__name__}"
            if ok != "ok":
                problems.append(f"{db.name} integrity: {ok}")
        states = [p for p in files if p.name == "aether_state.json"]
        if not states:
            problems.append("aether_state.json missing")
        else:
            try:
                data = json.loads(states[0].read_text())
                missing = [k for k in REQUIRED_STATE_KEYS if k not in data]
                if missing:
                    problems.append(f"state lacks keys {missing}")
            except Exception as e:  # noqa: BLE001
                problems.append(f"aether_state.json does not parse: {type(e).__name__}")
        gc = [p for p in files if p.name == "git_commit.txt"]
        if not gc or not gc[0].read_text().strip():
            problems.append("git_commit.txt missing or empty")
    return res("RD2", not problems, "; ".join(problems) if problems else f"{newest.name} restores cleanly")


def check_rd3(home=HOME, run=run):
    arcs = list_archives(home)
    if not arcs:
        return res("RD3", False, "no backup archives found")
    name = arcs[0].name
    rc, out = run(["journalctl", "-u", "aether-backup.service", "--since", f"{MAX_BACKUP_AGE_H} hours ago",
                   "--no-pager"], timeout=60)
    ok = f"uploaded {name} offsite" in out
    return res("RD3", ok, f"upload of {name} confirmed in the backup log" if ok else
               f"no 'uploaded {name} offsite' line in the last {MAX_BACKUP_AGE_H} h (journal rc={rc})")


def check_rd4(home=HOME, run=run):
    problems = []
    rc, out = run(["systemctl", "show", engine_service(), "-p", "Restart", "-p", "WatchdogUSec"], timeout=30)
    props = dict(ln.split("=", 1) for ln in out.splitlines() if "=" in ln)
    if props.get("Restart") != "always":
        problems.append(f"Restart={props.get('Restart')}")
    if props.get("WatchdogUSec") in (None, "", "0", "infinity"):
        problems.append(f"WatchdogUSec={props.get('WatchdogUSec')}")
    env = pathlib.Path(home) / ".env"
    has_url = env.exists() and any(ln.startswith("HEALTHCHECK_URL=") and ln.split("=", 1)[1].strip().strip('"')
                                   for ln in env.read_text().splitlines())
    if not has_url:
        problems.append("HEALTHCHECK_URL not set in .env")
    for t in TIMERS:
        rc, out = run(["systemctl", "is-active", t], timeout=30)
        if out.strip() != "active":
            problems.append(f"{t} is {out.strip() or 'unknown'}")
    return res("RD4", not problems, "; ".join(problems) if problems else
               "Restart=always, watchdog set, HEALTHCHECK_URL set, timers active")


def check_rd5(home=HOME):
    arcs = list_archives(home)
    if not arcs:
        return res("RD5", False, "no backup archives found")
    with tarfile.open(arcs[0]) as tf:
        names = {os.path.basename(m.name) for m in tf.getmembers() if m.isfile()}
        has_evidence = any("evidence" in pathlib.PurePosixPath(m.name).parts for m in tf.getmembers())
    required = list(REQUIRED_IN_ARCHIVE)
    required += [f for f in OPTIONAL_STATE_FILES if (pathlib.Path(home) / f).exists()]
    missing = [r for r in required if r not in names]
    if (pathlib.Path(home) / "tests" / "evidence").exists() and not has_evidence:
        missing.append("tests/evidence")
    return res("RD5", not missing, f"archive lacks {missing}" if missing else
               "all required files present" + (" incl. tests/evidence" if has_evidence else ""))


# ------------------------------------------------------------------ orchestration
CHECKS = [("A1", check_a1), ("A2", check_a2), ("A3", check_a3), ("A4", check_a4), ("A5", check_a5),
          ("RD1", check_rd1), ("RD2", check_rd2), ("RD3", check_rd3), ("RD4", check_rd4), ("RD5", check_rd5)]


def run_all(checks=CHECKS):
    results = []
    for cid, fn in checks:
        t = time.time()
        try:
            r = fn()
        except Exception as e:  # noqa: BLE001
            r = res(cid, False, f"check crashed: {type(e).__name__}: {str(e)[:300]}")
        r["seconds"] = round(time.time() - t, 1)
        results.append(r)
    return results


def summary_text(results, head, date):
    failed = [r for r in results if r["ok"] is False]
    off = [r["id"] for r in results if r["ok"] is None]
    if not failed:
        extra = f" (not enabled: {', '.join(off)})" if off else ""
        return False, f"[AETHER daily] {date} all checks green{extra}. commit {head[:7]}"
    lines = [f"[ALERT] AETHER daily checks FAILED {date} (commit {head[:7]}):"]
    lines += [f"  {r['id']}: {r['detail'][:140]}" for r in failed]
    return True, "\n".join(lines)


def write_evidence(results, head, home=HOME, now=None):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    folder = pathlib.Path(home) / "tests" / "evidence" / "daily"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{now:%Y-%m-%d}.json"
    if path.exists():
        path = folder / f"{now:%Y-%m-%d}-{now:%H%M%S}.json"
    body = {"date": f"{now:%Y-%m-%d}", "utc": now.isoformat(timespec="seconds"), "head": head,
            "all_green": all(r["ok"] is True for r in results),
            "results": results}
    body["sha256"] = hashlib.sha256(json.dumps(results, sort_keys=True).encode()).hexdigest()
    path.write_text(json.dumps(body, indent=2))
    return path


def ping(url, failed, timeout=10, opener=urllib.request.urlopen):
    if not url:
        return None
    target = url.rstrip("/") + ("/fail" if failed else "")
    try:
        opener(target, timeout=timeout).read()
        return True
    except Exception:  # noqa: BLE001
        return False


TELEGRAM_MAX = 3900


def telegram_send(text, token, chat, opener=urllib.request.urlopen, timeout=15):
    """Send a Telegram message and return its message_id, but only if Telegram's own reply says ok.

    Why not notify.send: a one-shot process like this one exits right after calling it, and an asynchronous send can
    be lost, so the old code reported 'summary sent' for messages that never arrived (found when the 07:24 and 08:32
    alerts were missing from the chat). The bot token is in the URL, so no error message here ever includes the URL."""
    if not token or not chat:
        raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set")
    data = urllib.parse.urlencode({"chat_id": chat, "text": text[:TELEGRAM_MAX],
                                   "disable_web_page_preview": "true"}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
    try:
        body = json.loads(opener(req, timeout=timeout).read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Telegram refused the message (HTTP {e.code})") from None
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Telegram send failed ({type(e).__name__})") from None
    if not isinstance(body, dict) or not body.get("ok"):
        raise RuntimeError("Telegram replied not-ok")
    return body.get("result", {}).get("message_id")


def default_send(text, alert):
    return telegram_send(text, os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID"))


def main(home=HOME, checks=CHECKS, send=default_send, ping_fn=ping, now=None):
    load_env(home)
    rc, head = run(["git", "rev-parse", "HEAD"], cwd=home, timeout=30)
    head = head.strip() if rc == 0 else "unknown"
    now = now or datetime.datetime.now(datetime.timezone.utc)
    results = run_all(checks)
    alert, text = summary_text(results, head, f"{now:%Y-%m-%d}")
    sent, mid = True, None
    try:
        mid = send(text, alert)
    except Exception as e:  # noqa: BLE001
        sent = False
        print(f"summary not sent: {type(e).__name__}: {str(e)[:100]}")
    pinged = ping_fn(os.environ.get("DAILY_HEALTHCHECK_URL"), alert)
    ok6 = sent and pinged is not False
    shown = "NOT sent" if not sent else (f"delivered (Telegram message {mid})" if mid is not None else "sent")
    detail = f"summary {shown}; ping " + {True: "sent", False: "FAILED", None: "not configured"}[pinged]
    results.append(res("A6", ok6 if pinged is not None else (True if sent else False), detail))
    if pinged is None and sent:
        results[-1]["detail"] += " (set DAILY_HEALTHCHECK_URL for the off-VM heartbeat)"
    path = write_evidence(results, head, home, now)
    print(f"evidence: {path}")
    for r in results:
        mark = {True: "OK  ", False: "FAIL", None: "OFF "}[r["ok"]]
        print(f"{mark} {r['id']:4s} {r['detail'][:150]}")
    return 1 if any(r["ok"] is False for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
