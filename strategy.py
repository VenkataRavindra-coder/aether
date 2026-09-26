"""AETHER trend-following strategy: pure logic, no broker or network access.

The same entry/exit/sizing functions drive both the live runner and the backtester,
so a backtest reflects exactly what the live engine would decide.

Bars are dicts: {"t": iso-timestamp, "o": open, "h": high, "l": low, "c": close, "v": volume}.
Long-only (Alpaca does not support crypto shorting).
"""
from dataclasses import dataclass


@dataclass
class Params:
    fast: int = 50
    slow: int = 200
    trend: int = 800
    rsi_period: int = 14
    rsi_max: float = 70.0
    atr_period: int = 14
    atr_mult: float = 3.0
    risk_pct: float = 0.01
    max_notional_pct: float = 0.25
    fee: float = 0.0025
    slippage: float = 0.0005

    @property
    def warmup(self) -> int:
        return max(self.trend, self.slow, self.rsi_period, self.atr_period) + 1


def ema(values, n):
    out = [None] * len(values)
    if len(values) < n:
        return out
    k = 2.0 / (n + 1)
    prev = sum(values[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(closes, n):
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    avg_gain, avg_loss = gains / n, losses / n
    out[n] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1 + avg_gain / avg_loss)
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (n - 1) + max(d, 0.0)) / n
        avg_loss = (avg_loss * (n - 1) + max(-d, 0.0)) / n
        out[i] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1 + avg_gain / avg_loss)
    return out


def atr(bars, n):
    out = [None] * len(bars)
    if len(bars) <= n:
        return out
    tr = [0.0] * len(bars)
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["h"], bars[i]["l"], bars[i - 1]["c"]
        tr[i] = max(h - l, abs(h - pc), abs(l - pc))
    prev = sum(tr[1:n + 1]) / n
    out[n] = prev
    for i in range(n + 1, len(bars)):
        prev = (prev * (n - 1) + tr[i]) / n
        out[i] = prev
    return out


def compute_indicators(bars, p: Params) -> dict:
    closes = [b["c"] for b in bars]
    return {
        "close": closes,
        "ema_fast": ema(closes, p.fast),
        "ema_slow": ema(closes, p.slow),
        "ema_trend": ema(closes, p.trend),
        "rsi": rsi(closes, p.rsi_period),
        "atr": atr(bars, p.atr_period),
    }


def ready(ind: dict, i: int, p: Params) -> bool:
    return i >= p.warmup and all(
        ind[k][i] is not None and ind[k][i - 1] is not None
        for k in ("ema_fast", "ema_slow", "ema_trend", "rsi", "atr")
    )


def entry_signal(ind: dict, i: int, p: Params) -> bool:
    """Fast EMA crosses above slow EMA on bar i, price above the long-term trend EMA,
    and RSI not overbought."""
    if not ready(ind, i, p):
        return False
    crossed_up = ind["ema_fast"][i - 1] <= ind["ema_slow"][i - 1] and ind["ema_fast"][i] > ind["ema_slow"][i]
    return bool(
        crossed_up
        and ind["close"][i] > ind["ema_trend"][i]
        and ind["rsi"][i] < p.rsi_max
    )


def exit_signal(ind: dict, i: int, p: Params) -> bool:
    """Trend has turned down: fast EMA below slow EMA."""
    if not ready(ind, i, p):
        return False
    return bool(ind["ema_fast"][i] < ind["ema_slow"][i])


def position_size(equity: float, entry: float, stop: float, p: Params) -> float:
    """Risk `risk_pct` of equity between entry and stop, capped at `max_notional_pct` of equity."""
    stop_dist = entry - stop
    if equity <= 0 or entry <= 0 or stop_dist <= 0:
        return 0.0
    qty = (equity * p.risk_pct) / stop_dist
    cap = (equity * p.max_notional_pct) / entry
    return max(0.0, min(qty, cap))


def backtest(bars, p: Params, start_equity: float = 100000.0) -> dict:
    """Long-only simulation with no lookahead: signals form at a bar's close and fill at the
    NEXT bar's open; stops are checked against each bar's low using the stop as of the prior
    close. Fees and slippage are charged on both sides."""
    n = len(bars)
    ind = compute_indicators(bars, p)
    if n <= p.warmup + 2:
        return {"error": f"Not enough bars ({n}); need more than {p.warmup + 2}."}

    cash = start_equity
    pos = None
    pending = None
    trades = []
    curve = []
    fees_paid = 0.0

    def close_position(price, t, reason):
        nonlocal cash, pos, fees_paid
        fill = price * (1 - p.slippage)
        proceeds = pos["qty"] * fill
        fee = proceeds * p.fee
        cash += proceeds - fee
        fees_paid += fee + pos["entry_fee"]
        pnl = proceeds - fee - pos["cost"]
        trades.append({
            "entry_t": pos["t"], "exit_t": t,
            "entry": round(pos["entry"], 4), "exit": round(fill, 4),
            "qty": round(pos["qty"], 6), "pnl": round(pnl, 2),
            "pnl_pct": round(pnl / pos["cost"] * 100, 2), "reason": reason,
        })
        pos = None

    for i in range(p.warmup, n):
        b = bars[i]

        if pending == "exit" and pos:
            close_position(b["o"], b["t"], "trend_exit")
        elif isinstance(pending, dict) and not pos:
            fill = b["o"] * (1 + p.slippage)
            stop = fill - pending["stop_dist"]
            equity_now = cash
            qty = position_size(equity_now, fill, stop, p)
            if qty > 0:
                cost = qty * fill
                fee = cost * p.fee
                if cost + fee <= cash:
                    cash -= cost + fee
                    pos = {"qty": qty, "entry": fill, "stop": stop, "t": b["t"],
                           "cost": cost + fee, "entry_fee": fee}
        pending = None

        if pos and b["l"] <= pos["stop"]:
            close_position(min(b["o"], pos["stop"]), b["t"], "stop")

        if pos:
            pos["stop"] = max(pos["stop"], ind["close"][i] - p.atr_mult * ind["atr"][i])
            equity = cash + pos["qty"] * ind["close"][i]
            if exit_signal(ind, i, p):
                pending = "exit"
        else:
            equity = cash
            if entry_signal(ind, i, p):
                pending = {"stop_dist": p.atr_mult * ind["atr"][i]}
        curve.append({"t": b["t"], "v": round(equity, 2)})

    if pos:
        close_position(bars[-1]["c"], bars[-1]["t"], "open_at_end")
        curve[-1]["v"] = round(cash, 2)

    final = cash
    peak, max_dd = start_equity, 0.0
    for pt in curve:
        peak = max(peak, pt["v"])
        max_dd = max(max_dd, (peak - pt["v"]) / peak)

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = -sum(t["pnl"] for t in losses)
    first_close = bars[p.warmup]["c"]

    step = max(1, len(curve) // 200)
    return {
        "bars": n,
        "period_start": bars[p.warmup]["t"],
        "period_end": bars[-1]["t"],
        "start_equity": start_equity,
        "final_equity": round(final, 2),
        "total_return_pct": round((final / start_equity - 1) * 100, 2),
        "buy_hold_return_pct": round((bars[-1]["c"] / first_close - 1) * 100, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "trades": len(trades),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 1) if trades else 0.0,
        "avg_win": round(gross_win / len(wins), 2) if wins else 0.0,
        "avg_loss": round(-gross_loss / len(losses), 2) if losses else 0.0,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "fees_paid": round(fees_paid, 2),
        "equity_curve": curve[::step],
        "recent_trades": trades[-15:][::-1],
    }
