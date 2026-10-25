#!/usr/bin/env python3
"""AETHER research: 200-day filter with idle money in T-bills (sma-tbill-v1), historical run.

Registered in docs/PREREG_sma_tbill_v1.md (tag `prereg-sma-tbill-v1`) before any result exists.
Research only: nothing here places orders and the engine never imports it. Rules live in st_rules.py.

  python3 aether-research/sma_tbill/st_hist.py run --log-trials   # 3 variants, criteria, verdict; logs once

Data: the registered ETF file (etf-breakout-v1 loader, hash-pinned) and the BIL file fetched once by fetch_bil.py,
whose SHA-256 must match data/bil_manifest.json AS COMMITTED AT THE TAG (so the data was frozen before the tag).

Fail closed: refused unless the tag is an ancestor of HEAD and tracked files are committed, if either data hash
differs, if BIL misses a trading day of the window, or if the trial database is missing.
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
sys.path.insert(0, str(RESEARCH / "etf_breakout"))
sys.path.insert(0, str(RESEARCH / "risk_balanced"))
sys.path.insert(0, str(HERE))

import eb_hist  # noqa: E402   (ETF loader, statistics, dollars: frozen with etf-breakout-v1)
import eb_rules  # noqa: E402
import rb_rules  # noqa: E402  (report-only context: risk-balanced-v1's base rule)
import st_rules as rules  # noqa: E402

TAG = "prereg-sma-tbill-v1"
BATCH = "sma-tbill-v1"
BIL_FILE = HERE / "data" / "bil_daily_yf.csv.gz"
BIL_MANIFEST = HERE / "data" / "bil_manifest.json"
START, END = date(2007, 6, 1), eb_hist.END          # first full month after BIL's inception .. 2026-10-06
HALF_SPLIT = date(2016, 1, 1)
FIRST_FULL_YEAR, LAST_FULL_YEAR = 2008, 2025
RESULTS = HERE / "results"
TRIAL_DB = RESEARCH / "research.sqlite"

MAX_DD_PCT = 20.0
YEAR_SHARE = 0.60
DSR_MIN = 0.95
BOOT_BLOCK, BOOT_DRAWS, BOOT_SEED = 21, 5000, 0
OUTSIDE_DB = 3
N_FLOOR = 55                 # 52 (registry on 2026-10-25, after risk-balanced-v1) + these 3
HIST_VAR_PER_PERIOD = eb_hist.HIST_VAR_PER_PERIOD


class Refused(Exception):
    pass


# ------------------------------------------------------------------ data
def load_bil(dates, path: Path = BIL_FILE):
    """(opens, closes) of BIL aligned to the ETF calendar; None before START. Refuses any gap from START on."""
    got = {}
    with gzip.open(path, "rt") as f:
        for r in csv.DictReader(f):
            if r["symbol"] == rules.IDLE:
                got[r["date"]] = (float(r["open"]), float(r["close"]))
    need = [d for d in dates if START.isoformat() <= d <= END.isoformat()]
    miss = [d for d in need if d not in got]
    if miss:
        raise Refused(f"BIL misses {len(miss)} trading days of the window (first {miss[0]}): refusing (no filling)")
    o = [got[d][0] if d in got and d >= START.isoformat() else None for d in dates]
    c = [got[d][1] if d in got and d >= START.isoformat() else None for d in dates]
    return o, c


def start_index(dates) -> int:
    i = next(i for i, d in enumerate(dates) if d >= START.isoformat())
    if i < max(rules.VARIANTS.values()) + 1:
        raise Refused("not enough warm-up history before the start date")
    return i


# ------------------------------------------------------------------ evaluation
def evaluate(dates, data, bil, n_info) -> dict:
    import numpy as np
    import research_gate as rg
    st = start_index(dates)
    ev = dates[st:]
    S = eb_hist.summary
    hold, _ = eb_rules.run_comparator(data, "hold", st)
    rb, _, _ = rb_rules.simulate(dates, data, st, rb_rules.inv_vol_weights, 63)
    bil_hold = [bil[1][i] / bil[1][i - 1] - 1 for i in range(st + 1, len(dates))]
    res = {"window": [ev[0], ev[-1]], "days": len(ev), "n_info": n_info, "variants": {},
           "comparators": {"hold_same_etfs": S(ev, hold),
                           "risk_balanced_rb63_report_only": S(ev, rb),
                           "bil_hold_report_only": S(ev[1:], bil_hold),
                           "spy_hold_report_only": S(ev, [data["SPY"][1][i] / data["SPY"][1][i - 1] - 1
                                                          for i in range(st, len(dates))]),
                           "sixty_forty_report_only": S(ev, eb_hist.sixty_forty(dates, data, st))}}
    h1 = [i for i, d in enumerate(ev) if d < HALF_SPLIT.isoformat()]
    h2 = [i for i, d in enumerate(ev) if d >= HALF_SPLIT.isoformat()]
    full = list(range(FIRST_FULL_YEAR, LAST_FULL_YEAR + 1))
    need = math.ceil(YEAR_SHARE * len(full))
    out = {}
    for name, n in rules.VARIANTS.items():
        r, sl = rules.run(data, bil, n, st)
        cash, _ = rules.run(data, bil, n, st, idle_cash=True)
        stress, _ = rules.run(data, bil, n, st, rules.STRESS_COST_BP_PER_SIDE)
        s, sc = S(ev, r), S(ev, cash)
        boot = rg.block_bootstrap_sharpes(r, block=BOOT_BLOCK, draws=BOOT_DRAWS, seed=BOOT_SEED)
        yrs = eb_hist.years(ev, r)
        out[name] = {"sma_n": n, "base": name == rules.BASE, **s,
                     "cash_version": sc,
                     "sharpe_boot95": [float(np.percentile(boot, 2.5)), float(np.percentile(boot, 97.5))],
                     "half1_cagr_pct": eb_hist.cagr([r[i] for i in h1]) * 100,
                     "half2_cagr_pct": eb_hist.cagr([r[i] for i in h2]) * 100,
                     "half1_cash_cagr_pct": eb_hist.cagr([cash[i] for i in h1]) * 100,
                     "half2_cash_cagr_pct": eb_hist.cagr([cash[i] for i in h2]) * 100,
                     "years_pct": {y: x * 100 for y, x in sorted(yrs.items())},
                     "years_up": sum(1 for y in full if yrs.get(y, 0.0) > 0), "years_full": len(full),
                     "stress_cagr_pct": eb_hist.cagr(stress) * 100,
                     "switches": {k: v["switches"] for k, v in sl.items()},
                     "dsr": rg.deflated_sharpe(r, n_info["n"], HIST_VAR_PER_PERIOD),
                     "_daily": list(zip(ev, r))}
    b = out[rules.BASE]
    c = b["cash_version"]
    hd = res["comparators"]["hold_same_etfs"]
    crit = {
        "C1 CAGR > 0 and block-bootstrap 95% lower bound of Sharpe > 0": b["cagr_pct"] > 0 and b["sharpe_boot95"][0] > 0,
        "C2 CAGR > the same rule with idle cash (T-bills add net return)": b["cagr_pct"] > c["cagr_pct"],
        "C3 Sharpe >= the same rule with idle cash": b["sharpe"] >= c["sharpe"],
        f"C4 max drawdown <= {MAX_DD_PCT:.0f}%": b["max_drawdown_pct"] <= MAX_DD_PCT,
        f"C5 CAGR > the idle-cash version in both halves (split {HALF_SPLIT})": b["half1_cagr_pct"] > b["half1_cash_cagr_pct"]
        and b["half2_cagr_pct"] > b["half2_cash_cagr_pct"],
        f"C6 positive in >= {need} of {len(full)} full years": b["years_up"] >= need,
        "C7 CAGR > 0 at 10 bp/side": b["stress_cagr_pct"] > 0,
        "C8 Sharpe >= buy-and-hold of the same five ETFs": b["sharpe"] >= hd["sharpe"],
        "C9 max drawdown <= buy-and-hold's": b["max_drawdown_pct"] <= hd["max_drawdown_pct"],
        f"C10 deflated Sharpe >= {DSR_MIN} (necessary, not sufficient)": not math.isnan(b["dsr"]["dsr"]) and b["dsr"]["dsr"] >= DSR_MIN,
        "C11 both neighbours CAGR > their idle-cash versions": all(out[k]["cagr_pct"] > out[k]["cash_version"]["cagr_pct"]
                                                                 for k in rules.VARIANTS if k != rules.BASE),
    }
    if b["cagr_pct"] <= 0 or b["cagr_pct"] <= c["cagr_pct"]:
        verdict = "historically unsupported"
    elif all(crit.values()):
        verdict = "historically supported"
    else:
        verdict = "mixed"
    res.update(variants=out, criteria=crit, verdict=verdict,
               dollars=eb_hist.dollars([x for _, x in b["_daily"]], eb_hist.years(ev, [x for _, x in b["_daily"]])))
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
        raise Refused(f"registration tag {TAG} not found (docs/PREREG_sma_tbill_v1.md section 9)")
    if _git("merge-base", "--is-ancestor", TAG, "HEAD").returncode:
        raise Refused(f"{TAG} is not an ancestor of HEAD")
    if _git("status", "--porcelain", "--untracked-files=no").stdout.strip():
        raise Refused("tracked files have uncommitted changes: commit first so the run is reproducible")
    for p in (BIL_MANIFEST, BIL_FILE):
        rel = p.relative_to(REPO).as_posix()
        if _git("cat-file", "-e", f"{TAG}:{rel}").returncode:
            raise Refused(f"{rel} is not in the tagged commit: the BIL data must be committed before the tag")


def check_bil(manifest_text: str, file_bytes: bytes):
    m = json.loads(manifest_text)
    h = hashlib.sha256(file_bytes).hexdigest()
    if h != m.get("sha256"):
        raise Refused(f"BIL file SHA-256 {h[:12]}... differs from the manifest ({str(m.get('sha256'))[:12]}...)")
    return h


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
        tid, total = trials.log_trial(name, ",".join(rules.SYMBOLS) + "+BIL", "backtest_daily",
                                      {"sma_n": v["sma_n"], "idle": "BIL", "prereg": TAG,
                                       "cost_bp_per_side": rules.COST_BP_PER_SIDE, "base": v["base"]},
                                      START, END, "none", {"sharpe": v["sharpe"]}, oos, db=str(db),
                                      daily_returns=v["_daily"], batch=BATCH, data_hash=data_hash)
        done.append((name, tid, total))
    return done


def render(res) -> str:
    ni, cm = res["n_info"], res["comparators"]
    f = eb_hist._f
    L = [f"200-day filter, idle in BIL, SPY QQQ IWM GLD TLT, {res['window'][0]} .. {res['window'][1]}, {res['days']} days; "
         f"N = {ni['n']} ({ni['logged']} logged + {ni['backfilled']} backfilled + {ni['outside_db']} outside + "
         f"{ni['pending']} pending, floor {N_FLOOR})", "",
         f"{'':<32}{'Sharpe':>8}{'CAGR %':>8}{'maxDD %':>9}{'total %':>9}"]
    for k, v in cm.items():
        L.append(f"{k:<32}{v['sharpe']:>+8.2f}{v['cagr_pct']:>+8.1f}{v['max_drawdown_pct']:>9.1f}{v['total_pct']:>+9.0f}")
    L += ["", f"{'variant':<9}{'Sharpe':>8}{'95% low':>9}{'CAGR %':>8}{'maxDD %':>9}{'cash SR':>9}{'cash CAGR':>10}"
              f"{'h1 vs cash':>12}{'h2 vs cash':>12}{'@10bp':>8}"]
    for k, v in res["variants"].items():
        c = v["cash_version"]
        L.append(f"{k:<9}{v['sharpe']:>+8.2f}{v['sharpe_boot95'][0]:>+9.2f}{v['cagr_pct']:>+8.1f}{v['max_drawdown_pct']:>9.1f}"
                 f"{c['sharpe']:>+9.2f}{c['cagr_pct']:>+10.1f}{v['half1_cagr_pct'] - v['half1_cash_cagr_pct']:>+12.2f}"
                 f"{v['half2_cagr_pct'] - v['half2_cash_cagr_pct']:>+12.2f}{v['stress_cagr_pct']:>+8.1f}")
    b = res["variants"][rules.BASE]
    L += ["", f"{rules.BASE} (judged): {res['verdict'].upper()}"]
    L += [f"  {'PASS' if ok else 'FAIL'}  {c}" for c, ok in res["criteria"].items()]
    L.append(f"  switches per ETF: " + ", ".join(f"{s} {n}" for s, n in b["switches"].items())
             + f"; deflated Sharpe {f(b['dsr']['dsr'], 3, False)} (luck threshold {b['dsr']['sr0_annual']:.2f})")
    L.append("  years: " + ", ".join(f"{y} {x:+.1f}%" for y, x in b["years_pct"].items()
                                     if FIRST_FULL_YEAR <= y <= LAST_FULL_YEAR))
    d = res["dollars"]
    L += ["", "Dollars (base variant's history; averages, not a forecast):"]
    for cap in eb_hist.CAPITALS:
        x = d[f"{cap:g}"]
        L.append(f"  ${cap:>7,.0f}: {f(x['usd_per_trading_day'])} per trading day, {f(x['usd_per_year'])} per year, "
                 f"worst full year {f(x['worst_full_year_usd'])}, max drawdown {f(-x['max_drawdown_usd'])}")
    L.append("  capital needed for $2 per trading day at this average: "
             + ("not reachable (average <= 0)" if d["capital_for_target"] is None else f"${d['capital_for_target']:,.0f}"))
    p = res["pbo_reported_only"]
    L += ["", "PBO across the 3 variants (reported only): " + (f"{p['pbo']:.2f}" if "pbo" in p else str(p)),
          "", "No verdict here permits paper or live trading or overrides S2 NO-GO, the test plan, the Risk Controller "
              "or the Oct 22 decision (prereg section 8)."]
    return "\n".join(L)


def save(res, hashes) -> Path:
    RESULTS.mkdir(parents=True, exist_ok=True)
    p = RESULTS / f"sma_tbill_{datetime.now():%Y%m%d_%H%M%S}.json"
    clean = {**res, "variants": {k: {kk: vv for kk, vv in v.items() if not kk.startswith("_")}
                                 for k, v in res["variants"].items()},
             "data_sha256": hashes, "tag": TAG, "commit": _git("rev-parse", "HEAD").stdout.strip()}
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
        h_etf = eb_hist.sha256(eb_hist.DATA_FILE)
        if h_etf != eb_hist.DATA_SHA:
            raise Refused(f"ETF data SHA-256 {h_etf[:12]}... is not the registered file: refusing")
        h_bil = check_bil(BIL_MANIFEST.read_text(), BIL_FILE.read_bytes())
        n_info = trial_n()
        dates, data = eb_hist.load()
        bil = load_bil(dates)
        res = evaluate(dates, data, bil, n_info)
        print(render(res))
        hashes = {"etf": h_etf, "bil": h_bil}
        print(f"\nsaved {save(res, hashes)}\ndata SHA-256 ETF {h_etf}\ndata SHA-256 BIL {h_bil}")
        if a.log_trials:
            for name, tid, total in log_trials(res, f"etf:{h_etf[:16]}+bil:{h_bil[:16]}"):
                print(f"logged {name}: trial id {tid}, research_trials now {total}")
        return 0
    except (Refused, eb_hist.Refused) as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
