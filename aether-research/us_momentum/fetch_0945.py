#!/usr/bin/env python3
"""AETHER research: 09:45 ET fill prices for the daily candidates (Amendment 1 section 2; feed per Amendment 3).

  .venv/bin/python aether-research/us_momentum/fetch_0945.py fetch     # once; paper data keys from .env

Downloads, for the eight symbols criterion 5 needs, only:
  - the OPEN of the five-minute bars stamped 09:45, 09:50 and 09:55 ET (Alpaca SIP feed, Adjustment.ALL), and
  - the session's daily open (same feed and adjustment), the last-resort substitute.
Nothing after 2026-10-06 is kept. It then writes docs/INTRADAY_MANIFEST_daily_v1.md: the file's SHA-256, the overlap
start (the first date every symbol has a 09:45 bar) and per-symbol coverage. It never computes or prints a return or
any performance figure. The manifest must be committed BEFORE the runner may use the file.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import hashlib
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
OUT = HERE / "data" / "intraday_0945_sip.csv.gz"   # Amendment 3: SIP, not IEX
MANIFEST = REPO / "docs" / "INTRADAY_MANIFEST_daily_v1.md"
CAL_FILE = HERE / "data" / "us_daily_yf.csv.gz"     # the registered data: its SPY rows are the trading calendar
SYMBOLS = ["SPY", "QQQ", "IWM", "EFA", "TLT", "GLD", "IEF", "SHY"]
SLOTS = ("09:45", "09:50", "09:55")
FETCH_START = dt.date(2016, 1, 1)
END = dt.date(2026, 10, 6)                          # nothing after the registered window is ever kept
NY = ZoneInfo("America/New_York")


class Refused(Exception):
    pass


def _load_env():
    f = REPO / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def calendar(path=CAL_FILE, end=END) -> list[str]:
    """Trading days from the registered daily file (SPY rows), as the runner's load_data defines them."""
    days = set()
    with gzip.open(path, "rt") as f:
        for r in csv.DictReader(f):
            if r["symbol"] == "SPY" and r["close"] not in ("", "nan") and r["date"] <= end.isoformat():
                days.add(r["date"])
    return sorted(days)


def coverage(rows, cal, symbols=SYMBOLS) -> dict:
    """Per symbol: first date with a 09:45 bar, days in the calendar from the overlap start, share of those days with
    a 09:45 bar, and how the rest would be filled (09:50, 09:55, daily open, nothing). Prices are never looked at."""
    have = {}
    for s, d, slot, _ in rows:
        have.setdefault((s, d), set()).add(slot)
    first = {s: min((d for (x, d), sl in have.items() if x == s and "09:45" in sl), default=None) for s in symbols}
    if any(v is None for v in first.values()):
        missing = [s for s, v in first.items() if v is None]
        raise Refused(f"no 09:45 bar at all for {missing}")
    overlap = max(first.values())
    days = [d for d in cal if d >= overlap]
    out = {"overlap_start": overlap, "sessions": len(days), "symbols": {}}
    for s in symbols:
        kinds = {"09:45": 0, "09:50": 0, "09:55": 0, "daily": 0, "none": 0}
        for d in days:
            sl = have.get((s, d), set())
            kinds[next((k for k in ("09:45", "09:50", "09:55", "daily") if k in sl), "none")] += 1
        out["symbols"][s] = {"first_0945": first[s], "coverage_0945": kinds["09:45"] / len(days) if days else 0.0,
                             **{f"n_{k.replace(':', '')}": v for k, v in kinds.items()}}
    return out


def write_manifest(cov, file_sha, path=MANIFEST, out_file=OUT):
    lines = ["# Intraday manifest for the daily candidates (Amendment 1, section 2)", "",
             "Written by `aether-research/us_momentum/fetch_0945.py` on the server. Commit this file BEFORE any run: the",
             "runner refuses a data file whose SHA-256 differs from the one below. Coverage only; no return or performance",
             "figure was computed.", "",
             f"sha256: {file_sha}", f"file: {out_file.relative_to(REPO)}", f"overlap_start: {cov['overlap_start']}",
             f"window_end: {END.isoformat()}", f"sessions: {cov['sessions']}",
             "feed: SIP (Amendment 3), Adjustment.ALL, 5-minute bars 09:45/09:50/09:55 ET + daily open", "",
             "| symbol | first 09:45 bar | 09:45 coverage | 09:50 | 09:55 | daily open | none |",
             "|---|---|---|---|---|---|---|"]
    for s, v in cov["symbols"].items():
        lines.append(f"| {s} | {v['first_0945']} | {v['coverage_0945']:.2%} | {v['n_0950']} | {v['n_0955']} | "
                     f"{v['n_daily']} | {v['n_none']} |")
    lines += ["", "A symbol below 98% 09:45 coverage makes every run that needs it INSUFFICIENT on criterion 5."]
    path.write_text("\n".join(lines) + "\n")


def fetch(out=OUT, manifest=MANIFEST):
    if out.exists():
        raise Refused(f"{out} already exists: one fetch only")
    _load_env()
    k, s = os.getenv("APCA_API_KEY_ID", ""), os.getenv("APCA_API_SECRET_KEY", "")
    if not k or not s:
        raise Refused("missing APCA_API_KEY_ID / APCA_API_SECRET_KEY (paper data keys)")
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    dc = StockHistoricalDataClient(k, s)
    rows = []
    for year in range(FETCH_START.year, END.year + 1):
        a = dt.datetime(year, 1, 1, tzinfo=NY)
        b = dt.datetime.combine(min(dt.date(year + 1, 1, 1), END + dt.timedelta(days=1)), dt.time(), tzinfo=NY)
        five = dc.get_stock_bars(StockBarsRequest(symbol_or_symbols=SYMBOLS, timeframe=TimeFrame(5, TimeFrameUnit.Minute),
                                                  start=a, end=b, feed=DataFeed.SIP, adjustment=Adjustment.ALL)).data
        day = dc.get_stock_bars(StockBarsRequest(symbol_or_symbols=SYMBOLS, timeframe=TimeFrame.Day, start=a, end=b,
                                                 feed=DataFeed.SIP, adjustment=Adjustment.ALL)).data
        n = 0
        for sym in SYMBOLS:
            for r in five.get(sym, []):
                t = r.timestamp.astimezone(NY)
                if t.date() <= END and t.strftime("%H:%M") in SLOTS:
                    rows.append((sym, t.date().isoformat(), t.strftime("%H:%M"), float(r.open)))
                    n += 1
            for r in day.get(sym, []):
                d = r.timestamp.astimezone(NY).date()
                if d <= END:
                    rows.append((sym, d.isoformat(), "daily", float(r.open)))
        print(f"{year}: {n} five-minute rows", flush=True)
    rows.sort()
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".part")
    with gzip.open(tmp, "wt", newline="") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "date", "slot", "open"])
        w.writerows(rows)
    tmp.rename(out)
    cov = coverage(rows, calendar())
    h = sha256(out)
    write_manifest(cov, h, manifest, out)
    print(f"\noverlap start {cov['overlap_start']}, {cov['sessions']} sessions")
    for s, v in cov["symbols"].items():
        print(f"  {s:4} first 09:45 {v['first_0945']}  coverage {v['coverage_0945']:.2%}  "
              f"substitutes 09:50 {v['n_0950']} / 09:55 {v['n_0955']} / daily {v['n_daily']} / none {v['n_none']}")
    print(f"\nSHA-256 {h}\nmanifest written: {manifest.relative_to(REPO)}  (commit it before any run)")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["fetch"])
    ap.parse_args(argv)
    try:
        fetch()
        return 0
    except Refused as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
