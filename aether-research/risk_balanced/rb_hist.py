#!/usr/bin/env python3
"""AETHER research: risk-balanced ETF holding (risk-balanced-v1), historical run.

Registered in docs/PREREG_risk_balanced_v1.md (tag `prereg-risk-balanced-v1`) before any result exists.
Research only: nothing here places orders and the engine never imports it. Rules live in rb_rules.py.

  python3 aether-research/risk_balanced/rb_hist.py run                # 3 variants, criteria, verdict
  python3 aether-research/risk_balanced/rb_hist.py run --log-trials   # also log the 3 variants, once

Data: the same hash-pinned Yahoo adjusted daily file as etf-breakout-v1 and S2 (loaded by eb_hist.load, which
refuses any gap). The 200-day filter comparator is etf-breakout-v1's registered comparator (eb_rules), unchanged.

Fail closed: refused unless the registration tag is an ancestor of HEAD and tracked files are committed, if the
data hash differs, if any symbol misses a trading day, or if the trial database is missing.
"""
from __future__ import annotations

import argparse
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
sys.path.insert(0, str(RESEARCH / "etf_breakout"))
sys.path.insert(0, str(HERE))

import eb_hist  # noqa: E402   (data loader, statistics, dollars: frozen with etf-breakout-v1)
import eb_rules  # noqa: E402  (the registered 200-day filter comparator)
import rb_rules as rules  # noqa: E402

TAG = "prereg-risk-balanced-v1"
BATCH = "risk-balanced-v1"
DATA_FILE, DATA_SHA = eb_hist.DATA_FILE, eb_hist.DATA_SHA
START, END, HALF_SPLIT = eb_hist.START, eb_hist.END, eb_hist.HALF_SPLIT
FIRST_FULL_YEAR, LAST_FULL_YEAR = 2008, 2025
RESULTS = HERE / "results"
TRIAL_DB = RESEARCH / "research.sqlite"

MAX_DD_PCT = 20.0
YEAR_SHARE = 0.60
DD_VS_FILTER = 1.25          # "not substantially more risk": max drawdown <= 1.25 x the 200-day filter's
DSR_MIN = 0.95
BOOT_BLOCK, BOOT_DRAWS, BOOT_SEED = 21, 5000, 0
OUTSIDE_DB = 3
N_FLOOR = 52                 # 49 (registry on 2026-10-25, after etf-breakout-v1) + these 3
HIST_VAR_PER_PERIOD = eb_hist.HIST_VAR_PER_PERIOD


class Refused(Exception):
    pass


def paired_sharpe_diff_ci(a, b, block=BOOT_BLOCK, draws=BOOT_DRAWS, seed=BOOT_SEED):
    """95% interval of Sharpe(a) - Sharpe(b) (annualised) from a moving-block bootstrap that resamples the SAME
    days for both series, so their correlation is kept."""
    import numpy as np
    x, y = np.asarray(a, float), np.asarray(b, float)
    if x.size != y.size or x.size < 2 * block:
        raise ValueError("series must have equal length >= 2 blocks")
    rng = np.random.default_rng(seed)
    nb = math.ceil(x.size / block)
    starts = rng.integers(0, x.size - block + 1, size=(draws, nb))
    idx = (starts[:, :, None] + np.arange(block)[None, None, :]).reshape(draws, -1)[:, :x.size]

    def sr(m):
        sd = m.std(axis=1, ddof=1)
        return np.where(sd > 0, m.mean(axis=1) / np.where(sd > 0, sd, 1.0), 0.0) * math.sqrt(252)
    d = sr(x[idx]) - sr(y[idx])
    return float(np.percentile(d, 2.5)), float(np.percentile(d, 97.5))


def evaluate(dates, data, n_info) -> dict:
    import numpy as np
    import research_gate as rg
    st = eb_hist.start_index(dates)
    if st < max(rules.VARIANTS.values()) + 1:
        raise Refused("not enough history for the volatility lookback")
    ev = dates[st:]
    S = eb_hist.summary
    ew, ew_turn, _ = rules.simulate(dates, data, st, rules.equal_weights, 0)
    sma, _ = eb_rules.run_comparator(data, "sma200", st)
    hold, _ = eb_rules.run_comparator(data, "hold", st)
    spy = [data["SPY"][1][i] / data["SPY"][1][i - 1] - 1 for i in range(st, len(dates))]
    res = {"window": [ev[0], ev[-1]], "days": len(ev), "n_info": n_info, "variants": {},
           "comparators": {"equal_weight_monthly": {**S(ev, ew), "avg_turnover": statistics.fmean(ew_turn)},
                           "sma200_same_etfs": S(ev, sma), "hold_same_etfs": S(ev, hold),
                           "spy_hold_report_only": S(ev, spy),
                           "sixty_forty_report_only": S(ev, eb_hist.sixty_forty(dates, data, st))}}
    ew_sr = res["comparators"]["equal_weight_monthly"]["sharpe"]
    h1 = [i for i, d in enumerate(ev) if d < HALF_SPLIT.isoformat()]
    h2 = [i for i, d in enumerate(ev) if d >= HALF_SPLIT.isoformat()]
    full = list(range(FIRST_FULL_YEAR, LAST_FULL_YEAR + 1))
    need = math.ceil(YEAR_SHARE * len(full))
    out = {}
    for name, n in rules.VARIANTS.items():
        r, turn, wlog = rules.simulate(dates, data, st, rules.inv_vol_weights, n)
        stress = rules.simulate(dates, data, st, rules.inv_vol_weights, n, rules.STRESS_COST_BP_PER_SIDE)[0]
        s = S(ev, r)
        boot = rg.block_bootstrap_sharpes(r, block=BOOT_BLOCK, draws=BOOT_DRAWS, seed=BOOT_SEED)
        yrs = eb_hist.years(ev, r)
        avg_w = {sy: statistics.fmean(w[sy] for _, w in wlog) for sy in rules.SYMBOLS}
        out[name] = {"vol_n": n, "base": name == rules.BASE, **s,
                     "sharpe_boot95": [float(np.percentile(boot, 2.5)), float(np.percentile(boot, 97.5))],
                     "sharpe_minus_equal_weight": s["sharpe"] - ew_sr,
                     "sharpe_diff_vs_equal_weight_95": list(paired_sharpe_diff_ci(r, ew)),
                     "half1_sharpe": eb_hist.sharpe([r[i] for i in h1]), "half2_sharpe": eb_hist.sharpe([r[i] for i in h2]),
                     "half1_ew_sharpe": eb_hist.sharpe([ew[i] for i in h1]), "half2_ew_sharpe": eb_hist.sharpe([ew[i] for i in h2]),
                     "years_pct": {y: x * 100 for y, x in sorted(yrs.items())},
                     "years_up": sum(1 for y in full if yrs.get(y, 0.0) > 0), "years_full": len(full),
                     "stress_cagr_pct": eb_hist.cagr(stress) * 100, "avg_turnover": statistics.fmean(turn),
                     "avg_weights": avg_w, "last_weights": wlog[-1][1], "rebalances": len(wlog),
                     "dsr": rg.deflated_sharpe(r, n_info["n"], HIST_VAR_PER_PERIOD),
                     "_daily": list(zip(ev, r))}
    b = out[rules.BASE]
    f = res["comparators"]["sma200_same_etfs"]
    crit = {
        "C1 CAGR > 0 and block-bootstrap 95% lower bound of Sharpe > 0": b["cagr_pct"] > 0 and b["sharpe_boot95"][0] > 0,
        "C2 Sharpe > equal weight (same ETFs, schedule and costs)": b["sharpe"] > ew_sr,
        "C3 paired bootstrap 95% lower bound of (Sharpe - equal-weight Sharpe) > 0": b["sharpe_diff_vs_equal_weight_95"][0] > 0,
        f"C4 max drawdown <= {MAX_DD_PCT:.0f}%": b["max_drawdown_pct"] <= MAX_DD_PCT,
        f"C5 Sharpe > equal weight in both halves (split {HALF_SPLIT})": b["half1_sharpe"] > b["half1_ew_sharpe"]
        and b["half2_sharpe"] > b["half2_ew_sharpe"],
        f"C6 positive in >= {need} of {len(full)} full years": b["years_up"] >= need,
        "C7 CAGR > 0 at 10 bp/side": b["stress_cagr_pct"] > 0,
        "C8 CAGR > the 200-day filter's (same ETFs)": b["cagr_pct"] > f["cagr_pct"],
        "C9 Sharpe >= the 200-day filter's": b["sharpe"] >= f["sharpe"],
        f"C10 max drawdown <= {DD_VS_FILTER} x the 200-day filter's": b["max_drawdown_pct"] <= DD_VS_FILTER * f["max_drawdown_pct"],
        f"C11 deflated Sharpe >= {DSR_MIN} (necessary, not sufficient)": not math.isnan(b["dsr"]["dsr"]) and b["dsr"]["dsr"] >= DSR_MIN,
        "C12 both neighbours Sharpe > equal weight": all(out[k]["sharpe"] > ew_sr for k in rules.VARIANTS if k != rules.BASE),
    }
    if b["cagr_pct"] <= 0 or b["sharpe"] <= ew_sr:
        verdict = "historically unsupported"     # no better than equal weight: the weighting adds nothing
    elif all(crit.values()):
        verdict = "historically supported"
    else:
        verdict = "mixed"
    res["variants"] = out
    res["criteria"] = crit
    res["verdict"] = verdict
    res["dollars"] = eb_hist.dollars([x for _, x in b["_daily"]], eb_hist.years(ev, [x for _, x in b["_daily"]]))
    try:
        res["pbo_reported_only"] = rg.pbo_cscv([[out[k]["_daily"][i][1] for k in rules.VARIANTS] for i in range(len(ev))])
    except Exception as e:
        res["pbo_reported_only"] = {"error": f"{type(e).__name__}: {e}"}
    return res


# ------------------------------------------------------------------ guards, trials, output
def _git(*a):
    return subprocess.run(["git", *a], cwd=REPO, capture_output=True, text=True)


def guards():
    if _git("rev-parse", "-q", "--verify", f"refs/tags/{TAG}").returncode:
        raise Refused(f"registration tag {TAG} not found (docs/PREREG_risk_balanced_v1.md section 9)")
    if _git("merge-base", "--is-ancestor", TAG, "HEAD").returncode:
        raise Refused(f"{TAG} is not an ancestor of HEAD")
    if _git("status", "--porcelain", "--untracked-files=no").stdout.strip():
        raise Refused("tracked files have uncommitted changes: commit first so the run is reproducible")


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
    return {"logged": logged, "backfilled": back, "outside_db": OUTSIDE_DB, "pending": pending,
            "n": max(N_FLOOR, logged + back + OUTSIDE_DB + pending), "already_logged": mine}


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
               "cagr_pct": v["cagr_pct"], "max_drawdown_pct": v["max_drawdown_pct"],
               "verdict": res["verdict"] if v["base"] else "neighbour"}
        tid, total = trials.log_trial(name, ",".join(rules.SYMBOLS), "backtest_daily",
                                      {"vol_n": v["vol_n"], "rebalance": "monthly", "prereg": TAG,
                                       "cost_bp_per_side": rules.COST_BP_PER_SIDE, "base": v["base"]},
                                      START, END, "none", {"sharpe": v["sharpe"]}, oos, db=str(db),
                                      daily_returns=v["_daily"], batch=BATCH, data_hash=data_hash)
        done.append((name, tid, total))
    return done


def render(res) -> str:
    ni, cm = res["n_info"], res["comparators"]
    f = eb_hist._f
    L = [f"Risk-balanced SPY QQQ IWM GLD TLT, monthly, {res['window'][0]} .. {res['window'][1]}, {res['days']} days; "
         f"N = {ni['n']} ({ni['logged']} logged + {ni['backfilled']} backfilled + {ni['outside_db']} outside + "
         f"{ni['pending']} pending, floor {N_FLOOR})", "",
         f"{'':<24}{'Sharpe':>8}{'CAGR %':>8}{'maxDD %':>9}{'total %':>9}"]
    for k, v in cm.items():
        L.append(f"{k:<24}{v['sharpe']:>+8.2f}{v['cagr_pct']:>+8.1f}{v['max_drawdown_pct']:>9.1f}{v['total_pct']:>+9.0f}")
    L += ["", f"{'variant':<10}{'Sharpe':>8}{'95% low':>9}{'vs EW':>8}{'diff low':>9}{'CAGR %':>8}{'maxDD %':>9}"
              f"{'h1 SR':>7}{'h2 SR':>7}{'@10bp':>8}{'turn':>7}"]
    for k, v in res["variants"].items():
        L.append(f"{k:<10}{v['sharpe']:>+8.2f}{v['sharpe_boot95'][0]:>+9.2f}{v['sharpe_minus_equal_weight']:>+8.2f}"
                 f"{v['sharpe_diff_vs_equal_weight_95'][0]:>+9.2f}{v['cagr_pct']:>+8.1f}{v['max_drawdown_pct']:>9.1f}"
                 f"{v['half1_sharpe']:>+7.2f}{v['half2_sharpe']:>+7.2f}{v['stress_cagr_pct']:>+8.1f}{v['avg_turnover']:>7.1%}")
    b = res["variants"][rules.BASE]
    L += ["", f"{rules.BASE} (judged): {res['verdict'].upper()}"]
    L += [f"  {'PASS' if ok else 'FAIL'}  {c}" for c, ok in res["criteria"].items()]
    L.append(f"  average weights: " + ", ".join(f"{s} {w:.0%}" for s, w in b["avg_weights"].items())
             + f"; deflated Sharpe {f(b['dsr']['dsr'], 3, False)} (luck threshold {b['dsr']['sr0_annual']:.2f})")
    L.append("  years: " + ", ".join(f"{y} {x:+.1f}%" for y, x in b["years_pct"].items()
                                     if FIRST_FULL_YEAR <= y <= LAST_FULL_YEAR))
    d = res["dollars"]
    L += ["", "Dollars (base variant's history; averages, not a forecast):"]
    for c in eb_hist.CAPITALS:
        x = d[f"{c:g}"]
        L.append(f"  ${c:>7,.0f}: {f(x['usd_per_trading_day'])} per trading day, {f(x['usd_per_year'])} per year, "
                 f"worst full year {f(x['worst_full_year_usd'])}, max drawdown {f(-x['max_drawdown_usd'])}")
    L.append("  capital needed for $2 per trading day at this average: "
             + ("not reachable (average <= 0)" if d["capital_for_target"] is None else f"${d['capital_for_target']:,.0f}"))
    p = res["pbo_reported_only"]
    L += ["", "PBO across the 3 variants (reported only): " + (f"{p['pbo']:.2f}" if "pbo" in p else str(p)),
          "", "No verdict here permits paper or live trading or overrides S2 NO-GO, the test plan, the Risk Controller "
              "or the Oct 22 decision (prereg section 8)."]
    return "\n".join(L)


def save(res, data_hash) -> Path:
    RESULTS.mkdir(parents=True, exist_ok=True)
    p = RESULTS / f"risk_balanced_{datetime.now():%Y%m%d_%H%M%S}.json"
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
        h = eb_hist.sha256(DATA_FILE)
        if h != DATA_SHA:
            raise Refused(f"data SHA-256 {h[:12]}... is not the registered file ({DATA_SHA[:12]}...): refusing")
        n_info = trial_n()
        dates, data = eb_hist.load()
        res = evaluate(dates, data, n_info)
        print(render(res))
        print(f"\nsaved {save(res, h)}\ndata SHA-256 {h}")
        if a.log_trials:
            for name, tid, total in log_trials(res, h):
                print(f"logged {name}: trial id {tid}, research_trials now {total}")
        return 0
    except (Refused, eb_hist.Refused) as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
