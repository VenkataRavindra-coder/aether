"""Strategy B: 15-minute bullish engulfing scalp on SPY, long only. Pure.

Pre-registered rules:
- 15-minute bars built from 1-minute bars, aligned to the session open (09:30, 09:45, ...). A
  15-minute bar counts only if at least 12 of its 15 minutes exist.
- Signal on 15-minute bar k (k >= 1): bar k-1 red (close < open), bar k green (close > open),
  bar k's body engulfs bar k-1's body (open_k <= close_k-1 and close_k >= open_k-1), and
  volume_k > volume_k-1. Bar k must END between 10:00 and 14:30 New York.
- Entry: open of the first 1-minute bar after bar k ends. Stop: bar k's low.
  Target: entry + 1.5 x (entry - stop). Flat 5 minutes before the close. One trade per day.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from .sim import flat_time, simulate

NAME = "engulf15"
STEP = timedelta(minutes=15)


@dataclass(frozen=True)
class Bar15:
    start: object
    end: object
    o: float
    h: float
    l: float
    c: float
    v: float


def bars15(bars, session) -> list[Bar15]:
    groups: dict[int, list] = {}
    for b in bars:
        groups.setdefault(int((b.t - session.open) / STEP), []).append(b)
    out = []
    last_t = bars[-1].t if bars else None
    for k in sorted(groups):
        g, start = groups[k], session.open + k * STEP
        end = start + STEP
        if last_t is None or last_t < end - timedelta(minutes=1) or len(g) < 12:
            continue   # still forming, or too many missing minutes
        out.append(Bar15(start, end, g[0].o, max(x.h for x in g), min(x.l for x in g), g[-1].c, sum(x.v for x in g)))
    return out


def _evaluate(bars, session) -> dict:
    out = {"strategy": NAME, "day": session.day}
    b15 = bars15(bars, session)
    lo_t = session.open.replace(hour=10, minute=0)
    hi_t = min(session.open.replace(hour=14, minute=30), session.close - timedelta(minutes=60))
    flat = flat_time(session)
    for prev, cur in zip(b15, b15[1:]):
        if prev.end != cur.start or not (lo_t <= cur.end <= hi_t):
            continue
        if prev.c < prev.o and cur.c > cur.o and cur.o <= prev.c and cur.c >= prev.o and cur.v > prev.v:
            idx = next((i for i, b in enumerate(bars) if b.t >= cur.end), None)
            why = f"bullish engulfing {cur.start:%H:%M}-{cur.end:%H:%M} (vol {cur.v:,.0f} > {prev.v:,.0f})"
            if idx is None:
                return {**out, "state": "signal", "trade": None, "reason": why + "; entry on the next bar"}
            if bars[idx].t >= flat:
                return {**out, "state": "no_trade", "trade": None, "reason": "signal too late to enter"}
            entry = bars[idx].o
            if entry <= cur.l:
                return {**out, "state": "no_trade", "trade": None, "reason": "entry at or below the stop"}
            target = entry + 1.5 * (entry - cur.l)
            return {**out, "state": "traded", "reason": why, "trade": simulate(bars, idx, cur.l, target, session)}
    if bars and bars[-1].t >= hi_t:
        return {**out, "state": "no_trade", "trade": None, "reason": f"no bullish engulfing bar ending 10:00-{hi_t:%H:%M}"}
    return {**out, "state": "watching", "trade": None, "reason": f"scanning 15-minute bars until {hi_t:%H:%M}"}


def evaluate(bars, session) -> dict:
    """The paper result plus a plain signal name: BULLISH_ENGULFING | SCANNING | NO_SIGNAL."""
    r = _evaluate(bars, session)
    sig = {"traded": "BULLISH_ENGULFING", "signal": "BULLISH_ENGULFING", "watching": "SCANNING"}.get(r["state"], "NO_SIGNAL")
    return {**r, "signal": sig}
