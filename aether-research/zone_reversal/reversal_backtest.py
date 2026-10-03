#!/usr/bin/env python3
"""AETHER research: zone-reversal backtest for QQQ and BTC/USD (1-minute bars).

Research only. This file never places orders. Rule definitions are in SPEC.md.

  python reversal_backtest.py fetch --symbol QQQ --asset stock --start 2019-01-01 --end 2026-09-01
  python reversal_backtest.py run   --symbol QQQ --asset stock --mode us_open --split-date 2024-01-01
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
from collections import Counter
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ET = "America/New_York"


# ----------------------------------------------------------------------------- params
@dataclass
class Params:
    symbol: str = "QQQ"
    asset: str = "stock"            # stock | crypto
    mode: str = "us_open"           # us_open | all_hours
    window_start: str = "09:45"     # ET; first bar an entry may fill on (us_open)
    window_end: str = "10:00"       # ET; last bar an entry may fill on, inclusive
    exit_time: str = "12:00"        # ET; time exit (us_open)
    max_hold_bars: int = 240        # time exit (all_hours)
    zone_lookback_days: int = 5
    zone_pivot_k: int = 2
    zone_width_atr: float = 0.25    # zone half-width, in 15-min ATR
    fast_lookback: int = 15         # bars
    fast_min_atr: float = 3.0       # min move into zone, in 1-min ATR
    fast_max_pullback: float = 0.3  # max bounce during the move, fraction of move
    break_lookback: int = 5
    hl_confirm_bars: int = 2
    candle_body_min: float = 0.6    # body / range
    candle_range_min_atr: float = 1.0
    stop_buffer_atr: float = 0.1
    target_r: float = 2.0
    setup_timeout_bars: int = 30
    max_trades_per_day: int = 1
    allow_short: bool = True
    equity: float = 5000.0          # fixed; risk never compounds
    risk_pct: float = 0.01
    max_leverage: float = 1.0       # notional cap = equity * max_leverage
    fee_rate: float = 0.0           # per side, fraction of notional
    slippage_bps: float = 1.0       # per side
    pdt_limit: bool = True          # stocks under $25k: max 3 day trades / 5 sessions


def asset_defaults(asset: str) -> dict:
    if asset == "crypto":
        # Alpaca crypto is long-only (no shorting); fee is the entry-tier taker rate, check yours.
        return dict(allow_short=False, fee_rate=0.0025, slippage_bps=5.0, pdt_limit=False)
    return dict(allow_short=True, fee_rate=0.0, slippage_bps=1.0, pdt_limit=True)


def _hm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


# ----------------------------------------------------------------------------- indicators
def atr(h, l, c, n=14, session_start=None):
    prev = np.r_[np.nan, c[:-1]]
    if session_start is not None:           # no overnight gap in the first bar's true range
        prev = np.where(session_start, np.nan, prev)
    tr = np.fmax(h - l, np.fmax(np.abs(h - prev), np.abs(l - prev)))
    return pd.Series(tr).rolling(n, min_periods=n).mean().to_numpy()


def build_zones(bars15: pd.DataFrame, cutoff: pd.Timestamp, p: Params):
    """Support/resistance zones from 15-min swing points strictly before `cutoff`."""
    hist = bars15[(bars15.index < cutoff) &
                  (bars15.index >= cutoff - pd.Timedelta(days=p.zone_lookback_days))]
    k = p.zone_pivot_k
    if len(hist) < max(20, 2 * k + 1):
        return []
    h, l, c = hist["high"].to_numpy(), hist["low"].to_numpy(), hist["close"].to_numpy()
    a = atr(h, l, c, 14)[-1]
    if not np.isfinite(a) or a <= 0:
        return []
    w = p.zone_width_atr * a
    levels = []
    for j in range(k, len(hist) - k):        # j + k < len: pivot confirmed before cutoff
        if h[j] == h[j - k:j + k + 1].max():
            levels.append(h[j])
        if l[j] == l[j - k:j + k + 1].min():
            levels.append(l[j])
    zones = []
    for lv in sorted(levels):
        lo, hi = lv - w, lv + w
        if zones and lo <= zones[-1][1]:
            zones[-1] = (zones[-1][0], max(zones[-1][1], hi))
        else:
            zones.append((lo, hi))
    return zones


def mirror(o, h, l, c, zones):
    """Map a short setup onto the long rules: negate prices, swap high/low."""
    return -o, -l, -h, -c, sorted((-hi, -lo) for lo, hi in zones)


# ----------------------------------------------------------------------------- setup detector
class LongDetector:
    """State machine for the long reversal. Uses only bars <= i at step(i).

    idle -> touched (fast move into support) -> broken (micro-downtrend break)
         -> hl (higher low confirmed) -> signal on a strong candle.
    step() returns the stop price when bar i completes a setup (entry at bar i+1 open).
    """

    def __init__(self, p: Params):
        self.p = p
        self.reset()

    def reset(self):
        self.state, self.touch_i, self.touch_low = "idle", -1, None
        self.zone, self.break_i, self.hl = None, -1, None

    def _fast_move_into(self, i, h, l, c, a, zones):
        p, L = self.p, self.p.fast_lookback
        if i < L:
            return None
        start, move = c[i - L], c[i - L] - l[i]
        if move < p.fast_min_atr * a:
            return None
        run_min, bounce = np.inf, 0.0
        for t in range(i - L + 1, i):
            run_min = min(run_min, l[t])
            bounce = max(bounce, c[t] - run_min)
        if bounce > p.fast_max_pullback * move:
            return None
        for lo, hi in reversed(zones):          # nearest zone below the starting price first
            width = hi - lo
            if start > hi and lo - width <= l[i] <= hi:
                return (lo, hi)
        return None

    def step(self, i, o, h, l, c, a_arr, zones):
        p, a = self.p, a_arr[i]
        if not np.isfinite(a) or a <= 0:
            return None
        if self.state != "idle":
            width = self.zone[1] - self.zone[0]
            if i - self.touch_i > p.setup_timeout_bars or c[i] < self.zone[0] - width:
                self.reset()

        if self.state == "idle":
            z = self._fast_move_into(i, h, l, c, a, zones)
            if z:
                self.state, self.touch_i, self.touch_low, self.zone = "touched", i, l[i], z
            return None

        if l[i] < self.touch_low:               # new low: the reversal restarts from here
            self.touch_low, self.state = l[i], "touched"

        if self.state == "touched":
            B = p.break_lookback
            if i > self.touch_i and i >= B and c[i] > h[i - B:i].max():
                self.state, self.break_i = "broken", i
            return None

        if self.state == "hl" and l[i] < self.hl:
            self.state = "broken"               # higher low failed; look for another

        if self.state == "broken":
            k = p.hl_confirm_bars
            j = i - k
            if j > self.break_i and l[j] <= l[j - 1] and l[j] < l[j + 1:i + 1].min() \
                    and l[j] > self.touch_low:
                self.state, self.hl = "hl", l[j]
            return None

        if self.state == "hl":
            rng, body = h[i] - l[i], c[i] - o[i]
            if (body > 0 and rng > 0 and body >= p.candle_body_min * rng
                    and rng >= p.candle_range_min_atr * a and c[i] > h[i - 1]):
                stop = self.hl - p.stop_buffer_atr * a
                self.reset()
                return stop
        return None


# ----------------------------------------------------------------------------- execution helpers
def size_position(p: Params, entry_abs: float, stop_dist: float):
    """Fixed-dollar risk, capped by notional. Returns (qty, capped)."""
    if stop_dist <= 0:
        return 0.0, False
    qty = p.risk_pct * p.equity / stop_dist
    cap = p.equity * p.max_leverage / entry_abs
    capped = qty > cap
    qty = min(qty, cap)
    qty = math.floor(qty + 1e-9) if p.asset == "stock" else math.floor(qty * 1e6 + 1e-6) / 1e6
    return float(qty), capped


def check_exit(pos, i, ot, ht, lt):
    """Stop/target on bar i in long-space. Stop is assumed first if both are touched."""
    first = i == pos["entry_i"]
    if lt <= pos["stop"]:
        return (pos["stop"] if first else min(ot, pos["stop"])), "stop"
    if ht >= pos["target"]:
        return (pos["target"] if first else max(ot, pos["target"])), "target"
    return None, None


def pdt_blocked(day_trade_dates, prior_sessions, today) -> bool:
    """True if 3 day trades already fall in the rolling 5 sessions ending today."""
    last5 = set(prior_sessions[-4:]) | {today}
    return sum(d in last5 for d in day_trade_dates) >= 3


# ----------------------------------------------------------------------------- engine
def run_backtest(df: pd.DataFrame, p: Params, zone_provider=None):
    """df: 1-min bars, UTC DatetimeIndex, columns open/high/low/close."""
    df = df.sort_index()
    df = df[~df.index.duplicated()]
    et = df.index.tz_convert(ET)
    mins = (et.hour * 60 + et.minute).to_numpy()
    if p.asset == "stock":
        keep = (mins >= 570) & (mins < 960)
        df, et, mins = df[keep], et[keep], mins[keep]

    o, h, l, c = (df[x].to_numpy(dtype=float) for x in ("open", "high", "low", "close"))
    n = len(df)
    et_date = np.array(et.date)
    utc_date = np.array(df.index.date)
    day = et_date if (p.mode == "us_open" or p.asset == "stock") else utc_date
    session_start = np.r_[True, day[1:] != day[:-1]] if p.asset == "stock" else None
    a = atr(h, l, c, 14, session_start)
    mo, mh, ml, mc = -o, -l, -h, -c

    if zone_provider is None:
        bars15 = df.resample("15min", label="left", closed="left").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()

        def zone_provider(d):
            if p.mode == "us_open" or p.asset == "stock":
                cutoff = pd.Timestamp(f"{d} 09:30", tz=ET).tz_convert("UTC")
            else:
                cutoff = pd.Timestamp(str(d), tz="UTC")
            return build_zones(bars15, cutoff, p)

    ws, we, xt = _hm(p.window_start), _hm(p.window_end), _hm(p.exit_time)
    active_from = 570 if p.mode == "us_open" else 0

    longd, shortd = LongDetector(p), LongDetector(p)
    zones, mzones = [], []
    pos, trades, skips = None, [], Counter()
    trades_today, cur_day = 0, None
    session_dates, day_trade_dates = [], []

    for i in range(n):
        if day[i] != cur_day:
            cur_day = day[i]
            zones = zone_provider(cur_day)
            mzones = sorted((-hi, -lo) for lo, hi in zones)
            longd.reset(); shortd.reset()
            trades_today = 0
            session_dates.append(cur_day)

        # 1) manage the open position on this bar
        if pos is not None:
            d = pos["dir"]
            ot, ht, lt, ct = (o[i], h[i], l[i], c[i]) if d > 0 else (mo[i], mh[i], ml[i], mc[i])
            px, why = check_exit(pos, i, ot, ht, lt)
            last_of_day = i == n - 1 or day[i + 1] != day[i]
            if px is None:
                if p.mode == "us_open" and (mins[i] >= xt or last_of_day):
                    px, why = ct, "time"
                elif p.mode == "all_hours" and (i - pos["entry_i"] + 1 >= p.max_hold_bars
                                                or i == n - 1 or (p.asset == "stock" and last_of_day)):
                    px, why = ct, "time"
            if px is not None:
                px -= abs(px) * p.slippage_bps / 1e4
                fee = p.fee_rate * pos["qty"] * (abs(pos["entry"]) + abs(px))
                pnl = pos["qty"] * (px - pos["entry"]) - fee
                risk = p.risk_pct * p.equity
                trades.append(dict(
                    entry_time=df.index[pos["entry_i"]], exit_time=df.index[i],
                    side="long" if d > 0 else "short",
                    entry=d * pos["entry"], stop=d * pos["stop"], target=d * pos["target"],
                    exit=d * px, qty=pos["qty"], notional=pos["qty"] * abs(pos["entry"]),
                    capped=pos["capped"], risk_intended=risk,
                    risk_actual=pos["qty"] * (pos["entry"] - pos["stop"]),
                    fees=fee, pnl=pnl, r=pnl / risk, exit_reason=why))
                pos = None

        # 2) detect setups (detectors use bars <= i only)
        if p.mode == "us_open" and not (active_from <= mins[i] <= xt):
            continue
        sigs = []
        s = longd.step(i, o, h, l, c, a, zones)
        if s is not None:
            sigs.append((1, s))
        if p.allow_short:
            s = shortd.step(i, mo, mh, ml, mc, a, mzones)
            if s is not None:
                sigs.append((-1, -s))           # stop stored in long-space below
        if not sigs:
            continue

        # 3) entry filters, then fill at the next bar's open
        for d, stop_real in sigs:
            j = i + 1
            if j >= n or day[j] != day[i]:
                skips["no_next_bar"] += 1; continue
            if p.mode == "us_open" and not (ws <= mins[j] <= we):
                skips["outside_window"] += 1; continue
            if pos is not None:
                skips["in_position"] += 1; continue
            if trades_today >= p.max_trades_per_day:
                skips["daily_limit"] += 1; continue
            if p.asset == "stock" and p.pdt_limit and p.equity < 25_000 and \
                    pdt_blocked(day_trade_dates, session_dates[:-1], day[j]):
                skips["pdt_limit"] += 1; continue
            ot = o[j] if d > 0 else mo[j]
            entry = ot + abs(ot) * p.slippage_bps / 1e4
            stop = d * stop_real
            if entry <= stop:
                skips["gap_past_stop"] += 1; continue
            qty, capped = size_position(p, abs(entry), entry - stop)
            if qty <= 0:
                skips["min_size"] += 1; continue
            pos = dict(dir=d, entry_i=j, entry=entry, stop=stop,
                       target=entry + p.target_r * (entry - stop), qty=qty, capped=capped)
            trades_today += 1
            day_trade_dates.append(day[j])

    cols = ["entry_time", "exit_time", "side", "entry", "stop", "target", "exit", "qty",
            "notional", "capped", "risk_intended", "risk_actual", "fees", "pnl", "r", "exit_reason"]
    return pd.DataFrame(trades, columns=cols), dict(skips)


# ----------------------------------------------------------------------------- metrics
def metrics(trades: pd.DataFrame, p: Params, start, end) -> dict:
    out = {"trades": int(len(trades))}
    if trades.empty:
        return out
    pnl, r = trades["pnl"], trades["r"]
    wins, losses = pnl[pnl > 0].sum(), -pnl[pnl < 0].sum()
    eq = np.r_[p.equity, p.equity + pnl.cumsum().to_numpy()]
    peak = np.maximum.accumulate(eq)
    freq = "B" if p.asset == "stock" else "D"
    days = pd.date_range(pd.Timestamp(start).normalize(), pd.Timestamp(end).normalize(),
                         freq=freq)
    daily = pnl.groupby(trades["exit_time"].dt.normalize()).sum().reindex(days, fill_value=0.0)
    ret = daily / p.equity                      # fixed-risk sizing: returns on fixed equity
    sd = ret.std(ddof=1)
    ann = 252 if p.asset == "stock" else 365
    out.update(
        win_rate=float((pnl > 0).mean()),
        avg_r=float(r.mean()),
        median_r=float(r.median()),
        profit_factor=float(wins / losses) if losses > 0 else None,
        total_pnl=float(pnl.sum()),
        total_return=float(pnl.sum() / p.equity),
        max_drawdown=float(((eq - peak) / peak).min()),
        sharpe=float(ret.mean() / sd * math.sqrt(ann)) if sd > 0 else None,
        daily_skew=float(ret.skew()), daily_kurtosis=float(ret.kurt() + 3),
        n_days=int(len(ret)),
        pct_capped=float(trades["capped"].mean()),
        avg_risk_actual=float(trades["risk_actual"].mean()),
        worst_losing_streak=int(_streak(pnl < 0)),
    )
    return out


def _streak(mask) -> int:
    best = run = 0
    for m in mask:
        run = run + 1 if m else 0
        best = max(best, run)
    return best


# ----------------------------------------------------------------------------- trial log
def log_trial(db_path, strategy, p, data_start, data_end, split, is_m, oos_m) -> int:
    con = sqlite3.connect(db_path)
    con.execute("""CREATE TABLE IF NOT EXISTS research_trials (
        id INTEGER PRIMARY KEY, created_at TEXT, strategy TEXT, symbol TEXT, mode TEXT,
        params_json TEXT, data_start TEXT, data_end TEXT, split_date TEXT,
        is_json TEXT, oos_json TEXT, is_sharpe REAL, oos_sharpe REAL,
        oos_trades INTEGER, oos_avg_r REAL)""")
    cur = con.execute(
        "INSERT INTO research_trials VALUES (NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (datetime.now(timezone.utc).isoformat(), strategy, p.symbol, p.mode,
         json.dumps(asdict(p), sort_keys=True), str(data_start), str(data_end), str(split),
         json.dumps(is_m), json.dumps(oos_m), is_m.get("sharpe"), oos_m.get("sharpe"),
         oos_m.get("trades"), oos_m.get("avg_r")))
    con.commit()
    n = con.execute("SELECT COUNT(*) FROM research_trials WHERE strategy=?", (strategy,)).fetchone()[0]
    con.close()
    return n


# ----------------------------------------------------------------------------- data
def _cache_name(symbol: str, month: pd.Timestamp) -> str:
    return f"{symbol.replace('/', '-')}_1min_{month:%Y-%m}.csv.gz"


def fetch(symbol, asset, start, end, cache_dir, feed="sip"):
    """Download 1-min bars month by month from Alpaca into gzipped CSVs. Needs alpaca-py."""
    from alpaca.data.timeframe import TimeFrame
    cache = Path(cache_dir); cache.mkdir(parents=True, exist_ok=True)
    if asset == "stock":
        from alpaca.data.enums import DataFeed
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        key = os.getenv("APCA_API_KEY_ID") or os.getenv("ALPACA_API_KEY")
        sec = os.getenv("APCA_API_SECRET_KEY") or os.getenv("ALPACA_SECRET_KEY")
        client = StockHistoricalDataClient(key, sec)
        make = lambda s, e: StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Minute,
                                             start=s, end=e, feed=DataFeed(feed))
        get = client.get_stock_bars
    else:
        from alpaca.data.historical import CryptoHistoricalDataClient
        from alpaca.data.requests import CryptoBarsRequest
        client = CryptoHistoricalDataClient()
        make = lambda s, e: CryptoBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Minute,
                                              start=s, end=e)
        get = client.get_crypto_bars

    now = pd.Timestamp.now(tz="UTC")
    for m in pd.date_range(pd.Timestamp(start, tz="UTC").replace(day=1), pd.Timestamp(end, tz="UTC"), freq="MS"):
        s, e = m, min(m + pd.offsets.MonthBegin(1), now - pd.Timedelta(minutes=20))
        f = cache / _cache_name(symbol, m)
        if f.exists() and e == m + pd.offsets.MonthBegin(1):
            continue                             # complete month already cached
        bars = get(make(s.to_pydatetime(), e.to_pydatetime())).df
        if bars.empty:
            print(f"{m:%Y-%m}: no data"); continue
        if isinstance(bars.index, pd.MultiIndex):
            bars = bars.xs(symbol, level=0)
        bars[["open", "high", "low", "close", "volume"]].to_csv(f, compression="gzip")
        print(f"{m:%Y-%m}: {len(bars):,} bars")


def load_bars(symbol, cache_dir, start=None, end=None, csv=None) -> pd.DataFrame:
    files = [Path(csv)] if csv else sorted(Path(cache_dir).glob(f"{symbol.replace('/', '-')}_1min_*.csv.gz"))
    if not files:
        raise SystemExit(f"no bar files for {symbol} in {cache_dir}; run fetch first")
    df = pd.concat(pd.read_csv(f, index_col=0) for f in files)
    df.index = pd.to_datetime(df.index, utc=True)
    df = df.sort_index()
    df = df[~df.index.duplicated()]
    if start:
        df = df[df.index >= pd.Timestamp(start, tz="UTC")]
    if end:
        df = df[df.index < pd.Timestamp(end, tz="UTC")]
    return df


# ----------------------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--symbol", required=True); f.add_argument("--asset", choices=["stock", "crypto"], required=True)
    f.add_argument("--start", required=True); f.add_argument("--end", required=True)
    f.add_argument("--cache-dir", default="data"); f.add_argument("--feed", default="sip", choices=["sip", "iex"])

    r = sub.add_parser("run")
    r.add_argument("--symbol", required=True); r.add_argument("--asset", choices=["stock", "crypto"], required=True)
    r.add_argument("--mode", choices=["us_open", "all_hours"], default="us_open")
    r.add_argument("--start"); r.add_argument("--end")
    r.add_argument("--split-date", required=True, help="in-sample before, out-of-sample from this date")
    r.add_argument("--params", default="{}", help='JSON overrides, e.g. \'{"target_r": 1.5}\'')
    r.add_argument("--cache-dir", default="data"); r.add_argument("--csv")
    r.add_argument("--out-dir", default="results"); r.add_argument("--trial-db", default="research.sqlite")
    r.add_argument("--strategy", default="zone_reversal_v1")
    args = ap.parse_args()

    if args.cmd == "fetch":
        fetch(args.symbol, args.asset, args.start, args.end, args.cache_dir, args.feed)
        return

    overrides = json.loads(args.params)
    unknown = set(overrides) - {x.name for x in fields(Params)}
    if unknown:
        raise SystemExit(f"unknown params: {sorted(unknown)}")
    p = Params(symbol=args.symbol, asset=args.asset, mode=args.mode,
               **{**asset_defaults(args.asset), **overrides})

    df = load_bars(p.symbol, args.cache_dir, args.start, args.end, args.csv)
    trades, skips = run_backtest(df, p)
    split = pd.Timestamp(args.split_date, tz="UTC")
    d0, d1 = df.index[0], df.index[-1]
    is_t, oos_t = trades[trades["entry_time"] < split], trades[trades["entry_time"] >= split]
    is_m, oos_m = metrics(is_t, p, d0, split - pd.Timedelta(days=1)), metrics(oos_t, p, split, d1)

    out = Path(args.out_dir); out.mkdir(parents=True, exist_ok=True)
    tag = f"{p.symbol.replace('/', '-')}_{p.mode}_{datetime.now():%Y%m%d_%H%M%S}"
    trades.to_csv(out / f"trades_{tag}.csv", index=False)
    n_trials = log_trial(args.trial_db, args.strategy, p, d0, d1, split.date(), is_m, oos_m)
    report = dict(params=asdict(p), data=[str(d0), str(d1)], skips=skips,
                  in_sample=is_m, out_of_sample=oos_m, trials_logged_for_strategy=n_trials)
    (out / f"summary_{tag}.json").write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
