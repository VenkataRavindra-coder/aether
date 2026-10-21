"""Paper fill and exit simulation, shared by both strategies. Pure; conservative by design.

Pre-registered (strategy.md "Tactical paper tests"):
- Entry: the OPEN of the 1-minute bar after the signal bar closes (no look-ahead).
- Stop: if a bar's low reaches the stop, exit at min(bar open, stop) (gaps fill worse).
- Target: if a bar's high reaches the target, exit at max(bar open, target). If stop and target are
  both inside one bar, the stop is assumed first.
- Flat by session close - 5 min: exit at the open of the first bar at or after that time.
- Costs: 2 bp per side (4 bp per round trip), subtracted from every trade.
"""
from __future__ import annotations

from datetime import timedelta

COST_BP_PER_SIDE = 2.0
FLAT_BEFORE_CLOSE = timedelta(minutes=5)


def flat_time(session):
    return session.close - FLAT_BEFORE_CLOSE


def simulate(bars, entry_idx: int, stop: float, target: float | None, session) -> dict:
    entry = bars[entry_idx]
    px_in = entry.o
    flat = flat_time(session)
    exit_px, exit_t, why = None, None, None
    for b in bars[entry_idx:]:
        if b.t >= flat and b is not entry:
            exit_px, exit_t, why = b.o, b.t, "flat_before_close"
            break
        if b.l <= stop:
            exit_px, exit_t, why = min(b.o, stop), b.t, "stop"
            break
        if target is not None and b.h >= target:
            exit_px, exit_t, why = max(b.o, target), b.t, "target"
            break
    if exit_px is None:   # bars ran out (data gap at the end of the day)
        last = bars[-1]
        exit_px, exit_t, why = last.c, last.t, "data_end"
    gross = (exit_px / px_in - 1) * 1e4
    return {"entry_time": entry.t.strftime("%H:%M"), "entry_px": round(px_in, 4),
            "exit_time": exit_t.strftime("%H:%M"), "exit_px": round(exit_px, 4), "exit_reason": why,
            "stop": round(stop, 4), "target": None if target is None else round(target, 4),
            "gross_bp": round(gross, 2), "net_bp": round(gross - 2 * COST_BP_PER_SIDE, 2)}
