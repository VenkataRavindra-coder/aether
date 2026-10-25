"""Multi-ETF channel breakout (registration etf-breakout-v1). Pure; the binding copy.

Registered in docs/PREREG_etf_breakout_v1.md (tag `prereg-etf-breakout-v1`) before any result exists.
Research only: nothing here places, changes or cancels an order, and the engine never imports it.
Changing any constant or rule below makes a NEW strategy that needs its own registration and counts toward N.

Universe: SPY, QQQ, IWM, GLD, TLT. Five independent sleeves, each starting with 1/5 of the capital; a sleeve
is either fully in its ETF or in cash (0% return). Sleeves are never rebalanced against each other.

Per sleeve, on each day's CLOSE t (adjusted daily data, open and close only):
- Entry (when flat): close_t > the highest close of the previous ENTRY_N days (t-ENTRY_N .. t-1).
- Exit  (when long): close_t < the lowest close of the previous EXIT_N days (t-EXIT_N .. t-1).
- The action is executed at the OPEN of day t+1. No action on the last day of data.
- Long only. No stop, no target, no leverage, no pyramiding.

Variants (exactly three, frozen): base (55, 20) and two neighbours (20, 10) and (100, 50). Only the base is
judged; the neighbours are a robustness check (criterion C12) and are logged as trials.

Accounting (whole sleeve, per day):
- entry day: equity x (1 - cost) x close / open;     exit day: equity x open / previous close x (1 - cost);
- held: equity x close / previous close;            flat: unchanged.
- Costs: 5 bp per side (stress 10 bp per side), charged on the sleeve's value at each entry and exit.

Comparators (same sleeves, same costs, same open execution):
- "sma200": long while close_t > its 200-day simple average (including day t), else cash (the S-A rule of
  daily-v1, applied to these five ETFs) -- the breakout must beat it to add anything new;
- "hold":   buy every sleeve at the first open of the window and hold (one entry cost).
"""
from __future__ import annotations

import math

SYMBOLS = ("SPY", "QQQ", "IWM", "GLD", "TLT")
VARIANTS = {"eb_55_20": (55, 20), "eb_20_10": (20, 10), "eb_100_50": (100, 50)}
BASE = "eb_55_20"
SMA_N = 200
COST_BP_PER_SIDE = 5.0
STRESS_COST_BP_PER_SIDE = 10.0


def breakout_actions(close, entry_n: int, exit_n: int, start: int):
    """Actions decided at each close t >= start - 1, executed at open t + 1: {t + 1: 'buy' | 'sell'}.

    The sleeve is flat at the open of `start`; a signal on close start-1 acts at the open of `start`."""
    acts, long_ = {}, False
    first = max(start - 1, entry_n, exit_n)
    for t in range(first, len(close) - 1):
        c = close[t]
        if not long_ and c > max(close[t - entry_n:t]):
            acts[t + 1] = "buy"
            long_ = True
        elif long_ and c < min(close[t - exit_n:t]):
            acts[t + 1] = "sell"
            long_ = False
    return acts


def sma_actions(close, n: int, start: int):
    """Comparator: long while close_t > SMA_n(t) (including t), acting at the next open."""
    acts, long_ = {}, False
    for t in range(max(start - 1, n - 1), len(close) - 1):
        sma = sum(close[t - n + 1:t + 1]) / n
        want = close[t] > sma
        if want != long_:
            acts[t + 1] = "buy" if want else "sell"
            long_ = want
    return acts


def sleeve(open_, close, acts, start: int, cost_bp_per_side: float = COST_BP_PER_SIDE):
    """Daily equity of one sleeve (starting at 1.0 before the open of `start`) and its closed trades."""
    c = cost_bp_per_side / 1e4
    eq, long_, entry_i, entry_eq = 1.0, False, None, None
    path, trades = [], []
    for i in range(start, len(close)):
        a = acts.get(i)
        if a == "buy" and not long_:
            entry_eq = eq
            eq *= (1 - c) * close[i] / open_[i]
            long_, entry_i = True, i
        elif a == "sell" and long_:
            eq *= open_[i] / close[i - 1] * (1 - c)
            trades.append({"entry_i": entry_i, "exit_i": i, "ret": eq / entry_eq - 1, "reason": "exit"})
            long_ = False
        elif long_:
            eq *= close[i] / close[i - 1]
        path.append(eq)
    open_trade = {"entry_i": entry_i, "exit_i": len(close) - 1, "ret": eq / entry_eq - 1, "reason": "data_end"} if long_ else None
    return path, trades, open_trade


def hold_actions(start: int):
    return {start: "buy"}


def portfolio(paths):
    """Daily returns of equal starting sleeves that are never rebalanced: sum of equities, day over day."""
    k = len(paths)
    eq = [sum(p[i] for p in paths) / k for i in range(len(paths[0]))]
    return [eq[0] - 1] + [eq[i] / eq[i - 1] - 1 for i in range(1, len(eq))], eq


def run_variant(data, entry_n: int, exit_n: int, start: int, cost_bp_per_side: float = COST_BP_PER_SIDE):
    """data: {symbol: (opens, closes)} on one shared calendar. Returns per-sleeve results and portfolio returns."""
    out = {}
    for s in SYMBOLS:
        o, c = data[s]
        path, trades, open_t = sleeve(o, c, breakout_actions(c, entry_n, exit_n, start), start, cost_bp_per_side)
        out[s] = {"path": path, "trades": trades, "open": open_t}
    rets, eq = portfolio([out[s]["path"] for s in SYMBOLS])
    return out, rets, eq


def run_comparator(data, kind: str, start: int, cost_bp_per_side: float = COST_BP_PER_SIDE):
    out = {}
    for s in SYMBOLS:
        o, c = data[s]
        acts = sma_actions(c, SMA_N, start) if kind == "sma200" else hold_actions(start)
        out[s] = sleeve(o, c, acts, start, cost_bp_per_side)[0]
    return portfolio([out[s] for s in SYMBOLS])


def is_finite(xs) -> bool:
    return all(math.isfinite(x) for x in xs)
