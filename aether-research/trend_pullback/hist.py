#!/usr/bin/env python3
"""AETHER research: trend-pullback candlestick registration (trend-pullback-v1), historical run.

Registered in docs/PREREG_trend_pullback_v1.md (tag `prereg-trend-pullback-v1`) before any result exists.
Research only: nothing here places orders and the engine never imports it. Rules live in rules.py.

  python3 aether-research/trend_pullback/hist.py run                # 4 variants, criteria, verdicts
  python3 aether-research/trend_pullback/hist.py run --log-trials   # also log the 4 variants, once

Data: the SPY SIP 1-minute file already fetched for tactical-hist-v1 (no new fetch). Its SHA-256 must match
the one recorded in strategy.md, or the run is refused.

Fail closed: refused unless the registration tag is an ancestor of HEAD and tracked files are committed,
if the data hash differs, if more than 2% of sessions fail verification, or if the trial database is missing.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
import subprocess
import sys
from bisect import bisect_right
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESEARCH = HERE.parent
REPO = RESEARCH.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(RESEARCH))
sys.path.insert(0, str(RESEARCH / "tactical_hist"))
sys.path.insert(0, str(HERE))

import rules  # noqa: E402
import tactical_hist as th  # noqa: E402
from aether_tactical.bars import BarError, verify  # noqa: E402

TAG = "prereg-trend-pullback-v1"
BATCH = "trend-pullback-v1"
SYMBOL = "SPY"
START, END = th.START, th.END                       # 2016-01-04 .. 2026-10-20, as tactical-hist-v1
DATA_SHA_PREFIX, DATA_SHA_SUFFIX = "1ca901b4", "5ee745"   # strategy.md, tactical-hist-v1 result
HALF_SPLIT = date(2021, 1, 1)
LAST_FULL_YEAR = 2025
RESULTS = HERE / "results"
TRIAL_DB = RESEARCH / "research.sqlite"

MAX_BAD_SHARE = 0.02
MIN_TRADES = 100
MIN_PF = 1.20
MAX_DD_PCT = 20.0
YEAR_SHARE = 0.60
RANDOM_P_MAX = 0.05
DSR_MIN = 0.95
BOOT_DRAWS, BOOT_SEED = 10_000, 0
RANDOM_SIMS, RANDOM_SEED = 1_000, 20261023
OUTSIDE_DB = 3        # daily_edges_backtest.py attempts (docs/TRIAL_INVENTORY_daily_v1.md)
N_FLOOR = 44          # 37 inventory + 3 daily-v1 + these 4
HIST_VAR_PER_PERIOD = th.HIST_VAR_PER_PERIOD


class Refused(Exception):
    pass


# ------------------------------------------------------------------ data
def verified_sessions(raw):
    """[(Session, bars or [] if it failed verification)], plus the number of failed sessions."""
    out, bad = [], 0
    for s, bars in raw:
        try:
            verify(bars, s)
            out.append((s, bars))
        except BarError:
            out.append((s, []))
            bad += 1
    if not out:
        raise Refused("no sessions loaded")
    if bad / len(out) > MAX_BAD_SHARE:
        raise Refused(f"{bad} of {len(out)} sessions failed verification (> {MAX_BAD_SHARE:.0%}): data problem")
    return out, bad


# ------------------------------------------------------------------ daily mark-to-market
class Calendar:
    def __init__(self, sessions):
        self.sessions = [s for s, _ in sessions]
        self.opens = [s.open for s in self.sessions]
        self.close_px = [bars[-1].c if bars else None for _, bars in sessions]
        self.open_px = [bars[0].o if bars else None for _, bars in sessions]

    def index(self, t):
        return bisect_right(self.opens, t) - 1


def daily_returns(cal: Calendar, trades, first: int, cost_bp_per_side: float):
    """Whole sleeve in each trade, marked at every session close; costs on the entry and exit days."""
    c = cost_bp_per_side / 1e4
    r = {i: 0.0 for i in range(first, len(cal.sessions))}
    for t in trades:
        a, b = cal.index(t["entry_t"]), cal.index(t["exit_t"])
        prev = t["entry_px"]
        r[a] = r.get(a, 0.0) - c
        for i in range(a, b + 1):
            if i == b:
                r[i] = r.get(i, 0.0) + t["exit_px"] / prev - 1 - c
            elif cal.close_px[i] is not None:
                r[i] = r.get(i, 0.0) + cal.close_px[i] / prev - 1
                prev = cal.close_px[i]
    return [(cal.sessions[i].day, r[i]) for i in sorted(r) if i >= first]


def buy_and_hold(cal: Calendar, first: int):
    out, prev = [], cal.open_px[first]
    for i in range(first, len(cal.sessions)):
        px = cal.close_px[i]
        if px is None or prev is None:
            out.append((cal.sessions[i].day, 0.0))
            prev = prev if px is None else px
            continue
        out.append((cal.sessions[i].day, px / prev - 1))
        prev = px
    return out


# ------------------------------------------------------------------ statistics
def sharpe(rets):
    if len(rets) < 2:
        return 0.0
    sd = statistics.stdev(rets)
    return statistics.fmean(rets) / sd * math.sqrt(252) if sd > 0 else 0.0


def max_drawdown_pct(rets):
    eq = peak = 1.0
    mdd = 0.0
    for x in rets:
        eq *= 1 + x
        peak = max(peak, eq)
        mdd = max(mdd, 1 - eq / peak)
    return mdd * 100


def bootstrap_mean_ci(xs, draws=BOOT_DRAWS, seed=BOOT_SEED):
    import numpy as np
    if len(xs) < 2:
        return (None, None)
    a = np.asarray(xs, dtype=float)
    idx = np.random.default_rng(seed).integers(0, a.size, size=(draws, a.size))
    means = a[idx].mean(axis=1)
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def random_entry_p(real_trades, pool, sims=RANDOM_SIMS, seed=RANDOM_SEED):
    """Share of matched random-entry runs whose mean net bp is >= the real mean. Matched = same number of
    trades per signal year, drawn without replacement from the trend-filtered null pool of that year."""
    import numpy as np
    if not real_trades:
        return {"p": None, "note": "no trades"}
    by_year: dict[int, list[float]] = {}
    for t in pool:
        by_year.setdefault(t["signal_start"].year, []).append(t["net_bp"])
    need: dict[int, int] = {}
    for t in real_trades:
        need[t["signal_start"].year] = need.get(t["signal_start"].year, 0) + 1
    short = {y: (k, len(by_year.get(y, []))) for y, k in need.items() if len(by_year.get(y, [])) < k}
    if short:
        return {"p": None, "note": f"null pool too small in {short}"}
    rng = np.random.default_rng(seed)
    pools = {y: np.asarray(by_year[y]) for y in need}
    real = statistics.fmean(t["net_bp"] for t in real_trades)
    sim_means = np.empty(sims)
    total = sum(need.values())
    for s in range(sims):
        acc = 0.0
        for y, k in need.items():
            acc += rng.choice(pools[y], size=k, replace=False).sum()
        sim_means[s] = acc / total
    return {"p": float((1 + np.sum(sim_means >= real)) / (1 + sims)), "real_mean": real,
            "null_mean": float(sim_means.mean()), "null_p95": float(np.percentile(sim_means, 95)),
            "pool_size": len(pool)}


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


# ------------------------------------------------------------------ evaluation
def evaluate_all(sessions, n_info, bad_sessions=0) -> dict:
    import research_gate as rg
    cal = Calendar(sessions)
    res = {"variants": {}, "n_info": n_info, "bad_sessions": bad_sessions, "sessions": len(sessions),
           "benchmarks": {}}
    markets = {tf: rules.Market(sessions, tf) for tf in rules.TIMEFRAMES}
    pools, first_idx = {}, {}
    for tf, m in markets.items():
        if len(m.candles) <= rules.WARMUP:
            raise Refused(f"{tf}: only {len(m.candles)} candles, warm-up needs {rules.WARMUP}")
        first_idx[tf] = cal.index(m.candles[rules.WARMUP].start)
        pools[tf] = rules.random_pool(m)
        bh = buy_and_hold(cal, first_idx[tf])
        res["benchmarks"][tf] = {"from": cal.sessions[first_idx[tf]].day, "buy_hold_sharpe": sharpe([x for _, x in bh]),
                                 "buy_hold_total_pct": (math.prod(1 + x for _, x in bh) - 1) * 100,
                                 "buy_hold_max_dd_pct": max_drawdown_pct([x for _, x in bh]),
                                 "null_pool_trades": len(pools[tf])}
    dailies = {}
    for name, (tf, pattern) in rules.VARIANTS.items():
        m, f = markets[tf], first_idx[tf]
        trades = rules.run(m, pattern)
        nets = [t["net_bp"] for t in trades]
        stress = [t["gross_bp"] - 2 * rules.STRESS_COST_BP_PER_SIDE for t in trades]
        daily = daily_returns(cal, trades, f, rules.COST_BP_PER_SIDE)
        dailies[name] = daily
        rets = [x for _, x in daily]
        n = len(nets)
        mean = statistics.fmean(nets) if n else None
        sd = statistics.stdev(nets) if n > 1 else 0.0
        lo, hi = bootstrap_mean_ci(nets)
        gains, losses = sum(x for x in nets if x > 0), -sum(x for x in nets if x < 0)
        pf = (gains / losses) if losses > 0 else (math.inf if gains > 0 else None)
        h1 = [t["net_bp"] for t in trades if t["signal_start"].date() < HALF_SPLIT]
        h2 = [t["net_bp"] for t in trades if t["signal_start"].date() >= HALF_SPLIT]
        by_year: dict[int, float] = {}
        for d, x in daily:
            y = int(d[:4])
            by_year[y] = (1 + by_year.get(y, 0.0)) * (1 + x) - 1
        first_day = date.fromisoformat(cal.sessions[f].day)
        full_years = [y for y in range(first_day.year + (first_day > date(first_day.year, 1, 7)), LAST_FULL_YEAR + 1)]
        years_up = sum(1 for y in full_years if by_year.get(y, 0.0) > 0)
        need_years = math.ceil(YEAR_SHARE * len(full_years))
        rnd = random_entry_p(trades, pools[tf])
        dsr = rg.deflated_sharpe(rets, n_info["n"], HIST_VAR_PER_PERIOD)
        sr, mdd = sharpe(rets), max_drawdown_pct(rets)
        bh_sr = res["benchmarks"][tf]["buy_hold_sharpe"]
        crit = {
            f"C1 trades >= {MIN_TRADES}": n >= MIN_TRADES,
            "C2 mean net > 0 at 5 bp/side and bootstrap 95% lower bound > 0": (mean or 0) > 0 and (lo or 0) > 0,
            f"C3 profit factor >= {MIN_PF}": pf is not None and pf >= MIN_PF,
            f"C4 max drawdown <= {MAX_DD_PCT:.0f}%": mdd <= MAX_DD_PCT,
            "C5 mean net > 0 in both halves (split 2021-01-01)": bool(h1) and bool(h2)
            and statistics.fmean(h1) > 0 and statistics.fmean(h2) > 0,
            f"C6 positive in >= {need_years} of {len(full_years)} full years": years_up >= need_years,
            "C7 mean net > 0 at 10 bp/side": bool(stress) and statistics.fmean(stress) > 0,
            f"C8 beats matched random entries (p <= {RANDOM_P_MAX})": rnd["p"] is not None and rnd["p"] <= RANDOM_P_MAX,
            "C9 Sharpe >= buy-and-hold Sharpe (same window)": sr >= bh_sr,
            f"C10 deflated Sharpe >= {DSR_MIN} (necessary, not sufficient)": not math.isnan(dsr["dsr"]) and dsr["dsr"] >= DSR_MIN,
        }
        if mean is not None and mean <= 0:
            verdict = "historically unsupported"
        elif all(crit.values()):
            verdict = "historically supported"
        elif n < MIN_TRADES:
            verdict = "insufficient evidence"
        else:
            verdict = "mixed"
        res["variants"][name] = {
            "timeframe": tf, "pattern": pattern, "trades": n, "win_rate": (sum(x > 0 for x in nets) / n) if n else None,
            "mean_net_bp": mean, "t_stat": (mean / (sd / math.sqrt(n))) if n > 1 and sd > 0 else None,
            "boot95": [lo, hi], "profit_factor": pf, "max_drawdown_pct": mdd, "sharpe": sr,
            "total_net_pct": (math.prod(1 + x for x in rets) - 1) * 100,
            "exposure_days": sum(1 for x in rets if x != 0.0), "eval_days": len(rets),
            "half1_mean_bp": statistics.fmean(h1) if h1 else None, "half2_mean_bp": statistics.fmean(h2) if h2 else None,
            "year_net_pct": {y: v * 100 for y, v in sorted(by_year.items())}, "years_up": years_up,
            "full_years": full_years, "stress_mean_bp": statistics.fmean(stress) if stress else None,
            "random_entry": rnd, "dsr": dsr, "buy_hold_sharpe": bh_sr, "criteria": crit, "verdict": verdict,
            "exit_reasons": {r: sum(t["exit_reason"] == r for t in trades) for r in sorted({t["exit_reason"] for t in trades})},
            "_daily": daily, "_trades": trades,
        }
    common = sorted(set.intersection(*(set(d for d, _ in v) for v in dailies.values())))
    try:
        mats = {k: dict(v) for k, v in dailies.items()}
        res["pbo_reported_only"] = rg.pbo_cscv([[mats[k][d] for k in rules.VARIANTS] for d in common])
    except Exception as e:
        res["pbo_reported_only"] = {"error": f"{type(e).__name__}: {e}"}
    return res


# ------------------------------------------------------------------ guards, logging, output
def _git(*a):
    return subprocess.run(["git", *a], cwd=REPO, capture_output=True, text=True)


def guards():
    if _git("rev-parse", "-q", "--verify", f"refs/tags/{TAG}").returncode:
        raise Refused(f"registration tag {TAG} not found (docs/PREREG_trend_pullback_v1.md section 9)")
    if _git("merge-base", "--is-ancestor", TAG, "HEAD").returncode:
        raise Refused(f"{TAG} is not an ancestor of HEAD")
    if _git("status", "--porcelain", "--untracked-files=no").stdout.strip():
        raise Refused("tracked files have uncommitted changes: commit first so the run is reproducible")


def check_data_hash(h: str):
    if not (h.startswith(DATA_SHA_PREFIX) and h.endswith(DATA_SHA_SUFFIX)):
        raise Refused(f"data SHA-256 {h[:8]}...{h[-6:]} is not the tactical-hist-v1 file "
                      f"({DATA_SHA_PREFIX}...{DATA_SHA_SUFFIX}): refusing")


def log_trials(res, data_hash, db: Path = TRIAL_DB):
    import research_gate as rg
    import trials
    if res["n_info"]["already_logged"]:
        raise Refused(f"batch {BATCH} already has {res['n_info']['already_logged']} trials: logged once only")
    done = []
    for name, (tf, pattern) in rules.VARIANTS.items():
        v = res["variants"][name]
        rets = [x for _, x in v["_daily"]]
        skew, kurt = rg.moments(rets)
        oos = {"sharpe": v["sharpe"], "days": v["eval_days"], "daily_skew": skew, "daily_kurtosis": kurt,
               "trades": v["trades"], "mean_net_bp": v["mean_net_bp"], "t_stat": v["t_stat"],
               "profit_factor": v["profit_factor"], "max_drawdown_pct": v["max_drawdown_pct"],
               "random_entry_p": v["random_entry"].get("p"), "verdict": v["verdict"]}
        tid, total = trials.log_trial(name, SYMBOL, f"backtest_{tf}",
                                      {"timeframe": tf, "pattern": pattern, "prereg": TAG,
                                       "cost_bp_per_side": rules.COST_BP_PER_SIDE},
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
    L = [f"SPY {START} .. {END}, SIP 1-minute, {res['sessions']} sessions ({res['bad_sessions']} failed verification); "
         f"N = {ni['n']} ({ni['logged']} logged + {ni['backfilled']} backfilled + {ni['outside_db']} outside db + "
         f"{ni['pending']} pending, floor {N_FLOOR})", ""]
    for tf, b in res["benchmarks"].items():
        L.append(f"{tf}: evaluated from {b['from']}; buy-and-hold Sharpe {b['buy_hold_sharpe']:+.2f}, total "
                 f"{b['buy_hold_total_pct']:+.1f}%, max DD {b['buy_hold_max_dd_pct']:.1f}%; null pool {b['null_pool_trades']} trades")
    L += ["", f"{'variant':<15}{'trades':>7}{'win%':>6}{'mean bp':>9}{'95% low':>9}{'t':>7}{'PF':>6}{'total %':>9}"
              f"{'maxDD %':>9}{'Sharpe':>8}"]
    for name, v in res["variants"].items():
        L.append(f"{name:<15}{v['trades']:>7}{(v['win_rate'] or 0) * 100:>6.0f}{_f(v['mean_net_bp'], 2):>9}"
                 f"{_f(v['boot95'][0], 2):>9}{_f(v['t_stat'], 2):>7}{_f(v['profit_factor'], 2, False):>6}"
                 f"{v['total_net_pct']:>+9.1f}{v['max_drawdown_pct']:>9.1f}{v['sharpe']:>+8.2f}")
    for name, v in res["variants"].items():
        L += ["", f"{name}: {v['verdict'].upper()}"]
        L += [f"  {'PASS' if ok else 'FAIL'}  {c}" for c, ok in v["criteria"].items()]
        L.append(f"  halves {_f(v['half1_mean_bp'], 2)} / {_f(v['half2_mean_bp'], 2)} bp; at 10 bp/side "
                 f"{_f(v['stress_mean_bp'], 2)} bp; random-entry p {_f(v['random_entry'].get('p'), 3, False)} "
                 f"(null mean {_f(v['random_entry'].get('null_mean'), 2)} bp)")
        L.append(f"  deflated Sharpe {_f(v['dsr']['dsr'], 3, False)} (luck threshold {v['dsr']['sr0_annual']:.2f}); "
                 f"exits {v['exit_reasons']}")
        L.append("  years: " + ", ".join(f"{y} {v['year_net_pct'].get(y, 0):+.1f}%" for y in v["full_years"]))
    p = res["pbo_reported_only"]
    L += ["", "PBO across the 4 variants (reported only; unstable with 4 variants on 2 timeframes): "
          + (f"{p['pbo']:.2f}" if "pbo" in p else str(p)),
          "", "No verdict here permits live trading or overrides S2 NO-GO, the test plan, the Risk Controller or the "
              "paper soak (prereg section 8)."]
    return "\n".join(L)


def save(res, data_hash) -> Path:
    RESULTS.mkdir(parents=True, exist_ok=True)
    p = RESULTS / f"trend_pullback_{datetime.now():%Y%m%d_%H%M%S}.json"
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
        if not th.BARS_FILE.exists():
            raise Refused(f"{th.BARS_FILE} missing: the tactical-hist-v1 data file is required (no new fetch)")
        h = th.sha256(th.BARS_FILE)
        check_data_hash(h)
        n_info = trial_n()
        sessions, bad = verified_sessions(th.load())
        res = evaluate_all(sessions, n_info, bad)
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
