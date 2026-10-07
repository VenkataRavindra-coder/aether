#!/usr/bin/env python3
"""AETHER research: long price history for the US ETF momentum rule (plan S12, S14, S15).

Alpaca's cached bars start around 2016, so 2008 and 2011 are missing from the regime slices. Yahoo (via
yfinance) goes back to 2006 for all eleven ETFs. This module fetches it into the SAME csv layout as the Alpaca
cache (symbol, date, open, close) and compares the two sources over their overlap.

  python long_history.py fetch [--start 2005-01-01]     # needs yfinance (research venv only)
  python long_history.py compare                        # needs only numpy/pandas

The helpers are pure functions so they are tested without network access or yfinance.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ENGINE_DIR = Path(os.getenv("AETHER_DIR", str(Path.home())))
sys.path.insert(0, str(ENGINE_DIR))
import us_strategy as us  # noqa: E402

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
ALPACA_CACHE = DATA / "us_daily_all.csv.gz"
YF_CACHE = DATA / "us_daily_yf.csv.gz"
MIN_ROWS = 2000                 # about eight years of trading days per symbol
MAX_GAP_DAYS = 10
RETURN_DIFF_MEDIAN_BPS = 5.0    # plan S14: median daily-return difference between sources
SIGNAL_AGREEMENT_MIN = 0.95     # plan S14: share of monthly signals that agree


# ------------------------------------------------------------------ normalising yfinance output
def normalize_yf(raw: pd.DataFrame, universe) -> pd.DataFrame:
    """Turn the table yf.download() returns into rows (symbol, date, open, close). Works whichever way this
    yfinance version orders the two column levels (ticker first or field first), and for one ticker."""
    if raw is None or len(raw) == 0:
        raise ValueError("yfinance returned no data")
    cols = raw.columns
    frames = []
    for sym in universe:
        if isinstance(cols, pd.MultiIndex):
            if sym in set(cols.get_level_values(0)):
                sub = raw[sym]
            elif sym in set(cols.get_level_values(1)):
                sub = raw.xs(sym, axis=1, level=1)
            else:
                raise ValueError(f"{sym} missing from the download")
        else:
            sub = raw
        sub = sub[["Open", "Close"]].dropna(subset=["Close"])
        idx = pd.DatetimeIndex(sub.index)
        if idx.tz is not None:
            idx = idx.tz_localize(None)        # keep the wall-clock day the source printed; never shift it
        frames.append(pd.DataFrame({"symbol": sym, "date": idx.strftime("%Y-%m-%d"),
                                    "open": sub["Open"].to_numpy(dtype=float),
                                    "close": sub["Close"].to_numpy(dtype=float)}))
    out = pd.concat(frames, ignore_index=True).sort_values(["symbol", "date"]).reset_index(drop=True)
    return out


def check_history(rows: pd.DataFrame, universe, min_rows=MIN_ROWS, max_gap=MAX_GAP_DAYS) -> list[str]:
    """Problems with a fetched history (empty list = fine). Fetching must fail loudly, never quietly."""
    problems = []
    for sym in universe:
        g = rows[rows["symbol"] == sym]
        if len(g) < min_rows:
            problems.append(f"{sym}: only {len(g)} rows (need {min_rows})")
            continue
        if not np.all(np.isfinite(g["close"])) or (g["close"] <= 0).any():
            problems.append(f"{sym}: non-finite or non-positive close")
        d = pd.to_datetime(g["date"])
        if not d.is_monotonic_increasing or d.duplicated().any():
            problems.append(f"{sym}: dates not strictly increasing")
            continue
        gap = d.diff().dt.days.max()
        if gap > max_gap:
            problems.append(f"{sym}: {int(gap)}-day gap in the history")
        step = g["close"].pct_change().abs().max()
        if step > us.__dict__.get("MAX_DAILY_MOVE", 0.40):
            problems.append(f"{sym}: a {step:.0%} one-day move (bad tick or unadjusted split?)")
    return problems


# ------------------------------------------------------------------ comparing two sources
def closes_table(rows: pd.DataFrame, field="close") -> pd.DataFrame:
    return rows.pivot(index="date", columns="symbol", values=field).sort_index()


def compare_returns(a: pd.DataFrame, b: pd.DataFrame, universe) -> dict:
    """Daily-return difference between two sources over their common dates. Levels differ because each source
    anchors its dividend adjustment at a different date, so RETURNS are what must agree."""
    common = a.index.intersection(b.index)
    if len(common) < 250:
        raise ValueError(f"only {len(common)} common dates between the two sources")
    ra = a.loc[common, list(universe)].pct_change().iloc[1:]
    rb = b.loc[common, list(universe)].pct_change().iloc[1:]
    diff = (ra - rb).abs() * 1e4
    per = {s: {"median_bps": round(float(diff[s].median()), 3), "p99_bps": round(float(diff[s].quantile(0.99)), 2),
               "max_bps": round(float(diff[s].max()), 1)} for s in universe}
    return {"common_dates": int(len(common)), "first": common[0], "last": common[-1], "per_symbol": per,
            "worst_median_bps": max(v["median_bps"] for v in per.values())}


def monthly_signals(closes: pd.DataFrame) -> dict:
    """{'YYYY-MM': sorted selected symbols} computed exactly as the backtest does: on the first trading day of each
    month, from closes strictly before that day, through the live us_strategy.target_weights."""
    dates = list(closes.index)
    cl = closes[us.UNIVERSE].to_numpy(dtype=float)
    col = {s: j for j, s in enumerate(us.UNIVERSE)}
    out = {}
    for i, d in enumerate(dates):
        if i > 0 and d[:7] != dates[i - 1][:7]:
            hist = {s: [x for x in cl[:i, col[s]] if np.isfinite(x)] for s in us.UNIVERSE}
            try:
                _, _, sel = us.target_weights(hist)
            except ValueError:
                continue
            out[d[:7]] = sorted(sel)
    return out


def compare_signals(sa: dict, sb: dict) -> dict:
    months = sorted(set(sa) & set(sb))
    if not months:
        raise ValueError("no months with a signal in both sources")
    diff = [m for m in months if sa[m] != sb[m]]
    return {"months": len(months), "agree": len(months) - len(diff),
            "agreement": round((len(months) - len(diff)) / len(months), 4), "differing_months": diff}


def verdict(ret: dict, sig: dict) -> dict:
    ok_ret = ret["worst_median_bps"] < RETURN_DIFF_MEDIAN_BPS
    ok_sig = sig["agreement"] >= SIGNAL_AGREEMENT_MIN
    return {"returns_ok": ok_ret, "signals_ok": ok_sig, "pass": ok_ret and ok_sig}


# ------------------------------------------------------------------ CLI
def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def cmd_fetch(start: str) -> None:
    import yfinance as yf  # research venv only: the engine's venv deliberately does not have it
    raw = yf.download(list(us.UNIVERSE), start=start, auto_adjust=True, progress=False, group_by="ticker",
                      threads=False)
    rows = normalize_yf(raw, us.UNIVERSE)
    problems = check_history(rows, us.UNIVERSE)
    for s in us.UNIVERSE:
        g = rows[rows["symbol"] == s]
        print(f"{s:4} {len(g):5} rows  {g['date'].iloc[0]} .. {g['date'].iloc[-1]}")
    if problems:
        raise SystemExit("REFUSING TO SAVE, problems found:\n  " + "\n  ".join(problems))
    DATA.mkdir(exist_ok=True)
    rows.to_csv(YF_CACHE, index=False, compression="gzip")
    print(f"saved {YF_CACHE}  sha256 {sha256_file(YF_CACHE)[:16]}")


def cmd_compare() -> None:
    a = closes_table(pd.read_csv(ALPACA_CACHE))
    b = closes_table(pd.read_csv(YF_CACHE))
    ret = compare_returns(a, b, us.UNIVERSE)
    sig = compare_signals(monthly_signals(a), monthly_signals(b))
    v = verdict(ret, sig)
    report = {"returns": ret, "signals": sig, "verdict": v,
              "alpaca_sha256": sha256_file(ALPACA_CACHE)[:16], "yahoo_sha256": sha256_file(YF_CACHE)[:16]}
    (HERE / "results").mkdir(exist_ok=True)
    out = HERE / "results" / "source_comparison.json"
    out.write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps(report, indent=2, default=str))
    print("S14:", "PASS" if v["pass"] else "FAIL")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--start", default="2005-01-01")
    sub.add_parser("compare")
    a = ap.parse_args()
    cmd_fetch(a.start) if a.cmd == "fetch" else cmd_compare()


if __name__ == "__main__":
    main()
