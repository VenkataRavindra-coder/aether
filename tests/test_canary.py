"""Micro-live phase 1 canary: risk key, executor entry point, canary.py, and isolation from the
engine. Offline: a fake broker, temp files for the halt, risk state and live ledger."""
import json
import re
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import risk_controller as rc
import safety_guard
import us_strategy

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("RISK_LOG", "0")
    monkeypatch.setenv("AETHER_RISK_STATE", str(tmp_path / "risk.json"))
    monkeypatch.setenv("AETHER_CANARY_RISK_STATE", str(tmp_path / "canary_risk.json"))
    monkeypatch.setenv("AETHER_HOME", str(tmp_path))
    monkeypatch.setenv("RISK_MAX_ORDER_USD_LIVE", "100")
    monkeypatch.setenv("RISK_MAX_DAILY_BUY_USD_LIVE", "100")
    monkeypatch.delenv("CANARY_MAX_ORDER_USD", raising=False)
    monkeypatch.delenv("CANARY_MAX_DAILY_BUY_USD", raising=False)
    monkeypatch.setenv("US_LIVE", "false")
    monkeypatch.setattr(rc, "HALT_FILE", tmp_path / "AETHER_HALT")
    return tmp_path


def ok(*a):
    return rc.check(*a)[0]


# ---------- risk_controller: the canary key -------------------------------------------------
def test_canary_live_entry_allowed_under_caps(env):
    assert ok("canary", "VTI", "BUY", 10, False, True)


def test_canary_per_order_cap_is_notional_15_by_default(env):
    assert not ok("canary", "VTI", "BUY", 15.01, False, True)
    assert rc.check("canary", "VTI", "BUY", 16, False, True)[1].startswith("over_canary_max_order_usd")


def test_canary_cap_never_looser_than_global_live_cap(env, monkeypatch):
    monkeypatch.setenv("CANARY_MAX_ORDER_USD", "500")
    monkeypatch.setenv("RISK_MAX_ORDER_USD_LIVE", "12")
    assert not ok("canary", "VTI", "BUY", 13, False, True)


def test_canary_daily_budget_separate_from_engine_budget(env):
    assert ok("canary", "VTI", "BUY", 15, False, True)
    assert ok("canary", "VTI", "BUY", 15, False, True)
    assert not ok("canary", "VTI", "BUY", 1, False, True)            # canary budget $30 used up
    assert json.loads((env / "canary_risk.json").read_text())["usd"] == 30
    assert not (env / "risk.json").exists()                            # engine budget untouched
    rc.set_kill("us", False)
    assert ok("us", "SPY", "BUY", 100, False, True)                    # rebalance not starved


def test_engine_spend_does_not_block_canary(env):
    rc.set_kill("us", False)
    assert ok("us", "SPY", "BUY", 100, False, True)
    assert ok("canary", "VTI", "BUY", 10, False, True)


def test_release_returns_canary_budget_only(env):
    assert ok("canary", "VTI", "BUY", 15, False, True)
    rc.release_live_buy(15, engine="canary")
    assert json.loads((env / "canary_risk.json").read_text())["usd"] == 0


def test_halt_blocks_canary_entry_but_not_exit(env):
    rc.HALT_FILE.touch()
    assert rc.check("canary", "VTI", "BUY", 5, False, True)[1] == "halted"
    assert ok("canary", "VTI", "SELL", 5, True, True)


def test_other_live_restrictions_unchanged(env):
    assert rc.check("crypto", "BTC/USD", "BUY", 5, False, True)[1] == "crypto_live_disabled"
    assert rc.check("stress", "SPY", "BUY", 5, False, True)[1] == "live_engine_not_allowed"


# ---------- executor entry point ------------------------------------------------------------
class FakeBroker:
    def __init__(self, positions=None, open_orders=(), history=(), equity=100.0, multiplier="1",
                 daytrades=0):
        self.positions = dict(positions or {})
        self.open_orders = list(open_orders)
        self.history = list(history)
        self.submitted = []
        self.cancelled = []
        self.acct = SimpleNamespace(status="ACTIVE", equity=str(equity), cash=str(equity),
                                    buying_power=str(equity), portfolio_value=str(equity),
                                    multiplier=multiplier, daytrade_count=daytrades,
                                    account_number="X1")

    def get_all_positions(self):
        return [SimpleNamespace(symbol=s, qty=str(q)) for s, q in self.positions.items()]

    def get_account(self):
        return self.acct

    def submit_order(self, order_data):
        self.submitted.append(order_data)
        return SimpleNamespace(id=f"o{len(self.submitted)}", status="accepted")

    def get_order_by_client_id(self, coid):
        raise RuntimeError("not found")

    def get_orders(self, req):
        if str(getattr(req.status, "value", req.status)).lower() == "open":
            return list(self.open_orders)
        return [o for o in self.history if not req.symbols or o.symbol in req.symbols]

    def cancel_order_by_id(self, oid):
        self.cancelled.append(oid)


def order(coid, symbol="VTI", filled=1.0, side="buy"):
    return SimpleNamespace(id=f"id-{coid}", client_order_id=coid, symbol=symbol, side=side,
                           qty="1", limit_price="300", status="filled", filled_qty=str(filled))


def live_exec(broker):
    from alpaca_executor import AlpacaExecutionClient
    c = AlpacaExecutionClient.__new__(AlpacaExecutionClient)
    c.paper, c.live_trading, c.consecutive_errors = False, False, 0
    c.api_key = c.api_secret = "k"
    c.client = broker
    return c


def mark_ledger(equity=100.0):
    safety_guard.mark(equity, on=datetime.now(safety_guard.ET).date())


def test_canary_limit_order_shape(env):
    mark_ledger()
    b = FakeBroker()
    r = live_exec(b)._canary_submit_limit_order("VTI", "BUY", 0.03, 300.004, "canary-buy-abc")
    assert r["status"] == "submitted"
    req = b.submitted[0]
    assert req.client_order_id == "canary-buy-abc"
    assert str(getattr(req.time_in_force, "value", req.time_in_force)).lower() == "day"
    assert req.limit_price == 300.0 and req.qty == 0.03


def test_canary_entry_point_refuses_paper_and_untagged_ids(env):
    c = live_exec(FakeBroker())
    assert c._canary_submit_limit_order("VTI", "BUY", 0.01, 300, "us-x")["status"] == "error"
    c.paper = True
    assert c._canary_submit_limit_order("VTI", "BUY", 0.01, 300, "canary-x")["message"] == "canary:live_account_only"


def test_canary_entry_blocked_without_ledger_and_budget_released(env):
    r = live_exec(FakeBroker())._canary_submit_limit_order("VTI", "BUY", 0.03, 300, "canary-buy-1")
    assert r["status"] == "error" and "ledger not initialised" in r["message"]
    assert json.loads((env / "canary_risk.json").read_text())["usd"] == 0


def test_canary_exit_passes_halt(env):
    mark_ledger()
    rc.HALT_FILE.touch()
    b = FakeBroker(positions={"VTI": 0.03})
    c = live_exec(b)
    assert c._canary_submit_limit_order("VTI", "BUY", 0.01, 300, "canary-buy-2")["message"] == "risk:halted"
    assert c._canary_submit_limit_order("VTI", "SELL", 0.03, 299, "canary-sell-2")["status"] == "submitted"


# ---------- canary.py --------------------------------------------------------------------------
import canary  # noqa: E402


def test_canary_symbol_outside_universe():
    assert canary.CANARY_SYMBOL == "VTI" and "VTI" not in us_strategy.UNIVERSE
    assert "SPY" in us_strategy.UNIVERSE   # why SPY is not the canary


def test_position_classified_from_client_order_id_alone(env):
    b = FakeBroker(positions={"VTI": 0.03}, history=[order("canary-buy-1")])
    assert canary.snapshot(live_exec(b))["canary_position"]["class"] == "canary"
    b = FakeBroker(positions={"VTI": 0.03}, history=[order("canary-buy-1"), order("manual-1")])
    assert canary.snapshot(live_exec(b))["canary_position"]["class"] == "unknown"


def args(**kw):
    return SimpleNamespace(**kw)


def test_buy_refused_while_canary_open_or_held(env, monkeypatch):
    monkeypatch.setattr(canary, "_quote", lambda s: None)
    mark_ledger()
    b = FakeBroker(positions={"VTI": 0.03}, history=[order("canary-buy-1")])
    assert canary.cmd_buy(live_exec(b), args(usd=8, limit=300)) == 1 and not b.submitted
    b = FakeBroker(open_orders=[order("canary-buy-2", filled=0)])
    assert canary.cmd_buy(live_exec(b), args(usd=8, limit=300)) == 1 and not b.submitted


def test_buy_refused_with_strategy_positions(env, monkeypatch):
    monkeypatch.setattr(canary, "_quote", lambda s: None)
    mark_ledger()
    b = FakeBroker(positions={"SPY": 1.0})
    assert canary.cmd_buy(live_exec(b), args(usd=8, limit=300)) == 1 and not b.submitted


def test_buy_refused_when_engine_is_live(env, monkeypatch):
    monkeypatch.setattr(canary, "_quote", lambda s: None)
    monkeypatch.setenv("US_LIVE", "true")
    mark_ledger()
    b = FakeBroker()
    assert canary.cmd_buy(live_exec(b), args(usd=8, limit=300)) == 1 and not b.submitted


def test_buy_price_band(env, monkeypatch):
    monkeypatch.setattr(canary, "_quote", lambda s: {"bid": 299.9, "ask": 300.0, "t": "x"})
    monkeypatch.setattr(canary, "_session_window_problem", lambda c: None)   # test_canary_report covers the window
    mark_ledger()
    b = FakeBroker()
    assert canary.cmd_buy(live_exec(b), args(usd=8, limit=304)) == 1 and not b.submitted
    assert canary.cmd_buy(live_exec(b), args(usd=8, limit=300)) == 0
    assert b.submitted[0].client_order_id.startswith("canary-buy-")


def test_margin_day_trade_guard(env, monkeypatch):
    monkeypatch.setattr(canary, "_quote", lambda s: None)
    mark_ledger()
    b = FakeBroker(multiplier="2", daytrades=3)
    assert canary.cmd_buy(live_exec(b), args(usd=8, limit=300)) == 1 and not b.submitted


def test_day_trade_guard_applies_at_multiplier_one(env, monkeypatch):
    """Alpaca is margin-only: multiplier 1 is an unleveraged margin account, so PDT still applies."""
    monkeypatch.setattr(canary, "_quote", lambda s: None)
    mark_ledger()
    b = FakeBroker(multiplier="1", daytrades=3)
    c = live_exec(b)
    rows = canary.preflight_checks(c, b.get_account(), canary.snapshot(c))
    dt = [r for r in rows if r[0].startswith("day trades in last 5 sessions")][0]
    assert dt[1] is False
    assert canary.cmd_buy(c, args(usd=8, limit=300)) == 1 and not b.submitted


def test_preflight_catches_double_counted_deposit(env):
    mark_ledger(100.0)
    safety_guard.record_cash_flow(100.0)          # the mistake: deposit already in the first mark
    b = FakeBroker(equity=100.0)
    c = live_exec(b)
    rows = canary.preflight_checks(c, b.get_account(), canary.snapshot(c))
    drift = [r for r in rows if r[0].startswith("ledger equity matches broker")][0]
    assert drift[1] is False


def test_assert_flat(env):
    assert canary.cmd_assert_flat(live_exec(FakeBroker()), args()) == 0
    held = FakeBroker(positions={"VTI": 0.03}, history=[order("canary-buy-1")])
    assert canary.cmd_assert_flat(live_exec(held), args()) == 1
    resting = FakeBroker(open_orders=[order("canary-buy-3", filled=0)])
    assert canary.cmd_assert_flat(live_exec(resting), args()) == 1


def test_cancel_never_touches_non_canary_orders(env):
    b = FakeBroker(open_orders=[order("canary-buy-3", filled=0), order("us-2026-10-SPY-BUY-0", "SPY", 0)])
    assert canary.cmd_cancel(live_exec(b), args(order_id=None)) == 0
    assert b.cancelled == ["id-canary-buy-3"]


# ---------- isolation: the engine can never reach the canary path ------------------------------
def test_engine_never_references_canary_path():
    src = (ROOT / "aether_engine.py").read_text()
    assert "_canary_" not in src
    assert not re.search(r"^\s*(import|from)\s+canary\b", src, re.M)
    assert not re.search(r"""['"]canary['"]""", src)


def test_canary_entry_point_only_called_from_canary_py():
    callers = [p.name for p in ROOT.glob("*.py")
               if "_canary_submit_limit_order" in p.read_text() and p.name not in ("alpaca_executor.py", "canary.py")]
    assert callers == []


# ---------- one halt-file path ---------------------------------------------------------------
def _rows(env):
    b = FakeBroker()
    c = live_exec(b)
    return {name: okk for name, okk, _ in canary.preflight_checks(c, b.get_account(), canary.snapshot(c))}


def test_halt_file_resolver_prefers_explicit_path(env, monkeypatch):
    monkeypatch.setenv("AETHER_HALT_FILE", str(env / "elsewhere" / "AETHER_HALT"))
    assert safety_guard.halt_file() == env / "elsewhere" / "AETHER_HALT"     # not AETHER_HOME/...


def test_preflight_fails_when_halt_paths_diverge(env, monkeypatch):
    mark_ledger()
    assert _rows(env)["one halt-file path (risk_controller == safety_guard)"] is True
    monkeypatch.setattr(rc, "HALT_FILE", env / "other" / "AETHER_HALT")
    assert _rows(env)["one halt-file path (risk_controller == safety_guard)"] is False
    b = FakeBroker()
    assert canary.cmd_preflight(live_exec(b), args()) == 1


def test_preflight_fails_on_stale_legacy_halt_file(env, monkeypatch):
    mark_ledger()
    monkeypatch.setenv("AETHER_HALT_FILE", str(env / "real_halt"))
    monkeypatch.setattr(rc, "HALT_FILE", env / "real_halt")
    (env / "AETHER_HALT").write_text("risk breaker: old path\n")      # AETHER_HOME/AETHER_HALT
    assert _rows(env)["no stale halt file at the legacy breaker path"] is False


def test_breaker_written_halt_is_seen_by_risk_controller(env, monkeypatch):
    monkeypatch.setenv("AETHER_HALT_FILE", str(env / "real_halt"))
    monkeypatch.setattr(rc, "HALT_FILE", safety_guard.halt_file())
    mark_ledger(100.0)
    safety_guard.mark(80.0)                      # 20% drawdown -> HALT latch writes the halt file
    assert (env / "real_halt").exists() and rc.halted()


# ---------- latched breaker refuses whole live plans ----------------------------------------
def _latch():
    mark_ledger(100.0)
    safety_guard.mark(80.0)


def test_latched_breaker_refuses_live_rotation_even_without_halt_file(env):
    _latch()
    if rc.HALT_FILE.exists():
        rc.HALT_FILE.unlink()                   # someone removed the halt file but did not reset
    rc.set_kill("us", False)
    ok_, why = rc.preflight("us", [("SELL", 50, True, True), ("BUY", 40, False, True)])
    assert not ok_ and why == "live_breaker_latched"
    assert rc.check("us", "SPY", "SELL", 50, True, True)[0]         # exits still pass
    assert rc.check("us", "SPY", "BUY", 40, False, False)[0]        # paper unaffected


def test_unreadable_ledger_fails_closed_for_live_entries(env):
    (env / "live_risk_ledger.json").write_text("{not json")
    rc.set_kill("us", False)
    assert rc.check("us", "SPY", "BUY", 10, False, True)[1] == "live_breaker_latched"
