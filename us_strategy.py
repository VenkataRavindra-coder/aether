"""ETF momentum strategy: pure logic, no network, no broker.

Rule (published-style, fixed in advance, not tuned): once a month, rank ten diversified ETFs by the
average of their 3, 6 and 12 month returns; hold the top three at equal weight; any of the three whose
score is not positive is replaced by short-term Treasuries (SHY). Signals use closes up to the last
completed month-end; execution happens on the next trading day.
"""

import datetime as _dt
import math

RISK_ETFS = ["SPY", "QQQ", "IWM", "EFA", "EEM", "IEF", "TLT", "GLD", "DBC", "VNQ"]
CASH_ETF = "SHY"
UNIVERSE = RISK_ETFS + [CASH_ETF]
LOOKBACKS = (63, 126, 252)
TOP_N = 3
MIN_BARS = max(LOOKBACKS) + 1


def momentum_score(closes):
    if len(closes) < MIN_BARS:
        return None
    return sum(closes[-1] / closes[-1 - n] - 1 for n in LOOKBACKS) / len(LOOKBACKS)


def target_weights(closes_by_symbol):
    """closes_by_symbol: {symbol: [adjusted closes, oldest first]} ending at the signal month-end.
    Returns (weights, scores, selected). Raises ValueError if history is insufficient."""
    scores = {}
    for s in RISK_ETFS:
        if not all(_good_close(c) for c in closes_by_symbol.get(s, [])):
            raise ValueError(f"non-finite or non-positive close for {s}")
        sc = momentum_score(closes_by_symbol.get(s, []))
        if sc is None:
            raise ValueError(f"insufficient history for {s} (need {MIN_BARS} closes)")
        scores[s] = sc
    ranked = sorted(RISK_ETFS, key=lambda s: (-scores[s], s))
    selected = ranked[:TOP_N]
    weights = {}
    for s in selected:
        dest = s if scores[s] > 0 else CASH_ETF
        weights[dest] = weights.get(dest, 0.0) + 1.0 / TOP_N
    return weights, scores, selected


# ---- Data-quality limits (plan D2/D3/D4/D5). Fixed before any test run; change only via plan section 12.
MAX_GAP_DAYS = 6          # longest normal gap between consecutive bars (long holiday weekends are <= 4)
MAX_DAILY_MOVE = 0.40     # |close / previous close - 1| above this is treated as a bad tick
MAX_STALENESS_DAYS = 5    # newest completed-month bar must be within this many days of month-end


def _good_close(c):
    return isinstance(c, (int, float)) and not isinstance(c, bool) and math.isfinite(c) and c > 0


def validate_bars(symbol, bars):
    """bars: [{'d': 'YYYY-MM-DD', 'c': close}] oldest first. Raises ValueError on anything untrustworthy:
    non-finite / non-positive / non-numeric close, duplicate or out-of-order dates, a gap longer than
    MAX_GAP_DAYS, or a daily move larger than MAX_DAILY_MOVE. Fail closed: the caller must not trade."""
    prev_d = prev_c = None
    for b in bars:
        c = b["c"]
        if not _good_close(c):
            raise ValueError(f"{symbol}: bad close {c!r} on {b['d']}")
        d = _dt.date.fromisoformat(b["d"])
        if prev_d is not None:
            if d <= prev_d:
                raise ValueError(f"{symbol}: dates not strictly increasing at {b['d']}")
            if (d - prev_d).days > MAX_GAP_DAYS:
                raise ValueError(f"{symbol}: {(d - prev_d).days}-day gap before {b['d']}")
            move = c / prev_c - 1.0
            if abs(move) > MAX_DAILY_MOVE:
                raise ValueError(f"{symbol}: {move:+.0%} move on {b['d']} exceeds {MAX_DAILY_MOVE:.0%}")
        prev_d, prev_c = d, c


def month_key(date_str):
    return date_str[:7]


def bars_before_month(bars, today_str):
    """Keep only bars from completed months: dated before the first day of today's month."""
    cutoff = today_str[:7] + "-01"
    return [b for b in bars if b["d"] < cutoff]


def plan_orders(holdings, prices, weights, equity, min_trade=1.0):
    """Turn target weights into orders. Sells first. Buys are notional dollars, scaled down if cash
    (equity minus current holdings value plus sale proceeds) cannot cover them. Never sells more than held."""
    orders = []
    held_value = sum(q * prices[s] for s, q in holdings.items() if q > 0 and s in prices)
    cash = equity - held_value
    proceeds = 0.0
    for s in sorted(set(holdings) | set(weights)):
        cur = holdings.get(s, 0.0)
        if cur <= 0 and s not in weights:
            continue
        price = prices[s]
        target_value = weights.get(s, 0.0) * equity
        diff = target_value - cur * price
        if diff < -min_trade:
            qty = cur if s not in weights else min(cur, -diff / price)
            orders.append({"symbol": s, "side": "SELL", "qty": qty, "est_value": qty * price})
            proceeds += qty * price
    buys = []
    for s in sorted(weights):
        cur = holdings.get(s, 0.0)
        diff = weights[s] * equity - cur * prices[s]
        if diff > min_trade:
            buys.append({"symbol": s, "side": "BUY", "notional": diff, "est_value": diff})
    need = sum(b["notional"] for b in buys)
    available = max(cash, 0.0) + proceeds
    if need > available and need > 0:
        k = available / need
        for b in buys:
            b["notional"] *= k
            b["est_value"] *= k
        buys = [b for b in buys if b["notional"] >= min_trade]
    return orders + buys
