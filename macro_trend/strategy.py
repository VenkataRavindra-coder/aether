"""Pure, deterministic SPY 200-session trend rule."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .data import DailyBars

@dataclass(frozen=True)
class TrendSignal:
    symbol: str
    trend: str
    signal: str
    latest_close: float
    sma_200: float
    signal_date: str
    calculated_at: str
    action_on_new_entry: str
    action_if_long: str
    mode: str
    feed: str
    adjustment: str
    bars_used: int


def evaluate_trend(bars: DailyBars, now: datetime | None = None) -> TrendSignal:
    if len(bars.closes) < 200:
        raise ValueError("At least 200 completed closes are required")
    sma = bars.sma(200)
    if sma <= 0:
        raise ValueError("SMA must be positive")
    bullish = bars.close > sma
    return TrendSignal(
        symbol=bars.symbol,
        trend="ABOVE_200_SMA" if bullish else "AT_OR_BELOW_200_SMA",
        signal="RISK_ON" if bullish else "DEFENSIVE_CASH",
        latest_close=round(bars.close, 6),
        sma_200=round(sma, 6),
        signal_date=bars.completed_session.isoformat(),
        calculated_at=(now or bars.fetched_at).astimezone().isoformat(),
        action_on_new_entry="BUY" if bullish else "DEFENSIVE_CASH",
        action_if_long="HOLD_LONG" if bullish else "EXIT",
        mode=bars.mode,
        feed=bars.feed,
        adjustment=bars.adjustment,
        bars_used=200,
    )
