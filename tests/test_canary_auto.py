"""Automated canary round trips (strategy.md 10, amendment 2026-10-21). Offline: the fake broker from
test_canary.py, temp files for the halt, risk state and live ledger."""
import re
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_canary import FakeBroker, env, live_exec, mark_ledger, order  # noqa: E402,F401  (env is a fixture)

import canary  # noqa: E402
import canary_auto as ca  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TODAY, YDAY = "2026-10-22", "2026-10-21"


def snap(pos=None, open_orders=(), cls="canary"):
    return {"canary_position": {"symbol": "VTI", "qty": 0.026, "class": cls} if pos else None,
            "canary_open": list(open_orders)}


def report(trips=0, failures=()):
    return {"completed_round_trips": trips, "target_round_trips": 10, "failures": list(failures)}


# ---------- decide(): the whole policy, pure ---------------------------------------------------
def test_flat_buys_until_ten_round_trips():
    assert ca.decide(snap(), report(0), None, TODAY, False)[0] == "buy"
    assert ca.decide(snap(), report(9), YDAY, TODAY, False)[0] == "buy"
    assert ca.decide(snap(), report(10), YDAY, TODAY, False)[0] == "done"


def test_never_sells_a_position_bought_today():
    assert ca.decide(snap(pos=True), report(), TODAY, TODAY, False)[0] == "hold"
    assert ca.decide(snap(pos=True), report(), YDAY, TODAY, False)[0] == "sell"


def test_sell_still_allowed_after_target_and_under_halt():
    assert ca.decide(snap(pos=True), report(10), YDAY, TODAY, True)[0] == "sell"


def test_halt_blocks_new_buys():
    assert ca.decide(snap(), report(3), YDAY, TODAY, True) == ("hold", "halt file present: no new entries")


def test_stops_on_any_real_failure():
    a, why = ca.decide(snap(), report(2, ["buy canary-buy-x: |shortfall| 14.0bp > 10.0bp"]), YDAY, TODAY, False)
    assert a == "stop" and "shortfall" in why
    assert ca.decide(snap(), report(2, ["protocol: second buy x before selling y"]), YDAY, TODAY, False)[0] == "stop"
    assert ca.decide(snap(), report(2, [f"cash {YDAY}: residual $0.2000"]), YDAY, TODAY, False)[0] == "stop"


def test_overnight_hold_and_todays_cash_line_are_not_failures():
    r = report(2, ["not flat: canary position open: {...}", f"cash {TODAY}: cash not recorded at session start and/or assert-flat"])
    assert ca.decide(snap(pos=True), r, YDAY, TODAY, False)[0] == "sell"


def test_stops_on_leftover_order_or_unknown_position():
    assert ca.decide(snap(open_orders=[object()]), report(), None, TODAY, False)[0] == "stop"
    assert ca.decide(snap(pos=True, cls="unknown"), report(), YDAY, TODAY, False)[0] == "stop"
    assert ca.decide(snap(pos=True), report(), None, TODAY, False)[0] == "stop"


def test_last_buy_fill_day_uses_newest_filled_canary_buy():
    def o(coid, side, at, filled=1.0):
        return SimpleNamespace(client_order_id=coid, side=side, filled_qty=str(filled), filled_at=at)
    hist = [o("canary-buy-1", "buy", "2026-10-19T15:00:00Z"), o("canary-sell-1", "sell", "2026-10-20T15:00:00Z"),
            o("canary-buy-2", "buy", "2026-10-21T15:00:00Z"), o("canary-buy-3", "buy", None, filled=0),
            o("manual-1", "buy", "2026-10-22T15:00:00Z")]
    assert ca.last_buy_fill_day(hist) == "2026-10-21"


# ---------- run(): off by default, and orders only through canary.py ------------------------------
def _wire(monkeypatch, broker, quote=(299.9, 300.0)):
    monkeypatch.setattr(canary, "_quote", lambda s: {"bid": quote[0], "ask": quote[1], "t": "x"})
    monkeypatch.setattr(canary, "_session_window_problem", lambda c: None)
    monkeypatch.setattr(canary, "broker_fill_activities", lambda c, s: [])
    monkeypatch.setattr(canary, "canary_log_rows", lambda db_path=None: [])
    monkeypatch.setattr(ca, "market_open", lambda c: True)
    monkeypatch.setattr(ca, "_notify", lambda t: None)
    monkeypatch.setattr(ca.time, "sleep", lambda s: None)
    return live_exec(broker)


def test_run_does_nothing_unless_enabled(env, monkeypatch):
    mark_ledger()
    b = FakeBroker()
    c = _wire(monkeypatch, b)
    monkeypatch.delenv("CANARY_AUTO", raising=False)
    assert ca.cmd_run(c) == 0 and not b.submitted
    monkeypatch.setenv("CANARY_AUTO", "0")
    assert ca.cmd_run(c) == 0 and not b.submitted


def test_run_buys_through_canary_py_when_flat(env, monkeypatch):
    mark_ledger()
    monkeypatch.setenv("CANARY_AUTO", "1")
    b = FakeBroker()
    c = _wire(monkeypatch, b)
    monkeypatch.setattr(ca, "wait_fill", lambda c, coid: True)
    assert ca.cmd_run(c) == 0
    assert len(b.submitted) == 1
    req = b.submitted[0]
    assert req.client_order_id.startswith("canary-buy-") and req.limit_price == 300.0
    assert abs(req.qty * req.limit_price - ca.BUY_USD) < 0.01


def test_run_retries_wide_spread_then_gives_up_without_sending(env, monkeypatch):
    mark_ledger()
    monkeypatch.setenv("CANARY_AUTO", "1")
    b = FakeBroker()
    c = _wire(monkeypatch, b, quote=(299.0, 300.0))   # ~33 bp spread > 10 bp line
    assert ca.cmd_run(c) == 0 and not b.submitted


def test_run_sells_prior_session_position_and_never_buys_if_sell_unfilled(env, monkeypatch):
    mark_ledger()
    monkeypatch.setenv("CANARY_AUTO", "1")
    bought = SimpleNamespace(id="id-b", client_order_id="canary-buy-1", symbol="VTI", side="buy", qty="0.026",
                             limit_price="300", status="filled", filled_qty="0.026", filled_avg_price="300",
                             submitted_at="2026-10-21T14:05:00Z", filled_at="2026-10-21T14:05:03Z")
    b = FakeBroker(positions={"VTI": 0.026}, history=[bought])
    c = _wire(monkeypatch, b)
    # complete evidence for yesterday's buy: broker FILL activity, quote at submit, cash at start and close
    monkeypatch.setattr(canary, "broker_fill_activities",
                        lambda c, s: [{"order_id": "id-b", "symbol": "VTI", "qty": "0.026", "price": "300"}])
    monkeypatch.setattr(canary, "canary_log_rows", lambda db_path=None: [
        {"ts": "2026-10-21T14:05:00+00:00", "event": "order",
         "data": {"client_order_id": "canary-buy-1", "quote": {"bid": 299.98, "ask": 300.0}, "cash_before": 100.0}},
        {"ts": "2026-10-21T19:52:00+00:00", "event": "assert_flat", "data": {"cash": 92.2}}])
    monkeypatch.setattr(ca, "today_et", lambda: TODAY)
    monkeypatch.setattr(ca, "wait_fill", lambda c, coid: False)
    assert ca.cmd_run(c) == 0
    assert [r.client_order_id.split("-")[1] for r in b.submitted] == ["sell"]
    assert b.submitted[0].limit_price == 299.9


def test_run_stops_with_nonzero_exit_on_leftover_order(env, monkeypatch):
    mark_ledger()
    monkeypatch.setenv("CANARY_AUTO", "1")
    b = FakeBroker(open_orders=[order("canary-buy-9", filled=0)])
    c = _wire(monkeypatch, b)
    assert ca.cmd_run(c) == 2 and not b.submitted


def test_never_calls_the_executor_entry_point_directly():
    src = (ROOT / "canary_auto.py").read_text()
    assert "_canary_submit_limit_order" not in src
    assert "canary.cmd_buy" in src and "canary.cmd_sell" in src


def test_env_loaded_before_canary_import():
    src = (ROOT / "canary_auto.py").read_text()
    assert src.index('_load_env_file(_HERE / ".env.live")') < src.index("\nimport canary")


# ---------- deployment and the written rule ----------------------------------------------------
def test_timers_run_in_new_york_time_inside_the_window_without_catch_up():
    run = (ROOT / "deploy" / "aether-canary-run.timer").read_text()
    close = (ROOT / "deploy" / "aether-canary-close.timer").read_text()
    assert "Mon..Fri *-*-* 10:05:00 America/New_York" in run and "Persistent=false" in run
    assert "Mon..Fri *-*-* 15:52:00 America/New_York" in close and "Persistent=false" in close
    lo, hi = canary.PREREG["window_et"]
    assert lo <= "10:05" <= hi
    for svc in ("aether-canary-run.service", "aether-canary-close.service"):
        assert "canary_auto.py" in (ROOT / "deploy" / svc).read_text()


def test_amendment_recorded_in_strategy_md():
    s = (ROOT / "strategy.md").read_text()
    assert "Automated round trips (amendment 2026-10-21" in s
    assert re.search(r"Off unless `CANARY_AUTO=1`", s)
    assert "`canary_auto.py`" in s


def test_jobs_visible_on_the_board():
    import map_status
    assert {"canary-run", "canary-close"} <= set(map_status.JOBS)
    assert '"canary-run"' in (ROOT / "board.html").read_text()


def test_one_run_never_buys_twice_even_if_position_read_lags(env, monkeypatch):
    mark_ledger()
    monkeypatch.setenv("CANARY_AUTO", "1")
    b = FakeBroker()                     # positions never update: simulates a lagging broker read
    c = _wire(monkeypatch, b)
    monkeypatch.setattr(ca, "wait_fill", lambda c, coid: True)
    ca.cmd_run(c)
    assert len(b.submitted) == 1


def test_cent_rounding_is_exact():
    assert ca._cents_down(299.9) == 299.9 and ca._cents_down(299.987) == 299.98
    assert ca._cents_up(312.4) == 312.4 and ca._cents_up(312.401) == 312.41
