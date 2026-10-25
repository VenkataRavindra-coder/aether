"""Multi-ETF channel breakout registration (aether-research/etf_breakout): offline, synthetic data only."""
import csv
import gzip
import math
import random
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "aether-research" / "etf_breakout"))
sys.path.insert(0, str(ROOT / "aether-research"))
import eb_hist as hist  # noqa: E402
import eb_rules as rules  # noqa: E402


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


def _data(n, seed=0):
    return {s: _walk(n, seed + i) for i, s in enumerate(rules.SYMBOLS)}


# ------------------------------------------------------------------ frozen definitions
def test_frozen_universe_variants_costs():
    assert rules.SYMBOLS == ("SPY", "QQQ", "IWM", "GLD", "TLT")
    assert rules.VARIANTS == {"eb_55_20": (55, 20), "eb_20_10": (20, 10), "eb_100_50": (100, 50)}
    assert rules.BASE == "eb_55_20" and rules.SMA_N == 200
    assert (rules.COST_BP_PER_SIDE, rules.STRESS_COST_BP_PER_SIDE) == (5.0, 10.0)
    assert hist.N_FLOOR == 49 and hist.START == date(2007, 3, 1) and hist.END == date(2026, 10, 6)
    assert hist.DATA_SHA == "e9524a2a60e1c609af72c991d1be8cc8d42f36d95bcafcf1b1b81dd1971ed9b6"


# ------------------------------------------------------------------ signals
def test_breakout_enters_on_new_high_and_exits_on_new_low():
    close = [10.0] * 5 + [11.0] + [11.5, 11.2, 10.5, 9.0, 9.5]
    acts = rules.breakout_actions(close, entry_n=3, exit_n=2, start=4)
    assert acts == {6: "buy", 9: "sell"}            # close 11 > max(10,10,10) on day 5 -> buy at open 6
    # equal to the prior high is not a breakout
    assert rules.breakout_actions([10.0] * 10, 3, 2, 4) == {}


def test_no_look_ahead_future_closes_cannot_change_past_actions():
    _, c = _walk(400, 3)
    base = rules.breakout_actions(c, 55, 20, 120)
    for cut in (150, 250, 350):
        changed = c[:cut + 1] + [x * 3 for x in c[cut + 1:]]
        got = rules.breakout_actions(changed, 55, 20, 120)
        assert {k: v for k, v in got.items() if k <= cut + 1} == {k: v for k, v in base.items() if k <= cut + 1}


def test_sma_comparator_uses_close_including_today():
    close = [10.0] * 199 + [10.0, 12.0, 12.0, 5.0, 5.0]
    acts = rules.sma_actions(close, 200, start=199)
    assert acts == {201: "buy", 203: "sell"}


# ------------------------------------------------------------------ accounting
def test_sleeve_accounting_matches_trade_return_and_costs():
    o = [100, 100, 101, 103, 104, 102, 100]
    c = [100, 100, 102, 104, 103, 101, 100]
    acts = {2: "buy", 5: "sell"}
    path, trades, open_t = rules.sleeve(o, c, acts, start=1, cost_bp_per_side=5.0)
    k = 1 - 5 / 1e4
    expect = k * 102 / 101 * 104 / 102 * 103 / 104 * (102 / 103) * k
    assert path[-1] == pytest.approx(expect)
    assert trades[0]["ret"] == pytest.approx(expect - 1)
    assert trades[0]["ret"] == pytest.approx(k * k * 102 / 101 - 1)   # = entry at open 101, exit at open 102
    assert open_t is None and path[0] == 1.0


def test_open_position_at_data_end_is_reported_not_counted():
    o, c = [10, 10, 10, 11], [10, 10, 11, 12]
    path, trades, open_t = rules.sleeve(o, c, {2: "buy"}, start=1)
    assert trades == [] and open_t["reason"] == "data_end"


def test_portfolio_is_sum_of_unrebalanced_sleeves():
    r, eq = rules.portfolio([[1.0, 1.1, 1.21], [1.0, 1.0, 0.5]])
    assert eq == pytest.approx([1.0, 1.05, 0.855])
    assert r == pytest.approx([0.0, 0.05, 0.855 / 1.05 - 1])


# ------------------------------------------------------------------ data
def _write_csv(path, dates, data, drop=None):
    with gzip.open(path, "wt", newline="") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "date", "open", "close"])
        for s in rules.SYMBOLS + ("SHY",):
            o, c = data.get(s, ([50.0] * len(dates), [50.0] * len(dates)))
            for i, d in enumerate(dates):
                if drop == (s, d):
                    continue
                w.writerow([s, d, o[i], c[i]])
        w.writerow(["SPY", "2026-10-07", 1, 1])                 # after END: must be ignored


def test_load_shared_calendar_and_refuses_gaps(tmp_path):
    dates = _dates(date(2005, 1, 3), date(2005, 3, 31))
    data = _data(len(dates))
    p = tmp_path / "d.csv.gz"
    _write_csv(p, dates, data)
    got_dates, got = hist.load(p)
    assert got_dates == dates and set(got) == set(rules.SYMBOLS)
    assert got["GLD"][1][3] == pytest.approx(data["GLD"][1][3])
    _write_csv(p, dates, data, drop=("TLT", dates[10]))
    with pytest.raises(hist.Refused):
        hist.load(p)


def test_data_hash_pinned():
    hist.check_data_hash(hist.DATA_SHA)
    with pytest.raises(hist.Refused):
        hist.check_data_hash("0" * 64)


# ------------------------------------------------------------------ trials
def _db(tmp, logged=15, back=28, batch_rows=0):
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
    assert hist.trial_n(_db(tmp_path))["n"] == 49                 # 15 + 28 + 3 + 3
    (tmp_path / "r.sqlite").unlink()
    assert hist.trial_n(_db(tmp_path, logged=20))["n"] == 54
    with pytest.raises(hist.Refused):
        hist.trial_n(tmp_path / "missing.sqlite")


# ------------------------------------------------------------------ end to end on synthetic data
def test_end_to_end_synthetic_criteria_dollars_and_log_once(tmp_path):
    dates = _dates()
    data = _data(len(dates), seed=7)
    db = _db(tmp_path)
    res = hist.evaluate(dates, data, hist.trial_n(db))
    assert res["window"] == ["2007-03-01", "2026-10-06"]
    assert len(res["criteria"]) == 12 and res["verdict"] in {"historically supported", "historically unsupported",
                                                            "insufficient evidence", "mixed"}
    assert set(res["variants"]) == set(rules.VARIANTS)
    assert set(res["comparators"]) == {"sma200_same_etfs", "hold_same_etfs", "spy_hold_report_only", "sixty_forty_report_only"}
    d = res["dollars"]
    assert set(d) == {"100", "1000", "5000", "capital_for_target"}
    assert d["5000"]["usd_per_trading_day"] == pytest.approx(50 * d["100"]["usd_per_trading_day"])
    assert 0 < res["correlation"]["effective_bets"] <= 5.0 + 1e-9
    text = hist.render(res)
    assert "capital needed for $2" in text and "S2 NO-GO" in text and "eb_55_20 (judged)" in text
    assert len(hist.log_trials(res, "h", db)) == 3
    with pytest.raises(hist.Refused):
        hist.log_trials({**res, "n_info": hist.trial_n(db)}, "h", db)
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM research_trials WHERE batch=?", (hist.BATCH,)).fetchone()[0] == 3
    con.close()


def test_noise_is_not_called_supported():
    """Driftless random walks must never pass: the criteria are not easy to satisfy by chance."""
    dates = _dates()
    for seed in (1, 2, 3):
        data = {s: _walk(len(dates), seed * 10 + i, drift=0.0) for i, s in enumerate(rules.SYMBOLS)}
        res = hist.evaluate(dates, data, {"n": 49, "logged": 15, "backfilled": 28, "outside_db": 3, "pending": 3,
                                          "already_logged": 0})
        assert res["verdict"] != "historically supported"


def test_dollars_math():
    d = hist.dollars([0.001] * 252, {2008: -0.1, 2009: 0.2})
    assert d["100"]["usd_per_trading_day"] == pytest.approx(0.1)
    assert d["5000"]["usd_per_year"] == pytest.approx(5000 * 0.001 * 252)
    assert d["1000"]["worst_full_year_usd"] == pytest.approx(-100.0)
    assert d["capital_for_target"] == pytest.approx(2000.0)
    assert hist.dollars([-0.001] * 10, {})["capital_for_target"] is None


def test_research_modules_place_no_orders():
    for f in (ROOT / "aether-research" / "etf_breakout").glob("*.py"):
        src = f.read_text()
        for bad in ("submit_order", "OrderRequest", "TradingClient", "cancel_order", "close_position",
                    "alpaca_executor", "APCA_", "requests.", "yfinance"):
            assert bad not in src, (f.name, bad)
