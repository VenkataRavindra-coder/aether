"""Intraday momentum on SPY (registration intraday-momentum-v1). Pure; the binding copy.

Registered in docs/PREREG_intraday_momentum_v1.md (tag `prereg-intraday-momentum-v1`) before any result exists.
Research only: nothing here places, changes or cancels an order, and the engine never imports it.
Changing any constant or rule below makes a NEW strategy that needs its own registration and counts toward N.

Hypothesis source: Gao, Han, Li and Zhou, "Market intraday momentum" (Journal of Financial Economics, 2018):
on SPY, the return from the previous close to 10:00 ET predicted the return of the last half hour. A published
anomaly is a hypothesis source, not evidence; it may have decayed after publication.

Input: regular-hours 1-minute bars per session (aether_tactical.bars.Bar, bar START times), already verified.
A session that failed verification is passed as an empty list: it cannot trade and cannot be the previous
session of the next day.

Per session i (i >= 1), all must hold or there is no trade that day:
- Previous close: the last bar of session i-1, which must start in that session's final 5 minutes.
- Signal minute: the bar starting at open + 29 min (09:59 on a normal day, it closes at 10:00).
  r1 = signal_minute.close / previous_close - 1  (includes the overnight gap, as in the source paper).
- Entry minute: the bar starting at close - 30 min (15:30 normal day, 12:30 early close). Fill = its OPEN.
- Exit: the last bar of the session, which must start in the final 5 minutes. Exit price = its CLOSE.
- Direction by variant:
  - "imom_long": long if r1 > 0, else no trade.
  - "imom_ls":   long if r1 > 0, short if r1 < 0, no trade if r1 == 0. Research only: the live account has
    shorting disabled, so this variant can never go live as is.
- Gross bp = direction x (exit / fill - 1) x 10,000. Net bp = gross - 2 x cost per side.
- Costs: 5 bp per side (10 bp round trip); stress 10 bp per side.
- Flat overnight, at most one trade per session, whole sleeve, no leverage, no stop, no target.

No look-ahead: the signal is final at 10:00, the order would be placed for the 15:30 minute.
"""
from __future__ import annotations

from datetime import timedelta

SIGNAL_OFFSET = timedelta(minutes=29)        # bar starting at open + 29 min closes at open + 30 min (10:00)
ENTRY_BEFORE_CLOSE = timedelta(minutes=30)   # bar starting at close - 30 min (15:30)
EDGE_WINDOW = timedelta(minutes=5)           # previous-close and exit bars must start in the final 5 minutes
COST_BP_PER_SIDE = 5.0
STRESS_COST_BP_PER_SIDE = 10.0

# Exactly two variants, frozen: name -> allows short
VARIANTS = {
    "imom_long": False,
    "imom_ls": True,
}


def _at(bars, t):
    """The bar starting exactly at t, or None. Bars are sorted by start time."""
    lo, hi = 0, len(bars)
    while lo < hi:
        mid = (lo + hi) // 2
        if bars[mid].t < t:
            lo = mid + 1
        else:
            hi = mid
    return bars[lo] if lo < len(bars) and bars[lo].t == t else None


def day_inputs(prev, cur):
    """(r1, fill, exit_px, entry_t, exit_t) for session `cur` given the previous (Session, bars), or None."""
    (ps, pbars), (s, bars) = prev, cur
    if not pbars or not bars:
        return None
    if pbars[-1].t < ps.close - EDGE_WINDOW or bars[-1].t < s.close - EDGE_WINDOW:
        return None
    sig = _at(bars, s.open + SIGNAL_OFFSET)
    ent = _at(bars, s.close - ENTRY_BEFORE_CLOSE)
    if sig is None or ent is None or ent.t <= sig.t:
        return None
    r1 = sig.c / pbars[-1].c - 1
    return r1, ent.o, bars[-1].c, ent.t, bars[-1].t


def direction(r1: float, allow_short: bool) -> int:
    if r1 > 0:
        return 1
    if r1 < 0 and allow_short:
        return -1
    return 0


def trade(day: str, inputs, allow_short: bool, cost_bp_per_side: float = COST_BP_PER_SIDE):
    r1, fill, exit_px, entry_t, exit_t = inputs
    d = direction(r1, allow_short)
    if d == 0:
        return None
    gross = d * (exit_px / fill - 1) * 1e4
    return {"day": day, "side": "long" if d > 0 else "short", "r1": r1, "entry_t": entry_t, "entry_px": fill,
            "exit_t": exit_t, "exit_px": exit_px, "gross_bp": gross, "net_bp": gross - 2 * cost_bp_per_side}


def eligible_days(sessions):
    """[(session index, day, inputs)] for every session that has all the bars a trade needs."""
    out = []
    for i in range(1, len(sessions)):
        x = day_inputs(sessions[i - 1], sessions[i])
        if x is not None:
            out.append((i, sessions[i][0].day, x))
    return out


def run(sessions, variant: str, cost_bp_per_side: float = COST_BP_PER_SIDE) -> list[dict]:
    """All trades of one variant, in day order."""
    allow_short = VARIANTS[variant]
    trades = []
    for _, day, x in eligible_days(sessions):
        t = trade(day, x, allow_short, cost_bp_per_side)
        if t is not None:
            trades.append(t)
    return trades


def last_half_hour_bp(sessions) -> dict[str, float]:
    """Null pool: the LONG gross bp of the last half hour on every eligible day, signal ignored."""
    return {day: (x[2] / x[1] - 1) * 1e4 for _, day, x in eligible_days(sessions)}
