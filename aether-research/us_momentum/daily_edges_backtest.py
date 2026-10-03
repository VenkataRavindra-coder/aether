#!/usr/bin/env python3
"""AETHER research: three pre-registered daily experiments on the cached daily bars.

  MR  (SPY and QQQ, run separately): 3 consecutive lower closes AND close > 200-day SMA -> buy next open.
       Exit at the next open after a close above the 5-day SMA, or after 5 holding sessions.
  TOM (SPY): buy at the close of the month's last trading day, sell at the close of the 3rd trading day.

Account model: $5,000, no leverage, fractional shares, full equity per trade, dividend-adjusted prices.
Costs: slippage per side (2 bps base, stress 5 and 10), plus a sell-side regulatory fee.
Cash earns 0% in the primary run (conservative); a SHY-yield variant is reported as a sensitivity.
Rules are frozen here. Data from 2022 on is a HISTORICAL HOLDOUT, not untouched validation.

  python daily_edges_backtest.py
"""
import glob
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
EQUITY0 = 5000.0
SELL_FEE_BPS = 0.3
BASE_SLIP, STRESS_SLIPS = 2.0, (5.0, 10.0)
SPLIT = "2022-01-01"
RULES = {"mr": {"down_closes": 3, "trend_sma": 200, "exit_sma": 5, "max_hold": 5},
         "tom": {"entry": "last trading day close", "exit": "3rd trading day close"}}


def load():
    df = pd.read_csv(HERE / "data" / "us_daily_all.csv.gz")
    o = df.pivot(index="date", columns="symbol", values="open").sort_index()
    c = df.pivot(index="date", columns="symbol", values="close").sort_index()
    cal = c.index[c["SPY"].notna()]
    o, c = o.reindex(cal), c.reindex(cal).ffill()
    o.index = c.index = pd.to_datetime(cal)
    return o, c


def cash_growth(closes, mode):
    if mode == "shy":
        return closes["SHY"].pct_change().fillna(0.0).clip(lower=-0.01).to_numpy()
    return np.zeros(len(closes))


def run_mr(o, c, sym, slip_bps, cash_mode="zero"):
    """Signal at close t, fill at open t+1: no look-ahead."""
    p = RULES["mr"]
    op, cl = o[sym].to_numpy(float), c[sym].to_numpy(float)
    s200 = pd.Series(cl).rolling(p["trend_sma"]).mean().to_numpy()
    s5 = pd.Series(cl).rolling(p["exit_sma"]).mean().to_numpy()
    g = cash_growth(c, cash_mode)
    slip, fee = slip_bps / 1e4, SELL_FEE_BPS / 1e4
    cash, sh, held, pend, trades, eq = EQUITY0, 0.0, 0, None, [], []
    start = p["trend_sma"]
    for i in range(len(cl)):
        if pend == "buy" and np.isfinite(op[i]):
            px = op[i] * (1 + slip)
            sh, cost, cash, held = cash / px, cash, 0.0, 0
            trades.append({"entry": c.index[i], "entry_px": px, "cost": cost})
        elif pend == "sell" and np.isfinite(op[i]):
            px = op[i] * (1 - slip)
            cash += sh * px * (1 - fee)
            t = trades[-1]
            t.update(exit=c.index[i], exit_px=px, pnl=cash - t["cost"], ret=cash / t["cost"] - 1)
            sh = 0.0
        pend = None
        cash *= 1 + (g[i] if sh == 0 else 0.0)
        if i >= start:
            if sh > 0:
                held += 1
                if cl[i] > s5[i] or held >= p["max_hold"]:
                    pend = "sell"
            elif (all(cl[i - k] < cl[i - k - 1] for k in range(p["down_closes"])) and cl[i] > s200[i]):
                pend = "buy"
        eq.append(cash + sh * cl[i])
    s = pd.Series(eq, index=c.index, name=f"{sym}_MR").iloc[start:]
    return s, [t for t in trades if "exit" in t], None


def _in_market(trades, idx):
    flags = pd.Series(False, index=idx)
    for t in trades:
        flags[(idx >= t["entry"]) & (idx < t.get("exit", idx[-1] + pd.Timedelta(days=1)))] = True
    return flags.to_numpy()


def run_tom(c, sym, slip_bps, cash_mode="zero"):
    """Calendar-known timing: entry and exit dates are fixed in advance, filled at the closing auction."""
    cl = c[sym].to_numpy(float)
    idx = c.index
    g = cash_growth(c, cash_mode)
    slip, fee = slip_bps / 1e4, SELL_FEE_BPS / 1e4
    last_day = [i for i in range(len(idx) - 1) if idx[i].month != idx[i + 1].month]
    entries = {i: i + 3 for i in last_day if i + 3 < len(idx)}
    exits = set(entries.values())
    cash, sh, trades, eq, cur = EQUITY0, 0.0, [], [], None
    for i in range(len(cl)):
        cash *= 1 + (g[i] if sh == 0 else 0.0)
        if i in exits and sh > 0:
            px = cl[i] * (1 - slip)
            cash += sh * px * (1 - fee)
            cur.update(exit=idx[i], exit_px=px, pnl=cash - cur["cost"], ret=cash / cur["cost"] - 1)
            trades.append(cur)
            sh = 0.0
        if i in entries and sh == 0:
            px = cl[i] * (1 + slip)
            cur = {"entry": idx[i], "entry_px": px, "cost": cash}
            sh, cash = cash / px, 0.0
        eq.append(cash + sh * cl[i])
    s = pd.Series(eq, index=idx, name=f"{sym}_TOM").iloc[200:]
    return s, [t for t in trades if t["entry"] >= s.index[0]], None


def metrics(eq, trades=None, exposure=None):
    r = eq.pct_change().dropna()
    yrs = (eq.index[-1] - eq.index[0]).days / 365.25
    dd = eq / eq.cummax() - 1
    m = {"start": str(eq.index[0].date()), "end": str(eq.index[-1].date()),
         "cagr": round(float((eq.iloc[-1] / eq.iloc[0]) ** (1 / yrs) - 1), 4),
         "sharpe": round(float(r.mean() / r.std() * math.sqrt(252)), 3) if r.std() > 0 else None,
         "max_dd": round(float(dd.min()), 4), "exposure": round(float((r != 0).mean()), 3) if exposure is None else exposure}
    if trades is not None:
        t = [x for x in trades if eq.index[0] <= x["entry"] <= eq.index[-1]]
        m.update(trades=len(t), win_rate=round(float(np.mean([x["pnl"] > 0 for x in t])), 3) if t else None,
                 avg_net_pnl_usd=round(float(np.mean([x["pnl"] for x in t])), 2) if t else None,
                 avg_net_ret_pct=round(float(np.mean([x["ret"] for x in t])) * 100, 3) if t else None)
    return m


def split_report(eq, trades, bench):
    cut = pd.Timestamp(SPLIT)
    b = bench.loc[eq.index]
    b = EQUITY0 * b / b.iloc[0]
    out = {}
    for name, sl in (("full", slice(None)), ("pre_2022", eq.index < cut), ("holdout_2022_on", eq.index >= cut)):
        e = eq[sl] if name != "full" else eq
        bb = b[sl] if name != "full" else b
        out[name] = {"strategy": metrics(e, trades), "buy_hold": metrics(bb)}
    return out


def log_trial(name, params, rep):
    con = sqlite3.connect(HERE.parent / "research.sqlite")
    con.execute("""CREATE TABLE IF NOT EXISTS research_trials (
        id INTEGER PRIMARY KEY, created_at TEXT, strategy TEXT, symbol TEXT, mode TEXT,
        params_json TEXT, data_start TEXT, data_end TEXT, split_date TEXT,
        is_json TEXT, oos_json TEXT, is_sharpe REAL, oos_sharpe REAL,
        oos_trades INTEGER, oos_avg_r REAL)""")
    pre, post = rep["pre_2022"]["strategy"], rep["holdout_2022_on"]["strategy"]
    con.execute("INSERT INTO research_trials VALUES (NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (datetime.now(timezone.utc).isoformat(), name, params["symbol"], "daily", json.dumps(params),
                 rep["full"]["strategy"]["start"], rep["full"]["strategy"]["end"], SPLIT, json.dumps(pre),
                 json.dumps(post), pre.get("sharpe"), post.get("sharpe"), post.get("trades"), None))
    con.commit()
    n = con.execute("SELECT COUNT(*) FROM research_trials").fetchone()[0]
    con.close()
    return n


def combine_with_momentum(sleeve):
    files = sorted(glob.glob(str(HERE / "results" / "us_momentum_equity_*.csv")))
    if not files:
        return {"note": "no momentum equity file found; run us_momentum_backtest.py first"}
    mom = pd.read_csv(files[-1], index_col=0, parse_dates=True)["strategy"]
    idx = mom.index.intersection(sleeve.index)
    m, s = mom.loc[idx] / mom.loc[idx].iloc[0], sleeve.loc[idx] / sleeve.loc[idx].iloc[0]
    combo = EQUITY0 * (0.5 * m + 0.5 * s)      # shared $5,000: two $2,500 sleeves, no rebalancing
    mm = m.resample("ME").last().pct_change().dropna()
    sm = s.resample("ME").last().pct_change().dropna()
    worst = mm.nsmallest(6).index
    return {"momentum_alone": metrics(EQUITY0 * m), "combined_50_50": metrics(combo),
            "daily_return_correlation": round(float(m.pct_change().corr(s.pct_change())), 3),
            "momentum_worst_6_months": {str(d.date())[:7]: {"momentum": round(float(mm[d]), 4),
                                                             "sleeve": round(float(sm.get(d, 0.0)), 4)}
                                        for d in worst}}


def main():
    o, c = load()
    results, n = {}, None
    for name, sym, kind in (("spy_mean_reversion_v1", "SPY", "mr"), ("qqq_mean_reversion_v1", "QQQ", "mr"),
                            ("spy_turn_of_month_v1", "SPY", "tom")):
        run = (lambda s, cm="zero": run_mr(o, c, sym, s, cm)) if kind == "mr" else (lambda s, cm="zero": run_tom(c, sym, s, cm))
        eq, trades, expo = run(BASE_SLIP)
        rep = split_report(eq, trades, c[sym])
        rep["full"]["strategy"]["exposure"] = round(float(_in_market(trades, eq.index).mean()), 3)
        rep["cost_stress"] = {f"{s:g}bps": metrics(run(s)[0], run(s)[1]) for s in STRESS_SLIPS}
        rep["cash_earns_shy"] = metrics(run(BASE_SLIP, "shy")[0])
        n = log_trial(name, {"symbol": sym, "rules": RULES[kind], "slip_bps": BASE_SLIP,
                             "sell_fee_bps": SELL_FEE_BPS, "equity": EQUITY0, "cash": "zero"}, rep)
        results[name] = {"report": rep, "vs_momentum": combine_with_momentum(eq), "_eq": eq}
    (HERE / "results").mkdir(exist_ok=True)
    tag = datetime.now().strftime("%Y%m%d_%H%M%S")
    clean = {k: {kk: vv for kk, vv in v.items() if kk != "_eq"} for k, v in results.items()}
    (HERE / "results" / f"daily_edges_{tag}.json").write_text(json.dumps(clean, indent=2, default=str))
    pd.concat([v["_eq"] for v in results.values()], axis=1).to_csv(HERE / "results" / f"daily_edges_equity_{tag}.csv")
    keys = ("cagr", "sharpe", "max_dd", "exposure", "trades", "win_rate", "avg_net_pnl_usd", "avg_net_ret_pct")
    for name, v in clean.items():
        print(f"\n=== {name} ===")
        for part in ("full", "pre_2022", "holdout_2022_on"):
            st, bh = v["report"][part]["strategy"], v["report"][part]["buy_hold"]
            print(f"  {part:16} strat {({k: st.get(k) for k in keys})}")
            print(f"  {'':16} hold  {({k: bh.get(k) for k in ('cagr', 'sharpe', 'max_dd')})}")
        for k, st in v["report"]["cost_stress"].items():
            print(f"  cost {k:9} {({x: st.get(x) for x in ('cagr', 'sharpe', 'max_dd', 'avg_net_ret_pct')})}")
        print(f"  cash at SHY yield {({x: v['report']['cash_earns_shy'].get(x) for x in ('cagr', 'sharpe')})}")
        vm = v["vs_momentum"]
        if "combined_50_50" in vm:
            print(f"  momentum alone   {({x: vm['momentum_alone'][x] for x in ('cagr', 'sharpe', 'max_dd')})}")
            print(f"  50/50 combined   {({x: vm['combined_50_50'][x] for x in ('cagr', 'sharpe', 'max_dd')})}")
            print(f"  daily corr {vm['daily_return_correlation']}  | momentum's worst months: {vm['momentum_worst_6_months']}")
    print(f"\ntrials logged in research.sqlite (all strategies): {n}")


if __name__ == "__main__":
    main()
