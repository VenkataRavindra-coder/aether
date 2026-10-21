"""The Trend Gate rule. Pure functions: no I/O, no clock, so it is fully testable.

Rule: the latest completed close is ABOVE the simple moving average of the last N closes (N=200)
  -> RISK_ON:  "BUY / HOLD_LONG"
  otherwise (at or below the average)
  -> RISK_OFF: "DEFENSIVE_CASH / EXIT"

Optional band (band_pct > 0): the status only flips once the close is more than band_pct above or
below the average; inside the band the previous status is kept. This cuts whipsaw trades when the
price hovers around the average. With no previous status, the plain rule decides.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

RISK_ON, RISK_OFF = "RISK_ON", "RISK_OFF"
ACTIONS = {RISK_ON: "BUY / HOLD_LONG", RISK_OFF: "DEFENSIVE_CASH / EXIT"}


@dataclass(frozen=True)
class TrendSignal:
    symbol: str
    status: str            # RISK_ON | RISK_OFF
    action: str            # "BUY / HOLD_LONG" | "DEFENSIVE_CASH / EXIT"
    as_of: str             # date of the close the signal is based on (New York session date)
    close: float
    sma: float
    sma_window: int
    distance_pct: float    # (close / sma - 1) * 100
    band_pct: float
    held_by_band: bool     # True when the band kept the previous status

    def to_dict(self) -> dict:
        return asdict(self)


def sma(closes: list[float], window: int) -> float:
    if window < 1 or len(closes) < window:
        raise ValueError(f"need {window} closes, have {len(closes)}")
    return sum(closes[-window:]) / window


def evaluate(symbol: str, dates: list[str], closes: list[float], window: int = 200,
             band_pct: float = 0.0, previous_status: str | None = None) -> TrendSignal:
    if len(dates) != len(closes):
        raise ValueError("dates and closes differ in length")
    avg = sma(closes, window)
    last = closes[-1]
    dist = (last / avg - 1) * 100
    plain = RISK_ON if last > avg else RISK_OFF
    status, held = plain, False
    if band_pct > 0 and previous_status in (RISK_ON, RISK_OFF) and abs(dist) <= band_pct:
        status, held = previous_status, previous_status != plain
    return TrendSignal(symbol=symbol, status=status, action=ACTIONS[status], as_of=dates[-1],
                       close=round(last, 4), sma=round(avg, 4), sma_window=window,
                       distance_pct=round(dist, 3), band_pct=band_pct, held_by_band=held)
