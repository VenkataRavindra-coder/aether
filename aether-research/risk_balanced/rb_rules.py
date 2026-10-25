"""Risk-balanced ETF holding (registration risk-balanced-v1). Pure; the binding copy.

Registered in docs/PREREG_risk_balanced_v1.md (tag `prereg-risk-balanced-v1`) before any result exists.
Research only: nothing here places, changes or cancels an order, and the engine never imports it.
Changing any constant or rule below makes a NEW strategy that needs its own registration and counts toward N.

Universe: SPY, QQQ, IWM, GLD, TLT. Always fully invested (never in cash). Adjusted daily open and close.

- Weights: inverse volatility. For each ETF, sigma = sample standard deviation of its last VOL_N daily
  close-to-close returns, all ending at the close BEFORE the rebalance day. w_s = (1/sigma_s) / sum(1/sigma).
- Rebalance: at the OPEN of the first trading day of every month (and at the open of the first day of the
  window). Between rebalances the holdings drift with prices.
- Costs: 5 bp per side on the traded notional, sum over ETFs of |target value - current value| at the open
  (stress: 10 bp). The first purchase pays cost on the whole portfolio.

Variants (exactly three, frozen): base VOL_N = 63; neighbours 21 and 126 (robustness only, criterion C12).

Comparator built by the same function: equal weights (1/5 each), same monthly schedule and costs.
"""
from __future__ import annotations

import math

SYMBOLS = ("SPY", "QQQ", "IWM", "GLD", "TLT")
VARIANTS = {"rb_63": 63, "rb_21": 21, "rb_126": 126}
BASE = "rb_63"
COST_BP_PER_SIDE = 5.0
STRESS_COST_BP_PER_SIDE = 10.0


def inv_vol_weights(closes: dict, i: int, n: int) -> dict:
    """Weights for a rebalance at the open of day i, from closes up to day i-1 (no look-ahead)."""
    inv = {}
    for s in SYMBOLS:
        c = closes[s]
        r = [c[k] / c[k - 1] - 1 for k in range(i - n, i)]
        m = sum(r) / n
        sd = math.sqrt(sum((x - m) ** 2 for x in r) / (n - 1))
        if not sd > 0:
            raise ValueError(f"{s}: zero volatility before day {i}")
        inv[s] = 1.0 / sd
    tot = sum(inv.values())
    return {s: v / tot for s, v in inv.items()}


def equal_weights(closes: dict, i: int, n: int = 0) -> dict:
    return {s: 1.0 / len(SYMBOLS) for s in SYMBOLS}


def rebalance_days(dates, start: int) -> list[int]:
    return [start] + [i for i in range(start + 1, len(dates)) if dates[i][:7] != dates[i - 1][:7]]


def simulate(dates, data, start: int, weight_fn, n: int, cost_bp_per_side: float = COST_BP_PER_SIDE):
    """Daily portfolio returns from `start` (value 1.0 before the first open), turnover per rebalance and the
    weights used. data: {symbol: (opens, closes)} on one shared calendar."""
    c = cost_bp_per_side / 1e4
    opens = {s: data[s][0] for s in SYMBOLS}
    closes = {s: data[s][1] for s in SYMBOLS}
    rebal = set(rebalance_days(dates, start))
    hold = {s: 0.0 for s in SYMBOLS}
    value_prev_close = 1.0
    rets, turnover, weights_log = [], [], []
    for i in range(start, len(dates)):
        if i in rebal:
            w = weight_fn(closes, i, n)
            v_open = (sum(hold[s] * opens[s][i] for s in SYMBOLS) if i != start else 1.0)
            traded = sum(abs(w[s] * v_open - hold[s] * opens[s][i]) for s in SYMBOLS)
            v_open -= c * traded
            hold = {s: w[s] * v_open / opens[s][i] for s in SYMBOLS}
            turnover.append(traded / (v_open + c * traded))
            weights_log.append((dates[i], w))
        v_close = sum(hold[s] * closes[s][i] for s in SYMBOLS)
        rets.append(v_close / value_prev_close - 1)
        value_prev_close = v_close
    return rets, turnover, weights_log
