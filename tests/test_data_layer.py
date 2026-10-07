"""AETHER data-layer tests for the US ETF signal path (plan D2, D3, D4, D5 + freshness).

Run from the repo root with the service's interpreter:
    venv/bin/python3 -m pytest -q tests/test_data_layer.py

Uses aether_engine.us_signals with a FAKE fetch function: no network, no broker, no keys.
Limits under test live in us_strategy.py: MAX_GAP_DAYS, MAX_DAILY_MOVE, MAX_STALENESS_DAYS.
"""
import datetime
import pathlib
import random
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import pytest

import us_strategy as us
from aether_engine import us_signals


def weekday_dates(end, n=420):
    """n weekdays ending on or before `end` (a datetime.date), oldest first."""
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= datetime.timedelta(days=1)
    return list(reversed(out))


def make_bars(seed, end, n=420, drift=0.0003, vol=0.01):
    rng = random.Random(seed)
    c, bars = 100.0, []
    for d in weekday_dates(end, n):
        c *= 1.0 + rng.gauss(drift, vol)
        bars.append({"d": d.isoformat(), "c": c})
    return bars


def universe(end, n=420):
    return {s: make_bars(i + 1, end, n) for i, s in enumerate(us.UNIVERSE)}


def fetcher(data):
    return lambda symbols: {s: list(data.get(s, [])) for s in symbols}


# ---------------------------------------------------------------- us_signals: good and stale data
def test_good_data_gives_signal_dated_at_month_end():
    data = universe(datetime.date(2026, 6, 30))
    sig = us_signals(fetcher(data), "2026-07-15")
    assert sig["signal_date"] == "2026-06-30"
    assert sum(sig["weights"].values()) == pytest.approx(1.0)


def test_weekend_month_end_is_accepted():
    # 2026-01-31 is a Saturday: last trading day is Fri 01-30 (1 day before month-end)
    data = universe(datetime.date(2026, 1, 30))
    assert us_signals(fetcher(data), "2026-02-02")["signal_date"] == "2026-01-30"


def test_holiday_weekend_month_end_is_accepted():
    # 2024-03-31 is a Sunday and Good Friday 03-29 is a holiday: last bar Thu 03-28 (3 days before)
    data = universe(datetime.date(2024, 3, 28))
    assert us_signals(fetcher(data), "2024-04-01")["signal_date"] == "2024-03-28"


def test_data_ending_mid_previous_month_is_rejected():
    """The old check only compared year-month, so a feed that stopped on the 12th passed."""
    data = universe(datetime.date(2026, 6, 12))
    with pytest.raises(ValueError, match="stale"):
        us_signals(fetcher(data), "2026-07-15")


def test_one_stale_symbol_blocks_the_whole_signal():
    data = universe(datetime.date(2026, 6, 30))
    data["SPY"] = [b for b in data["SPY"] if b["d"] <= "2026-06-12"]
    with pytest.raises(ValueError, match="stale"):
        us_signals(fetcher(data), "2026-07-15")


def test_staleness_boundary():
    # month-end 2026-06-30; newest bar 06-25 is exactly 5 days before: allowed. 06-24 is 6: rejected
    ok = universe(datetime.date(2026, 6, 25))
    assert us_signals(fetcher(ok), "2026-07-15")["signal_date"] == "2026-06-25"
    bad = universe(datetime.date(2026, 6, 24))
    with pytest.raises(ValueError, match="stale"):
        us_signals(fetcher(bad), "2026-07-15")


# ---------------------------------------------------------------- us_signals: bad values (D3)
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), 0.0, -3.0])
def test_bad_close_anywhere_in_history_blocks_signal(bad):
    data = universe(datetime.date(2026, 6, 30))
    bars = [dict(b) for b in data["QQQ"]]
    bars[100]["c"] = bad  # mid-history, not just the lookback anchors
    data["QQQ"] = bars
    with pytest.raises(ValueError):
        us_signals(fetcher(data), "2026-07-15")


@pytest.mark.parametrize("factor", [10.0, 0.1, 1.6, 0.55])
def test_price_spike_blocks_signal(factor):
    data = universe(datetime.date(2026, 6, 30))
    bars = [dict(b) for b in data["EEM"]]
    bars[200]["c"] = bars[199]["c"] * factor  # +900%, -90%, +60%, -45%
    data["EEM"] = bars
    with pytest.raises(ValueError, match="move"):
        us_signals(fetcher(data), "2026-07-15")


# ---------------------------------------------------------------- us_signals: structure (D4, D5)
def test_duplicate_date_blocks_signal():
    data = universe(datetime.date(2026, 6, 30))
    bars = list(data["IWM"])
    bars.insert(150, dict(bars[150]))
    data["IWM"] = bars
    with pytest.raises(ValueError, match="increasing"):
        us_signals(fetcher(data), "2026-07-15")


def test_out_of_order_dates_block_signal():
    data = universe(datetime.date(2026, 6, 30))
    bars = list(data["TLT"])
    bars[120], bars[121] = bars[121], bars[120]
    data["TLT"] = bars
    with pytest.raises(ValueError, match="increasing"):
        us_signals(fetcher(data), "2026-07-15")


def test_missing_week_in_middle_blocks_signal():
    data = universe(datetime.date(2026, 6, 30))
    bars = list(data["GLD"])
    del bars[200:205]  # five missing trading days -> 9-day gap
    data["GLD"] = bars
    with pytest.raises(ValueError, match="gap"):
        us_signals(fetcher(data), "2026-07-15")


def test_symbol_with_too_little_history_blocks_signal():
    data = universe(datetime.date(2026, 6, 30))
    data["VNQ"] = data["VNQ"][-100:]
    with pytest.raises(ValueError):
        us_signals(fetcher(data), "2026-07-15")


# ---------------------------------------------------------------- validate_bars limits
def _bars(days, closes):
    d0 = datetime.date(2026, 1, 5)
    return [{"d": (d0 + datetime.timedelta(days=x)).isoformat(), "c": c} for x, c in zip(days, closes)]


def test_validate_bars_gap_boundary():
    us.validate_bars("X", _bars([0, 6], [100.0, 101.0]))  # 6-day gap allowed
    with pytest.raises(ValueError, match="gap"):
        us.validate_bars("X", _bars([0, 7], [100.0, 101.0]))


def test_validate_bars_move_boundary():
    us.validate_bars("X", _bars([0, 1], [100.0, 139.0]))  # +39% allowed
    with pytest.raises(ValueError, match="move"):
        us.validate_bars("X", _bars([0, 1], [100.0, 141.0]))  # +41% rejected


def test_validate_bars_rejects_non_numeric_close():
    with pytest.raises(ValueError):
        us.validate_bars("X", _bars([0, 1], [100.0, "101.0"]))
    with pytest.raises(ValueError):
        us.validate_bars("X", _bars([0, 1], [100.0, None]))


def test_validate_bars_empty_is_ok():
    us.validate_bars("X", [])  # emptiness is caught by target_weights, not here
