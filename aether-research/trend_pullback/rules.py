"""Trend-pullback candlestick rules on SPY (registration trend-pullback-v1). Pure; the binding copy.

Registered in docs/PREREG_trend_pullback_v1.md (tag `prereg-trend-pullback-v1`) before any result exists.
Research only: nothing here places, changes or cancels an order, and the engine never imports it.
Changing any constant or rule below makes a NEW strategy that needs its own registration and counts toward N.

Input: regular-hours 1-minute bars per session (aether_tactical.bars.Bar), already verified. A session that
failed verification is passed as an empty list: its slots exist but hold no minutes.

Candles (the two timeframes):
- "1h": hourly slots aligned to the session open (09:30-10:30, ..., 14:30-15:30 on a normal day). Only FULL
  hours count as slots: the 15:30-16:00 tail (12:30-13:00 on an early close) is never a signal or entry
  slot and does not feed the EMAs, but positions are held through it and exits can happen in it.
  A slot becomes a candle only if it has >= 54 of its 60 minutes.
- "1d": one slot per session (open to close). A candle needs >= 90% of the session's minutes.
Missing candles are skipped by the EMAs; a pattern needs its two candles in consecutive slots.

Indicators (on candle closes, per timeframe, continuous across days):
- EMA(n): alpha = 2 / (n + 1), seeded with the simple average of the first n closes; EMA20 and EMA50.
- No signal before candle index 150 (3 x 50, so the EMAs have settled).

Signal on completed candle i (all must hold):
- Trend: close_i > EMA50_i.
- Pullback: low_i <= EMA20_i (touches or crosses).
- Pattern, one of (each variant uses exactly one):
  - Bullish engulfing: candle i-1 red (close < open), candle i green (close > open), open_i <= close_i-1,
    close_i >= open_i-1, and body_i > body_i-1 (strictly).
  - Hammer: range = high - low > 0; body = |close - open| <= 0.35 x range; lower wick = min(open, close) - low
    >= 2 x body and >= 0.6 x range; upper wick = high - max(open, close) <= 0.10 x range. Either colour.

Entry, stop, target, exits (simulated on 1-minute bars):
- Entry window: the slot right after the signal slot only (for 1h after the last full hour, that is the next
  session's first hour). No minutes in it: no trade.
- Entry: the first minute in the window whose high is ABOVE the signal candle's high. Fill = max(that
  minute's open, signal high), so a gap above the trigger fills at the (worse) open. Not triggered in the
  window: the signal expires.
- Stop = signal candle's low. Risk R = fill - stop. Target = fill + 2R.
- On the entry minute only the stop is checked (worst case); after it, each minute: if low <= stop exit at
  min(open, stop); elif high >= target exit at max(open, target). Stop first if both. Overnight gaps fill at
  the next open by the same rule.
- Maximum hold: to the end of the 6th 1h slot after the entry slot (1h), or of the 10th session after the
  entry session (1d); exit at the close of the last minute before that time.
- Data end: exit at the last available close (reason data_end).
- One position at a time: a new signal candle must start after the previous trade's exit minute.
- Costs: 5 bp per side (10 bp round trip), stress 10 bp per side.
"""
from __future__ import annotations

import math
from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime, timedelta

EMA_FAST, EMA_SLOW = 20, 50
WARMUP = 150
MIN_HOUR_MINUTES = 54
MIN_DAY_SHARE = 0.90
TARGET_R = 2.0
MAX_HOLD_SLOTS = {"1h": 6, "1d": 10}
COST_BP_PER_SIDE = 5.0
STRESS_COST_BP_PER_SIDE = 10.0
HAMMER_BODY_MAX, HAMMER_LOWER_X_BODY, HAMMER_LOWER_MIN, HAMMER_UPPER_MAX = 0.35, 2.0, 0.60, 0.10

TIMEFRAMES = ("1h", "1d")
PATTERNS = ("engulfing", "hammer")
# Exactly four variants, frozen: name -> (timeframe, pattern)
VARIANTS = {
    "tpb_engulf_1h": ("1h", "engulfing"),
    "tpb_hammer_1h": ("1h", "hammer"),
    "tpb_engulf_1d": ("1d", "engulfing"),
    "tpb_hammer_1d": ("1d", "hammer"),
}


@dataclass(frozen=True)
class Slot:
    idx: int
    session: int          # index into the sessions list
    start: datetime
    end: datetime


@dataclass(frozen=True)
class Candle:
    slot: int
    start: datetime
    end: datetime
    o: float
    h: float
    l: float
    c: float


class Market:
    """All minutes of all sessions in one time-ordered list, plus the slots and candles of one timeframe."""

    def __init__(self, sessions, tf: str):
        if tf not in TIMEFRAMES:
            raise ValueError(f"unknown timeframe {tf}")
        self.tf = tf
        self.sessions = [s for s, _ in sessions]
        self.minutes = [b for _, bars in sessions for b in bars]
        self.times = [b.t for b in self.minutes]
        if any(a >= b for a, b in zip(self.times, self.times[1:])):
            raise ValueError("minutes are not strictly increasing across sessions")
        self.slots: list[Slot] = []
        for si, s in enumerate(self.sessions):
            if tf == "1d":
                self.slots.append(Slot(len(self.slots), si, s.open, s.close))
            else:
                k = 0
                while s.open + timedelta(hours=k + 1) <= s.close:
                    a = s.open + timedelta(hours=k)
                    self.slots.append(Slot(len(self.slots), si, a, a + timedelta(hours=1)))
                    k += 1
        self.candles: list[Candle] = []
        for sl in self.slots:
            lo, hi = self._span(sl.start, sl.end)
            n = hi - lo
            need = MIN_HOUR_MINUTES if tf == "1h" else math.ceil(MIN_DAY_SHARE * (sl.end - sl.start) / timedelta(minutes=1))
            if n < need:
                continue
            ms = self.minutes[lo:hi]
            self.candles.append(Candle(sl.idx, sl.start, sl.end, ms[0].o, max(m.h for m in ms),
                                       min(m.l for m in ms), ms[-1].c))
        closes = [c.c for c in self.candles]
        self.ema_fast, self.ema_slow = ema(closes, EMA_FAST), ema(closes, EMA_SLOW)

    def _span(self, a, b):
        return bisect_left(self.times, a), bisect_left(self.times, b)


def ema(values, n):
    out = [None] * len(values)
    if len(values) < n:
        return out
    e = sum(values[:n]) / n
    out[n - 1] = e
    k = 2.0 / (n + 1)
    for i in range(n, len(values)):
        e = values[i] * k + e * (1 - k)
        out[i] = e
    return out


def is_engulfing(prev: Candle, cur: Candle) -> bool:
    return (prev.c < prev.o and cur.c > cur.o and cur.o <= prev.c and cur.c >= prev.o
            and (cur.c - cur.o) > (prev.o - prev.c))


def is_hammer(c: Candle) -> bool:
    rng = c.h - c.l
    if rng <= 0:
        return False
    body = abs(c.c - c.o)
    lower = min(c.o, c.c) - c.l
    upper = c.h - max(c.o, c.c)
    return (body <= HAMMER_BODY_MAX * rng and lower >= HAMMER_LOWER_X_BODY * body
            and lower >= HAMMER_LOWER_MIN * rng and upper <= HAMMER_UPPER_MAX * rng)


def context_ok(m: Market, i: int) -> bool:
    """Warm-up done, trend up, and an entry slot exists after the candle."""
    return i >= WARMUP and m.ema_slow[i] is not None and m.candles[i].c > m.ema_slow[i] \
        and m.candles[i].slot + 1 < len(m.slots)


def is_signal(m: Market, i: int, pattern: str) -> bool:
    if not context_ok(m, i):
        return False
    cur = m.candles[i]
    if cur.l > m.ema_fast[i]:
        return False
    if pattern == "hammer":
        return is_hammer(cur)
    if pattern == "engulfing":
        if i < 1 or m.candles[i - 1].slot != cur.slot - 1:
            return False
        return is_engulfing(m.candles[i - 1], cur)
    raise ValueError(f"unknown pattern {pattern}")


def simulate(m: Market, i: int, cost_bp_per_side: float = COST_BP_PER_SIDE):
    """The trade a signal on candle i would make, or None if the entry never triggers."""
    sig = m.candles[i]
    win = m.slots[sig.slot + 1]
    lo, hi = m._span(win.start, win.end)
    k = next((j for j in range(lo, hi) if m.minutes[j].h > sig.h), None)
    if k is None:
        return None
    stop = sig.l
    fill = max(m.minutes[k].o, sig.h)
    risk = fill - stop
    if risk <= 0:
        return None
    target = fill + TARGET_R * risk
    lim_slot = win.idx + MAX_HOLD_SLOTS[m.tf]
    limit = m.slots[lim_slot].end if lim_slot < len(m.slots) else None
    exit_px = exit_t = why = None
    if m.minutes[k].l <= stop:
        exit_px, exit_t, why = stop, m.minutes[k].t, "stop_entry_minute"
    else:
        j = k + 1
        while j < len(m.minutes):
            b = m.minutes[j]
            if limit is not None and b.t >= limit:
                last = m.minutes[j - 1]
                exit_px, exit_t, why = last.c, last.t, "max_hold"
                break
            if b.l <= stop:
                exit_px, exit_t, why = min(b.o, stop), b.t, "stop"
                break
            if b.h >= target:
                exit_px, exit_t, why = max(b.o, target), b.t, "target"
                break
            j += 1
        if exit_px is None:
            last = m.minutes[-1]
            exit_px, exit_t, why = last.c, last.t, "data_end"
    gross = (exit_px / fill - 1) * 1e4
    return {"signal_start": sig.start, "signal_end": sig.end, "entry_t": m.minutes[k].t, "entry_px": fill,
            "exit_t": exit_t, "exit_px": exit_px, "exit_reason": why, "stop": stop, "target": target,
            "gross_bp": gross, "net_bp": gross - 2 * cost_bp_per_side}


def run(m: Market, pattern: str, cost_bp_per_side: float = COST_BP_PER_SIDE) -> list[dict]:
    """All trades of one variant, one position at a time, in time order."""
    trades, free_after = [], None
    for i in range(len(m.candles)):
        if free_after is not None and m.candles[i].start <= free_after:
            continue
        if is_signal(m, i, pattern):
            t = simulate(m, i, cost_bp_per_side)
            if t is not None:
                trades.append(t)
                free_after = t["exit_t"]
    return trades


def random_pool(m: Market, cost_bp_per_side: float = COST_BP_PER_SIDE) -> list[dict]:
    """Null model for the matched random-entry benchmark: the same entry/stop/target/hold mechanics on EVERY
    candle that passes warm-up and the trend filter (no pullback, no pattern). Triggered trades only."""
    out = []
    for i in range(len(m.candles)):
        if context_ok(m, i):
            t = simulate(m, i, cost_bp_per_side)
            if t is not None:
                out.append(t)
    return out
