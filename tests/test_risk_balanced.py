"""Risk-balanced ETF holding registration (aether-research/risk_balanced): offline, synthetic data only."""
import math
import random
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "aether-research" / "risk_balanced"))
sys.path.insert(0, str(ROOT / "aether-research" / "etf_breakout"))
sys.path.insert(0, str(ROOT / "aether-research"))
import rb_hist as hist  # noqa: E402
import rb_rules as rules  # noqa: E402


def _dates(start=date(2005, 1, 3), end=date(2026, 10, 6)):
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _walk(n, seed, drift=0.0002, vol=0.01):
    rnd, px, o, c = random.Random(seed), 100.0, [], []
    for _ in range(n):
        op = px * (1 + rnd.gauss(0, vol / 4))
        cl = op * (1 + rnd.gauss(drift, vol))
        o.append(op)
        c.append(cl)
        px = cl
    return o, c


def test_frozen_definitions():
    assert rules.SYMBOLS == ("SPY", "QQQ", "IWM", "GLD", "TLT")
    assert rules.VARIANTS == {"rb_63": 63, "rb_21": 21, "rb_126": 126} and rules.BASE == "rb_63"
    assert (rules.COST_BP_PER_SIDE, rules.STRESS_COST_BP_PER_SIDE) == (5.0, 10.0)
    assert hist.N_FLOOR == 52 and hist.DATA_SHA == "e9524a2a60e1c609af72c991d1be8cc8d42f36d95bcafcf1b1b81dd1971ed9b6"
    assert hist.DD_VS_FILTER == 1.25


def test_inverse_vol_weights_sum_to_one_and_favour_calm_assets():
    n = 300
    closes = {s: _walk(n, i, vol=0.005 if s == "TLT" else 0.02)[1] for i, s in enumerate(rules.SYMBOLS)}
    w = rules.inv_vol_weights(closes, 200, 63)
    assert sum(w.values()) == pytest.approx(1.0)
    assert w["TLT"] > 0.4 and all(w["TLT"] > w[s] for s in rules.SYMBOLS if s != "TLT")


def test_weights_use_only_closes_before_the_rebalance_day():
    n = 300
    closes = {s: _walk(n, i)[1] for i, s in enumerate(rules.SYMBOLS)}
    w = rules.inv_vol_weights(closes, 200, 63)
    changed = {s: c[:200] + [x * 5 for x in c[200:]] for s, c in closes.items()}
    assert rules.inv_vol_weights(changed, 200, 63) == w


def test_rebalance_days_first_trading_day_of_each_month():
    dates = _dates(date(2007, 2, 26), date(2007, 4, 10))
    st = dates.index("2007-03-01")
    days = [dates[i] for i in rules.rebalance_days(dates, st)]
    assert days == ["2007-03-01", "2007-04-02"]


def test_simulate_costs_and_drift_exact_on_two_assets(monkeypatch):
    monkeypatch.setattr(rules, "SYMBOLS", ("A", "B"))
    dates = ["2007-02-28", "2007-03-01", "2007-03-02"]
    data = {"A": ([10, 10, 11], [10, 10, 12]), "B": ([20, 20, 20], [20, 20, 18])}
    r, turn, wl = rules.simulate(dates, data, 1, lambda c, i, n: {"A": 0.5, "B": 0.5}, 0, cost_bp_per_side=10.0)
    v0 = 1 - 0.001                                   # cost on buying the whole portfolio
    assert r[0] == pytest.approx(v0 - 1)
    v1 = 0.5 * v0 / 10 * 12 + 0.5 * v0 / 20 * 18     # drift, no rebalance on 03-02
    assert r[1] == pytest.approx(v1 / v0 - 1) and turn == [pytest.approx(1.0, rel=1e-3)] and len(wl) == 1


def test_paired_sharpe_diff_ci():
    rnd = random.Random(1)
    a = [rnd.gauss(0.001, 0.01) for _ in range(2000)]
    lo, hi = hist.paired_sharpe_diff_ci(a, a)
    assert lo == pytest.approx(0.0) and hi == pytest.approx(0.0)
    better = [x + 0.0008 for x in a]
    assert hist.paired_sharpe_diff_ci(better, a)[0] > 0


def _db(tmp, logged=18, back=28, batch_rows=0):
    import trials
    db = tmp / "r.sqlite"
    con = trials._connect(str(db))
    for i in range(logged):
        con.execute("INSERT INTO research_trials (strategy, batch) VALUES (?, ?)", (f"x{i}", "other"))
    for i in range(batch_rows):
        con.execute("INSERT INTO research_trials (strategy, batch) VALUES (?, ?)", (f"y{i}", hist.BATCH))
    if back:
        con.execute("INSERT INTO untracked_trials (family, count) VALUES ('f', ?)", (back,))
    con.commit()
    con.close()
    return db


def test_trial_n(tmp_path):
    assert hist.trial_n(_db(tmp_path))["n"] == 52                # 18 + 28 + 3 + 3
    with pytest.raises(hist.Refused):
        hist.trial_n(tmp_path / "missing.sqlite")


def test_end_to_end_synthetic_and_log_once(tmp_path):
    dates = _dates()
    data = {s: _walk(len(dates), 7 + i) for i, s in enumerate(rules.SYMBOLS)}
    db = _db(tmp_path)
    res = hist.evaluate(dates, data, hist.trial_n(db))
    assert res["window"] == ["2007-03-01", "2026-10-06"] and len(res["criteria"]) == 12
    assert res["verdict"] in {"historically supported", "historically unsupported", "mixed"}
    assert set(res["comparators"]) >= {"equal_weight_monthly", "sma200_same_etfs", "hold_same_etfs"}
    b = res["variants"]["rb_63"]
    assert sum(b["avg_weights"].values()) == pytest.approx(1.0) and b["rebalances"] > 200
    assert set(res["dollars"]) == {"100", "1000", "5000", "capital_for_target"}
    text = hist.render(res)
    assert "rb_63 (judged)" in text and "capital needed for $2" in text and "S2 NO-GO" in text
    assert len(hist.log_trials(res, "h", db)) == 3
    with pytest.raises(hist.Refused):
        hist.log_trials({**res, "n_info": hist.trial_n(db)}, "h", db)
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM research_trials WHERE batch=?", (hist.BATCH,)).fetchone()[0] == 3
    con.close()


def test_identical_assets_are_not_called_supported():
    """With five copies of one series every weighting equals equal weight, so the weighting cannot beat it."""
    dates = _dates()
    one = _walk(len(dates), 3)
    res = hist.evaluate(dates, {s: one for s in rules.SYMBOLS},
                        {"n": 52, "logged": 18, "backfilled": 28, "outside_db": 3, "pending": 3, "already_logged": 0})
    assert res["verdict"] == "historically unsupported"


def test_research_modules_place_no_orders():
    for f in (ROOT / "aether-research" / "risk_balanced").glob("*.py"):
        src = f.read_text()
        for bad in ("submit_order", "OrderRequest", "TradingClient", "cancel_order", "close_position",
                    "alpaca_executor", "APCA_", "requests.", "yfinance"):
            assert bad not in src, (f.name, bad)
