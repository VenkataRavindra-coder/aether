"""Fixes from the 2026-10-17 production audit. Offline: fake brokers, temp files, no network.

Execution path: C1 live limit orders, C2 9-decimal quantities, L1 position_unknown, L2 budget on
unknown outcome, L3 crypto client_order_id, L4 canary quote fail-closed.
Dashboard/alerts/credentials: S1-S5, E1-E3, C3 health-check name, K1 .env.live, K2 settings
route removed.
"""
import json
import queue
import random
import threading
import time
from datetime import datetime
from decimal import Decimal
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
    monkeypatch.delenv("LIVE_LIMIT_BAND", raising=False)
    monkeypatch.delenv("LIVE_MAX_SPREAD", raising=False)
    monkeypatch.setattr(rc, "HALT_FILE", tmp_path / "AETHER_HALT")
    rc.set_kill("us", False)
    rc.set_kill("crypto", False)
    yield tmp_path
    rc.set_kill("us", None)
    rc.set_kill("crypto", None)


def decimals(x: float) -> int:
    e = Decimal(repr(x)).normalize().as_tuple().exponent
    return max(0, -e)


# ---------- C2: quantities never exceed 9 decimals, never round up ---------------------------
def test_qty9_rounds_down_to_nine_places():
    assert us_strategy.qty9(0.041152263040999996) == 0.041152263
    assert us_strategy.qty9(1.9999999999) == 1.999999999          # down, never up to 2.0
    assert us_strategy.qty9(3.0) == 3.0
    rng = random.Random(17)
    for _ in range(5000):
        q = rng.uniform(0, 50) / rng.choice([1, 3, 7, 101, 333.3])
        r = us_strategy.qty9(q)
        assert r <= q and decimals(r) <= 9 and q - r < 1e-9 + 1e-15


def test_plan_orders_trim_quantity_is_broker_valid():
    holdings = {"SPY": 1.0, "QQQ": 0.5}
    prices = {"SPY": 433.37, "QQQ": 377.13, "EEM": 41.91}
    weights = {"SPY": 0.2, "QQQ": 0.4, "EEM": 0.4}
    orders = us_strategy.plan_orders(holdings, prices, weights, equity=700.0)
    sells = [o for o in orders if o["side"] == "SELL"]
    assert sells, "expected at least one trim"
    for o in sells:
        assert decimals(o["qty"]) <= 9 and 0 < o["qty"] <= holdings[o["symbol"]]


# ---------- live executor fakes -----------------------------------------------------------------
class FakeLiveBroker:
    def __init__(self, positions=None, equity=100.0, submit_exc=None, existing=None, positions_exc=None):
        self.positions = dict(positions or {})
        self.submitted = []
        self.submit_exc = submit_exc
        self.existing = existing
        self.positions_exc = positions_exc
        self.acct = SimpleNamespace(equity=str(equity), cash=str(equity), buying_power=str(equity),
                                    portfolio_value=str(equity))

    def get_all_positions(self):
        if self.positions_exc:
            raise self.positions_exc
        return [SimpleNamespace(symbol=s, qty=str(q)) for s, q in self.positions.items()]

    def get_account(self):
        return self.acct

    def submit_order(self, order_data):
        self.submitted.append(order_data)
        if self.submit_exc:
            raise self.submit_exc
        return SimpleNamespace(id=f"o{len(self.submitted)}", status="accepted")

    def get_order_by_client_id(self, coid):
        if self.existing is None:
            raise RuntimeError("lookup failed")
        return self.existing


def make_client(broker, paper=False, quote=None, live_trading=False):
    from alpaca_executor import AlpacaExecutionClient
    c = AlpacaExecutionClient.__new__(AlpacaExecutionClient)
    c.paper, c.live_trading, c.consecutive_errors = paper, live_trading, 0
    c.api_key = c.api_secret = "k"
    c.client = broker
    c.latest_quote = lambda symbol: quote
    return c


def mark_ledger(equity=100.0):
    safety_guard.mark(equity, on=datetime.now(safety_guard.ET).date())


def tif(req):
    return str(getattr(req.time_in_force, "value", req.time_in_force)).lower()


# ---------- C1: the engine's LIVE path sends marketable limit orders ----------------------------
def test_live_buy_is_a_marketable_limit_order(env):
    from alpaca.trading.requests import LimitOrderRequest
    mark_ledger()
    b = FakeLiveBroker()
    c = make_client(b, quote={"bid": 299.90, "ask": 300.00, "t": "now"})
    r = c.submit_market_order("SPY", "BUY", notional=50.0, client_order_id="us-2026-11-SPY-BUY-0")
    assert r["status"] == "submitted"
    req = b.submitted[0]
    assert isinstance(req, LimitOrderRequest) and tif(req) == "day"
    assert req.limit_price == round(300.00 * 1.002, 2)                   # ask + 0.2% band
    assert req.qty * req.limit_price <= 50.0 and decimals(req.qty) <= 9  # never over the notional
    assert req.client_order_id == "us-2026-11-SPY-BUY-0"


def test_live_sell_limit_is_below_bid(env):
    mark_ledger()
    b = FakeLiveBroker(positions={"SPY": 0.1})
    c = make_client(b, quote={"bid": 299.90, "ask": 300.00, "t": "now"})
    r = c.submit_market_order("SPY", "SELL", qty=0.1, client_order_id="c1")
    assert r["status"] == "submitted"
    assert b.submitted[0].limit_price == round(299.90 * 0.998, 2) and b.submitted[0].qty == 0.1


@pytest.mark.parametrize("quote,reason", [
    (None, "no_quote"),
    ({"bid": 0.0, "ask": 300.0, "t": "x"}, "bad_quote"),
    ({"bid": 301.0, "ask": 300.0, "t": "x"}, "bad_quote"),             # crossed
    ({"bid": 290.0, "ask": 300.0, "t": "x"}, "wide_spread"),           # ~3.4% > 0.5%
])
def test_live_order_fails_closed_on_bad_quote(env, quote, reason):
    mark_ledger()
    b = FakeLiveBroker(positions={"SPY": 0.1})
    c = make_client(b, quote=quote)
    r = c.submit_market_order("SPY", "BUY", notional=20.0, client_order_id="c2")
    assert r["status"] == "error" and r["message"].startswith(f"guard:{reason}") and not b.submitted


def test_paper_path_still_sends_market_orders(env):
    from alpaca.trading.requests import MarketOrderRequest
    b = FakeLiveBroker()
    c = make_client(b, paper=True, quote=None)        # no quote needed on paper
    r = c.submit_market_order("SPY", "BUY", notional=20.0, client_order_id="p1")
    assert r["status"] == "submitted" and isinstance(b.submitted[0], MarketOrderRequest)


# ---------- L1: unknown position is reported as unknown, not as a naked short -------------------
def test_live_sell_with_unreadable_position_says_position_unknown(env):
    mark_ledger()
    b = FakeLiveBroker(positions_exc=RuntimeError("503"))
    c = make_client(b, quote={"bid": 299.9, "ask": 300.0, "t": "x"})
    r = c.submit_market_order("SPY", "SELL", qty=0.1, client_order_id="c3")
    assert r == {"status": "error", "message": "guard:position_unknown"} and not b.submitted


# ---------- L2: "maybe placed" never releases the real-money budget -----------------------------
def test_timeout_with_failed_lookup_is_outcome_unknown(env):
    import requests
    mark_ledger()
    b = FakeLiveBroker(submit_exc=requests.exceptions.Timeout("read timed out"))
    c = make_client(b, quote={"bid": 299.9, "ask": 300.0, "t": "x"})
    r = c.submit_market_order("SPY", "BUY", notional=20.0, client_order_id="c4")
    assert r["status"] == "error" and r["outcome_unknown"] is True


def test_client_error_is_a_known_refusal(env):
    class Refused(Exception):
        status_code = 422
    mark_ledger()
    b = FakeLiveBroker(submit_exc=Refused("qty must be <= 9 decimal places"))
    c = make_client(b, quote={"bid": 299.9, "ask": 300.0, "t": "x"})
    r = c.submit_market_order("SPY", "BUY", notional=20.0, client_order_id="c5")
    assert r["status"] == "error" and r["outcome_unknown"] is False


def test_engine_keeps_budget_reserved_when_outcome_unknown(env, monkeypatch):
    import aether_engine as eng
    released = []

    class B:
        paper = False
        consecutive_errors = 1

        def submit_market_order(self, *a, **k):
            return {"status": "error", "message": "read timed out", "outcome_unknown": True}

        def get_positions_map(self):
            return {}

    monkeypatch.setattr(eng, "us_broker", B())
    monkeypatch.setattr(eng.risk_controller, "release_live_buy", lambda usd, **k: released.append(usd))
    results, _, _ = eng._us_execute_paper([{"symbol": "SPY", "side": "BUY", "notional": 10.0}],
                                          {"SPY": 100.0}, 100.0, mkey="2026-11")
    assert results[0]["state"] == "error" and results[0]["detail"] == "read timed out"
    assert released == []                                       # money may be spent: keep it counted

    class B2(B):
        def submit_market_order(self, *a, **k):
            return {"status": "error", "message": "insufficient buying power", "outcome_unknown": False}

    monkeypatch.setattr(eng, "us_broker", B2())
    eng._us_execute_paper([{"symbol": "SPY", "side": "BUY", "notional": 10.0}], {"SPY": 100.0}, 100.0,
                          mkey="2026-11")
    assert released == [10.0]                                   # definite refusal: budget returned


# ---------- L2 (canary): same rule -------------------------------------------------------------
def test_canary_keeps_budget_when_outcome_unknown(env):
    import requests
    mark_ledger()
    b = FakeLiveBroker(submit_exc=requests.exceptions.ConnectionError("reset"))
    c = make_client(b)
    r = c._canary_submit_limit_order("VTI", "BUY", 0.03, 300.0, "canary-buy-x")
    assert r["status"] == "error" and r["outcome_unknown"] is True
    assert json.loads((env / "canary_risk.json").read_text())["usd"] == pytest.approx(9.0)


# ---------- L3: crypto/webhook path carries a client_order_id and recovers ----------------------
def test_execute_order_uses_client_order_id_and_recovers(env):
    import requests
    existing = SimpleNamespace(id="already-there", status="accepted")
    b = FakeLiveBroker(submit_exc=requests.exceptions.Timeout("t"), existing=existing)
    c = make_client(b, paper=True, live_trading=True)
    r = c.execute_order("BTC/USD", 0.001, "BUY", 60000.0)
    assert b.submitted[0].client_order_id.startswith("aether-buy-")
    assert r["status"] == "success" and r["order_id"] == "already-there" and r["recovered"] is True
    assert len(b.submitted) == 1


# ---------- L4: canary buy refuses without a quote ---------------------------------------------
def test_canary_buy_refused_without_quote(env, monkeypatch):
    import canary
    monkeypatch.setenv("US_LIVE", "false")
    monkeypatch.setattr(canary, "_quote", lambda s: None)
    monkeypatch.setattr(canary, "preflight_checks", lambda c, a, s: [])
    b = FakeLiveBroker()
    b.acct.multiplier = "1"
    monkeypatch.setattr(canary, "snapshot", lambda c: {"canary_position": None, "canary_open": []})
    mark_ledger()   # every other gate open: only the missing quote can refuse
    assert canary.cmd_buy(make_client(b), SimpleNamespace(usd=8, limit=300)) == 1 and not b.submitted
    monkeypatch.setattr(canary, "_quote", lambda s: {"bid": 299.9, "ask": 300.0, "t": "x"})
    assert canary.cmd_buy(make_client(b), SimpleNamespace(usd=8, limit=300)) == 0 and len(b.submitted) == 1


# ---------- S1/S2/S3/S4: dashboard ------------------------------------------------------------
def test_dashboard_reads_engine_state_and_backups_from_repo_dir(env, monkeypatch):
    import dashboard_v2 as d
    monkeypatch.setattr(d, "_REPO_DIR", env)
    monkeypatch.setenv("US_LIVE", "false")
    (env / "aether_state.json").write_text(json.dumps({"us_paper": {
        "live_proposal": {"id": "p7", "status": "PENDING", "date": "2026-11-02"},
        "last_rebalance_month": "2026-10"}}))
    (env / "backups").mkdir()
    (env / "backups" / "aether-20261017T000000Z.tar.gz").write_text("x")
    s = d.safety()
    assert s["proposal"] == {"id": "p7", "status": "PENDING", "date": "2026-11-02"}
    assert s["last_rebalance_month"] == "2026-10" and s["last_backup_utc"] is not None


def test_engine_state_path_matches_dashboard():
    import aether_engine as eng
    import dashboard_v2 as d
    assert Path(eng.STATE_FILE).resolve() == (d._REPO_DIR / "aether_state.json").resolve()


def test_dashboard_headline_follows_us_live_with_matching_keys(monkeypatch):
    import dashboard_v2 as d
    seen = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    def fake_urlopen(req, timeout=None):
        seen["url"], seen["key"] = req.full_url, req.headers.get("Apca-api-key-id")
        return Resp()

    monkeypatch.setattr(d.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("LIVE_TRADING", "false")
    monkeypatch.setenv("US_LIVE", "true")
    monkeypatch.setenv("APCA_LIVE_API_KEY_ID", "AKLIVE")
    monkeypatch.setenv("APCA_LIVE_API_SECRET_KEY", "s")
    d._get("/v2/account")
    assert seen["url"].startswith("https://api.alpaca.markets") and seen["key"] == "AKLIVE"
    monkeypatch.setenv("US_LIVE", "false")
    monkeypatch.setenv("LIVE_TRADING", "true")   # crypto flag must NOT switch the headline to live
    d._get("/v2/account")
    assert seen["url"].startswith("https://paper-api.alpaca.markets") and seen["key"] == "test-dummy-key"


def test_overview_serves_stale_instead_of_queueing(monkeypatch):
    import dashboard_v2 as d
    gate, started = threading.Event(), threading.Event()

    def slow_build():
        started.set()
        gate.wait(5)
        return {"generated_at": "new"}

    monkeypatch.setattr(d, "_build", slow_build)
    monkeypatch.setitem(d._CACHE, "t", 0.0)
    monkeypatch.setitem(d._CACHE, "data", {"generated_at": "old"})
    t = threading.Thread(target=d.overview)
    t.start()
    assert started.wait(2)
    t0 = time.time()
    r = d.overview()                                   # a rebuild is in flight
    assert time.time() - t0 < 0.5 and r["generated_at"] == "old" and r["stale"] is True
    gate.set()
    t.join(2)
    assert d.overview()["generated_at"] == "new"


# ---------- K2: no route can write credentials from the browser ---------------------------------
def test_settings_route_removed():
    import dashboard_v2 as d
    paths = {getattr(r, "path", "") for r in d.router.routes}
    assert "/api/settings/alpaca" not in paths
    html = (ROOT / "dashboard_v2.html").read_text()
    assert "/api/settings/alpaca" not in html and "keysDialog" not in html


# ---------- E2: dropped alerts are counted -----------------------------------------------------
def test_dropped_telegram_alerts_are_counted(monkeypatch):
    import notify
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    q = queue.Queue(maxsize=1)
    q.put_nowait("x")
    monkeypatch.setattr(notify, "_queue", q)
    monkeypatch.setattr(notify, "_worker_started", True)
    before = notify.dropped_count()
    assert notify.send("alert") is False and notify.dropped_count() == before + 1


# ---------- C3: either health-check name reaches the watchdog ----------------------------------
def test_watchdog_accepts_either_healthcheck_name(monkeypatch):
    import watchdog
    monkeypatch.delenv("HEALTHCHECK_URL", raising=False)
    monkeypatch.setenv("HEALTHCHECK_PING_URL", "https://hc.example/legacy")
    assert watchdog._healthcheck_url() == "https://hc.example/legacy"
    monkeypatch.setenv("HEALTHCHECK_URL", "https://hc.example/main")
    assert watchdog._healthcheck_url() == "https://hc.example/main"


# ---------- K1: live keys can live in .env.live ------------------------------------------------
def test_ops_loader_reads_env_live(env, monkeypatch):
    import aether_ops
    (env / ".env").write_text("TELEGRAM_CHAT_ID_X=1\n")
    (env / ".env.live").write_text("APCA_LIVE_API_KEY_ID=AKX\nAPCA_LIVE_API_SECRET_KEY=SX\n")
    monkeypatch.delenv("APCA_LIVE_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_LIVE_API_SECRET_KEY", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID_X", raising=False)
    aether_ops.load_env()
    import os
    assert os.environ["APCA_LIVE_API_KEY_ID"] == "AKX" and os.environ["TELEGRAM_CHAT_ID_X"] == "1"


def test_only_engine_unit_loads_env_live():
    units = {p.name: p.read_text() for p in (ROOT / "deploy").glob("*.service")}
    assert "EnvironmentFile=-/home/ratelark/ratelark/.env.live" in units["aether.service"]
    for name, text in units.items():
        if name != "aether.service":
            assert ".env.live" not in text, name
