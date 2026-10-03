"""Run: python test_reversal.py   (no pytest needed)"""
import numpy as np
import pandas as pd

from reversal_backtest import (LongDetector, Params, check_exit, metrics, mirror,
                               pdt_blocked, run_backtest, size_position)

ZONE = [(99.8, 100.2)]


def long_setup():
    """Fast drop into 99.8-100.2, trend break, higher low at 100.4, strong candle at bar 23."""
    rows = []
    for t in range(15):
        cl = 103 - 0.2 * t
        rows.append((cl + 0.2, cl + 0.22, cl - 0.02, cl))
    rows += [
        (100.2, 100.22, 99.9, 100.0),     # 15 touch
        (100.0, 100.1, 99.95, 100.05),    # 16
        (100.05, 100.15, 100.0, 100.1),   # 17
        (100.1, 100.95, 100.08, 100.9),   # 18 trend break
        (100.9, 100.92, 100.6, 100.65),   # 19
        (100.65, 100.7, 100.4, 100.45),   # 20 higher low
        (100.5, 100.6, 100.48, 100.55),   # 21
        (100.55, 100.65, 100.5, 100.6),   # 22 higher low confirmed
        (100.6, 101.02, 100.58, 101.0),   # 23 strong candle -> signal
        (101.0, 101.1, 100.9, 101.05),    # 24
    ]
    return [np.array(x, dtype=float) for x in zip(*rows)]


def signals(o, h, l, c, zones, p=None):
    d, a = LongDetector(p or Params()), np.full(len(o), 0.2)
    return [(i, s) for i in range(len(o)) if (s := d.step(i, o, h, l, c, a, zones)) is not None]


def test_long_setup_fires_once_with_expected_stop():
    sig = signals(*long_setup(), ZONE)
    assert len(sig) == 1 and sig[0][0] == 23, sig
    assert abs(sig[0][1] - (100.4 - 0.1 * 0.2)) < 1e-9


def test_short_is_exact_mirror():
    o, h, l, c = long_setup()
    # real short setup = long setup reflected around 100 (spike up into resistance 99.8-100.2)
    mo, mh, ml, mc, mz = mirror(200 - o, 200 - l, 200 - h, 200 - c, ZONE)
    sig = signals(mo, mh, ml, mc, mz)
    assert len(sig) == 1 and sig[0][0] == 23, sig
    assert abs(-sig[0][1] - (200 - 100.38)) < 1e-9


def test_no_lookahead():
    o, h, l, c = long_setup()
    rng = np.random.default_rng(0)
    base = signals(o, h, l, c, ZONE)
    for cut in range(10, len(o)):
        junk = [x.copy() for x in (o, h, l, c)]
        for x in junk:
            x[cut + 1:] = rng.uniform(50, 150, len(x) - cut - 1)
        got = [s for s in signals(*junk, ZONE) if s[0] <= cut]
        assert got == [s for s in base if s[0] <= cut], cut


def test_no_signal_without_fast_move():
    o, h, l, c = long_setup()
    sig = signals(o, h, l, c, ZONE, Params(fast_min_atr=50))
    assert sig == []


def test_sizing_and_notional_cap():
    p = Params(asset="stock", equity=5000, risk_pct=0.01, max_leverage=1.0)
    qty, capped = size_position(p, 600.0, 1.0)      # wants 50 sh = $30k notional
    assert (qty, capped) == (8.0, True)
    qty, capped = size_position(p, 600.0, 10.0)     # 5 sh = $3k, fits
    assert (qty, capped) == (5.0, False)
    pc = Params(asset="crypto", equity=5000, risk_pct=0.01, max_leverage=1.0)
    qty, capped = size_position(pc, 60000.0, 1000.0)   # 0.05 BTC = $3k, fits
    assert abs(qty - 0.05) < 1e-9 and not capped


def test_exit_rules():
    pos = dict(entry_i=5, stop=99.0, target=102.0)
    assert check_exit(pos, 5, 100, 102.5, 98.5) == (99.0, "stop")      # both touched: stop first
    assert check_exit(pos, 6, 98.0, 98.5, 97.0) == (98.0, "stop")      # gap through stop
    assert check_exit(pos, 6, 100, 102.1, 99.5) == (102.0, "target")
    assert check_exit(pos, 6, 100, 101, 99.5) == (None, None)


def test_pdt():
    s = ["d1", "d2", "d3", "d4", "d5"]
    assert pdt_blocked(["d3", "d4", "d5"], s, "d6") is True
    assert pdt_blocked(["d1", "d2", "d3"], s, "d6") is False           # d1 aged out


def test_engine_smoke_random_walk():
    rng = np.random.default_rng(1)
    idx = pd.date_range("2025-01-01", periods=60 * 24 * 40, freq="1min", tz="UTC")
    close = 60000 * np.exp(np.cumsum(rng.normal(0, 0.0008, len(idx))))
    op = np.r_[close[0], close[:-1]]
    spread = np.abs(rng.normal(0, 0.0005, len(idx))) * close
    df = pd.DataFrame(dict(open=op, close=close, high=np.maximum(op, close) + spread,
                           low=np.minimum(op, close) - spread), index=idx)
    for mode in ("us_open", "all_hours"):
        p = Params(symbol="BTC/USD", asset="crypto", mode=mode, allow_short=False,
                   fee_rate=0.0025, slippage_bps=5, pdt_limit=False, fast_min_atr=2.0)
        trades, skips = run_backtest(df, p)
        if not trades.empty:
            assert (trades["side"] == "long").all()
            assert (trades["exit_time"] >= trades["entry_time"]).all()
            if mode == "us_open":
                et = trades["entry_time"].dt.tz_convert("America/New_York")
                m = et.dt.hour * 60 + et.dt.minute
                assert ((m >= 585) & (m <= 600)).all()
            assert (trades.groupby(trades["entry_time"].dt.tz_convert("America/New_York").dt.date
                                   if mode == "us_open" else trades["entry_time"].dt.date).size() <= 1).all()
        m = metrics(trades, p, idx[0], idx[-1])
        print(f"  smoke {mode}: {m.get('trades')} trades, skips={skips}")


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for t in tests:
        t()
        print("ok ", t.__name__)
    print(f"{len(tests)} tests passed")
