#!/usr/bin/env python3
"""AETHER research: tactical strategies A (orb15) and B (engulf15) on SPY 1-minute history.

Registered in docs/PREREG_tactical_hist_v1.md (tag `prereg-tactical-hist-v1`) before any data was fetched.
Research only: nothing here places orders or is imported by the engine. The rules are the engine's own
modules (aether_tactical), imported, never copied.

  python3 aether-research/tactical_hist/tactical_hist.py fetch               # once; Alpaca SIP, paper data keys
  python3 aether-research/tactical_hist/tactical_hist.py run                 # all variants, C1-C6, verdicts
  python3 aether-research/tactical_hist/tactical_hist.py run --log-trials    # also log the 6 variants, once

Fail closed: `run` is refused unless the registration tag is an ancestor of HEAD and tracked files are
committed; `fetch` refuses if the data already exists; more than 2% bad sessions refuses the run.
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import gzip
import hashlib
import json
import math
import os
import sqlite3
import statistics
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESEARCH = HERE.parent
REPO = RESEARCH.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(RESEARCH))

from aether_tactical import engulfing_engine, orb_engine, sim  # noqa: E402
from aether_tactical.bars import Bar, BarError, verify  # noqa: E402
from aether_tactical.market_time import NY, Session  # noqa: E402

TAG = "prereg-tactical-hist-v1"
BATCH = "tactical-hist-v1"
SYMBOL = "SPY"
START, END = date(2016, 1, 4), date(2026, 10, 20)   # nothing on or after 2026-10-21 (forward test) is read
FULL_YEARS = list(range(2016, 2026))
DATA = Path(os.getenv("TACTICAL_HIST_DATA", str(HERE / "data")))
BARS_FILE, CAL_FILE = DATA / "spy_1m_sip.csv.gz", DATA / "calendar.csv"
RESULTS = HERE / "results"
TRIAL_DB = RESEARCH / "research.sqlite"

MAX_BAD_SHARE = 0.02
MIN_TRADES, MIN_T, MIN_YEARS_UP, DSR_MIN = 100, 2.0, 6, 0.95
STRESS_COST_BP = 4.0
N_FLOOR = 37                                   # 31 from daily-v1 + these 6
HIST_VAR_PER_PERIOD = 0.0003242764550264549    # same surviving stage-1 record daily-v1 uses (amendment 2)

# (variant name, engine module, attribute, value, is_baseline)
VARIANTS = [
    ("orb15", orb_engine, "RANGE", timedelta(minutes=15), True),
    ("orb10", orb_engine, "RANGE", timedelta(minutes=10), False),
    ("orb30", orb_engine, "RANGE", timedelta(minutes=30), False),
    ("engulf15_v1.5", engulfing_engine, "VOL_MULT", 1.5, True),
    ("engulf15_v1.3", engulfing_engine, "VOL_MULT", 1.3, False),
    ("engulf15_v2.0", engulfing_engine, "VOL_MULT", 2.0, False),
]
FAMILY = {"orb15": "A", "orb10": "A", "orb30": "A", "engulf15_v1.5": "B", "engulf15_v1.3": "B", "engulf15_v2.0": "B"}


class Refused(Exception):
    pass


# ------------------------------------------------------------------ data
def _load_env():
    f = REPO / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def fetch(out_dir: Path = DATA) -> str:
    """Download SPY SIP 1-minute bars and the market calendar for START..END, once."""
    bars_f, cal_f = out_dir / BARS_FILE.name, out_dir / CAL_FILE.name
    if bars_f.exists() or cal_f.exists():
        raise Refused(f"{bars_f} already exists: one fetch only (prereg section 2)")
    _load_env()
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import GetCalendarRequest
    k, s = os.getenv("APCA_API_KEY_ID", ""), os.getenv("APCA_API_SECRET_KEY", "")
    if not k or not s:
        raise Refused("missing APCA_API_KEY_ID / APCA_API_SECRET_KEY (paper data keys)")
    cal = TradingClient(k, s, paper=True).get_calendar(GetCalendarRequest(start=START, end=END))
    sessions = [(c.date, c.open, c.close) for c in cal if START <= c.date <= END]
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp_cal, tmp_bars = cal_f.with_suffix(".part"), bars_f.with_suffix(".part")
    with tmp_cal.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["day", "open", "close"])
        for d, o, c in sessions:
            w.writerow([d.isoformat(), o.strftime("%H:%M"), c.strftime("%H:%M")])
    dc = StockHistoricalDataClient(k, s)
    n = 0
    with gzip.open(tmp_bars, "wt", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t", "o", "h", "l", "c", "v"])
        m = date(START.year, START.month, 1)
        while m <= END:
            nxt = date(m.year + (m.month == 12), m.month % 12 + 1, 1)
            a = datetime(m.year, m.month, m.day, tzinfo=NY)
            b = datetime.combine(min(nxt, END + timedelta(days=1)), datetime.min.time(), tzinfo=NY)
            req = StockBarsRequest(symbol_or_symbols=SYMBOL, timeframe=TimeFrame.Minute, start=a, end=b,
                                   feed=DataFeed.SIP, adjustment=Adjustment.RAW)
            rows = dc.get_stock_bars(req).data.get(SYMBOL, [])
            for r in rows:
                t = r.timestamp.astimezone(NY)
                if t.date() <= END:
                    w.writerow([t.isoformat(), r.open, r.high, r.low, r.close, r.volume])
                    n += 1
            print(f"{m:%Y-%m}: {len(rows)} bars", flush=True)
            m = nxt
    tmp_cal.rename(cal_f)
    tmp_bars.rename(bars_f)
    h = sha256(bars_f)
    print(f"\n{n} bars, {len(sessions)} sessions\nSHA-256 {bars_f.name}: {h}")
    return h


def sha256(p: Path) -> str:
    d = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            d.update(chunk)
    return d.hexdigest()


def load(data_dir: Path = DATA):
    """[(Session, [Bar, ...])] in day order, regular-hours bars only, nothing outside START..END."""
    cal = {}
    with (data_dir / CAL_FILE.name).open() as f:
        for r in csv.DictReader(f):
            d = date.fromisoformat(r["day"])
            if START <= d <= END:
                oh, om = map(int, r["open"].split(":"))
                ch, cm = map(int, r["close"].split(":"))
                cal[d] = Session(d.isoformat(), datetime(d.year, d.month, d.day, oh, om, tzinfo=NY),
                                 datetime(d.year, d.month, d.day, ch, cm, tzinfo=NY))
    by_day: dict[date, list[Bar]] = {d: [] for d in cal}
    with gzip.open(data_dir / BARS_FILE.name, "rt") as f:
        for r in csv.DictReader(f):
            t = datetime.fromisoformat(r["t"]).astimezone(NY)
            s = cal.get(t.date())
            if s is not None and s.open <= t < s.close:
                by_day[t.date()].append(Bar(t, float(r["o"]), float(r["h"]), float(r["l"]), float(r["c"]),
                                            float(r["v"])))
    return [(cal[d], sorted(by_day[d], key=lambda b: b.t)) for d in sorted(cal)]


# ------------------------------------------------------------------ scoring
@contextlib.contextmanager
def patched(module, attr, value):
    old = getattr(module, attr)
    setattr(module, attr, value)
    try:
        yield
    finally:
        setattr(module, attr, old)


def score_variant(sessions, module, attr, value):
    """One row per session: day, ok (bars verified), trade (sim dict or None)."""
    out = []
    with patched(module, attr, value):
        for s, bars in sessions:
            try:
                verify(bars, s)
            except BarError as e:
                out.append({"day": s.day, "ok": False, "trade": None, "reason": str(e)})
                continue
            r = module.evaluate(bars, s)
            out.append({"day": s.day, "ok": True, "trade": r.get("trade") if r.get("state") == "traded" else None})
    return out


def net_bp(trade, cost_bp_per_side=sim.COST_BP_PER_SIDE):
    return trade["gross_bp"] - 2 * cost_bp_per_side


def metrics(rows, cost_bp_per_side=sim.COST_BP_PER_SIDE) -> dict:
    nets = [net_bp(r["trade"], cost_bp_per_side) for r in rows if r["trade"]]
    daily = [(r["day"], (net_bp(r["trade"], cost_bp_per_side) / 1e4) if r["trade"] else 0.0) for r in rows]
    n = len(nets)
    mean = statistics.fmean(nets) if n else None      # no trades: no mean, never "lost money"
    sd = statistics.stdev(nets) if n > 1 else 0.0
    gains, losses = sum(x for x in nets if x > 0), -sum(x for x in nets if x < 0)
    rets = [x for _, x in daily]
    eq, peak, mdd = 1.0, 1.0, 0.0
    for x in rets:
        eq *= 1 + x
        peak = max(peak, eq)
        mdd = max(mdd, 1 - eq / peak)
    dsd = statistics.stdev(rets) if len(rets) > 1 else 0.0
    by_year: dict[int, float] = {}
    for d, x in daily:
        y = int(d[:4])
        by_year[y] = (1 + by_year.get(y, 0.0)) * (1 + x) - 1
    return {"sessions": len(rows), "bad_sessions": sum(not r["ok"] for r in rows), "trades": n,
            "wins": sum(x > 0 for x in nets), "win_rate": (sum(x > 0 for x in nets) / n) if n else None,
            "mean_net_bp": mean, "t_stat": (mean / (sd / math.sqrt(n))) if n > 1 and sd > 0 else None,
            "profit_factor": (gains / losses) if losses > 0 else (math.inf if gains > 0 else None),
            "total_net_pct": (eq - 1) * 100, "max_drawdown_pct": mdd * 100,
            "sharpe": (statistics.fmean(rets) / dsd * math.sqrt(252)) if dsd > 0 else 0.0,
            "year_net_pct": {y: v * 100 for y, v in sorted(by_year.items())}, "daily": daily}


def trial_n(db: Path = TRIAL_DB) -> dict:
    """N for the deflated Sharpe: logged + backfilled + the six variants if not yet logged, floor N_FLOOR."""
    if not db.exists():
        raise Refused(f"trial database {db} not found: N cannot be known without it")
    con = sqlite3.connect(db)
    try:
        logged = con.execute("SELECT COUNT(*) FROM research_trials").fetchone()[0]
        mine = con.execute("SELECT COUNT(*) FROM research_trials WHERE batch=?", (BATCH,)).fetchone()[0]
        try:
            back = int(con.execute("SELECT COALESCE(SUM(count), 0) FROM untracked_trials").fetchone()[0])
        except sqlite3.OperationalError:
            back = 0
    except sqlite3.OperationalError as e:
        raise Refused(f"trial database unreadable: {e}")
    finally:
        con.close()
    pending = len(VARIANTS) - mine
    total = logged + back + pending
    return {"logged": logged, "backfilled": back, "pending": pending, "n": max(N_FLOOR, total),
            "already_logged": mine}


def evaluate_all(sessions, n_info) -> dict:
    import research_gate as rg
    if not sessions:
        raise Refused("no sessions loaded")
    res = {"variants": {}, "strategies": {}, "n_info": n_info}
    raw = {}
    for name, module, attr, value, base in VARIANTS:
        rows = score_variant(sessions, module, attr, value)
        raw[name] = rows
        m = metrics(rows)
        bad = m["bad_sessions"] / max(1, m["sessions"])
        if bad > MAX_BAD_SHARE:
            raise Refused(f"{name}: {m['bad_sessions']} of {m['sessions']} sessions failed verification "
                          f"({bad:.1%} > {MAX_BAD_SHARE:.0%}): data problem, not a result")
        res["variants"][name] = {**{k: v for k, v in m.items() if k != "daily"}, "baseline": base,
                                 "param": {attr: str(value) if isinstance(value, timedelta) else value}}
        res["variants"][name]["_daily"] = m["daily"]
    for fam, base_name, neigh in (("A", "orb15", ("orb10", "orb30")),
                                  ("B", "engulf15_v1.5", ("engulf15_v1.3", "engulf15_v2.0"))):
        b = res["variants"][base_name]
        stress = metrics(raw[base_name], STRESS_COST_BP)
        dsr = rg.deflated_sharpe([x for _, x in b["_daily"]], n_info["n"], HIST_VAR_PER_PERIOD)
        years_up = sum(1 for y in FULL_YEARS if b["year_net_pct"].get(y, 0.0) > 0)
        try:
            names = (base_name,) + neigh
            pbo = rg.pbo_cscv([[res["variants"][v]["_daily"][i][1] for v in names]
                               for i in range(len(b["_daily"]))])
        except Exception as e:
            pbo = {"error": f"{type(e).__name__}: {e}"}
        crit = {
            "C1 trades >= 100": b["trades"] >= MIN_TRADES,
            "C2 mean net > 0 and t >= 2.0": (b["mean_net_bp"] or 0) > 0 and (b["t_stat"] or 0) >= MIN_T,
            "C3 both neighbours mean net > 0": all((res["variants"][v]["mean_net_bp"] or 0) > 0 for v in neigh),
            "C4 net positive in >= 6 of 10 full years": years_up >= MIN_YEARS_UP,
            "C5 deflated Sharpe >= 0.95": not math.isnan(dsr["dsr"]) and dsr["dsr"] >= DSR_MIN,
            "C6 mean net > 0 at 4 bp per side": (stress["mean_net_bp"] or 0) > 0,
        }
        verdict = ("historically supported" if all(crit.values()) else
                   "historically unsupported" if b["mean_net_bp"] is not None and b["mean_net_bp"] <= 0 else "mixed")
        res["strategies"][fam] = {"baseline": base_name, "criteria": crit, "verdict": verdict,
                                  "years_up": years_up, "dsr": dsr, "pbo_reported_only": pbo,
                                  "stress_4bp": {k: stress[k] for k in ("trades", "mean_net_bp", "total_net_pct")}}
    return res


# ------------------------------------------------------------------ guards, logging, output
def _git(*a):
    return subprocess.run(["git", *a], cwd=REPO, capture_output=True, text=True)


def guards():
    if _git("rev-parse", "-q", "--verify", f"refs/tags/{TAG}").returncode:
        raise Refused(f"registration tag {TAG} not found (create it as docs/PREREG_tactical_hist_v1.md section 8 says)")
    if _git("merge-base", "--is-ancestor", TAG, "HEAD").returncode:
        raise Refused(f"{TAG} is not an ancestor of HEAD")
    if _git("status", "--porcelain", "--untracked-files=no").stdout.strip():
        raise Refused("tracked files have uncommitted changes: commit first so the run is reproducible")


def log_trials(res, data_hash, db: Path = TRIAL_DB):
    import trials
    if res["n_info"]["already_logged"]:
        raise Refused(f"batch {BATCH} already has {res['n_info']['already_logged']} trials: logged once only")
    import research_gate as rg
    done = []
    for name, *_ in VARIANTS:
        v = res["variants"][name]
        rets = [x for _, x in v["_daily"]]
        skew, kurt = rg.moments(rets)
        oos = {"sharpe": v["sharpe"], "days": v["sessions"], "daily_skew": skew, "daily_kurtosis": kurt,
               "trades": v["trades"], "mean_net_bp": v["mean_net_bp"], "t_stat": v["t_stat"],
               "profit_factor": v["profit_factor"], "max_drawdown_pct": v["max_drawdown_pct"]}
        tid, total = trials.log_trial(f"tactical_{name}", SYMBOL, "backtest_intraday",
                                      {**v["param"], "family": FAMILY[name], "baseline": v["baseline"], "prereg": TAG},
                                      START, END, "none", {"sharpe": v["sharpe"]}, oos, oos_trades=v["trades"],
                                      oos_avg_r=v["mean_net_bp"], db=str(db), daily_returns=v["_daily"],
                                      batch=BATCH, data_hash=data_hash)
        done.append((name, tid, total))
    return done


def _f(x, nd=1, sign=True):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    if isinstance(x, float) and math.isinf(x):
        return "inf"
    return (f"{x:+.{nd}f}" if sign else f"{x:.{nd}f}") if isinstance(x, float) else str(x)


def render(res) -> str:
    ni = res["n_info"]
    L = [f"SPY {START} .. {END}, SIP 1-minute, frozen engine code; N = {ni['n']} "
         f"({ni['logged']} logged + {ni['backfilled']} backfilled + {ni['pending']} pending, floor {N_FLOOR})", "",
         f"{'variant':<15}{'trades':>7}{'win%':>6}{'mean bp':>9}{'t':>7}{'PF':>6}{'total %':>9}{'maxDD %':>9}{'Sharpe':>8}"]
    for name, v in res["variants"].items():
        L.append(f"{name + (' *' if v['baseline'] else ''):<15}{v['trades']:>7}"
                 f"{(v['win_rate'] or 0) * 100:>6.0f}{_f(v['mean_net_bp'], 2):>9}{_f(v['t_stat'], 2):>7}"
                 f"{_f(v['profit_factor'], 2, False):>6}{v['total_net_pct']:>+9.1f}{v['max_drawdown_pct']:>9.1f}"
                 f"{v['sharpe']:>+8.2f}")
    L.append("(* = baseline; only baselines can earn a verdict)")
    for fam, s in res["strategies"].items():
        b = res["variants"][s["baseline"]]
        L += ["", f"Strategy {fam} ({s['baseline']}): {s['verdict'].upper()}"]
        L += [f"  {'PASS' if ok else 'FAIL'}  {c}" for c, ok in s["criteria"].items()]
        L.append(f"  years up {s['years_up']}/10: " + ", ".join(f"{y} {b['year_net_pct'].get(y, 0):+.1f}%" for y in FULL_YEARS))
        L.append(f"  deflated Sharpe {_f(s['dsr']['dsr'], 3)} (Sharpe {s['dsr']['sr_annual']:+.2f} vs luck "
                 f"threshold {s['dsr']['sr0_annual']:.2f}); at 4 bp/side mean {_f(s['stress_4bp']['mean_net_bp'], 2)} bp")
        p = s["pbo_reported_only"]
        L.append("  PBO (3 variants, reported only): " + (f"{p['pbo']:.2f}" if isinstance(p, dict) and "pbo" in p else str(p)))
    L += ["", "Forward 40-session paper test is unchanged. Live needs forward 'worth a live micro test' AND "
              "'historically supported' (prereg section 7)."]
    return "\n".join(L)


def save(res, data_hash) -> Path:
    RESULTS.mkdir(parents=True, exist_ok=True)
    p = RESULTS / f"tactical_hist_{datetime.now():%Y%m%d_%H%M%S}.json"
    clean = {**res, "variants": {k: {kk: vv for kk, vv in v.items() if kk != "_daily"} for k, v in res["variants"].items()},
             "data_sha256": data_hash, "tag": TAG, "commit": _git("rev-parse", "HEAD").stdout.strip()}
    p.write_text(json.dumps(clean, indent=2, default=str))
    return p


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fetch")
    r = sub.add_parser("run")
    r.add_argument("--log-trials", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "fetch":
            fetch()
            return 0
        guards()
        if not BARS_FILE.exists():
            raise Refused(f"{BARS_FILE} missing: run `fetch` first")
        h = sha256(BARS_FILE)
        n_info = trial_n()
        res = evaluate_all(load(), n_info)
        print(render(res))
        print(f"\nsaved {save(res, h)}\ndata SHA-256 {h}")
        if a.log_trials:
            for name, tid, total in log_trials(res, h):
                print(f"logged {name}: trial id {tid}, research_trials now {total}")
        return 0
    except Refused as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
