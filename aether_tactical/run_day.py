"""Score one finished session for both strategies and append to the paper ledger.

  python3 -m aether_tactical.run_day              # newest finished session (today after 16:10 New York)
  python3 -m aether_tactical.run_day --day 2026-10-21

Run by the aether-tactical timer (Mon-Fri 16:20 New York). Paper only: never places an order.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta

from . import engulfing_engine, ledger, orb_engine
from .bars import BarError, fetch_session
from .market_time import now_ny, session_for

log = logging.getLogger("aether.tactical")
ENGINES = (orb_engine, engulfing_engine)


def last_finished_session(day: date, session_lookup=session_for):
    """The newest session on or before `day` that closed at least 10 minutes ago (looks back 10 days)."""
    for back in range(10):
        s = session_lookup(day - timedelta(days=back))
        if s is not None and now_ny() >= s.close + timedelta(minutes=10):
            return s
    return None


def score(day: date, symbol: str = "SPY", fetch=fetch_session, session_lookup=session_for) -> list[dict]:
    s = last_finished_session(day, session_lookup)
    if s is None:
        return []
    try:
        bars = fetch(symbol, s, s.close + timedelta(minutes=1))
        results = [e.evaluate(bars, s) for e in ENGINES]
    except BarError as e:
        results = [{"strategy": e2.NAME, "day": s.day, "state": "no_data", "trade": None, "reason": str(e)} for e2 in ENGINES]
    for r in results:
        r["symbol"] = symbol
        ledger.append(r)
    return results


def _notify(results, summ):
    try:
        import notify
        lines = []
        for r in results:
            t = r.get("trade")
            lines.append(f"{r['strategy']}: " + (f"{t['net_bp']:+.1f} bp net ({t['exit_reason']})" if t else r["reason"]))
        lines += [f"{k}: {v['verdict']} · {v['trades']} trades, mean {v['mean_net_bp']} bp" for k, v in summ.items()]
        notify.send_sync("📒 Tactical paper (no real orders)\n" + "\n".join(lines))
    except Exception:
        pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--day", help="YYYY-MM-DD (New York); default today")
    a = ap.parse_args(argv)
    day = date.fromisoformat(a.day) if a.day else now_ny().date()
    results = score(day)
    if not results:
        print(f"no finished session on or before {day} to score.")
        return 0
    summ = ledger.summary()
    for r in results:
        t = r.get("trade")
        print(f"{r['strategy']:9} {r['state']:9} " + (f"in {t['entry_time']} @ {t['entry_px']} out {t['exit_time']} "
              f"@ {t['exit_px']} ({t['exit_reason']}) net {t['net_bp']:+.1f} bp" if t else r["reason"]))
    for k, v in summ.items():
        print(f"{k}: {v}")
    _notify(results, summ)
    return 0


if __name__ == "__main__":
    import os
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    for f in (root / ".env",):   # paper keys only; live keys are never needed here
        if f.exists():
            for line in f.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    sys.exit(main())
