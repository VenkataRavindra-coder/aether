"""AETHER C-layer tests for the US ETF engine (us_strategy.py: pure logic).

Run from the repo root with the service's interpreter:
    venv/bin/python3 -m pytest -q tests/test_c_layer.py

Covers (plan v2.1 IDs, retargeted to the real monthly-rotation code):
  C1  lookahead (truncate at the signal day, compare with full data) + positive control
  C2  determinism
  C11 indicator correctness (momentum_score vs exact-arithmetic reference) + known answers
  K5  current-month / partial bars cannot reach the signal (EST and EDT dates)
  D3-style: non-finite or non-positive closes must be rejected (see note on that test)
plan_orders invariants (docstring rules): never oversell, buys never exceed cash+proceeds.

Pure logic only: no network, no broker, no keys. Safe to run on the VM at any time.
"""
import datetime
import math
import pathlib
import random
import sys
from fractions import Fraction

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import pytest

import us_strategy as us


# ---------------------------------------------------------------- helpers
def geometric(rate, n=us.MIN_BARS + 20, start=100.0):
    """Closes growing by a constant factor per bar (rate=0.001 -> +0.1% per bar)."""
    return [start * (1.0 + rate) ** i for i in range(n)]


def uniform_universe(rate, symbols=None):
    return {s: geometric(rate) for s in (symbols or us.RISK_ETFS)}


def weekdays(start, n):
    d = start
    out = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += datetime.timedelta(days=1)
    return out


def random_bars(seed, dates, drift=0.0003, vol=0.01):
    rng = random.Random(seed)
    c = 100.0
    bars = []
    for d in dates:
        c *= 1.0 + rng.gauss(drift, vol)
        bars.append({"d": d.isoformat(), "c": c})
    return bars


def closes_of(bars):
    return [b["c"] for b in bars]


# ---------------------------------------------------------------- C11
def exact_momentum(closes):
    """Independent reference using exact rational arithmetic (no float accumulation)."""
    c = [Fraction(x) for x in closes]
    parts = [c[-1] / c[-1 - n] - 1 for n in (63, 126, 252)]
    return float(sum(parts) / 3)


def test_c11_momentum_matches_exact_reference_on_random_series():
    rng = random.Random(42)
    for _ in range(50):
        closes = [100.0]
        for _ in range(us.MIN_BARS + 200):
            closes.append(closes[-1] * (1.0 + rng.gauss(0.0003, 0.012)))
        assert us.momentum_score(closes) == pytest.approx(exact_momentum(closes), abs=1e-12)


def test_c11_momentum_known_answer_constant_growth():
    # +1% per bar: score = mean(1.01^63-1, 1.01^126-1, 1.01^252-1)
    closes = geometric(0.01, n=300)
    expected = ((1.01 ** 63 - 1) + (1.01 ** 126 - 1) + (1.01 ** 252 - 1)) / 3
    assert us.momentum_score(closes) == pytest.approx(expected, rel=1e-9)


def test_c11_short_history_gives_no_score():
    assert us.momentum_score(geometric(0.001, n=us.MIN_BARS - 1)) is None
    assert us.momentum_score(geometric(0.001, n=us.MIN_BARS)) is not None


def test_c11_insufficient_history_for_any_symbol_raises():
    data = uniform_universe(0.001)
    data["TLT"] = data["TLT"][: us.MIN_BARS - 1]
    with pytest.raises(ValueError):
        us.target_weights(data)


def test_c11_missing_symbol_raises():
    data = uniform_universe(0.001)
    del data["GLD"]
    with pytest.raises(ValueError):
        us.target_weights(data)


def test_c11_all_positive_top3_equal_weight():
    data = {s: geometric(0.001 * (i + 1)) for i, s in enumerate(us.RISK_ETFS)}
    weights, scores, selected = us.target_weights(data)
    # strongest growth = last three symbols in RISK_ETFS order
    assert set(selected) == set(us.RISK_ETFS[-3:])
    assert weights == {s: pytest.approx(1 / 3) for s in selected}
    assert sum(weights.values()) == pytest.approx(1.0)


def test_c11_all_negative_goes_fully_to_cash_etf():
    data = uniform_universe(-0.001)
    weights, _, _ = us.target_weights(data)
    assert weights == {us.CASH_ETF: pytest.approx(1.0)}


def test_c11_flat_market_is_cash_not_risk():
    # score exactly 0 is "not positive" -> replaced by the cash ETF
    data = {s: [100.0] * (us.MIN_BARS + 20) for s in us.RISK_ETFS}
    weights, scores, _ = us.target_weights(data)
    assert all(v == 0 for v in scores.values())
    assert weights == {us.CASH_ETF: pytest.approx(1.0)}


def test_c11_mixed_two_positive_one_negative_gives_one_third_cash():
    data = {s: geometric(-0.001) for s in us.RISK_ETFS}
    data["SPY"] = geometric(0.002)
    data["QQQ"] = geometric(0.003)
    weights, _, selected = us.target_weights(data)
    assert "SPY" in selected and "QQQ" in selected
    assert weights["SPY"] == pytest.approx(1 / 3)
    assert weights["QQQ"] == pytest.approx(1 / 3)
    assert weights[us.CASH_ETF] == pytest.approx(1 / 3)
    assert sum(weights.values()) == pytest.approx(1.0)


def test_c11_ties_break_alphabetically():
    data = uniform_universe(0.001)  # identical series -> identical scores
    _, _, selected = us.target_weights(data)
    assert selected == sorted(us.RISK_ETFS)[: us.TOP_N]


def test_c11_weights_always_sum_to_one_random_universes():
    rng = random.Random(7)
    for _ in range(200):
        data = {s: geometric(rng.uniform(-0.002, 0.003)) for s in us.RISK_ETFS}
        weights, _, selected = us.target_weights(data)
        assert len(selected) == us.TOP_N
        assert sum(weights.values()) == pytest.approx(1.0)
        assert all(w > 0 for w in weights.values())
        assert set(weights) <= set(us.UNIVERSE)


# ---------------------------------------------------------------- C2
def test_c2_target_weights_deterministic():
    rng = random.Random(3)
    data = {s: geometric(rng.uniform(-0.001, 0.002)) for s in us.RISK_ETFS}
    assert us.target_weights(data) == us.target_weights(data)


def test_c2_plan_orders_deterministic():
    holdings = {"SPY": 3.0, "GLD": 5.0}
    prices = {"SPY": 500.0, "GLD": 200.0, "QQQ": 400.0}
    weights = {"QQQ": 0.5, "SPY": 0.5}
    a = us.plan_orders(holdings, prices, weights, 5000.0)
    b = us.plan_orders(holdings, prices, weights, 5000.0)
    assert a == b


# ---------------------------------------------------------------- C1
DATES = weekdays(datetime.date(2022, 1, 3), 900)  # ~3.5 years of weekdays
ALL_BARS = {s: random_bars(100 + i, DATES) for i, s in enumerate(us.UNIVERSE)}


def signal_from(bars_by_symbol, today):
    closes = {s: closes_of(us.bars_before_month(b, today)) for s, b in bars_by_symbol.items()}
    return us.target_weights(closes)[0]


def first_days_of_month(dates, last_n=18):
    seen, out = set(), []
    for d in dates:
        k = d.strftime("%Y-%m")
        if k not in seen:
            seen.add(k)
            out.append(d)
    return out[-last_n:]


def test_c1_signal_unchanged_by_future_data():
    """Truncate every symbol's data at the signal day; the signal must equal the full-data signal."""
    for day in first_days_of_month(DATES):
        today = day.isoformat()
        truncated = {s: [b for b in bars if b["d"] <= today] for s, bars in ALL_BARS.items()}
        assert signal_from(truncated, today) == signal_from(ALL_BARS, today), today


def test_c1_positive_control_detector_can_see_leakage():
    """A deliberately leaky signal (uses ALL bars) must differ from the clean one at least once,
    otherwise the lookahead test above could not detect anything."""
    differs = 0
    for day in first_days_of_month(DATES, last_n=12):
        today = day.isoformat()
        clean = signal_from(ALL_BARS, today)
        leaky = us.target_weights({s: closes_of(b) for s, b in ALL_BARS.items()})[0]
        truncated_leaky = us.target_weights(
            {s: closes_of([x for x in b if x["d"] <= today]) for s, b in ALL_BARS.items()})[0]
        if leaky != truncated_leaky:
            differs += 1
        assert clean  # sanity: clean signal exists
    assert differs > 0, "leakage not detectable on this data: C1 would be blind"


# ---------------------------------------------------------------- K5 (retargeted)
@pytest.mark.parametrize("today", ["2026-07-15", "2027-01-13", "2026-07-01", "2027-03-01"])
def test_k5_current_month_and_partial_bars_never_reach_the_signal(today):
    """US signals use completed months only. A +10% bar dated today (or any day of the current
    month, e.g. an after-hours/partial bar) must not change the weights. July = EDT, January = EST;
    daily bars carry dates not clock times, so DST cannot matter, which this asserts structurally."""
    cutoff = datetime.date.fromisoformat(today[:7] + "-01")
    dates = [d for d in weekdays(cutoff - datetime.timedelta(days=500), 400) if d < cutoff]
    base = {s: random_bars(500 + i, dates) for i, s in enumerate(us.UNIVERSE)}
    control = signal_from(base, today)

    spiked = {}
    for s, bars in base.items():
        extra = list(bars)
        last_c = bars[-1]["c"]
        extra.append({"d": today, "c": last_c * 1.10})  # +10% bar dated today
        extra.append({"d": today[:7] + "-02", "c": last_c * 5.0})  # absurd bar earlier/later same month
        spiked[s] = extra
    assert signal_from(spiked, today) == control


def test_k5_month_boundary_rules():
    bars = [{"d": "2026-06-30", "c": 1.0}, {"d": "2026-07-01", "c": 2.0}, {"d": "2026-07-02", "c": 3.0}]
    kept = us.bars_before_month(bars, "2026-07-15")
    assert [b["d"] for b in kept] == ["2026-06-30"]
    # signal day itself is the first of the month: that day's bar is still excluded
    assert [b["d"] for b in us.bars_before_month(bars, "2026-07-01")] == ["2026-06-30"]
    assert [b["d"] for b in us.bars_before_month(bars, "2026-08-01")] == ["2026-06-30", "2026-07-01", "2026-07-02"]


# ---------------------------------------------------------------- plan_orders invariants
def by_side(orders, side):
    return [o for o in orders if o["side"] == side]


def test_plan_orders_fresh_account_buys_full_weights():
    prices = {"SPY": 500.0, "QQQ": 400.0, "GLD": 200.0}
    weights = {"SPY": 1 / 3, "QQQ": 1 / 3, "GLD": 1 / 3}
    orders = us.plan_orders({}, prices, weights, 5000.0)
    assert by_side(orders, "SELL") == []
    assert sum(o["notional"] for o in by_side(orders, "BUY")) == pytest.approx(5000.0)


def test_plan_orders_never_sells_more_than_held():
    prices = {"SPY": 500.0, "QQQ": 400.0}
    orders = us.plan_orders({"SPY": 2.0}, prices, {"QQQ": 1.0}, 1000.0)
    sells = by_side(orders, "SELL")
    assert len(sells) == 1 and sells[0]["symbol"] == "SPY"
    assert sells[0]["qty"] <= 2.0 + 1e-12


def test_plan_orders_buys_scaled_to_available_cash():
    prices = {"QQQ": 400.0, "GLD": 200.0}
    orders = us.plan_orders({}, prices, {"QQQ": 1.0, "GLD": 1.0}, 5000.0)  # weights sum to 2
    assert sum(o["notional"] for o in by_side(orders, "BUY")) == pytest.approx(5000.0)


def test_plan_orders_at_target_generates_nothing():
    prices = {"SPY": 500.0, "QQQ": 400.0}
    holdings = {"SPY": 5.0, "QQQ": 6.25}  # 2500 + 2500
    assert us.plan_orders(holdings, prices, {"SPY": 0.5, "QQQ": 0.5}, 5000.0) == []


def test_plan_orders_sells_come_before_buys():
    prices = {"SPY": 500.0, "QQQ": 400.0}
    orders = us.plan_orders({"SPY": 10.0}, prices, {"QQQ": 1.0}, 5000.0)
    sides = [o["side"] for o in orders]
    assert sides == sorted(sides, key=lambda s: 0 if s == "SELL" else 1)


def test_plan_orders_total_buys_never_exceed_cash_plus_proceeds_random():
    rng = random.Random(11)
    syms = us.UNIVERSE
    for _ in range(300):
        prices = {s: rng.uniform(20, 600) for s in syms}
        holdings = {s: rng.uniform(0, 5) for s in rng.sample(syms, rng.randint(0, 4))}
        picks = rng.sample(us.RISK_ETFS, 3)
        weights = {s: 1 / 3 for s in picks}
        held_value = sum(q * prices[s] for s, q in holdings.items())
        equity = held_value + rng.uniform(0, 3000)
        orders = us.plan_orders(holdings, prices, weights, equity)
        sells = by_side(orders, "SELL")
        buys = by_side(orders, "BUY")
        for o in sells:
            assert o["qty"] <= holdings[o["symbol"]] + 1e-9
        cash = equity - held_value
        proceeds = sum(o["est_value"] for o in sells)
        assert sum(b["notional"] for b in buys) <= max(cash, 0.0) + proceeds + 1e-6


# ---------------------------------------------------------------- D3-style (bad data)
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), 0.0, -5.0])
def test_bad_closes_must_be_rejected_not_silently_scored(bad):
    """Fail-closed expectation: a non-finite or non-positive close must make target_weights raise
    (ValueError) instead of producing weights. If this FAILS, the pure function trusts its input;
    that is only acceptable if the caller (fetch_daily / aether_engine) validates the data first,
    and that validation then needs its own test (plan D3/D4). Do not weaken this test to pass it."""
    data = uniform_universe(0.001)
    series = list(data["SPY"])
    series[-1 - 63] = bad  # corrupt a lookback anchor
    data["SPY"] = series
    with pytest.raises((ValueError, ZeroDivisionError)):
        us.target_weights(data)
