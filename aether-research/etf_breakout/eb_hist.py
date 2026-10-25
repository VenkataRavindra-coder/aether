#!/usr/bin/env python3
"""AETHER research: multi-ETF channel breakout (etf-breakout-v1), historical run.

Registered in docs/PREREG_etf_breakout_v1.md (tag `prereg-etf-breakout-v1`) before any result exists.
Research only: nothing here places orders and the engine never imports it. Rules live in eb_rules.py.

  python3 aether-research/etf_breakout/eb_hist.py run                # 3 variants, criteria, verdict
  python3 aether-research/etf_breakout/eb_hist.py run --log-trials   # also log the 3 variants, once

Data: the Yahoo adjusted daily file S2 used (aether-research/us_momentum/data/us_daily_yf.csv.gz). Its SHA-256
must equal the registered value, or the run is refused. Nothing after 2026-10-06 exists in it.

Fail closed: refused unless the registration tag is an ancestor of HEAD and tracked files are committed, if the
data hash differs, if any symbol misses a trading day, or if the trial database is missing.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import math
import sqlite3
import statistics
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESEARCH = HERE.parent
REPO = RESEARCH.parent
sys.path.insert(0, str(RESEARCH))
sys.path.insert(0, str(HERE))

import eb_rules as rules  # noqa: E402

TAG = "prereg-etf-breakout-v1"
BATCH = "etf-breakout-v1"
DATA_FILE = RESEARCH / "us_momentum" / "data" / "us_daily_yf.csv.gz"
DATA_SHA = "e9524a2a60e1c609af72c991d1be8cc8d42f36d95bcafcf1b1b81dd1971ed9b6"
WARMUP_FROM, START, END = date(2005, 1, 3), date(2007, 3, 1), date(2026, 10, 6)
HALF_SPLIT = date(2016, 1, 1)
FIRST_FULL_YEAR, LAST_FULL_YEAR = 2008, 2025
RESULTS = HERE / "results"
TRIAL_DB = RESEARCH / "research.sqlite"

MIN_TRADES = 100
MIN_PF = 1.30
MAX_DD_PCT = 20.0
YEAR_SHARE = 0.60
MAX_SYMBOL_SHARE = 0.50
DSR_MIN = 0.95
BOOT_BLOCK, BOOT_DRAWS, BOOT_SEED = 21, 5000, 0
OUTSIDE_DB = 3
BACKFILLED_FLOOR = 28
N_FLOOR = 49          # 46 (registry on 2026-10-25) + these 3
HIST_VAR_PER_PERIOD = 0.0003242764550264549   # same surviving stage-1 record every registration uses
CAPITALS = (100.0, 1_000.0, 5_000.0)
TARGET_USD_PER_DAY = 2.0


class Refused(Exception):
    pass


# ------------------------------------------------------------------ data
def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load(path: Path = DATA_FILE):
    """(dates, {symbol: (opens, closes)}) on the shared calendar WARMUP_FROM..END. Refuses on any gap."""
    by: dict[str, dict[str, tuple[float, float]]] = {s: {} for s in rules.SYMBOLS}
    with gzip.open(path, "rt") as f:
        for r in csv.DictReader(f):
            s = r["symbol"]
            if s in by and WARMUP_FROM.isoformat() <= r["date"] <= END.isoformat():
                by[s][r["date"]] = (float(r["open"]), float(r["close"]))
    union = sorted(set().union(*(set(v) for v in by.values())))
    for s, v in by.items():
        miss = len(union) - len(v)
        if miss:
            raise Refused(f"{s} misses {miss} of {len(union)} trading days: refusing (no filling)")
        if not all(math.isfinite(x) and x > 0 for o_c in v.values() for x in o_c):
            raise Refused(f"{s} has a non-positive or non-finite price")
    data = {s: ([by[s][d][0] for d in union], [by[s][d][1] for d in union]) for s in rules.SYMBOLS}
    return union, data


def start_index(dates) -> int:
    i = next(i for i, d in enumerate(dates) if d >= START.isoformat())
    if i < max(max(v) for v in rules.VARIANTS.values()) + 1 or i < rules.SMA_N:
        raise Refused("not enough warm-up history before the start date")
    return i


# ------------------------------------------------------------------ statistics
def sharpe(r):
    if len(r) < 2:
        return 0.0
    sd = statistics.stdev(r)
    return statistics.fmean(r) / sd * math.sqrt(252) if sd > 0 else 0.0


def cagr(r):
    eq = math.prod(1 + x for x in r)
    return eq ** (252 / len(r)) - 1 if r and eq > 0 else -1.0


def max_dd(r):
    eq = peak = 1.0
    m = 0.0
    for x in r:
        eq *= 1 + x
        peak = max(peak, eq)
        m = max(m, 1 - eq / peak)
    return m


def years(dates, r):
    out: dict[int, float] = {}
    for d, x in zip(dates, r):
        y = int(d[:4])
        out[y] = (1 + out.get(y, 0.0)) * (1 + x) - 1
    return out


def summary(dates, r) -> dict:
    return {"sharpe": sharpe(r), "cagr_pct": cagr(r) * 100, "max_drawdown_pct": max_dd(r) * 100,
            "total_pct": (math.prod(1 + x for x in r) - 1) * 100, "days": len(r)}


def correlation(a, b):
    ma, mb = statistics.fmean(a), statistics.fmean(b)
    sa = math.sqrt(sum((x - ma) ** 2 for x in a))
    sb = math.sqrt(sum((y - mb) ** 2 for y in b))
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (sa * sb) if sa > 0 and sb > 0 else 0.0


def sixty_forty(dates, data, start):
    """Report only: 60% SPY / 40% TLT at closes, rebalanced at the close of each month's first trading day, no cost."""
    spy, tlt = data["SPY"][1], data["TLT"][1]
    w = (0.6, 0.4)
    h = None
    out = []
    for i in range(start, len(dates)):
        if h is None:
            h = (w[0] / spy[i - 1], w[1] / tlt[i - 1])
        prev = h[0] * spy[i - 1] + h[1] * tlt[i - 1]
        now = h[0] * spy[i] + h[1] * tlt[i]
        out.append(now / prev - 1)
        if dates[i][:7] != dates[i - 1][:7]:
            h = (w[0] * now / spy[i], w[1] * now / tlt[i])
    return out


def dollars(daily, years_map) -> dict:
    """What the base variant's history would have meant at each capital level (no compounding of the mean)."""
    m = statistics.fmean(daily)
    worst_year = min((v for y, v in years_map.items() if FIRST_FULL_YEAR <= y <= LAST_FULL_YEAR), default=None)
    dd = max_dd(daily)
    out = {}
    for c in CAPITALS:
        out[f"{c:g}"] = {"usd_per_trading_day": c * m, "usd_per_year": c * m * 252,
                         "worst_full_year_usd": None if worst_year is None else c * worst_year,
                         "max_drawdown_usd": c * dd}
    out["capital_for_target"] = (TARGET_USD_PER_DAY / m) if m > 0 else None
    return out


# ------------------------------------------------------------------ evaluation
def evaluate(dates, data, n_info) -> dict:
    import numpy as np
    import research_gate as rg
    st = start_index(dates)
    ev = dates[st:]
    res = {"window": [ev[0], ev[-1]], "days": len(ev), "n_info": n_info, "variants": {}, "comparators": {}}
    var = {}
    for name, (en, ex) in rules.VARIANTS.items():
        sleeves, r, _ = rules.run_variant(data, en, ex, st)
        stress = rules.run_variant(data, en, ex, st, rules.STRESS_COST_BP_PER_SIDE)[1]
        trades = [dict(t, symbol=s) for s in rules.SYMBOLS for t in sleeves[s]["trades"]]
        rets = [t["ret"] for t in trades]
        gains, losses = sum(x for x in rets if x > 0), -sum(x for x in rets if x < 0)
        profit = {s: sleeves[s]["path"][-1] - 1.0 for s in rules.SYMBOLS}   # per unit of sleeve capital
        pos = sum(max(v, 0.0) for v in profit.values())
        var[name] = {"r": r, "stress": stress, "trades": trades, "sleeves": sleeves,
                     "pf": (gains / losses) if losses > 0 else (math.inf if gains > 0 else None),
                     "symbol_share": {s: (max(v, 0.0) / pos if pos > 0 else None) for s, v in profit.items()},
                     "symbol_profit": profit}
    sma_r, _ = rules.run_comparator(data, "sma200", st)
    hold_r, _ = rules.run_comparator(data, "hold", st)
    spy_r = [data["SPY"][1][i] / data["SPY"][1][i - 1] - 1 for i in range(st, len(dates))]
    res["comparators"] = {"sma200_same_etfs": summary(ev, sma_r), "hold_same_etfs": summary(ev, hold_r),
                          "spy_hold_report_only": summary(ev, spy_r),
                          "sixty_forty_report_only": summary(ev, sixty_forty(dates, data, st))}
    ret = {s: [data[s][1][i] / data[s][1][i - 1] - 1 for i in range(st, len(dates))] for s in rules.SYMBOLS}
    pairs = [(a, b) for i, a in enumerate(rules.SYMBOLS) for b in rules.SYMBOLS[i + 1:]]
    avg_etf = statistics.fmean(correlation(ret[a], ret[b]) for a, b in pairs)
    res["correlation"] = {"avg_pairwise_etf": avg_etf,
                          "effective_bets": len(rules.SYMBOLS) / (1 + (len(rules.SYMBOLS) - 1) * avg_etf),
                          "pairs": {f"{a}/{b}": correlation(ret[a], ret[b]) for a, b in pairs}}
    for name, v in var.items():
        r = v["r"]
        s = summary(ev, r)
        boot = rg.block_bootstrap_sharpes(r, block=BOOT_BLOCK, draws=BOOT_DRAWS, seed=BOOT_SEED)
        lo, hi = float(np.percentile(boot, 2.5)), float(np.percentile(boot, 97.5))
        yrs = years(ev, r)
        full = list(range(FIRST_FULL_YEAR, LAST_FULL_YEAR + 1))
        up = sum(1 for y in full if yrs.get(y, 0.0) > 0)
        need = math.ceil(YEAR_SHARE * len(full))
        h1 = [x for d, x in zip(ev, r) if d < HALF_SPLIT.isoformat()]
        h2 = [x for d, x in zip(ev, r) if d >= HALF_SPLIT.isoformat()]
        dsr = rg.deflated_sharpe(r, n_info["n"], HIST_VAR_PER_PERIOD)
        rets = [t["ret"] for t in v["trades"]]
        n_tr = len(rets)
        wins = [x for x in rets if x > 0]
        loss = [x for x in rets if x <= 0]
        v["summary"] = {**s, "trades": n_tr, "win_rate": len(wins) / n_tr if n_tr else None,
                        "avg_win_pct": statistics.fmean(wins) * 100 if wins else None,
                        "avg_loss_pct": statistics.fmean(loss) * 100 if loss else None,
                        "profit_factor": v["pf"], "sharpe_boot95": [lo, hi],
                        "half1_sharpe": sharpe(h1), "half2_sharpe": sharpe(h2),
                        "years_pct": {y: x * 100 for y, x in sorted(yrs.items())}, "years_up": up, "years_full": len(full),
                        "stress_cagr_pct": cagr(v["stress"]) * 100, "dsr": dsr,
                        "symbol_share": v["symbol_share"], "symbol_profit_per_sleeve": v["symbol_profit"],
                        "open_at_end": sum(1 for x in v["sleeves"].values() if x["open"]),
                        "per_symbol_trades": {sy: len(v["sleeves"][sy]["trades"]) for sy in rules.SYMBOLS}}
        v["need_years"] = need
    base = var[rules.BASE]["summary"]
    neighbours = [k for k in rules.VARIANTS if k != rules.BASE]
    shares = [x for x in base["symbol_share"].values() if x is not None]
    crit = {
        f"C1 closed trades >= {MIN_TRADES}": base["trades"] >= MIN_TRADES,
        "C2 CAGR > 0 and block-bootstrap 95% lower bound of Sharpe > 0": base["cagr_pct"] > 0 and base["sharpe_boot95"][0] > 0,
        f"C3 trade profit factor >= {MIN_PF}": base["profit_factor"] is not None and base["profit_factor"] >= MIN_PF,
        f"C4 max drawdown <= {MAX_DD_PCT:.0f}%": base["max_drawdown_pct"] <= MAX_DD_PCT,
        f"C5 Sharpe > 0 in both halves (split {HALF_SPLIT})": base["half1_sharpe"] > 0 and base["half2_sharpe"] > 0,
        f"C6 positive in >= {var[rules.BASE]['need_years']} of {base['years_full']} full years": base["years_up"] >= var[rules.BASE]["need_years"],
        "C7 CAGR > 0 at 10 bp/side": base["stress_cagr_pct"] > 0,
        "C8 Sharpe > 200-day filter on the same five ETFs": base["sharpe"] > res["comparators"]["sma200_same_etfs"]["sharpe"],
        "C9 Sharpe >= buy-and-hold of the same five ETFs": base["sharpe"] >= res["comparators"]["hold_same_etfs"]["sharpe"],
        f"C10 deflated Sharpe >= {DSR_MIN} (necessary, not sufficient)": not math.isnan(base["dsr"]["dsr"]) and base["dsr"]["dsr"] >= DSR_MIN,
        f"C11 no ETF > {MAX_SYMBOL_SHARE:.0%} of the profit": bool(shares) and max(shares) <= MAX_SYMBOL_SHARE,
        "C12 both neighbours CAGR > 0": all(var[k]["summary"]["cagr_pct"] > 0 for k in neighbours),
    }
    if base["cagr_pct"] <= 0:
        verdict = "historically unsupported"
    elif all(crit.values()):
        verdict = "historically supported"
    elif base["trades"] < MIN_TRADES:
        verdict = "insufficient evidence"
    else:
        verdict = "mixed"
    for name, v in var.items():
        res["variants"][name] = {"params": rules.VARIANTS[name], "base": name == rules.BASE, **v["summary"],
                                 "_daily": list(zip(ev, v["r"])), "_trades": v["trades"]}
    res["criteria"] = crit
    res["verdict"] = verdict
    res["dollars"] = dollars(var[rules.BASE]["r"], years(ev, var[rules.BASE]["r"]))
    try:
        res["pbo_reported_only"] = rg.pbo_cscv([[var[k]["r"][i] for k in rules.VARIANTS] for i in range(len(ev))])
    except Exception as e:
        res["pbo_reported_only"] = {"error": f"{type(e).__name__}: {e}"}
    return res


# ------------------------------------------------------------------ guards, trials, output
def _git(*a):
    return subprocess.run(["git", *a], cwd=REPO, capture_output=True, text=True)


def guards():
    if _git("rev-parse", "-q", "--verify", f"refs/tags/{TAG}").returncode:
        raise Refused(f"registration tag {TAG} not found (docs/PREREG_etf_breakout_v1.md section 10)")
    if _git("merge-base", "--is-ancestor", TAG, "HEAD").returncode:
        raise Refused(f"{TAG} is not an ancestor of HEAD")
    if _git("status", "--porcelain", "--untracked-files=no").stdout.strip():
        raise Refused("tracked files have uncommitted changes: commit first so the run is reproducible")


def check_data_hash(h: str):
    if h != DATA_SHA:
        raise Refused(f"data SHA-256 {h[:12]}... is not the registered file ({DATA_SHA[:12]}...): refusing")


def trial_n(db: Path = TRIAL_DB) -> dict:
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
    pending = len(rules.VARIANTS) - mine
    total = logged + back + OUTSIDE_DB + pending
    return {"logged": logged, "backfilled": back, "outside_db": OUTSIDE_DB, "pending": pending,
            "n": max(N_FLOOR, total), "already_logged": mine}


def log_trials(res, data_hash, db: Path = TRIAL_DB):
    import research_gate as rg
    import trials
    if res["n_info"]["already_logged"]:
        raise Refused(f"batch {BATCH} already has {res['n_info']['already_logged']} trials: logged once only")
    done = []
    for name, v in res["variants"].items():
        rets = [x for _, x in v["_daily"]]
        skew, kurt = rg.moments(rets)
        oos = {"sharpe": v["sharpe"], "days": v["days"], "daily_skew": skew, "daily_kurtosis": kurt,
               "trades": v["trades"], "cagr_pct": v["cagr_pct"], "profit_factor": v["profit_factor"],
               "max_drawdown_pct": v["max_drawdown_pct"], "verdict": res["verdict"] if v["base"] else "neighbour"}
        tid, total = trials.log_trial(name, ",".join(rules.SYMBOLS), "backtest_daily",
                                      {"entry_n": v["params"][0], "exit_n": v["params"][1], "prereg": TAG,
                                       "cost_bp_per_side": rules.COST_BP_PER_SIDE, "base": v["base"]},
                                      START, END, "none", {"sharpe": v["sharpe"]}, oos, oos_trades=v["trades"],
                                      oos_avg_r=None, db=str(db), daily_returns=v["_daily"], batch=BATCH,
                                      data_hash=data_hash)
        done.append((name, tid, total))
    return done


def _f(x, nd=2, sign=True):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    if isinstance(x, float) and math.isinf(x):
        return "inf"
    return f"{x:+.{nd}f}" if sign else f"{x:.{nd}f}"


def render(res) -> str:
    ni, cm, co = res["n_info"], res["comparators"], res["correlation"]
    L = [f"SPY QQQ IWM GLD TLT daily (Yahoo adjusted) {res['window'][0]} .. {res['window'][1]}, {res['days']} days; "
         f"N = {ni['n']} ({ni['logged']} logged + {ni['backfilled']} backfilled + {ni['outside_db']} outside + "
         f"{ni['pending']} pending, floor {N_FLOOR})",
         f"ETF correlation: average pairwise {co['avg_pairwise_etf']:+.2f}, effective independent bets {co['effective_bets']:.1f} of 5", "",
         f"{'':<22}{'Sharpe':>8}{'CAGR %':>8}{'maxDD %':>9}{'total %':>9}"]
    for k, v in cm.items():
        L.append(f"{k:<22}{v['sharpe']:>+8.2f}{v['cagr_pct']:>+8.1f}{v['max_drawdown_pct']:>9.1f}{v['total_pct']:>+9.0f}")
    L += ["", f"{'variant':<12}{'trades':>7}{'win%':>6}{'PF':>6}{'Sharpe':>8}{'95% low':>9}{'CAGR %':>8}{'maxDD %':>9}"
              f"{'h1 SR':>7}{'h2 SR':>7}{'@10bp':>8}"]
    for k, v in res["variants"].items():
        L.append(f"{k:<12}{v['trades']:>7}{(v['win_rate'] or 0) * 100:>6.0f}{_f(v['profit_factor'], 2, False):>6}"
                 f"{v['sharpe']:>+8.2f}{v['sharpe_boot95'][0]:>+9.2f}{v['cagr_pct']:>+8.1f}{v['max_drawdown_pct']:>9.1f}"
                 f"{v['half1_sharpe']:>+7.2f}{v['half2_sharpe']:>+7.2f}{v['stress_cagr_pct']:>+8.1f}")
    b = res["variants"][rules.BASE]
    L += ["", f"{rules.BASE} (judged): {res['verdict'].upper()}"]
    L += [f"  {'PASS' if ok else 'FAIL'}  {c}" for c, ok in res["criteria"].items()]
    L.append(f"  avg win {_f(b['avg_win_pct'])}%, avg loss {_f(b['avg_loss_pct'])}%; deflated Sharpe "
             f"{_f(b['dsr']['dsr'], 3, False)} (luck threshold {b['dsr']['sr0_annual']:.2f}); open at end {b['open_at_end']}")
    L.append("  trades per ETF: " + ", ".join(f"{s} {n}" for s, n in b["per_symbol_trades"].items()))
    L.append("  profit share per ETF: " + ", ".join(f"{s} {'—' if x is None else f'{x:.0%}'}" for s, x in b["symbol_share"].items()))
    L.append("  years: " + ", ".join(f"{y} {x:+.1f}%" for y, x in b["years_pct"].items()
                                     if FIRST_FULL_YEAR <= y <= LAST_FULL_YEAR))
    d = res["dollars"]
    L += ["", "Dollars (base variant's history; averages, not a forecast):"]
    for c in CAPITALS:
        x = d[f"{c:g}"]
        L.append(f"  ${c:>7,.0f}: {_f(x['usd_per_trading_day'])} per trading day, {_f(x['usd_per_year'])} per year, "
                 f"worst full year {_f(x['worst_full_year_usd'])}, max drawdown {_f(-x['max_drawdown_usd'])}")
    L.append("  capital needed for $2 per trading day at this average: "
             + ("not reachable (average <= 0)" if d["capital_for_target"] is None else f"${d['capital_for_target']:,.0f}"))
    p = res["pbo_reported_only"]
    L += ["", "PBO across the 3 variants (reported only): " + (f"{p['pbo']:.2f}" if "pbo" in p else str(p)),
          "", "No verdict here permits paper or live trading or overrides S2 NO-GO, the test plan, the Risk Controller "
              "or the Oct 22 decision (prereg section 9)."]
    return "\n".join(L)


def save(res, data_hash) -> Path:
    RESULTS.mkdir(parents=True, exist_ok=True)
    p = RESULTS / f"etf_breakout_{datetime.now():%Y%m%d_%H%M%S}.json"
    clean = {**res, "variants": {k: {kk: vv for kk, vv in v.items() if not kk.startswith("_")}
                                 for k, v in res["variants"].items()},
             "data_sha256": data_hash, "tag": TAG, "commit": _git("rev-parse", "HEAD").stdout.strip()}
    p.write_text(json.dumps(clean, indent=2, default=str))
    return p


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--log-trials", action="store_true")
    a = ap.parse_args(argv)
    try:
        guards()
        h = sha256(DATA_FILE)
        check_data_hash(h)
        n_info = trial_n()
        dates, data = load()
        res = evaluate(dates, data, n_info)
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
