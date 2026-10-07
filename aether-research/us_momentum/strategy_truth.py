#!/usr/bin/env python3
"""AETHER strategy-truth runner, STAGE 1 (plan S1, S2, S5, S9-close, S10 to S14, S16 to S19).

Runs the live rule (us_strategy.py through us_momentum_backtest.simulate) on the long Yahoo history, as
pre-registered in docs/PREREG_us_momentum.md, and reports every item as PASS, FAIL, INFO or NOT RUN. Nothing is
tuned and nothing is selected: this only measures.

  python strategy_truth.py run                 # long Yahoo history (pre-registered primary sample)
  python strategy_truth.py run --cache alpaca  # the Alpaca-only sample (about 2016 onward)

Stage 2 (S3 PBO over the 81-variant family, S6 neighbourhood, S7 placebo, S8 shuffled null, S9 time shifts,
S15, S20) needs a fast simulator and is reported here as NOT RUN.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import math
import os
import sqlite3
import statistics
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ENGINE_DIR = Path(os.getenv("AETHER_DIR", str(Path.home())))
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ENGINE_DIR))
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import us_strategy as us  # noqa: E402
import research_gate as rg  # noqa: E402
import us_momentum_backtest as bt  # noqa: E402

DATA = HERE / "data"
RESULTS = HERE / "results"
CACHES = {"yf": DATA / "us_daily_yf.csv.gz", "alpaca": DATA / "us_daily_all.csv.gz"}
TRIAL_DB = HERE.parent / "research.sqlite"
PREREG_TAG = "prereg-us-momentum"

SEED = 20261007                    # pre-registration section 7
BOOT_DRAWS, MC_DRAWS, BLOCK = 5000, 10000, 21
DSR_MIN = 0.95                     # plan S2
DD_LIMIT = 0.27                    # strategy.md / US_MAX_DRAWDOWN_PCT default
MAX_YEAR_SHARE = 0.40              # plan S13
MIN_ROUND_TRIPS = 100              # plan S18
PF_WINDOW, PF_AMEND_ABOVE = 20, 0.20
SLICES = {"2008": ("2008-01-01", "2008-12-31"), "2011": ("2011-01-01", "2011-12-31"),
          "2015-16": ("2015-01-01", "2016-12-31"), "2018Q4": ("2018-10-01", "2018-12-31"),
          "2020": ("2020-01-01", "2020-12-31"), "2022": ("2022-01-01", "2022-12-31")}
NOT_RUN_STAGE2 = {"S3": "PBO over the 81-variant family", "S6": "parameter neighbourhood",
                  "S7": "placebo strategies", "S8": "shuffled-returns null", "S9-shift": "1, 2, 5 day execution shifts",
                  "S15": "corporate-action audit", "S20": "live-size feasibility at $50-$100"}


# ------------------------------------------------------------------ helpers
def sha256_file(p) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def sha256_series(s: pd.Series) -> str:
    return hashlib.sha256(np.asarray(s.to_numpy(dtype=float)).tobytes() + str(list(s.index.astype(str))).encode()).hexdigest()


def load_cache(path):
    """Same calendar and fill rules as us_momentum_backtest.load, for an arbitrary cache file."""
    df = pd.read_csv(path)
    opens = df.pivot(index="date", columns="symbol", values="open").sort_index()
    closes = df.pivot(index="date", columns="symbol", values="close").sort_index()
    cal = closes.index[closes["SPY"].notna()]
    return opens.reindex(cal), closes.reindex(cal).ffill()


def trial_stats(db) -> dict:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        sharpes = [r[0] for r in con.execute("SELECT oos_sharpe FROM research_trials WHERE oos_sharpe IS NOT NULL")]
        n_logged = con.execute("SELECT COUNT(*) FROM research_trials").fetchone()[0]
        try:
            n_back = int(con.execute("SELECT COALESCE(SUM(count), 0) FROM untracked_trials").fetchone()[0])
        except sqlite3.OperationalError:
            n_back = 0
    finally:
        con.close()
    per = [x / math.sqrt(rg.PERIODS) for x in sharpes]
    var = statistics.variance(per) if len(per) >= 2 else (0.5 / math.sqrt(rg.PERIODS)) ** 2
    return {"n_total": n_logged + n_back, "n_logged": n_logged, "n_backfilled": n_back, "var_per_period": var,
            "trial_sharpes": sharpes}


def required_sharpe(t, n, var, skew, kurt, target=DSR_MIN) -> float:
    """Annual Sharpe at which the deflated Sharpe reaches `target` for this sample length and trial count."""
    sr0 = rg.expected_max_sr(n, var)
    lo, hi = 0.0, 8.0
    for _ in range(60):
        mid = (lo + hi) / 2
        p = rg.prob_sr_above(mid / math.sqrt(rg.PERIODS), sr0, t, skew, kurt)
        if p != p or p < target:
            lo = mid
        else:
            hi = mid
    return hi


def round_trip_returns(log, opens, closes, slip_bps=2.0) -> list:
    """Position-level return from the month an ETF is entered to the month it is exited (open to open), net of
    round-trip slippage. A proxy for trade P&L that ignores weights: enough for trade counts (S18) and profit-factor
    windows (S19). Positions still open at the end are not counted."""
    def px(date, s):
        v = opens.at[date, s] if date in opens.index and s in opens.columns else float("nan")
        return float(v) if np.isfinite(v) else float(closes.at[date, s])
    prev, entered, out = set(), {}, []
    for entry in log:
        d, now = entry["date"], set(entry["weights"])
        for s in now - prev:
            entered[s] = d
        for s in prev - now:
            out.append(px(d, s) / px(entered.pop(s), s) - 1.0 - 2 * slip_bps / 1e4)
        prev = now
    return out


def slice_report(eq, spy, slices=SLICES, dd_limit=DD_LIMIT) -> dict:
    out = {}
    for name, (a, b) in slices.items():
        e, s = eq[(eq.index >= a) & (eq.index <= b)], spy[(spy.index >= a) & (spy.index <= b)]
        if len(e) < 20:
            out[name] = {"testable": False}
            continue
        dd = float((e / e.cummax() - 1).min())
        out[name] = {"testable": True, "return": round(float(e.iloc[-1] / e.iloc[0] - 1), 4), "max_dd": round(dd, 4),
                     "spy_max_dd": round(float((s / s.cummax() - 1).min()), 4) if len(s) >= 20 else None,
                     "within_limit": -dd <= dd_limit}
    return out


def beats(m_s: dict, m_b: dict) -> bool:
    """Better risk-adjusted: higher Sharpe OR higher Calmar."""
    return bool((m_s.get("sharpe") or -9) > (m_b.get("sharpe") or -9) or (m_s.get("calmar") or -9) > (m_b.get("calmar") or -9))


def item(passed, detail, **extra):
    """None means informational. Anything else is coerced to a real bool first: numpy comparisons return numpy.bool_,
    which `is True` would silently reject and report as INFO instead of PASS or FAIL."""
    if passed is not None:
        passed = bool(passed)
    return {"result": "PASS" if passed is True else "FAIL" if passed is False else "INFO", "detail": detail, **extra}


# ------------------------------------------------------------------ the evaluation
def evaluate(opens, closes, db_path, *, boot_draws=BOOT_DRAWS, mc_draws=MC_DRAWS, seed=SEED, slip_bps=2.0,
             equity0=5000.0, dd_limit=DD_LIMIT) -> dict:
    items = {}
    eq, log = bt.simulate(opens, closes, equity0, slip_bps)
    eq_again, _ = bt.simulate(opens, closes, equity0, slip_bps)
    r = eq.pct_change().dropna()
    base = bt.metrics(eq)
    skew, kurt = rg.moments(r.to_numpy())
    ts = trial_stats(db_path)

    # S16 reproducibility
    h1, h2 = sha256_series(eq), sha256_series(eq_again)
    items["S16"] = item(h1 == h2, "two identical runs give byte-identical equity" if h1 == h2 else "runs differ",
                        equity_sha256=h1[:16])

    # S2 deflated Sharpe with the full trial count
    d_full = rg.deflated_sharpe(r.to_numpy(), ts["n_total"], ts["var_per_period"])
    r_oos = r[r.index >= "2022-01-01"]
    d_oos = rg.deflated_sharpe(r_oos.to_numpy(), ts["n_total"], ts["var_per_period"]) if len(r_oos) > 250 else None
    d_one = rg.deflated_sharpe(r.to_numpy(), 1, ts["var_per_period"])
    need = required_sharpe(len(r), ts["n_total"], ts["var_per_period"], skew, kurt)
    items["S2"] = item(bool(d_full["dsr"] >= DSR_MIN),
                       f"DSR {d_full['dsr']:.3f} (needs {DSR_MIN}) | Sharpe {d_full['sr_annual']:.2f} vs luck threshold "
                       f"{d_full['sr0_annual']:.2f} | N={ts['n_total']} | Sharpe needed for {DSR_MIN}: {need:.2f}",
                       dsr=d_full["dsr"], sharpe=d_full["sr_annual"], sr0=d_full["sr0_annual"], n=ts["n_total"],
                       sharpe_needed=need, years=round(len(r) / 252, 1),
                       dsr_2022_on=None if d_oos is None else d_oos["dsr"], psr_if_n_is_1=d_one["psr"])

    # S5 cost stress
    stress = {}
    for mult in (2, 3):
        e, _ = bt.simulate(opens, closes, equity0, slip_bps * mult)
        m = bt.metrics(e)
        stress[mult] = {"cagr": m["cagr"], "sharpe": m["sharpe"]}
    items["S5"] = item(stress[2]["cagr"] > 0, f"net CAGR at x2 costs {stress[2]['cagr']:.2%}, x3 {stress[3]['cagr']:.2%} "
                       f"(baseline {base['cagr']:.2%})", stress=stress)

    # S9 (partial): execute at the CLOSE of the first trading day, as the live engine does in the afternoon
    e_close, _ = bt.simulate(closes, closes, equity0, slip_bps)
    mc_ = bt.metrics(e_close)
    items["S9-close"] = item(None, f"close-of-day execution: CAGR {mc_['cagr']:.2%}, Sharpe {mc_['sharpe']} "
                             f"(open execution: {base['cagr']:.2%}, {base['sharpe']})", close_metrics=mc_)

    # S10 block bootstrap
    boot = rg.block_bootstrap_sharpes(r.to_numpy(), BLOCK, boot_draws, seed)
    p5 = float(np.percentile(boot, 5))
    items["S10"] = item(p5 > 0, f"5th percentile Sharpe {p5:.2f} (median {np.median(boot):.2f}) over {boot_draws} draws",
                        p5=p5)

    # S11 Monte Carlo drawdown on monthly returns
    monthly = eq.resample("ME").last().pct_change().dropna()
    mdd = rg.mc_max_drawdown(monthly.to_numpy(), mc_draws, seed)
    p95 = float(np.percentile(mdd, 95))
    items["S11"] = item(p95 <= dd_limit, f"95th percentile max drawdown {p95:.1%} (limit {dd_limit:.0%}); observed "
                        f"{abs(base['max_dd']):.1%}", p95=p95, observed=abs(base["max_dd"]))

    # S12 regime slices
    spy, sixty = bt.benchmarks(closes, eq.index, equity0)
    sl = slice_report(eq, spy, dd_limit=dd_limit)
    untestable = [k for k, v in sl.items() if not v["testable"]]
    breached = [k for k, v in sl.items() if v["testable"] and not v["within_limit"]]
    if untestable:
        items["S12"] = item(None, f"slices not testable on this sample: {untestable}; breaches among the rest: {breached}",
                            slices=sl)
    else:
        items["S12"] = item(not breached, "no slice breaches the drawdown limit" if not breached else
                            f"limit breached in {breached}", slices=sl)

    # S13 yearly concentration
    share = rg.max_year_share(r)
    items["S13"] = item(bool(share <= MAX_YEAR_SHARE), f"largest single-year share of total profit {share:.0%} "
                        f"(limit {MAX_YEAR_SHARE:.0%})", max_share=share)

    # S17 benchmarks
    m_spy, m_60 = bt.metrics(spy), bt.metrics(sixty)
    ok17 = beats(base, m_spy) and beats(base, m_60)
    items["S17"] = item(ok17, f"Sharpe/Calmar strategy {base['sharpe']}/{base['calmar']} | SPY {m_spy['sharpe']}/"
                        f"{m_spy['calmar']} | 60/40 {m_60['sharpe']}/{m_60['calmar']}", spy=m_spy, sixty=m_60)

    # S18 trade count and S19 profit-factor windows
    trades = round_trip_returns(log, opens, closes, slip_bps)
    items["S18"] = item(len(trades) >= MIN_ROUND_TRIPS, f"{len(trades)} round trips over {len(r) / 252:.1f} years "
                        f"(needs {MIN_ROUND_TRIPS})", round_trips=len(trades))
    if len(trades) >= PF_WINDOW:
        f = rg.pf_below_one_frequency(trades, PF_WINDOW, 5000, seed)
        items["S19"] = item(f <= PF_AMEND_ABOVE, f"{f:.0%} of {PF_WINDOW}-trade windows show profit factor below 1 by "
                            f"chance" + ("" if f <= PF_AMEND_ABOVE else ": AMEND the PF kill rule before L1"),
                            frequency=f, proxy="position-level returns, weights ignored")
    else:
        items["S19"] = item(None, "fewer round trips than one window")

    for k, v in NOT_RUN_STAGE2.items():
        items[k] = {"result": "NOT RUN", "detail": f"stage 2: {v}"}
    items["S4"] = {"result": "NOT DUE", "detail": "forward holdout (data after 2026-10-07), evaluated once at the end of the soak"}
    return {"baseline": base, "items": items, "years": round(len(r) / 252, 1), "rebalances": len(log),
            "sample": [base["start"], base["end"]], "trials": {k: v for k, v in ts.items() if k != "trial_sharpes"},
            "equity_sha256": h1, "seed": seed}


# ------------------------------------------------------------------ provenance and logging
def prereg_status(repo=ENGINE_DIR, tag=PREREG_TAG) -> dict:
    try:
        ts = subprocess.run(["git", "log", "-1", "--format=%ct", tag], cwd=repo, capture_output=True, text=True, timeout=30)
        if ts.returncode != 0 or not ts.stdout.strip():
            return {"ok": False, "detail": f"tag {tag} not found"}
        when = datetime.datetime.fromtimestamp(int(ts.stdout.strip()), datetime.timezone.utc)
        return {"ok": when < datetime.datetime.now(datetime.timezone.utc), "detail": f"tag {tag} committed {when:%Y-%m-%d %H:%M} UTC"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detail": f"git check failed: {type(e).__name__}"}


def s14_status(results_dir=RESULTS, caches=CACHES) -> dict:
    p = Path(results_dir) / "source_comparison.json"
    if not p.exists():
        return {"result": "NOT RUN", "detail": "no source_comparison.json (run long_history.py compare)"}
    rep = json.loads(p.read_text())
    stale = (rep.get("alpaca_sha256") != sha256_file(caches["alpaca"])[:16]
             or rep.get("yahoo_sha256") != sha256_file(caches["yf"])[:16])
    if stale:
        return {"result": "FAIL", "detail": "comparison is stale: the cache files changed since it was made"}
    v = rep["verdict"]
    return {"result": "PASS" if v["pass"] else "FAIL",
            "detail": f"worst median return difference {rep['returns']['worst_median_bps']} bps, "
                      f"signals agree {rep['signals']['agreement']:.1%}"}


def config_hash(data_sha, code_sha, slip_bps, equity0, seed) -> str:
    return hashlib.sha256(json.dumps({"data": data_sha, "code": code_sha, "slip": slip_bps, "equity": equity0,
                                      "seed": seed}, sort_keys=True).encode()).hexdigest()


def log_run(db, purpose, cfg_hash, data_sha, code_sha, summary) -> bool:
    """One row per distinct configuration, in a table SEPARATE from research_trials (pre-registration section 4):
    robustness and evaluation runs must never change N. Returns False if this configuration was already logged."""
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE IF NOT EXISTS research_runs (id INTEGER PRIMARY KEY, created_at TEXT, purpose TEXT,
                   config_hash TEXT UNIQUE, data_sha TEXT, code_sha TEXT, summary_json TEXT)""")
    try:
        with con:
            con.execute("INSERT INTO research_runs (created_at, purpose, config_hash, data_sha, code_sha, summary_json)"
                        " VALUES (?,?,?,?,?,?)", (datetime.datetime.now(datetime.timezone.utc).isoformat(), purpose,
                                                  cfg_hash, data_sha, code_sha, json.dumps(summary, default=str)))
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        con.close()


def code_fingerprint() -> str:
    parts = [ENGINE_DIR / "us_strategy.py", HERE.parent / "research_gate.py", HERE / "strategy_truth.py",
             HERE / "us_momentum_backtest.py"]
    return hashlib.sha256("".join(sha256_file(p) for p in parts).encode()).hexdigest()


def render(res: dict, pre: dict, s14: dict) -> str:
    lines = [f"Sample {res['sample'][0]} .. {res['sample'][1]} ({res['years']} years, {res['rebalances']} rebalances), "
             f"N={res['trials']['n_total']} attempts", ""]
    lines.append(f"{'S1':9s} {'PASS' if pre['ok'] else 'FAIL':8s} pre-registration: {pre['detail']}")
    lines.append(f"{'S14':9s} {s14['result']:8s} {s14['detail']}")
    for k in sorted(res["items"], key=lambda x: (x.split('-')[0][1:].zfill(3), x)):
        it = res["items"][k]
        lines.append(f"{k:9s} {it['result']:8s} {it['detail']}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--cache", default="yf", choices=list(CACHES))
    r.add_argument("--boot", type=int, default=BOOT_DRAWS)
    r.add_argument("--mc", type=int, default=MC_DRAWS)
    a = ap.parse_args()
    path = CACHES[a.cache]
    opens, closes = load_cache(path)
    res = evaluate(opens, closes, TRIAL_DB, boot_draws=a.boot, mc_draws=a.mc)
    pre, s14 = prereg_status(), s14_status()
    data_sha, code_sha = sha256_file(path), code_fingerprint()
    res.update(prereg=pre, s14=s14, data_sha256=data_sha, code_sha256=code_sha, cache=a.cache)
    cfg = config_hash(data_sha, code_sha, 2.0, 5000.0, SEED)
    new = log_run(TRIAL_DB, f"strategy_truth_stage1_{a.cache}", cfg, data_sha, code_sha,
                  {k: v["result"] for k, v in res["items"].items()})
    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / f"strategy_truth_{a.cache}_{datetime.datetime.now():%Y%m%d_%H%M%S}.json"
    out.write_text(json.dumps(res, indent=2, default=str))
    print(render(res, pre, s14))
    print(f"\ndata sha256 {data_sha[:16]}  code sha256 {code_sha[:16]}  config {'logged' if new else 'already logged'}")
    print(f"written {out}")


if __name__ == "__main__":
    main()
