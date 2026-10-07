"""Tests for aether-research/us_momentum/long_history.py (plan S12, S14, S15 data layer).
Synthetic data only: no yfinance, no network, no files.

Run from the repo root:  venv/bin/python3 -m pytest -q tests/test_long_history.py
"""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
os.environ.setdefault("AETHER_DIR", str(ROOT))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "aether-research" / "us_momentum"))

import numpy as np
import pandas as pd
import pytest

import long_history as lh
import us_strategy as us

UNI = us.UNIVERSE


def walk(seed, n=3000, drift=0.0003, vol=0.01):
    return 100.0 * np.cumprod(1 + np.random.default_rng(seed).normal(drift, vol, n))


def make_rows(n=3000, scale=None, noise_bps=None, seed0=1):
    dates = pd.bdate_range("2010-01-04", periods=n).strftime("%Y-%m-%d")
    frames = []
    for i, s in enumerate(UNI):
        c = walk(seed0 + i, n) * (1.0 if scale is None else scale)
        if noise_bps and s == noise_bps[0]:
            c = c * np.cumprod(1 + np.random.default_rng(99).normal(0, noise_bps[1] / 1e4, n))
        frames.append(pd.DataFrame({"symbol": s, "date": dates, "open": c * 0.999, "close": c}))
    return pd.concat(frames, ignore_index=True)


def yf_style(rows, ticker_first, tz=None):
    """Rebuild what yf.download returns from our row layout, in either column order."""
    pieces = {}
    for s in UNI:
        g = rows[rows["symbol"] == s]
        idx = pd.DatetimeIndex(g["date"])
        if tz:
            idx = idx.tz_localize(tz)
        pieces[s] = pd.DataFrame({"Open": g["open"].to_numpy(), "High": g["close"].to_numpy(),
                                  "Low": g["open"].to_numpy(), "Close": g["close"].to_numpy(),
                                  "Volume": 1000}, index=idx)
    if ticker_first:
        return pd.concat(pieces, axis=1)                         # (ticker, field)
    wide = pd.concat(pieces, axis=1)
    wide.columns = wide.columns.swaplevel(0, 1)                  # (field, ticker)
    return wide.sort_index(axis=1)


# ---------------------------------------------------------------- normalize_yf
@pytest.mark.parametrize("ticker_first", [True, False])
def test_normalize_handles_both_column_orders(ticker_first):
    rows = make_rows(n=300)
    out = lh.normalize_yf(yf_style(rows, ticker_first), UNI)
    assert list(out.columns) == ["symbol", "date", "open", "close"]
    assert set(out["symbol"]) == set(UNI) and len(out) == len(rows)
    a = out.sort_values(["symbol", "date"]).reset_index(drop=True)
    b = rows.sort_values(["symbol", "date"]).reset_index(drop=True)
    assert np.allclose(a["close"], b["close"]) and list(a["date"]) == list(b["date"])


@pytest.mark.parametrize("tz", ["America/New_York", "UTC"])
def test_normalize_keeps_the_printed_day_whatever_the_timezone(tz):
    """A daily stamp at midnight UTC must stay the same calendar day, not slip back to the evening before."""
    rows = make_rows(n=300)
    out = lh.normalize_yf(yf_style(rows, True, tz=tz), UNI)
    a = out.sort_values(["symbol", "date"]).reset_index(drop=True)
    b = rows.sort_values(["symbol", "date"]).reset_index(drop=True)
    assert list(a["date"]) == list(b["date"])


def test_normalize_drops_rows_without_a_close():
    rows = make_rows(n=300)
    raw = yf_style(rows, True)
    raw.loc[raw.index[10], ("SPY", "Close")] = np.nan
    out = lh.normalize_yf(raw, UNI)
    assert len(out[out["symbol"] == "SPY"]) == 299 and len(out[out["symbol"] == "QQQ"]) == 300


def test_normalize_rejects_empty_and_missing_symbols():
    with pytest.raises(ValueError):
        lh.normalize_yf(pd.DataFrame(), UNI)
    raw = yf_style(make_rows(n=300), True).drop(columns="TLT", level=0)
    with pytest.raises(ValueError, match="TLT"):
        lh.normalize_yf(raw, UNI)


# ---------------------------------------------------------------- check_history
def test_a_clean_history_passes():
    assert lh.check_history(make_rows(), UNI) == []


def test_short_history_is_reported():
    rows = make_rows()
    rows = rows[~((rows["symbol"] == "DBC") & (rows["date"] < "2020-01-01"))]
    assert any("DBC" in p and "rows" in p for p in lh.check_history(rows, UNI))


def test_non_finite_and_non_positive_closes_are_reported():
    rows = make_rows()
    rows.loc[rows.index[5], "close"] = float("nan")
    assert any("non-finite" in p for p in lh.check_history(rows, UNI))
    rows = make_rows()
    rows.loc[rows.index[5], "close"] = -1.0
    assert any("non-positive" in p for p in lh.check_history(rows, UNI))


def test_duplicate_and_unordered_dates_are_reported():
    rows = make_rows()
    dup = pd.concat([rows, rows[rows["symbol"] == "SPY"].iloc[[100]]], ignore_index=True)
    assert any("increasing" in p for p in lh.check_history(dup, UNI))


def test_a_long_gap_is_reported():
    rows = make_rows()
    rows = rows[~((rows["symbol"] == "GLD") & (rows["date"] > "2015-03-01") & (rows["date"] < "2015-03-25"))]
    assert any("GLD" in p and "gap" in p for p in lh.check_history(rows, UNI))


def test_an_unadjusted_split_looks_like_a_spike():
    rows = make_rows()
    i = rows[rows["symbol"] == "EFA"].index[1500]
    rows.loc[rows["symbol"].eq("EFA") & (rows.index >= i), "close"] *= 0.5     # a 2:1 split left unadjusted
    assert any("EFA" in p and "move" in p for p in lh.check_history(rows, UNI))


# ---------------------------------------------------------------- comparing sources
def test_two_sources_with_different_levels_but_the_same_returns_agree():
    a = lh.closes_table(make_rows())
    b = lh.closes_table(make_rows(scale=1.37))            # a different dividend anchor: same returns
    res = lh.compare_returns(a, b, UNI)
    assert res["worst_median_bps"] < 1e-6 and res["common_dates"] == 3000


def test_a_noisy_source_is_detected():
    a = lh.closes_table(make_rows())
    b = lh.closes_table(make_rows(noise_bps=("TLT", 40)))
    res = lh.compare_returns(a, b, UNI)
    assert res["per_symbol"]["TLT"]["median_bps"] > 10 and res["per_symbol"]["SPY"]["median_bps"] < 1e-6
    assert res["worst_median_bps"] == res["per_symbol"]["TLT"]["median_bps"]


def test_too_little_overlap_is_an_error():
    a = lh.closes_table(make_rows(n=3000))
    b = lh.closes_table(make_rows(n=3000)).iloc[-100:]
    with pytest.raises(ValueError):
        lh.compare_returns(a, b, UNI)


def test_monthly_signals_use_the_live_rule_on_closes_before_each_month():
    c = lh.closes_table(make_rows(n=1500))
    sig = lh.monthly_signals(c)
    assert sig and all(len(v) == us.TOP_N for v in sig.values())
    first = sorted(sig)[0]
    # the first possible signal needs 253 prior closes: it cannot appear before about a year of data
    assert first >= "2011-01"
    # reproduce one month by hand
    month = sorted(sig)[40]
    dates = list(c.index)
    i = next(k for k, d in enumerate(dates) if d[:7] == month and d[:7] != dates[k - 1][:7])
    hist = {s: list(c[s].iloc[:i]) for s in us.UNIVERSE}
    assert sorted(us.target_weights(hist)[2]) == sig[month]


def test_identical_sources_have_identical_signals_and_pass():
    c = lh.closes_table(make_rows(n=1500))
    sig = lh.compare_signals(lh.monthly_signals(c), lh.monthly_signals(c * 1.2))
    assert sig["agreement"] == 1.0 and sig["differing_months"] == []
    ret = lh.compare_returns(c, c * 1.2, UNI) if len(c) >= 250 else None
    assert lh.verdict(ret, sig)["pass"] is True


def test_signal_agreement_arithmetic_and_errors():
    sa = {f"2020-{m:02d}": ["A"] for m in range(1, 11)}
    sb = dict(sa)
    sb["2020-03"] = ["B"]
    r = lh.compare_signals(sa, sb)
    assert r["agree"] == 9 and r["months"] == 10 and r["agreement"] == 0.9 and r["differing_months"] == ["2020-03"]
    with pytest.raises(ValueError):
        lh.compare_signals({"2020-01": ["A"]}, {"2021-01": ["A"]})


@pytest.mark.parametrize("worst,agreement,expect", [(4.9, 0.95, True), (5.0, 0.99, False), (1.0, 0.94, False),
                                                    (6.0, 0.80, False)])
def test_verdict_thresholds(worst, agreement, expect):
    v = lh.verdict({"worst_median_bps": worst}, {"agreement": agreement})
    assert v["pass"] is expect


def test_a_jump_on_the_first_day_of_a_month_cannot_change_that_months_signal():
    """Signals use closes strictly BEFORE the first trading day. Plant a 5x jump in a symbol that was not selected,
    starting on that very day: the month's selection must not change (a lookahead bug would pick it up)."""
    c = lh.closes_table(make_rows(n=1500))
    sig = lh.monthly_signals(c)
    month = sorted(sig)[40]
    dates = list(c.index)
    i = next(k for k, d in enumerate(dates) if d[:7] == month and d[:7] != dates[k - 1][:7])
    loser = next(s for s in us.RISK_ETFS if s not in sig[month])
    spiked = c.copy()
    spiked.iloc[i:, spiked.columns.get_loc(loser)] *= 5.0
    assert lh.monthly_signals(spiked)[month] == sig[month]
