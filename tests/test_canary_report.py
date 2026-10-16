"""Pre-registered canary execution measurement (strategy.md 10, "Execution measurement").
Offline: fake broker orders/fills and fake log rows; nothing is sent."""
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import canary
import safety_guard

ROOT = Path(__file__).resolve().parent.parent
T0 = datetime(2026, 10, 19, 15, 0, tzinfo=timezone.utc)          # Mon 11:00 ET


def order(coid, side, qty, px, submitted=T0, fill_s=5, status="filled"):
    filled = submitted + timedelta(seconds=fill_s) if fill_s is not None else None
    return SimpleNamespace(id=f"id-{coid}", client_order_id=coid, side=side, filled_qty=str(qty),
                           filled_avg_price=None if px is None else str(px), status=status,
                           submitted_at=submitted, filled_at=filled, symbol="VTI")


def fill(o, qty=None, px=None):
    return {"order_id": o.id, "qty": str(qty if qty is not None else o.filled_qty),
            "price": str(px if px is not None else o.filled_avg_price), "symbol": "VTI"}


Q = {"bid": 299.98, "ask": 300.02}          # mid 300.00, spread ~1.3 bp


def logrow(ts, event, **data):
    return {"ts": ts.isoformat(), "event": event, "data": data}


# ---------- the thresholds are locked in two places that must agree ---------------------------
def test_prereg_constants_match_strategy_md():
    text = (ROOT / "strategy.md").read_text()
    block = re.search(r"```prereg\n(.*?)```", text, re.S).group(1)
    doc = dict(line.split(": ", 1) for line in block.strip().splitlines())
    p = canary.PREREG
    assert float(doc["max_fill_seconds"]) == p["max_fill_seconds"]
    assert float(doc["max_abs_shortfall_bp"]) == p["max_abs_shortfall_bp"]
    assert float(doc["max_spread_bp"]) == p["max_spread_bp"]
    assert float(doc["max_cash_residual_usd"]) == p["max_cash_residual_usd"]
    assert doc["window_et"] == "-".join(p["window_et"])
    assert int(doc["target_round_trips"]) == p["target_round_trips"]


def test_spread_threshold_equals_live_engine_guard():
    import alpaca_executor
    assert canary.PREREG["max_spread_bp"] == float(alpaca_executor.LIVE_MAX_SPREAD_DEFAULT) * 1e4


# ---------- per-leg measurement -----------------------------------------------------------------
def test_buy_at_ask_is_half_spread_cost():
    o = order("canary-buy-1", "buy", 0.05, 300.02)
    m = canary.leg_metrics(o, [fill(o)], Q)
    assert m["pass"] and m["shortfall_bp"] == pytest.approx(0.6667, abs=1e-3)
    assert m["spread_bp"] == pytest.approx(1.3333, abs=1e-3) and m["time_to_fill_s"] == 5


def test_sell_at_bid_is_also_a_positive_cost():
    o = order("canary-sell-1", "sell", 0.05, 299.98)
    assert canary.leg_metrics(o, [fill(o)], Q)["shortfall_bp"] == pytest.approx(0.6667, abs=1e-3)


@pytest.mark.parametrize("kw,quote,frag", [
    ({"fill_s": 901}, Q, "time to fill"),
    ({"px": 300.40}, Q, "|shortfall|"),                         # +13.3 bp
    ({}, {"bid": 298.0, "ask": 302.0}, "spread at submit"),     # 133 bp
    ({}, None, "no usable quote"),
    ({"fill_s": None}, Q, "missing submitted_at/filled_at"),
])
def test_leg_fails_on_each_prereg_line(kw, quote, frag):
    px = kw.pop("px", 300.02)
    o = order("canary-buy-x", "buy", 0.05, px, **kw)
    m = canary.leg_metrics(o, [fill(o)], quote)
    assert not m["pass"] and any(frag in p for p in m["problems"])


def test_leg_fails_when_broker_fill_records_disagree():
    o = order("canary-buy-2", "buy", 0.05, 300.02)
    assert "fill qty" in canary.leg_metrics(o, [fill(o, qty=0.04)], Q)["problems"][0]
    assert "VWAP" in canary.leg_metrics(o, [fill(o, px=300.10)], Q)["problems"][0]
    assert "no FILL activity" in canary.leg_metrics(o, [], Q)["problems"][0]


# ---------- round trips and protocol ------------------------------------------------------------
def legs_for(*specs):
    out = []
    for i, (side, px) in enumerate(specs):
        o = order(f"canary-{side}-{i}", side, 0.05, px, submitted=T0 + timedelta(minutes=10 * i))
        out.append(canary.leg_metrics(o, [fill(o)], Q))
    return out


def test_round_trip_pairing_and_pnl():
    trips, viol, open_buy = canary.round_trips(legs_for(("buy", 300.02), ("sell", 299.98)))
    assert len(trips) == 1 and not viol and open_buy is None
    assert trips[0]["pnl_usd"] == pytest.approx(-0.002, abs=1e-6)


def test_one_in_one_out_violations():
    _, viol, _ = canary.round_trips(legs_for(("buy", 300.02), ("buy", 300.02), ("sell", 299.98)))
    assert any("second buy" in v for v in viol)
    _, viol, _ = canary.round_trips(legs_for(("sell", 299.98)))
    assert any("no open canary buy" in v for v in viol)


# ---------- session cash reconciliation ----------------------------------------------------------
def test_session_cash_reconciles_to_fills():
    legs = legs_for(("buy", 300.02), ("sell", 299.98))
    flow = sum(l["cash_flow"] for l in legs)
    rows = [logrow(T0, "order", client_order_id="canary-buy-0", cash_before=100.0),
            logrow(T0 + timedelta(minutes=30), "assert_flat", cash=100.0 + flow - 0.01)]   # 1c reg fee
    s = canary.session_cash(rows, legs)["2026-10-19"]
    assert s["pass"] and s["residual_usd"] == pytest.approx(-0.01)


def test_session_cash_flags_outside_money_and_missing_marks():
    legs = legs_for(("buy", 300.02), ("sell", 299.98))
    flow = sum(l["cash_flow"] for l in legs)
    rows = [logrow(T0, "order", cash_before=100.0),
            logrow(T0 + timedelta(minutes=30), "assert_flat", cash=200.0 + flow)]           # a deposit
    assert not canary.session_cash(rows, legs)["2026-10-19"]["pass"]
    assert not canary.session_cash(rows[:1], legs)["2026-10-19"]["pass"]


# ---------- whole report -------------------------------------------------------------------------
def scenario(sell_px=299.98, cash_end_delta=0.0):
    b = order("canary-buy-a", "buy", 0.05, 300.02)
    s = order("canary-sell-a", "sell", 0.05, sell_px, submitted=T0 + timedelta(minutes=20))
    resting = order("canary-buy-r", "buy", 0, None, submitted=T0 + timedelta(hours=1),
                    fill_s=None, status="canceled")
    foreign = order("manual-1", "buy", 1, 300.0)
    flow = -0.05 * 300.02 + 0.05 * sell_px
    rows = [logrow(T0, "order", client_order_id="canary-buy-a", quote=Q, cash_before=100.0),
            logrow(T0 + timedelta(minutes=20), "order", client_order_id="canary-sell-a", quote=Q,
                   cash_before=100.0 - 0.05 * 300.02),
            logrow(T0 + timedelta(hours=2), "assert_flat", cash=100.0 + flow + cash_end_delta)]
    return [b, s, resting, foreign], [fill(b), fill(s)], rows


def test_clean_round_trip_passes():
    orders_all, fills, rows = scenario()
    rep = canary.build_report(orders_all, fills, rows, [])
    assert rep["verdict"] == "PASS" and rep["completed_round_trips"] == 1
    assert [x["client_order_id"] for x in rep["not_filled"]] == ["canary-buy-r"]   # listed, not measured
    assert all(l["client_order_id"].startswith("canary-") for l in rep["legs"])     # manual order ignored


def test_report_fails_on_bad_fill_cash_gap_or_not_flat():
    orders_all, fills, rows = scenario(sell_px=299.50)                 # sell 16.7 bp under mid
    assert canary.build_report(orders_all, fills, rows, [])["verdict"] == "FAIL"
    orders_all, fills, rows = scenario(cash_end_delta=1.00)
    assert canary.build_report(orders_all, fills, rows, [])["verdict"] == "FAIL"
    orders_all, fills, rows = scenario()
    assert canary.build_report(orders_all, fills, rows, ["canary position open"])["verdict"] == "FAIL"


def test_no_canary_fills_is_no_data_not_pass():
    assert canary.build_report([], [], [], [])["verdict"] == "NO DATA"


def test_cmd_report_writes_evidence_and_exit_code(tmp_path, monkeypatch, capsys):
    orders_all, fills, rows = scenario()
    monkeypatch.setattr(canary, "EVIDENCE_DIR", tmp_path / "ev")
    monkeypatch.setattr(canary, "orders", lambda c, status, symbols=None: orders_all)
    monkeypatch.setattr(canary, "broker_fill_activities", lambda c, since: fills)
    monkeypatch.setattr(canary, "canary_log_rows", lambda: rows)
    monkeypatch.setattr(canary, "snapshot", lambda c: {"canary_position": None, "canary_open": []})
    logged = []
    monkeypatch.setattr(canary, "_log", lambda event, **kw: logged.append((event, kw)))
    assert canary.cmd_report(SimpleNamespace(), SimpleNamespace(since="2026-10-19")) == 0
    ev = list((tmp_path / "ev").glob("report-*.json"))
    assert len(ev) == 1 and json.loads(ev[0].read_text())["verdict"] == "PASS"
    assert "CANARY MEASUREMENT: PASS" in capsys.readouterr().out
    assert logged[-1][0] == "report" and logged[-1][1]["decision"] == "PASS"


# ---------- buy-time protocol ---------------------------------------------------------------------
ET = safety_guard.ET


@pytest.mark.parametrize("when,clock,ok", [
    (datetime(2026, 10, 19, 10, 30, tzinfo=ET), SimpleNamespace(is_open=True), True),
    (datetime(2026, 10, 19, 15, 45, tzinfo=ET), SimpleNamespace(is_open=True), True),
    (datetime(2026, 10, 19, 9, 45, tzinfo=ET), SimpleNamespace(is_open=True), False),
    (datetime(2026, 10, 19, 15, 46, tzinfo=ET), SimpleNamespace(is_open=True), False),
    (datetime(2026, 10, 17, 11, 0, tzinfo=ET), SimpleNamespace(is_open=True), False),   # Saturday
    (datetime(2026, 10, 19, 11, 0, tzinfo=ET), SimpleNamespace(is_open=False), False),  # holiday
    (datetime(2026, 10, 19, 11, 0, tzinfo=ET), None, False),                            # clock down
])
def test_buy_window(when, clock, ok):
    c = SimpleNamespace(get_clock=lambda: clock)
    assert (canary._session_window_problem(c, now=when) is None) is ok


def test_buy_and_sell_record_what_the_report_needs(monkeypatch):
    from test_audit_oct17 import FakeLiveBroker, make_client, mark_ledger  # noqa: F401
    logged = []
    monkeypatch.setattr(canary, "_log", lambda event, **kw: logged.append((event, kw)))
    monkeypatch.setattr(canary, "preflight_checks", lambda c, a, s: [])
    monkeypatch.setattr(canary, "snapshot", lambda c: {"canary_position": None, "canary_open": []})
    monkeypatch.setattr(canary, "_session_window_problem", lambda c: None)
    monkeypatch.setattr(canary, "_quote", lambda s: dict(Q, t="x"))
    b = FakeLiveBroker()
    b.acct.multiplier = "1"
    c = make_client(b)
    monkeypatch.setattr(c, "_canary_submit_limit_order", lambda *a: {"status": "submitted", "order_id": "o1"})
    assert canary.cmd_buy(c, SimpleNamespace(usd=8, limit=300.0)) == 0
    ev, kw = logged[-1]
    assert ev == "order" and kw["client_order_id"].startswith("canary-buy-")
    assert kw["quote"]["bid"] == Q["bid"] and kw["cash_before"] == 100.0 and kw["side"] == "buy"
    monkeypatch.setattr(canary, "snapshot", lambda c: {"canary_position": None, "canary_open": []})
    assert canary.cmd_assert_flat(c, None) == 0
    assert logged[-1][0] == "assert_flat" and logged[-1][1]["cash"] == 100.0


def test_sell_is_never_time_gated(monkeypatch):
    called = []
    monkeypatch.setattr(canary, "_session_window_problem", lambda c: called.append(1) or "closed")
    monkeypatch.setattr(canary, "snapshot", lambda c: {"canary_position": None, "canary_open": []})
    canary.cmd_sell(SimpleNamespace(), SimpleNamespace(limit=299.0))
    assert called == []
