"""Price-sanity guard tests (plan C8, D6): pure helper plus the reference closes us_signals now returns.

Run from the repo root:  venv/bin/python3 -m pytest -q tests/test_price_sanity.py
"""
import datetime
import pathlib
import random
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import pytest

import us_strategy as us
from aether_engine import us_signals

REF = {"SPY": 500.0, "QQQ": 400.0, "GLD": 200.0}


def test_normal_prices_pass():
    assert us.price_sanity({"SPY": 503.0, "QQQ": 397.0, "GLD": 201.0}, REF) == []


def test_boundary_at_the_default_limit():
    assert us.price_sanity({"SPY": 500.0 * 1.099}, REF) == []
    assert us.price_sanity({"SPY": 500.0 * 1.101}, REF)
    assert us.price_sanity({"SPY": 500.0 * 0.901}, REF) == []   # -9.9%
    assert us.price_sanity({"SPY": 500.0 * 0.899}, REF)          # -10.1%


@pytest.mark.parametrize("factor", [1.25, 0.75, 5.0, 0.1])
def test_spikes_are_reported_by_symbol(factor):
    problems = us.price_sanity({"SPY": 500.0, "QQQ": 400.0 * factor}, REF)
    assert len(problems) == 1 and problems[0].startswith("QQQ")


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), 0.0, -3.0, None, "400"])
def test_unusable_prices_are_problems(bad):
    problems = us.price_sanity({"QQQ": bad}, REF)
    assert problems and "unusable" in problems[0]


def test_missing_or_bad_reference_is_a_problem():
    assert "no valid reference" in us.price_sanity({"TLT": 90.0}, REF)[0]
    assert "no valid reference" in us.price_sanity({"SPY": 500.0}, {"SPY": float("nan")})[0]
    assert "no valid reference" in us.price_sanity({"SPY": 500.0}, {"SPY": 0.0})[0]


def test_custom_limit_and_all_problems_are_listed():
    assert us.price_sanity({"SPY": 510.0}, REF, max_dev=0.01)
    assert us.price_sanity({"SPY": 510.0}, REF, max_dev=0.05) == []
    both = us.price_sanity({"SPY": 900.0, "QQQ": 100.0}, REF)
    assert len(both) == 2


def test_empty_prices_have_no_problems():
    assert us.price_sanity({}, REF) == []


# ---------------------------------------------------------------- us_signals now returns reference closes
def weekday_dates(end, n=420):
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= datetime.timedelta(days=1)
    return list(reversed(out))


def universe(end):
    data = {}
    for i, s in enumerate(us.UNIVERSE):
        rng, c, bars = random.Random(i + 1), 100.0, []
        for d in weekday_dates(end):
            c *= 1 + rng.gauss(0.0003, 0.01)
            bars.append({"d": d.isoformat(), "c": c})
        data[s] = bars
    return data


def test_signals_return_a_reference_close_for_every_symbol():
    data = universe(datetime.date(2026, 7, 14))
    sig = us_signals(lambda syms: data, "2026-07-15")
    assert set(sig["latest_close"]) == set(us.UNIVERSE)
    for s in us.UNIVERSE:
        assert sig["latest_close"][s] == data[s][-1]["c"]  # newest bar before today


def test_todays_partial_bar_is_never_the_reference():
    data = universe(datetime.date(2026, 7, 14))
    spiked = {s: bars + [{"d": "2026-07-15", "c": bars[-1]["c"] * 1.5}] for s, bars in data.items()}
    sig = us_signals(lambda syms: spiked, "2026-07-15")
    assert sig["latest_close"]["SPY"] == data["SPY"][-1]["c"]


def test_reference_comes_from_the_unfiltered_series_not_the_month_filter():
    # signals use completed months only (June), but the reference is the last close before today (July 14)
    data = universe(datetime.date(2026, 7, 14))
    sig = us_signals(lambda syms: data, "2026-07-15")
    assert sig["signal_date"] == "2026-06-30"
    assert sig["latest_close"]["QQQ"] == data["QQQ"][-1]["c"]


def test_a_bad_latest_price_against_these_references_is_caught():
    data = universe(datetime.date(2026, 7, 14))
    sig = us_signals(lambda syms: data, "2026-07-15")
    prices = {s: sig["latest_close"][s] * 1.01 for s in us.UNIVERSE}
    assert us.price_sanity(prices, sig["latest_close"]) == []
    prices["EEM"] = sig["latest_close"]["EEM"] * 1.3
    assert [p.split(":")[0] for p in us.price_sanity(prices, sig["latest_close"])] == ["EEM"]
