"""ETF momentum strategy: pure logic, no network, no broker.

Rule (published-style, fixed in advance, not tuned): once a month, rank ten diversified ETFs by the
average of their 3, 6 and 12 month returns; hold the top three at equal weight; any of the three whose
score is not positive is replaced by short-term Treasuries (SHY). Signals use closes up to the last
completed month-end; execution happens on the next trading day.
"""

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
