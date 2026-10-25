"""200-day trend filter with idle money in T-bills (registration sma-tbill-v1). Pure; the binding copy.

Registered in docs/PREREG_sma_tbill_v1.md (tag `prereg-sma-tbill-v1`) before any result exists.
Research only: nothing here places, changes or cancels an order, and the engine never imports it.
Changing any constant or rule below makes a NEW strategy that needs its own registration and counts toward N.

Universe: SPY, QQQ, IWM, GLD, TLT; idle asset BIL (1-3 month US Treasury bills ETF). Five independent sleeves, each
starting with 1/5 of the capital, never rebalanced against each other. Adjusted daily open and close.

Per sleeve, on each day's CLOSE t:
- ON  if close_t > the simple average of the last SMA_N closes INCLUDING day t; otherwise OFF.
- ON holds the ETF, OFF holds BIL. A change of state at close t is executed at the OPEN of day t+1:
  sell the one, buy the other.
- The sleeve starts on the first day of the window in whatever its state was at the previous close.

Accounting (whole sleeve, per day):
- holding X all day:            equity x close_X / previous close_X
- switching from A to B at open: equity x open_A / previous close_A x (1 - c) x (1 - c) x close_B / open_B
- first day:                    equity x (1 - c) x close / open of the asset bought
- Costs: c = 5 bp per side on every sale and purchase, BIL included (stress 10 bp per side).

Variants (exactly three, frozen): base SMA_N = 200; neighbours 150 and 250 (robustness only, criterion C11).
Comparator built by the same function: the same rule with idle money in cash at 0% (the etf-breakout-v1 comparator
"sma200" for N = 200).
"""
from __future__ import annotations

SYMBOLS = ("SPY", "QQQ", "IWM", "GLD", "TLT")
IDLE = "BIL"
VARIANTS = {"st_200": 200, "st_150": 150, "st_250": 250}
BASE = "st_200"
COST_BP_PER_SIDE = 5.0
STRESS_COST_BP_PER_SIDE = 10.0


def states(close, n: int, start: int) -> list[bool]:
    """ON/OFF at each close from start-1 to the end (index 0 = close start-1). Uses closes up to that day only."""
    out = []
    for t in range(start - 1, len(close)):
        if t < n - 1:
            raise ValueError("not enough history for the moving average")
        out.append(close[t] > sum(close[t - n + 1:t + 1]) / n)
    return out


def sleeve(etf, idle, n: int, start: int, cost_bp_per_side: float = COST_BP_PER_SIDE, idle_cash: bool = False):
    """Daily equity path (1.0 before the first open) and the number of switches.
    etf, idle: (opens, closes). idle_cash=True makes the idle asset earn 0% with no costs (the comparator)."""
    c = cost_bp_per_side / 1e4
    eo, ec = etf
    io, ic = idle
    st = states(ec, n, start)
    on = st[0]
    eq = 1.0
    path, switches = [], 0
    for k, i in enumerate(range(start, len(ec))):
        if k == 0:
            if on:
                eq *= (1 - c) * ec[i] / eo[i]
            elif not idle_cash:
                eq *= (1 - c) * ic[i] / io[i]
        else:
            want = st[k]                                  # state at close i-1 decides the open of day i
            if want != on:
                switches += 1
                if on:                                    # ETF -> idle
                    eq *= eo[i] / ec[i - 1] * (1 - c)
                    if not idle_cash:
                        eq *= (1 - c) * ic[i] / io[i]
                else:                                     # idle -> ETF
                    if not idle_cash:
                        eq *= io[i] / ic[i - 1] * (1 - c)
                    eq *= (1 - c) * ec[i] / eo[i]
                on = want
            elif on:
                eq *= ec[i] / ec[i - 1]
            elif not idle_cash:
                eq *= ic[i] / ic[i - 1]
        path.append(eq)
    return path, switches


def portfolio(paths):
    k = len(paths)
    eq = [sum(p[i] for p in paths) / k for i in range(len(paths[0]))]
    return [eq[0] - 1] + [eq[i] / eq[i - 1] - 1 for i in range(1, len(eq))], eq


def run(data, idle, n: int, start: int, cost_bp_per_side: float = COST_BP_PER_SIDE, idle_cash: bool = False):
    """data: {symbol: (opens, closes)}; idle: (opens, closes) of BIL on the same calendar."""
    out = {s: sleeve(data[s], idle, n, start, cost_bp_per_side, idle_cash) for s in SYMBOLS}
    rets, eq = portfolio([out[s][0] for s in SYMBOLS])
    return rets, {s: {"path": out[s][0], "switches": out[s][1]} for s in SYMBOLS}
