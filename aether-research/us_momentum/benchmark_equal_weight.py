#!/usr/bin/env python3
"""S2 benchmark: does the momentum TIMING add anything over holding the same ETFs passively?

Benchmark, not a candidate strategy: nothing is selected from it, so it is NOT logged as a trial and
N stays 28 (docs/PREREG_us_momentum.md section 4). It does not change any S2 result.

Passive rule: the same 11 ETFs as us_strategy.UNIVERSE (SHY included), equal weight (1/11 each),
rebalanced on the same first-trading-day-of-month schedule, through the SAME simulator
(us_momentum_backtest.simulate: same execution at the open, same 2 bp/side slippage, same plan_orders
sizing, same adjusted data). It starts on the same day as the strategy because it only trades when the
strategy's own rule could (enough history for us_strategy.target_weights), so the windows are identical.

  python benchmark_equal_weight.py            # long Yahoo history, the S2 primary sample
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import json
import math

import numpy as np

import strategy_truth as st
from strategy_truth import bt, rg, us


@contextlib.contextmanager
def equal_weight_rule():
    original = us.target_weights

    def passive(hist):
        _, scores, _ = original(hist)            # same availability rule (raises/None exactly as S2 would)
        w = {s: 1.0 / len(us.UNIVERSE) for s in us.UNIVERSE}
        return w, scores, sorted(us.UNIVERSE)

    us.target_weights = passive
    try:
        yield
    finally:
        us.target_weights = original


def profile(eq, log, opens, closes, slip_bps, seed, mc_draws, boot_draws):
    r = eq.pct_change().dropna()
    m = bt.metrics(eq)
    monthly = eq.resample("ME").last().pct_change().dropna()
    mdd = rg.mc_max_drawdown(monthly.to_numpy(), mc_draws, seed)
    boot = rg.block_bootstrap_sharpes(r.to_numpy(), st.BLOCK, boot_draws, seed)
    stress = {}
    for mult in (2, 3):
        e, _ = bt.simulate(opens, closes, 5000.0, slip_bps * mult)
        stress[f"x{mult}"] = {k: bt.metrics(e)[k] for k in ("cagr", "sharpe")}
    turn = [x["turnover"] for x in log]
    return {**m, "mc_dd_p95": round(float(np.percentile(mdd, 95)), 4),
            "boot_sharpe_p5": round(float(np.percentile(boot, 5)), 3),
            "avg_monthly_turnover": round(float(np.mean(turn)), 4) if turn else None,
            "annual_turnover": round(float(np.mean(turn)) * 12, 3) if turn else None,
            "rebalances": len(log), "cost_stress": stress}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default="yf", choices=list(st.CACHES))
    ap.add_argument("--mc", type=int, default=st.MC_DRAWS)
    ap.add_argument("--boot", type=int, default=st.BOOT_DRAWS)
    a = ap.parse_args()
    path = st.CACHES[a.cache]
    opens, closes = st.load_cache(path)
    slip = 2.0

    eq_s, log_s = bt.simulate(opens, closes, 5000.0, slip)
    with equal_weight_rule():
        eq_p, log_p = bt.simulate(opens, closes, 5000.0, slip)
        prof_p = profile(eq_p, log_p, opens, closes, slip, st.SEED, a.mc, a.boot)
    prof_s = profile(eq_s, log_s, opens, closes, slip, st.SEED, a.mc, a.boot)
    assert (eq_s.index[0], eq_s.index[-1]) == (eq_p.index[0], eq_p.index[-1]), "windows differ"

    # Timing value: the daily return difference (strategy minus passive).
    rs, rp = eq_s.pct_change().dropna(), eq_p.pct_change().dropna()
    active = (rs - rp).dropna()
    years = len(active) / 252
    ir = float(active.mean() / active.std() * math.sqrt(252)) if active.std() > 0 else None
    diff = {
        "cagr_diff": round(prof_s["cagr"] - prof_p["cagr"], 4),
        "sharpe_diff": round(prof_s["sharpe"] - prof_p["sharpe"], 3),
        "max_dd_diff": round(prof_s["max_dd"] - prof_p["max_dd"], 4),
        "mc_dd_p95_diff": round(prof_s["mc_dd_p95"] - prof_p["mc_dd_p95"], 4),
        "calmar_diff": round(prof_s["calmar"] - prof_p["calmar"], 3),
        "active_return_annual": round(float(active.mean() * 252), 4),
        "tracking_error": round(float(active.std() * math.sqrt(252)), 4),
        "information_ratio": None if ir is None else round(ir, 3),
        "ir_t_stat": None if ir is None else round(ir * math.sqrt(years), 2),
        "years": round(years, 1),
    }
    out = {
        "what": "S2 benchmark: momentum timing vs passive equal-weight of the same 11 ETFs (not a trial; N unchanged)",
        "run_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "data_sha256": st.sha256_file(path), "code_sha256": st.code_fingerprint(), "cache": a.cache,
        "seed": st.SEED, "slip_bps": slip, "strategy": prof_s, "passive_equal_weight": prof_p, "timing_value": diff,
    }
    st.RESULTS.mkdir(exist_ok=True)
    p = st.RESULTS / f"benchmark_equal_weight_{a.cache}_{datetime.datetime.now():%Y%m%d_%H%M%S}.json"
    p.write_text(json.dumps(out, indent=2, default=str))
    keys = ("cagr", "vol", "sharpe", "max_dd", "calmar", "mc_dd_p95", "boot_sharpe_p5", "annual_turnover")
    print(f"{'':16}{'strategy':>12}{'passive EW':>12}")
    for k in keys:
        print(f"{k:16}{prof_s[k]!s:>12}{prof_p[k]!s:>12}")
    print(json.dumps(diff, indent=2))
    print(f"window {prof_s['start']} -> {prof_s['end']} | data {out['data_sha256'][:16]} | written {p}")


if __name__ == "__main__":
    main()
