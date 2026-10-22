"""Strategy A: 15-minute opening range breakout on SPY, long only. Pure.

Pre-registered rules:
- Range: high/low of the first 15 one-minute bars (09:30-09:44 New York). All 15 must exist,
  otherwise no trade that day ("incomplete range").
- Signal: the first 1-minute bar that CLOSES above the range high, starting 09:45 and starting
  before 11:30 (or close - 60 min on early-close days, whichever is earlier).
- Entry: next bar's open. Stop: range low. No profit target. Flat 5 minutes before the close.
- At most one trade per day.
"""
from __future__ import annotations

from datetime import timedelta

from .sim import flat_time, simulate

NAME = "orb15"
RANGE = timedelta(minutes=15)


def entry_cutoff(session):
    return min(session.open.replace(hour=11, minute=30), session.close - timedelta(minutes=60))


def _evaluate(bars, session) -> dict:
    out = {"strategy": NAME, "day": session.day}
    range_end = session.open + RANGE
    rng = [b for b in bars if b.t < range_end]
    if not bars or bars[-1].t < range_end - timedelta(minutes=1):
        return {**out, "state": "forming_range", "trade": None, "reason": "opening range not complete yet"}
    need = RANGE // timedelta(minutes=1)   # 15; derived so research neighbours can vary RANGE alone
    if len(rng) != need:
        return {**out, "state": "no_trade", "trade": None, "reason": f"incomplete range ({len(rng)} of {need} bars)"}
    hi, lo = max(b.h for b in rng), min(b.l for b in rng)
    out.update(range_high=round(hi, 4), range_low=round(lo, 4))
    cut, flat = entry_cutoff(session), flat_time(session)
    for i, b in enumerate(bars):
        if b.t < range_end or b.t >= cut:
            continue
        if b.c > hi:
            if i + 1 >= len(bars):
                return {**out, "state": "signal", "trade": None,
                        "reason": f"breakout close {b.c:.2f} at {b.t:%H:%M}; entry on the next bar"}
            if bars[i + 1].t >= flat:
                return {**out, "state": "no_trade", "trade": None, "reason": "signal too late to enter"}
            return {**out, "state": "traded", "reason": f"close {b.c:.2f} > range high {hi:.2f} at {b.t:%H:%M}",
                    "trade": simulate(bars, i + 1, lo, None, session)}
    if bars[-1].t >= cut - timedelta(minutes=1):
        return {**out, "state": "no_trade", "trade": None, "reason": f"no close above {hi:.2f} before {cut:%H:%M}"}
    return {**out, "state": "watching", "trade": None, "reason": f"waiting for a close above {hi:.2f} (until {cut:%H:%M})"}


def evaluate(bars, session) -> dict:
    """The paper result plus a plain signal name:
    WAITING_FOR_RANGE | LONG_BREAKOUT | SHORT_BREAKOUT | NO_SIGNAL.
    SHORT_BREAKOUT (a close below the range low before the cutoff) is reported for information only:
    the paper strategy is long only (the account cannot short fractional shares)."""
    r = _evaluate(bars, session)
    if r["state"] == "forming_range":
        sig = "WAITING_FOR_RANGE"
    elif r["state"] in ("traded", "signal"):
        sig = "LONG_BREAKOUT"
    else:
        sig = "NO_SIGNAL"
        lo = r.get("range_low")
        if lo is not None:
            end, cut = session.open + RANGE, entry_cutoff(session)
            if any(end <= b.t < cut and b.c < lo for b in bars):
                sig = "SHORT_BREAKOUT"
    return {**r, "signal": sig}
