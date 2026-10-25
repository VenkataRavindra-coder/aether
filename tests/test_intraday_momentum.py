"""Intraday momentum registration (aether-research/intraday_momentum): offline, synthetic bars, temp files only."""
import math
import random
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "aether-research" / "intraday_momentum"))
sys.path.insert(0, str(ROOT / "aether-research" / "tactical_hist"))
sys.path.insert(0, str(ROOT / "aether-research"))
import im_hist as hist  # noqa: E402
import im_rules as rules  # noqa: E402
from aether_tactical.bars import Bar  # noqa: E402
from aether_tactical.market_time import NY, Session  # noqa: E402


def _days(n, start=date(2016, 1, 4)):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _session(d, close_h=16):
    return Session(d.isoformat(), datetime(d.year, d.month, d.day, 9, 30, tzinfo=NY),
                   datetime(d.year, d.month, d.day, close_h, 0, tzinfo=NY))


def _flat(s, px):
    n = int((s.close - s.open) / timedelta(minutes=1))
    return [Bar(s.open + timedelta(minutes=i), px, px, px, px, 1000) for i in range(n)]


def _set(bars, s, minute_from_open, o=None, c=None):
    """Replace one bar (by minute index from the open) with given open/close; keeps it a valid bar."""
    i = minute_from_open
    b = bars[i]
    o = b.o if o is None else o
    c = b.c if c is None else c
    bars[i] = Bar(b.t, o, max(o, c), min(o, c), c, b.v)


def _day(d, prev_close, r1, last_ret, close_h=16):
    """A session whose 09:59 bar closes at prev_close*(1+r1) and whose last half hour returns last_ret."""
    s = _session(d, close_h)
    px = prev_close * (1 + r1)
    bars = _flat(s, px)
    n = len(bars)
    _set(bars, s, 29, o=prev_close, c=px)
    _set(bars, s, n - 1, o=px, c=px * (1 + last_ret))
    return s, bars


def _random_sessions(n, seed=1, drift=0.00002):
    rnd, px, out = random.Random(seed), 200.0, []
    for d in _days(n):
        s = _session(d)
        bars = []
        for i in range(390):
            o = px
            c = o * (1 + rnd.gauss(drift, 0.0008))
            h, l = max(o, c) * (1 + abs(rnd.gauss(0, 0.0003))), min(o, c) * (1 - abs(rnd.gauss(0, 0.0003)))
            bars.append(Bar(s.open + timedelta(minutes=i), o, h, l, c, 1000))
            px = c
        out.append((s, bars))
    return out


# ------------------------------------------------------------------ frozen definitions
def test_exactly_two_frozen_variants_and_constants():
    assert rules.VARIANTS == {"imom_long": False, "imom_ls": True}
    assert rules.SIGNAL_OFFSET == timedelta(minutes=29)
    assert rules.ENTRY_BEFORE_CLOSE == timedelta(minutes=30)
    assert (rules.COST_BP_PER_SIDE, rules.STRESS_COST_BP_PER_SIDE) == (5.0, 10.0)
    assert hist.N_FLOOR == 46


def test_signal_entry_exit_prices_and_timing():
    d0, d1 = _days(2)
    s0 = _session(d0)
    prev = (s0, _flat(s0, 100.0))
    cur = _day(d1, 100.0, 0.01, 0.002)
    r1, fill, exit_px, entry_t, exit_t = rules.day_inputs(prev, cur)
    assert r1 == pytest.approx(0.01)
    assert entry_t == cur[0].close - timedelta(minutes=30) and entry_t.hour == 15 and entry_t.minute == 30
    assert fill == pytest.approx(101.0)           # entry bar's OPEN
    assert exit_px == pytest.approx(101.0 * 1.002)  # last bar's CLOSE
    assert exit_t == cur[0].close - timedelta(minutes=1)


def test_long_only_skips_down_mornings_and_ls_shorts_them():
    d0, d1, d2 = _days(3)
    s0 = _session(d0)
    sess = [(s0, _flat(s0, 100.0)), _day(d1, 100.0, 0.01, 0.002)]
    sess.append(_day(d2, sess[1][1][-1].c, -0.01, -0.003))
    long_ = rules.run(sess, "imom_long")
    ls = rules.run(sess, "imom_ls")
    assert [t["side"] for t in long_] == ["long"]
    assert [t["side"] for t in ls] == ["long", "short"]
    assert long_[0]["gross_bp"] == pytest.approx(20.0)
    assert long_[0]["net_bp"] == pytest.approx(10.0)            # 10 bp round trip
    assert ls[1]["gross_bp"] == pytest.approx(30.0)              # short gains when the last half hour falls
    assert ls[1]["net_bp"] == pytest.approx(20.0)


def test_zero_r1_no_trade():
    assert rules.direction(0.0, True) == 0 and rules.direction(0.0, False) == 0


def test_early_close_uses_1230_entry():
    d0, d1 = _days(2)
    s0 = _session(d0)
    cur = _day(d1, 100.0, 0.005, 0.001, close_h=13)
    x = rules.day_inputs((s0, _flat(s0, 100.0)), cur)
    assert x[3].hour == 12 and x[3].minute == 30


def test_missing_bars_or_failed_sessions_mean_no_trade():
    d0, d1 = _days(2)
    s0 = _session(d0)
    good_prev = (s0, _flat(s0, 100.0))
    s, bars = _day(d1, 100.0, 0.01, 0.002)
    assert rules.day_inputs((s0, []), (s, bars)) is None                          # previous session failed
    assert rules.day_inputs(good_prev, (s, [])) is None                           # this session failed
    assert rules.day_inputs(good_prev, (s, [b for b in bars if b.t != s.open + timedelta(minutes=29)])) is None
    assert rules.day_inputs(good_prev, (s, [b for b in bars if b.t != s.close - timedelta(minutes=30)])) is None
    assert rules.day_inputs(good_prev, (s, bars[:-10])) is None                   # no bar in final 5 minutes
    assert rules.day_inputs((s0, good_prev[1][:-10]), (s, bars)) is None          # previous close not final


def test_daily_returns_flat_overnight_and_zero_on_no_trade_days():
    sess = _random_sessions(20, seed=3)
    trades = rules.run(sess, "imom_long")
    daily = hist.daily_returns(sess, trades)
    assert len(daily) == 19
    assert sum(1 for _, x in daily if x != 0) == len(trades)
    assert math.fsum(x for _, x in daily) == pytest.approx(math.fsum(t["net_bp"] for t in trades) / 1e4)


def test_random_day_p_is_matched_and_deterministic():
    real = [{"day": "2017-01-03", "net_bp": 50.0}] * 5
    pool = {f"2017-02-{i:02d}": float(i - 15) for i in range(1, 29)}
    a, b = hist.random_entry_p(real, pool, False), hist.random_entry_p(real, pool, False)
    assert a == b and a["p"] == pytest.approx(1 / 1001)
    worse = [{"day": "2017-01-03", "net_bp": -50.0}] * 5
    assert hist.random_entry_p(worse, pool, False)["p"] == pytest.approx(1.0)
    assert hist.random_entry_p(real * 10, dict(list(pool.items())[:3]), False)["p"] is None
    ls = hist.random_entry_p(real, pool, True)
    assert ls["p"] == pytest.approx(1 / 1001) and abs(ls["null_mean"] + 10) < 3   # random sides: ~ -cost


def _db(tmp, logged=0, back=0, batch_rows=0):
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


def test_trial_n_counts_registry_and_floor(tmp_path):
    assert hist.trial_n(_db(tmp_path, logged=9, back=28))["n"] == 46        # floor (9 + 28 + 3 + 2 = 42)
    (tmp_path / "r.sqlite").unlink()
    assert hist.trial_n(_db(tmp_path, logged=20, back=28))["n"] == 53       # 20 + 28 + 3 + 2
    (tmp_path / "r.sqlite").unlink()
    assert hist.trial_n(_db(tmp_path, logged=20, back=28, batch_rows=2))["pending"] == 0
    with pytest.raises(hist.Refused):
        hist.trial_n(tmp_path / "missing.sqlite")


def test_data_hash_pinned():
    hist.check_data_hash("1ca901b4" + "0" * 50 + "5ee745")
    with pytest.raises(hist.Refused):
        hist.check_data_hash("deadbeef" + "0" * 50 + "5ee745")


def test_too_many_bad_sessions_refused():
    def broken(n_bad, n):
        return [(s, [Bar(b.t, -1, b.h, b.l, b.c, b.v) for b in bars]) if i < n_bad else (s, bars)
                for i, (s, bars) in enumerate(_random_sessions(n))]
    with pytest.raises(hist.Refused):
        hist.verified_sessions(broken(2, 50))
    ok, n_bad = hist.verified_sessions(broken(1, 100))
    assert n_bad == 1 and ok[0][1] == []


def test_end_to_end_synthetic_and_log_once(tmp_path):
    sess, _ = hist.verified_sessions(_random_sessions(260, seed=11, drift=0.00004))
    db = _db(tmp_path, logged=9, back=28)
    res = hist.evaluate_all(sess, hist.trial_n(db))
    assert set(res["variants"]) == set(rules.VARIANTS)
    for v in res["variants"].values():
        assert v["verdict"] in {"historically supported", "historically unsupported", "insufficient evidence", "mixed"}
        assert len(v["criteria"]) == 10
    assert res["variants"]["imom_ls"]["trades"] >= res["variants"]["imom_long"]["trades"]
    text = hist.render(res)
    assert "S2 NO-GO" in text and "imom_long" in text
    assert len(hist.log_trials(res, "h", db)) == 2
    with pytest.raises(hist.Refused):
        hist.log_trials({**res, "n_info": hist.trial_n(db)}, "h", db)
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM research_trials WHERE batch=?", (hist.BATCH,)).fetchone()[0] == 2
    con.close()


def test_research_modules_place_no_orders():
    for f in (ROOT / "aether-research" / "intraday_momentum").glob("*.py"):
        src = f.read_text()
        for bad in ("submit_order", "MarketOrderRequest", "LimitOrderRequest", "cancel_order", "close_position",
                    "alpaca_executor", "APCA_LIVE", "TradingClient"):
            assert bad not in src, (f.name, bad)
