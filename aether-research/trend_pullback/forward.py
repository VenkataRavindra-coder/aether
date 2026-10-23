#!/usr/bin/env python3
"""AETHER research: trend-pullback forward journal (registration trend-pullback-v1, section 7). No orders.

Each weekday after the close it caches the finished SPY session (IEX 1-minute, regular hours), replays the
frozen rules (rules.py) over the cache, and appends every NEW closed forward trade to an append-only journal.
It never places, changes or cancels an order; it only reads market data and the calendar.

Isolation: it uses the SECOND paper account's keys (TP_APCA_API_KEY_ID / TP_APCA_API_SECRET_KEY) and refuses
to start if those are missing or equal to the main paper or the live keys. It writes only inside
aether-research/trend_pullback/forward_data/ and never touches the engine, the soak or the Risk Controller.

  python3 aether-research/trend_pullback/forward.py run      # update cache + journal (the timer runs this)
  python3 aether-research/trend_pullback/forward.py report   # forward summary per variant

Determinism check: a trade already in the journal that replays differently is written as a mismatch row
and the run exits non-zero; existing rows are never rewritten.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import os
import statistics
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HERE))

import rules  # noqa: E402
from aether_tactical.bars import Bar, BarError, verify  # noqa: E402
from aether_tactical.market_time import NY, Session  # noqa: E402

FORWARD_START = datetime(2026, 10, 26, 9, 30, tzinfo=NY)   # first session after registration
FORWARD_END = date(2027, 10, 26)
FORWARD_MIN_TRADES = 30
BACKFILL_SESSIONS = 220                                    # warm-up: >= 150 daily candles before the start
DATA = Path(os.getenv("TPB_FORWARD_DATA", str(HERE / "forward_data")))
SYMBOL = "SPY"


class Refused(Exception):
    pass


# ------------------------------------------------------------------ isolation
def keys(env=os.environ):
    k, s = env.get("TP_APCA_API_KEY_ID", ""), env.get("TP_APCA_API_SECRET_KEY", "")
    if not k or not s:
        raise Refused("missing TP_APCA_API_KEY_ID / TP_APCA_API_SECRET_KEY (second paper account)")
    others = {env.get("APCA_API_KEY_ID", ""), env.get("APCA_LIVE_API_KEY_ID", "")} - {""}
    if k in others:
        raise Refused("TP_APCA_API_KEY_ID equals the main paper or the live key: the forward test must use the "
                      "second paper account")
    return k, s


def clients(env=os.environ):
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.trading.client import TradingClient
    k, s = keys(env)
    return StockHistoricalDataClient(k, s), TradingClient(k, s, paper=True)


# ------------------------------------------------------------------ cache (one file per session)
def _file(day: str) -> Path:
    return DATA / "sessions" / f"{day}.csv.gz"


def save_session(s: Session, bars, ok: bool):
    p = _file(s.day)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".part")
    with gzip.open(tmp, "wt", newline="") as f:
        w = csv.writer(f)
        w.writerow(["#", s.open.isoformat(), s.close.isoformat(), "ok" if ok else "failed"])
        for b in bars:
            w.writerow([b.t.isoformat(), b.o, b.h, b.l, b.c, b.v])
    tmp.rename(p)


def load_sessions():
    """[(Session, bars or [] if it failed verification)] from the cache, in day order."""
    out = []
    for p in sorted((DATA / "sessions").glob("*.csv.gz")):
        with gzip.open(p, "rt") as f:
            r = csv.reader(f)
            head = next(r)
            s = Session(p.name[:10], datetime.fromisoformat(head[1]).astimezone(NY),
                        datetime.fromisoformat(head[2]).astimezone(NY))
            bars = [Bar(datetime.fromisoformat(x[0]).astimezone(NY), *map(float, x[1:6])) for x in r]
        out.append((s, bars if head[3] == "ok" else []))
    return out


def calendar(tc, start: date, end: date):
    from alpaca.trading.requests import GetCalendarRequest
    out = []
    for c in tc.get_calendar(GetCalendarRequest(start=start, end=end)) or []:
        o = c.open if c.open.tzinfo else c.open.replace(tzinfo=NY)
        cl = c.close if c.close.tzinfo else c.close.replace(tzinfo=NY)
        out.append(Session(c.date.isoformat(), o.astimezone(NY), cl.astimezone(NY)))
    return out


def update_cache(dc, tc, now: datetime, fetch=None):
    """Cache every finished session from the backfill start to now. Returns (added, failed)."""
    from aether_tactical.bars import fetch_session
    fetch = fetch or (lambda s: fetch_session(SYMBOL, s, now=s.close + timedelta(minutes=1), client=dc))
    sessions = calendar(tc, FORWARD_START.date() - timedelta(days=400), now.date())
    before = [s for s in sessions if s.open < FORWARD_START][-BACKFILL_SESSIONS:]
    after = [s for s in sessions if s.open >= FORWARD_START]
    added = failed = 0
    for s in before + after:
        if s.close + timedelta(minutes=10) > now or _file(s.day).exists():
            continue
        try:
            bars = verify(fetch(s), s)
            save_session(s, bars, True)
        except BarError:
            save_session(s, [], False)
            failed += 1
        added += 1
    return added, failed


# ------------------------------------------------------------------ journal
def journal_path() -> Path:
    return DATA / "journal.jsonl"


def journal() -> list[dict]:
    p = journal_path()
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.exists() else []


def _row(variant, t):
    return {"variant": variant, "signal_start": t["signal_start"].isoformat(),
            "entry_t": t["entry_t"].isoformat(), "entry_px": round(t["entry_px"], 4),
            "exit_t": t["exit_t"].isoformat(), "exit_px": round(t["exit_px"], 4), "exit_reason": t["exit_reason"],
            "stop": round(t["stop"], 4), "target": round(t["target"], 4), "net_bp": round(t["net_bp"], 2)}


def replay(sessions):
    """Closed forward trades per variant (a trade still open at the data end is not closed)."""
    out = {}
    markets = {tf: rules.Market(sessions, tf) for tf in rules.TIMEFRAMES}
    for name, (tf, pattern) in rules.VARIANTS.items():
        m = markets[tf]
        out[name] = [t for t in rules.run(m, pattern)
                     if t["signal_start"] >= FORWARD_START and t["exit_reason"] != "data_end"]
    return out


def append_new(trades_by_variant) -> dict:
    have = {(r["variant"], r["signal_start"]): r for r in journal() if r.get("kind", "trade") == "trade"}
    new, mismatches = [], []
    for name, trades in trades_by_variant.items():
        for t in trades:
            row = {"kind": "trade", **_row(name, t)}
            old = have.get((name, row["signal_start"]))
            if old is None:
                new.append(row)
            elif {k: old.get(k) for k in row} != row:
                mismatches.append({"kind": "mismatch", "variant": name, "signal_start": row["signal_start"],
                                   "journal": old, "replay": row,
                                   "noted_at": datetime.now(NY).isoformat(timespec="seconds")})
    if new or mismatches:
        journal_path().parent.mkdir(parents=True, exist_ok=True)
        with journal_path().open("a") as f:
            for r in new + mismatches:
                f.write(json.dumps(r, separators=(",", ":")) + "\n")
    return {"new": len(new), "mismatches": len(mismatches)}


def summary() -> dict:
    rows = [r for r in journal() if r.get("kind", "trade") == "trade"]
    out = {}
    for name in rules.VARIANTS:
        nets = [r["net_bp"] for r in rows if r["variant"] == name]
        n = len(nets)
        mean = statistics.fmean(nets) if n else None
        sd = statistics.stdev(nets) if n > 1 else 0.0
        done = n >= FORWARD_MIN_TRADES or date.today() >= FORWARD_END
        state = "collecting" if not done else ("consistent" if (mean or 0) > 0 else "not consistent")
        out[name] = {"trades": n, "mean_net_bp": mean,
                     "t_stat": (mean / (sd / math.sqrt(n))) if n > 1 and sd > 0 else None, "state": state}
    out["_mismatches"] = sum(1 for r in journal() if r.get("kind") == "mismatch")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["run", "report"])
    a = ap.parse_args(argv)
    try:
        if a.cmd == "run":
            dc, tc = clients()
            added, failed = update_cache(dc, tc, datetime.now(NY))
            res = append_new(replay(load_sessions()))
            print(f"cache +{added} sessions ({failed} failed verification); journal +{res['new']} trades, "
                  f"{res['mismatches']} mismatches")
            if res["mismatches"]:
                print("REFUSED: replay disagrees with the journal (determinism check)", file=sys.stderr)
                return 3
        print(json.dumps(summary(), indent=2))
        return 0
    except Refused as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
