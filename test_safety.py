"""Must-pass before micro-live:  python -m pytest -q test_safety.py"""
from datetime import date, datetime

import pytest

import safety_guard as g
import heartbeat
from watchdog import assess

D1, D2, D3 = date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7)  # Mon-Wed


def at(d, h=10):
    return datetime(d.year, d.month, d.day, h, tzinfo=g.ET)


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AETHER_HOME", str(tmp_path))
    for k in list(__import__("os").environ):
        if k.startswith("GUARD_"):
            monkeypatch.delenv(k)
    return tmp_path


def buy(notional=10, eq=100, now=None):
    return g.check_live_order("buy", "SPY", notional, 0, eq, now=now or at(D2))


# --- exits always pass --------------------------------------------------
def test_exit_passes_with_halt_file():
    g.halt_file().write_text("x")
    assert g.check_live_order("sell", "SPY", 5, 0.1, 100).allowed


def test_exit_passes_when_latched_and_with_no_ledger():
    assert g.check_live_order("sell", "SPY", 0.3, 0.01, None).allowed  # no ledger, tiny
    g.mark(100, D1); g.mark(90, D2)                                     # latches
    assert g.load_ledger()["latched"]
    assert g.check_live_order("sell", "SPY", 5, 0.1, 90, now=at(D2)).allowed


def test_no_shorts():
    assert not g.check_live_order("sell", "SPY", 5, 0, 100).allowed
    assert not g.check_live_order("sell", "SPY", 5, 0.1, 100, sell_qty=0.2).allowed


# --- entries ------------------------------------------------------------
def test_entry_blocked_by_halt_file():
    g.mark(100, D1); g.halt_file().write_text("x")
    assert not buy().allowed


def test_entry_blocked_without_ledger():
    assert "not initialised" in buy().reason


def test_entry_blocked_when_mark_stale():
    g.mark(100, D1)
    assert not buy(now=at(date(2026, 10, 12))).allowed


def test_entry_blocked_without_equity():
    g.mark(100, D1)
    assert not buy(eq=None).allowed


def test_min_notional_skip_never_round_up():
    g.mark(100, D1)
    d = buy(notional=0.8)
    assert not d.allowed and d.notional == 0 and "not rounded up" in d.reason
    assert buy(notional=1.0).allowed


def test_8pct_drop_in_week_latches_not_just_reduces():
    g.mark(100, D1); g.mark(92, D2)            # weekly 8% >= 5% -> HALT wins over REDUCE
    assert g.load_ledger()["latched"]


def test_reduce_level_halves_size(monkeypatch):
    monkeypatch.setenv("GUARD_WEEKLY_LOSS", "0.5"); monkeypatch.setenv("GUARD_MONTHLY_LOSS", "0.5")
    g.mark(100, D1); g.mark(92, D2)
    assert g.load_ledger()["last_status"]["level"] == "REDUCE"
    d = buy(notional=10, eq=92, now=at(D3))
    assert d.allowed and d.notional == 5.0
    assert not buy(notional=1.5, eq=92, now=at(D3)).allowed   # 0.75 < $1 -> skip


def test_threshold_ordering(monkeypatch):
    lim = g.limits()
    assert (lim.dd_warn, lim.dd_reduce, lim.dd_halt) == pytest.approx((0.05, 0.075, 0.10))
    monkeypatch.setenv("GUARD_OOS_WORST_DD", "0.03")
    lim = g.limits()
    assert lim.dd_halt == pytest.approx(0.06)
    assert lim.dd_warn < lim.dd_reduce < lim.dd_halt


def test_daily_breaker_blocks_then_resets_next_day():
    g.mark(100, D1)
    assert not buy(eq=97.5, now=at(D2)).allowed      # -2.5% vs prior close
    g.mark(97.5, D2)                                  # new close
    assert buy(eq=97.5, now=at(D3)).allowed           # reset next day


def test_weekly_breaker_latches_and_needs_reset():
    g.mark(100, D1); s = g.mark(94, D2)
    assert s["level"] == "HALT" and g.halt_file().exists()
    g.halt_file().unlink()                            # rm alone is NOT enough
    assert "latched" in buy(eq=94, now=at(D3)).reason
    g.mark(94, date(2026, 10, 13))                    # new week, still latched
    assert not buy(eq=94, now=at(date(2026, 10, 13))).allowed
    g.reset_after_review()
    assert buy(eq=94, now=at(date(2026, 10, 14))).allowed


def test_drawdown_halt_respects_oos(monkeypatch):
    monkeypatch.setenv("GUARD_OOS_WORST_DD", "0.02")  # halt at 4%
    g.mark(100, D1); s = g.mark(95.5, D2)
    assert s["level"] == "HALT" and any("drawdown" in r for r in s["reasons"])


def test_cash_flows_not_pnl():
    g.mark(50, D1)
    g.record_cash_flow(50, D1)
    s = g.mark(100, D2)
    assert s["level"] == "OK" and s["peak"] == 100 and s["weekly_loss"] == 0
    g.record_cash_flow(-40, D2)
    s = g.mark(60, D3)
    assert s["level"] == "OK" and s["drawdown"] == 0


def test_gate_does_not_write_ledger():
    g.mark(100, D1)
    before = g.ledger_file().read_text()
    buy(); g.check_live_order("sell", "SPY", 5, 1, 100)
    assert g.ledger_file().read_text() == before


# --- heartbeat / watchdog ----------------------------------------------
def test_heartbeat_never_raises(monkeypatch):
    heartbeat.beat("engine")
    assert heartbeat.heartbeat_file().exists()
    monkeypatch.setenv("AETHER_HOME", "/proc/nope")
    heartbeat.beat("engine")  # must not raise


def test_watchdog_assess():
    now = 10_000.0
    ok = {"engine": {"ts": now - 60}}
    assert assess(now, ok, True, {}) == []
    assert any("NOT active" in p for p in assess(now, ok, False, {}))
    assert any("stale" in p for p in assess(now, {"engine": {"ts": now - 3600}}, True, {}))
    led = {"last_mark": {"date": "2026-10-01", "equity": 100}}
    assert any("risk mark" in p for p in assess(now, ok, True, led, today=date(2026, 10, 9)))
