#!/usr/bin/env python3
"""AETHER research: backtest of the LIVE US ETF momentum strategy.

Imports ~/us_strategy.py unchanged, so the tested rules are the traded rules.
Matches the engine: dividend+split adjusted daily closes, signals from completed months only,
rebalance at the open of the first trading day of each month via us_strategy.plan_orders.

  python us_momentum_backtest.py fetch            # daily bars since 2015 (Alpaca data starts ~2016)
  python us_momentum_backtest.py run              # full period + in/out split + yearly table
"""
import argparse
import hashlib
import json
import math
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ENGINE_DIR = Path(os.getenv("AETHER_DIR", str(Path.home())))
sys.path.insert(0, str(ENGINE_DIR))
import us_strategy as us  # noqa: E402

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"


def cache_path(adj):
    return DATA / f"us_daily_{adj}.csv.gz"


# ------------------------------------------------------------------ data
def fetch(start, adjustment, feed):
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    key, sec = os.getenv("APCA_API_KEY_ID"), os.getenv("APCA_API_SECRET_KEY")
    if not key or not sec:
        raise SystemExit("export APCA_API_KEY_ID and APCA_API_SECRET_KEY first")
    end = pd.Timestamp.now(tz="UTC").normalize() - pd.Timedelta(days=1)
    req = StockBarsRequest(symbol_or_symbols=list(us.UNIVERSE), timeframe=TimeFrame.Day,
                           start=pd.Timestamp(start, tz="UTC").to_pydatetime(), end=end.to_pydatetime(),
                           adjustment=Adjustment(adjustment), feed=DataFeed(feed))
    df = StockHistoricalDataClient(key, sec).get_stock_bars(req).df.reset_index()
    df["date"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert("America/New_York").dt.strftime("%Y-%m-%d")
    out = df[["symbol", "date", "open", "close"]].sort_values(["symbol", "date"])
    DATA.mkdir(exist_ok=True)
    out.to_csv(cache_path(adjustment), index=False, compression="gzip")
    for s, g in out.groupby("symbol"):
        print(f"{s:4} {len(g):5} bars  {g['date'].iloc[0]} .. {g['date'].iloc[-1]}")


def load(adj):
    df = pd.read_csv(cache_path(adj))
    opens = df.pivot(index="date", columns="symbol", values="open").sort_index()
    closes = df.pivot(index="date", columns="symbol", values="close").sort_index()
    cal = closes.index[closes["SPY"].notna()]                  # NYSE calendar via SPY
    return opens.reindex(cal), closes.reindex(cal).ffill()


# ------------------------------------------------------------------ simulation
def simulate(opens, closes, equity0=5000.0, slip_bps=2.0):
    """Monthly rebalance exactly as the engine does it. Returns (daily equity series, rebalance log)."""
    slip = slip_bps / 1e4
    dates = list(closes.index)
    cl = closes[us.UNIVERSE].to_numpy(dtype=float)
    op = opens[us.UNIVERSE].to_numpy(dtype=float)
    col = {s: j for j, s in enumerate(us.UNIVERSE)}
    holdings, cash, eq, log = {}, equity0, [], []
    started = False
    for i, d in enumerate(dates):
        if i > 0 and d[:7] != dates[i - 1][:7]:                 # first trading day of a month
            hist = {s: [x for x in cl[:i, col[s]] if np.isfinite(x)] for s in us.UNIVERSE}
            try:
                w, scores, sel = us.target_weights(hist)       # closes strictly before this month
            except ValueError:
                w = None
            if w is not None:
                prices = {s: float(op[i, col[s]]) if np.isfinite(op[i, col[s]]) else float(cl[i - 1, col[s]])
                          for s in us.UNIVERSE}
                equity = cash + sum(q * prices[s] for s, q in holdings.items())
                traded = 0.0
                for o in us.plan_orders(holdings, prices, w, equity):
                    s = o["symbol"]
                    if o["side"] == "SELL":
                        cash += o["qty"] * prices[s] * (1 - slip)
                        holdings[s] = holdings.get(s, 0.0) - o["qty"]
                        traded += o["qty"] * prices[s]
                    else:
                        holdings[s] = holdings.get(s, 0.0) + o["notional"] / (prices[s] * (1 + slip))
                        cash -= o["notional"]
                        traded += o["notional"]
                holdings = {s: q for s, q in holdings.items() if q > 1e-9}
                started = True
                log.append({"date": d, "selected": sel, "weights": {k: round(v, 4) for k, v in w.items()},
                            "cash_weight": round(w.get(us.CASH_ETF, 0.0), 4), "turnover": traded / equity})
        if started:
            eq.append((d, cash + sum(q * cl[i, col[s]] for s, q in holdings.items())))
    s = pd.Series([v for _, v in eq], index=pd.to_datetime([d for d, _ in eq]), name="strategy")
    return s, log


def benchmarks(closes, index, equity0=5000.0):
    c = closes.loc[index.strftime("%Y-%m-%d")].set_axis(index)
    spy = equity0 * c["SPY"] / c["SPY"].iloc[0]
    r = c[["SPY", "IEF"]].pct_change().fillna(0.0)
    sixty = equity0 * (1 + 0.6 * r["SPY"] + 0.4 * r["IEF"]).cumprod()   # daily-rebalanced 60/40, approx.
    return spy.rename("SPY"), sixty.rename("60/40")


def metrics(eq: pd.Series) -> dict:
    if len(eq) < 30:
        return {"days": int(len(eq))}
    r = eq.pct_change().dropna()
    years = (eq.index[-1] - eq.index[0]).days / 365.25
    dd = eq / eq.cummax() - 1
    monthly = eq.resample("ME").last().pct_change().dropna()
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1
    return {
        "start": str(eq.index[0].date()), "end": str(eq.index[-1].date()),
        "cagr": round(cagr, 4), "vol": round(r.std() * math.sqrt(252), 4),
        "sharpe": round(r.mean() / r.std() * math.sqrt(252), 3) if r.std() > 0 else None,
        "max_dd": round(dd.min(), 4), "calmar": round(cagr / -dd.min(), 3) if dd.min() < 0 else None,
        "worst_month": round(monthly.min(), 4), "pct_up_months": round((monthly > 0).mean(), 3),
        "daily_skew": round(float(r.skew()), 3), "daily_kurtosis": round(float(r.kurt() + 3), 3),
        "days": int(len(r)),
    }


def yearly(series_list) -> pd.DataFrame:
    """Calendar-year returns; the first (partial) year is measured from the series start."""
    out = {}
    for s in series_list:
        ye = s.resample("YE").last()
        base = ye.shift(1)
        base.iloc[0] = s.iloc[0]
        out[s.name] = ye / base - 1
    df = pd.DataFrame(out)
    df.index = df.index.year
    return df.round(3)


# ------------------------------------------------------------------ trial log
def log_trial(db, params, full, is_m, oos_m, split):
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE IF NOT EXISTS research_trials (
        id INTEGER PRIMARY KEY, created_at TEXT, strategy TEXT, symbol TEXT, mode TEXT,
        params_json TEXT, data_start TEXT, data_end TEXT, split_date TEXT,
        is_json TEXT, oos_json TEXT, is_sharpe REAL, oos_sharpe REAL,
        oos_trades INTEGER, oos_avg_r REAL)""")
    con.execute("INSERT INTO research_trials VALUES (NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (datetime.now(timezone.utc).isoformat(), "us_etf_momentum_v1", "ETF10+SHY", "monthly",
                 json.dumps(params, sort_keys=True), full.get("start"), full.get("end"), split,
                 json.dumps(is_m), json.dumps(oos_m), is_m.get("sharpe"), oos_m.get("sharpe"), None, None))
    con.commit()
    n = con.execute("SELECT COUNT(*) FROM research_trials").fetchone()[0]
    con.close()
    return n


# ------------------------------------------------------------------ CLI
def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--start", default="2015-01-01")
    f.add_argument("--adjustment", default="all", choices=["all", "raw", "split", "dividend"])
    f.add_argument("--feed", default="sip", choices=["sip", "iex"])
    r = sub.add_parser("run")
    r.add_argument("--adjustment", default="all")
    r.add_argument("--equity", type=float, default=5000.0)
    r.add_argument("--slippage-bps", type=float, default=2.0)
    r.add_argument("--split-date", default="2022-01-01")
    r.add_argument("--trial-db", default=str(HERE.parent / "research.sqlite"))
    a = ap.parse_args()

    if a.cmd == "fetch":
        fetch(a.start, a.adjustment, a.feed)
        return

    opens, closes = load(a.adjustment)
    eq, log = simulate(opens, closes, a.equity, a.slippage_bps)
    spy, sixty = benchmarks(closes, eq.index, a.equity)
    split = pd.Timestamp(a.split_date)
    full = metrics(eq)
    is_m, oos_m = metrics(eq[eq.index < split]), metrics(eq[eq.index >= split])
    code_hash = hashlib.sha256((ENGINE_DIR / "us_strategy.py").read_bytes()).hexdigest()[:12]
    params = {"adjustment": a.adjustment, "equity": a.equity, "slippage_bps": a.slippage_bps,
              "us_strategy_sha256_12": code_hash, "universe": us.UNIVERSE, "lookbacks": list(us.LOOKBACKS),
              "top_n": us.TOP_N}
    n = log_trial(a.trial_db, params, full, is_m, oos_m, a.split_date)

    held = pd.Series([s for x in log for s in x["weights"]]).value_counts()
    out = {
        "strategy": full, "SPY_buy_hold": metrics(spy), "60_40": metrics(sixty),
        "in_sample": is_m, "out_of_sample": oos_m,
        "rebalances": len(log), "avg_turnover": round(float(np.mean([x["turnover"] for x in log])), 3),
        "pct_months_with_cash": round(float(np.mean([x["cash_weight"] > 0 for x in log])), 3),
        "months_held": held.to_dict(), "last_rebalance": log[-1] if log else None,
        "us_strategy_sha256_12": code_hash, "trials_logged_total": n,
    }
    HERE.joinpath("results").mkdir(exist_ok=True)
    tag = datetime.now().strftime("%Y%m%d_%H%M%S")
    HERE.joinpath("results", f"us_momentum_{tag}.json").write_text(json.dumps(out, indent=2, default=str))
    pd.concat([eq, spy, sixty], axis=1).to_csv(HERE / "results" / f"us_momentum_equity_{tag}.csv")
    print(json.dumps(out, indent=2, default=str))
    print("\nCalendar-year returns:")
    print(yearly([eq, spy, sixty]).to_string())


if __name__ == "__main__":
    main()
