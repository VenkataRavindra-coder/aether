"""Strategy registry, research page and research health check: offline, temp files only."""
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import research_health  # noqa: E402
import research_registry as rr  # noqa: E402

NY = rr.NY
SCHED = {"days": "Mon..Fri", "time": "16:25", "tz": "America/New_York"}


# ------------------------------------------------------------------ the committed registry
def test_committed_registry_is_valid_and_complete():
    reg = rr.load_registry()
    assert rr.validate(reg) == []
    ids = {s["id"] for s in reg["strategies"]}
    for must in ("s2_us_etf_momentum_v1", "daily_v1_sa", "daily_v1_sb", "daily_v1_sc", "tactical_orb15",
                 "tactical_engulf15_v1.5", "tpb_hammer_1h", "tpb_engulf_1h", "imom_long", "imom_ls",
                 "eb_55_20", "eb_20_10", "eb_100_50", "rb_63", "rb_21", "rb_126", "st_200", "st_150", "st_250"):
        assert must in ids
    trial_ids = sorted(i for s in reg["strategies"] for i in s["trials"]["ids"])
    assert trial_ids == list(range(1, 25))                    # every logged trial 1-24 claimed exactly once
    n = len(trial_ids) + reg["trial_accounting"]["backfilled"] + reg["trial_accounting"]["outside_db"]
    assert n == 55
    s2 = next(s for s in reg["strategies"] if s["id"] == "s2_us_etf_momentum_v1")
    assert s2["status"] == "frozen_no_go"
    for s in reg["strategies"]:
        assert s["live"] is None                              # no live strategy exists


def test_validate_catches_problems(tmp_path):
    reg = {"strategies": [
        {"id": "a", "status": "retired", "stage": "historical", "reasons": [], "trials": {"ids": [1]}},
        {"id": "a", "status": "weird", "stage": "moon", "trials": {"ids": [1]}, "live": {"x": 1},
         "historical": {"source": "results_json", "results_file": "nope.json"}}]}
    errs = " | ".join(rr.validate(reg, tmp_path))
    for bit in ("duplicate id", "status 'weird'", "stage 'moon'", "retired without reasons", "nope.json missing",
                "live record", "claimed by two"):
        assert bit in errs


def test_historical_reads_results_files_not_copies():
    reg = rr.load_registry()
    hammer = next(s for s in reg["strategies"] if s["id"] == "tpb_hammer_1h")
    h = rr.historical(hammer)
    assert h["trades"] == 81 and h["verdict"] == "insufficient evidence"
    assert h["random_p"] == pytest.approx(0.014, abs=0.001)
    imom = rr.historical(next(s for s in reg["strategies"] if s["id"] == "imom_long"))
    assert imom["verdict"] == "historically unsupported" and imom["trades"] == 1504
    eb = rr.historical(next(s for s in reg["strategies"] if s["id"] == "eb_55_20"))
    assert eb["verdict"] == "mixed" and eb["trades"] == 268 and eb["criteria_passed"] == 8 and eb["criteria_total"] == 12
    assert eb["cagr_pct"] == pytest.approx(3.98, abs=0.01) and eb["buy_hold_sharpe"] == pytest.approx(0.85, abs=0.01)
    st = rr.historical(next(s for s in reg["strategies"] if s["id"] == "st_200"))
    assert st["verdict"] == "historically unsupported" and "idle-cash version" in st["benchmark"]
    nb = rr.historical(next(s for s in reg["strategies"] if s["id"] == "eb_100_50"))
    assert nb["verdict"] == "not judged (neighbour)" and nb["criteria_total"] is None


# ------------------------------------------------------------------ evidence grade
def test_grade_ranks_evidence_not_return():
    base = {"status": "collecting"}
    assert rr.grade({"status": "retired"}, {"mean_net_bp": 99}, None)[0] == "rejected"
    assert rr.grade(base, {"verdict": "historically supported"}, None)[0] == "promising"
    assert rr.grade(base, {"verdict": "historically supported"},
                    {"trades": 30, "target_trades": 30, "mean_net_bp": 1.0})[0] == "strong"
    lead = {"verdict": "insufficient evidence", "mean_net_bp": 6.3, "random_p": 0.014, "half1_mean_bp": 12.8,
            "half2_mean_bp": 0.6}
    assert rr.grade(base, lead, None)[0] == "weak lead"
    big_but_shaky = {"verdict": "insufficient evidence", "mean_net_bp": 31.0, "random_p": 0.32,
                     "half1_mean_bp": -12.0, "half2_mean_bp": 95.5}
    assert rr.grade(base, big_but_shaky, None)[0] == "inconclusive"   # higher return, weaker evidence


def test_build_ranks_by_evidence_and_separates_stages(tmp_path):
    st = rr.build(now=datetime(2026, 10, 25, 9, 0, tzinfo=NY), db=tmp_path / "none.sqlite", ledger=tmp_path / "none.jsonl")
    assert st["ok"] and st["registry_problems"] == []
    first = st["strategies"][0]
    assert first["id"] == "rb_63" and first["grade"] == "weak lead"          # highest DSR among weak leads
    leads = [i["id"] for i in st["strategies"] if i["grade"] == "weak lead"]
    assert "tpb_hammer_1h" in leads and leads[-1] == "tpb_hammer_1h"         # lowest DSR of the weak leads
    assert next(i for i in st["strategies"] if i["id"] == "rb_21")["grade"] == "inconclusive"   # low bound -0.00
    for it in st["strategies"]:
        assert set(("historical", "forward", "paper", "live")) <= set(it)
        assert it["live"] is None
    assert st["summary"]["live_usd_per_day"] is None
    assert st["trials"]["n"] == 55 and st["trials"]["db"] is None


# ------------------------------------------------------------------ schedule and DST
def test_last_and_next_scheduled_respect_new_york_dst():
    # Sunday 2026-11-01 is the switch back to EST. Monday's 16:25 New York is 21:25 UTC, the Friday before 20:25 UTC.
    mon_after = datetime(2026, 11, 2, 21, 50, tzinfo=rr.ZoneInfo("UTC"))
    t = rr.last_scheduled(mon_after, SCHED)
    assert (t.astimezone(NY).hour, t.astimezone(NY).minute, t.date().isoformat()) == (16, 25, "2026-11-02")
    assert t.utcoffset() == timedelta(hours=-5)
    sun = datetime(2026, 11, 1, 12, 0, tzinfo=NY)
    prev = rr.last_scheduled(sun, SCHED)
    assert prev.date().isoformat() == "2026-10-30" and prev.utcoffset() == timedelta(hours=-4)
    nxt = rr.next_scheduled(sun, SCHED)
    assert nxt.date().isoformat() == "2026-11-02" and nxt.utcoffset() == timedelta(hours=-5)
    # within the grace period after 16:25 the previous weekday is still the expected run
    assert rr.last_scheduled(datetime(2026, 10, 27, 16, 30, tzinfo=NY), SCHED).date().isoformat() == "2026-10-26"


# ------------------------------------------------------------------ forward health
def _fwd(tmp, status=None, started="2026-10-26"):
    f = {"status_file": "fd/last_run.json", "journal": "fd/journal.jsonl", "schedule": SCHED, "unit": "u",
         "started": started, "variant": "v", "target_trades": 30}
    if status is not None:
        (tmp / "fd").mkdir(exist_ok=True)
        (tmp / "fd" / "last_run.json").write_text(json.dumps(status))
    return f


def test_health_not_started_before_first_run(tmp_path):
    h = rr.forward_health(_fwd(tmp_path), datetime(2026, 10, 25, 10, 0, tzinfo=NY), tmp_path)
    assert h["state"] == "not started" and h["problems"] == []


def test_health_never_ran_after_start_is_an_error(tmp_path):
    h = rr.forward_health(_fwd(tmp_path), datetime(2026, 10, 26, 17, 0, tzinfo=NY), tmp_path)
    assert h["state"] == "error" and "never recorded a run" in h["problems"][0]


def test_health_ok_then_skipped(tmp_path):
    ok = {"ok": True, "finished_at": "2026-10-26T16:26:40-04:00", "summary": {"_mismatches": 0}, "duplicates": 0,
          "failed_forward_sessions": [], "latest_session": "2026-10-26"}
    f = _fwd(tmp_path, ok)
    assert rr.forward_health(f, datetime(2026, 10, 26, 17, 0, tzinfo=NY), tmp_path)["state"] == "ok"
    late = rr.forward_health(f, datetime(2026, 10, 27, 17, 0, tzinfo=NY), tmp_path)
    assert late["state"] == "error" and "skipped run" in late["problems"][0]


def test_health_reports_failure_missing_data_duplicates_mismatches(tmp_path):
    bad = {"ok": False, "error": "refused: missing TP keys", "finished_at": "2026-10-26T16:26:00-04:00",
           "failed_forward_sessions": ["2026-10-26"], "duplicates": 2, "summary": {"_mismatches": 1}}
    h = rr.forward_health(_fwd(tmp_path, bad), datetime(2026, 10, 26, 17, 0, tzinfo=NY), tmp_path)
    text = " | ".join(h["problems"])
    for bit in ("last run failed: refused", "missing data for 2026-10-26", "2 duplicate", "1 replay mismatches"):
        assert bit in text


def test_health_never_raises_on_garbage(tmp_path):
    (tmp_path / "fd").mkdir()
    (tmp_path / "fd" / "last_run.json").write_text("{not json")
    h = rr.forward_health(_fwd(tmp_path), datetime(2026, 10, 26, 17, 0, tzinfo=NY), tmp_path)
    assert h["state"] == "error" and "could not read" in h["problems"][0]


# ------------------------------------------------------------------ forward and paper evidence from real files
def test_forward_and_paper_numbers_come_from_files(tmp_path):
    (tmp_path / "fd").mkdir()
    rows = [{"kind": "trade", "variant": "v", "signal_start": "2026-10-26T10:30:00-04:00",
             "exit_t": "2026-10-27T11:00:00-04:00", "net_bp": 20.0},
            {"kind": "trade", "variant": "v", "signal_start": "2026-10-28T10:30:00-04:00",
             "exit_t": "2026-10-28T14:00:00-04:00", "net_bp": -10.0},
            {"kind": "trade", "variant": "other", "signal_start": "x", "exit_t": "2026-10-28T14:00:00-04:00", "net_bp": 99},
            {"kind": "mismatch", "variant": "v", "signal_start": "y"}]
    (tmp_path / "fd" / "journal.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    f = rr.forward({"forward": _fwd(tmp_path)}, tmp_path)
    assert f["trades"] == 2 and f["mean_net_bp"] == pytest.approx(5.0)
    assert f["net_usd_5k"] == pytest.approx(5.0)              # (20 - 10) bp of $5,000
    assert [d["cumulative"] for d in f["daily_usd_5k"]] == [pytest.approx(10.0), pytest.approx(5.0)]
    led = tmp_path / "ledger.jsonl"
    led.write_text("\n".join(json.dumps(r) for r in [
        {"day": "2026-10-26", "strategy": "orb15", "status": "traded", "trade": {"net_bp": 12.0, "pnl_usd": 3.5}},
        {"day": "2026-10-27", "strategy": "orb15", "status": "no_trade", "trade": None},
        {"day": "2026-10-28", "strategy": "orb15", "status": "unscored: exit fill not found", "trade": None},
        {"day": "2026-10-26", "strategy": "engulf15", "status": "traded", "trade": {"net_bp": -5.0, "pnl_usd": -1.0}}]))
    p = rr.paper({"paper": {"source": "tactical_exec_ledger", "strategy": "orb15", "target_trades": 40}}, led)
    assert p["trades"] == 1 and p["net_usd"] == pytest.approx(3.5) and p["sessions"] == 3
    assert p["unscored"] == ["2026-10-28"]


# ------------------------------------------------------------------ trial accounting
def test_trial_check_flags_drift(tmp_path):
    db = tmp_path / "r.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE research_trials (id INTEGER PRIMARY KEY)")
    con.execute("CREATE TABLE untracked_trials (count INTEGER)")
    con.executemany("INSERT INTO research_trials (id) VALUES (?)", [(i,) for i in range(1, 26)])
    con.execute("INSERT INTO untracked_trials VALUES (28)")
    con.commit()
    con.close()
    reg = rr.load_registry()
    t = rr.trial_check(reg, db)
    assert t["db"] == 25 and "[25]" in t["problem"]           # trial 25 logged but not in the registry
    con = sqlite3.connect(db)
    con.execute("DELETE FROM research_trials WHERE id = 25")
    con.commit()
    con.close()
    assert rr.trial_check(reg, db)["problem"] is None


# ------------------------------------------------------------------ health job
def test_health_job_alerts_on_problems_and_sends_weekly_on_friday(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(rr, "STATUS_OUT", tmp_path / "status.json")
    fake = {"ok": True, "generated_at": "2026-10-30T16:45:00-04:00", "registry_problems": [],
            "forward_jobs": {"aether-trendpb-forward": {"problems": ["skipped run: nothing finished since Fri"]}},
            "trials": {"problem": None}, "errors": [], "strategies": [],
            "summary": {"strategies": 0, "by_grade": {}, "best": None, "target_usd_per_day": 2.0,
                        "paper_usd_per_day": None}}
    monkeypatch.setattr(rr, "build", lambda now=None: fake)
    assert research_health.run(now=datetime(2026, 10, 30, 16, 45, tzinfo=NY), send=sent.append) == 0
    assert len(sent) == 2 and sent[0].startswith("[RESEARCH] 1 problem") and "weekly" in sent[1]
    assert json.loads((tmp_path / "status.json").read_text())["generated_at"] == fake["generated_at"]
    sent.clear()
    fake["forward_jobs"] = {}
    research_health.run(now=datetime(2026, 10, 28, 16, 45, tzinfo=NY), send=sent.append)
    assert sent == []                                          # a quiet Wednesday sends nothing


# ------------------------------------------------------------------ page, endpoint and isolation
def test_api_and_page():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import research_view
    app = FastAPI()
    app.include_router(research_view.build_router())
    c = TestClient(app)
    d = c.get("/api/research").json()
    assert d["ok"] and len(d["strategies"]) == 25
    page = c.get("/research").text
    assert "/api/research" in page and "Forward (shadow)" in page
    for f in ("board.html", "tactical.html", "trend.html", "autotest.html", "research.html"):
        assert 'href="/research"' in (ROOT / f).read_text(), f


def test_research_modules_cannot_place_orders():
    for f in ("research_registry.py", "research_view.py", "research_health.py"):
        src = (ROOT / f).read_text()
        for bad in ("submit_order", "OrderRequest", "TradingClient", "alpaca_executor", "APCA_LIVE", "cancel_order",
                    "close_position", "import canary", "tactical_paper_exec"):
            assert bad not in src, (f, bad)


def test_engine_mounts_the_research_router():
    src = (ROOT / "aether_engine.py").read_text()
    assert "from research_view import build_router" in src and "_build_research_router()" in src
