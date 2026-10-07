"""Tests for aether-research/us_momentum/strategy_truth.py (stage-1 strategy-truth runner).
Synthetic data and throwaway databases and git repos only: no network, nothing real is read or written.

Run from the repo root:  venv/bin/python3 -m pytest -q tests/test_strategy_truth.py
"""
import json
import math
import os
import pathlib
import sqlite3
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
os.environ.setdefault("AETHER_DIR", str(ROOT))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "aether-research"))
sys.path.insert(0, str(ROOT / "aether-research" / "us_momentum"))

import numpy as np
import pandas as pd
import pytest

import research_gate as rg
import strategy_truth as st
import us_strategy as us

UNI = us.UNIVERSE


# ---------------------------------------------------------------- synthetic world
def synthetic(n=2500, start="2006-02-01", seed0=1):
    dates = pd.bdate_range(start, periods=n)
    opens, closes = {}, {}
    for i, s in enumerate(UNI):
        r = np.random.default_rng(seed0 + i).normal(0.0003, 0.01, n)
        c = 100.0 * np.cumprod(1 + r)
        closes[s] = c
        opens[s] = c * (1 + np.random.default_rng(100 + i).normal(0, 0.001, n))
    idx = dates.strftime("%Y-%m-%d")
    return pd.DataFrame(opens, index=idx), pd.DataFrame(closes, index=idx)


def make_db(path, sharpes=(0.83, 0.58, 0.52, 0.14), backfilled=(20, 4)):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE research_trials (id INTEGER PRIMARY KEY, strategy TEXT, oos_sharpe REAL)")
    con.execute("CREATE TABLE untracked_trials (id INTEGER PRIMARY KEY, family TEXT, count INTEGER)")
    for i, s in enumerate(sharpes):
        con.execute("INSERT INTO research_trials (strategy, oos_sharpe) VALUES (?, ?)", (f"t{i}", s))
    for i, c in enumerate(backfilled):
        con.execute("INSERT INTO untracked_trials (family, count) VALUES (?, ?)", (f"f{i}", c))
    con.commit()
    con.close()


# ---------------------------------------------------------------- trial statistics
def test_trial_stats_counts_every_attempt(tmp_path):
    db = tmp_path / "r.sqlite"
    make_db(db)
    ts = st.trial_stats(db)
    assert ts["n_logged"] == 4 and ts["n_backfilled"] == 24 and ts["n_total"] == 28
    per = [x / math.sqrt(252) for x in (0.83, 0.58, 0.52, 0.14)]
    assert ts["var_per_period"] == pytest.approx(np.var(per, ddof=1))


def test_trial_stats_without_the_backfill_table_or_with_one_trial(tmp_path):
    db = tmp_path / "r.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE research_trials (id INTEGER PRIMARY KEY, strategy TEXT, oos_sharpe REAL)")
    con.execute("INSERT INTO research_trials (strategy, oos_sharpe) VALUES ('a', 0.7)")
    con.commit()
    con.close()
    ts = st.trial_stats(db)
    assert ts["n_total"] == 1 and ts["var_per_period"] == pytest.approx((0.5 / math.sqrt(252)) ** 2)


def test_required_sharpe_matches_the_deflated_sharpe_it_is_derived_from():
    var = (0.29 / math.sqrt(252)) ** 2
    need = st.required_sharpe(2400, 28, var, -0.3, 8.0)
    sr0 = rg.expected_max_sr(28, var)
    assert rg.prob_sr_above(need / math.sqrt(252), sr0, 2400, -0.3, 8.0) == pytest.approx(0.95, abs=1e-6)
    assert st.required_sharpe(4800, 28, var, -0.3, 8.0) < need < st.required_sharpe(1200, 28, var, -0.3, 8.0)
    assert st.required_sharpe(2400, 1, var, -0.3, 8.0) < need            # fewer attempts: an easier gate


# ---------------------------------------------------------------- round trips (S18, S19)
def test_round_trip_returns_by_hand():
    opens = pd.DataFrame({"A": [100.0, 110.0, 120.0, 90.0], "B": [50.0, 50.0, 55.0, 55.0]},
                         index=["2020-01-02", "2020-02-03", "2020-03-02", "2020-04-01"])
    closes = opens.copy()
    log = [{"date": "2020-01-02", "weights": {"A": 0.5, "B": 0.5}},
           {"date": "2020-02-03", "weights": {"B": 1.0}},                    # A exits: 100 -> 110
           {"date": "2020-03-02", "weights": {"A": 0.5, "B": 0.5}},          # A re-enters at 120
           {"date": "2020-04-01", "weights": {"B": 1.0}}]                    # A exits again: 120 -> 90
    out = st.round_trip_returns(log, opens, closes, slip_bps=2.0)
    assert len(out) == 2 and out[0] == pytest.approx(0.10 - 4e-4) and out[1] == pytest.approx(-0.25 - 4e-4)


def test_positions_still_open_are_not_counted():
    idx = ["2020-01-02", "2020-02-03"]
    df = pd.DataFrame({"A": [100.0, 120.0]}, index=idx)
    assert st.round_trip_returns([{"date": idx[0], "weights": {"A": 1.0}}, {"date": idx[1], "weights": {"A": 1.0}}],
                                 df, df) == []


# ---------------------------------------------------------------- regime slices (S12)
def test_slice_report_measures_each_slice_from_its_own_start():
    idx = pd.bdate_range("2019-12-02", "2021-01-29")
    v = np.full(len(idx), 100.0)
    v[idx >= "2020-02-20"] = 70.0                                  # a 30% fall inside 2020
    eq = pd.Series(v, index=idx)
    spy = pd.Series(np.linspace(100, 110, len(idx)), index=idx)
    rep = st.slice_report(eq, spy)
    assert rep["2020"]["testable"] and rep["2020"]["max_dd"] == pytest.approx(-0.30) and not rep["2020"]["within_limit"]
    assert rep["2008"] == {"testable": False}                       # no data that year
    rep2 = st.slice_report(eq, spy, dd_limit=0.35)
    assert rep2["2020"]["within_limit"]


# ---------------------------------------------------------------- small helpers
@pytest.mark.parametrize("passed,expect", [(True, "PASS"), (False, "FAIL"), (None, "INFO"), (np.bool_(True), "PASS"),
                                           (np.bool_(False), "FAIL"), (np.float64(0.1) > 0, "PASS"), (1, "PASS"), (0, "FAIL")])
def test_item_never_turns_a_verdict_into_info_by_accident(passed, expect):
    assert st.item(passed, "x")["result"] == expect


def test_beats_needs_a_higher_sharpe_or_a_higher_calmar():
    assert st.beats({"sharpe": 0.8, "calmar": 0.3}, {"sharpe": 0.7, "calmar": 0.5})
    assert st.beats({"sharpe": 0.6, "calmar": 0.6}, {"sharpe": 0.7, "calmar": 0.5})
    assert not st.beats({"sharpe": 0.6, "calmar": 0.4}, {"sharpe": 0.7, "calmar": 0.5})
    assert not st.beats({"sharpe": None, "calmar": None}, {"sharpe": 0.1, "calmar": 0.1})


def test_config_hash_changes_with_every_input():
    base = st.config_hash("d", "c", 2.0, 5000.0, 1)
    assert base == st.config_hash("d", "c", 2.0, 5000.0, 1)
    assert len({base, st.config_hash("d2", "c", 2.0, 5000.0, 1), st.config_hash("d", "c2", 2.0, 5000.0, 1),
                st.config_hash("d", "c", 4.0, 5000.0, 1), st.config_hash("d", "c", 2.0, 6000.0, 1),
                st.config_hash("d", "c", 2.0, 5000.0, 2)}) == 6


def test_log_run_is_idempotent_and_never_touches_the_trial_table(tmp_path):
    db = tmp_path / "r.sqlite"
    make_db(db)
    assert st.log_run(db, "p", "hash1", "d", "c", {"S2": "FAIL"}) is True
    assert st.log_run(db, "p", "hash1", "d", "c", {"S2": "FAIL"}) is False      # same configuration: not a new run
    assert st.log_run(db, "p", "hash2", "d", "c", {"S2": "FAIL"}) is True
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM research_runs").fetchone()[0] == 2
    assert con.execute("SELECT COUNT(*) FROM research_trials").fetchone()[0] == 4       # N is unchanged
    con.close()
    assert st.trial_stats(db)["n_total"] == 28


# ---------------------------------------------------------------- provenance checks (S1, S14)
def git(repo, *args):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", *args], cwd=repo, env=env, capture_output=True, text=True, check=True).stdout.strip()


def test_prereg_status_needs_the_tag(tmp_path):
    git(tmp_path, "init", "-q")
    (tmp_path / "a.txt").write_text("x")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "c")
    assert st.prereg_status(tmp_path, "prereg-x")["ok"] is False
    git(tmp_path, "tag", "prereg-x")
    res = st.prereg_status(tmp_path, "prereg-x")
    assert res["ok"] is True and "prereg-x" in res["detail"]


def test_s14_status_cases(tmp_path):
    caches = {"alpaca": tmp_path / "a.csv.gz", "yf": tmp_path / "y.csv.gz"}
    caches["alpaca"].write_bytes(b"alpaca-data")
    caches["yf"].write_bytes(b"yahoo-data")
    assert st.s14_status(tmp_path, caches)["result"] == "NOT RUN"
    rep = {"alpaca_sha256": st.sha256_file(caches["alpaca"])[:16], "yahoo_sha256": st.sha256_file(caches["yf"])[:16],
           "verdict": {"pass": True}, "returns": {"worst_median_bps": 1.5}, "signals": {"agreement": 0.97}}
    (tmp_path / "source_comparison.json").write_text(json.dumps(rep))
    assert st.s14_status(tmp_path, caches)["result"] == "PASS"
    rep["verdict"]["pass"] = False
    (tmp_path / "source_comparison.json").write_text(json.dumps(rep))
    assert st.s14_status(tmp_path, caches)["result"] == "FAIL"
    rep["verdict"]["pass"] = True
    (tmp_path / "source_comparison.json").write_text(json.dumps(rep))
    caches["yf"].write_bytes(b"yahoo-data-REFETCHED")                 # the cache changed after the comparison
    out = st.s14_status(tmp_path, caches)
    assert out["result"] == "FAIL" and "stale" in out["detail"]


# ---------------------------------------------------------------- the whole evaluation on a synthetic world
@pytest.fixture(scope="module")
def evaluated(tmp_path_factory):
    d = tmp_path_factory.mktemp("truth")
    db = d / "r.sqlite"
    make_db(db)
    opens, closes = synthetic()
    res = st.evaluate(opens, closes, db, boot_draws=300, mc_draws=300)
    res2 = st.evaluate(opens, closes, db, boot_draws=300, mc_draws=300)
    return res, res2, db


def test_every_plan_item_is_reported_and_stage_two_is_honest(evaluated):
    res, _, _ = evaluated
    it = res["items"]
    assert {"S2", "S4", "S5", "S9-close", "S10", "S11", "S12", "S13", "S16", "S17", "S18", "S19"} <= set(it)
    for k in ("S3", "S6", "S7", "S8", "S9-shift", "S15", "S20"):
        assert it[k]["result"] == "NOT RUN"
    assert it["S4"]["result"] == "NOT DUE" and it["S9-close"]["result"] == "INFO"
    assert all(v["result"] in {"PASS", "FAIL", "INFO", "NOT RUN", "NOT DUE"} for v in it.values())


def test_the_evaluation_is_reproducible_and_deterministic(evaluated):
    res, res2, _ = evaluated
    assert res["items"]["S16"]["result"] == "PASS"
    assert res["equity_sha256"] == res2["equity_sha256"]
    for k in ("S2", "S5", "S10", "S11", "S13"):
        assert res["items"][k] == res2["items"][k]


def test_s2_uses_the_full_trial_count_and_reports_the_gap(evaluated):
    res, _, _ = evaluated
    s2 = res["items"]["S2"]
    assert s2["n"] == 28 and res["trials"]["n_total"] == 28
    assert 0 <= s2["dsr"] <= 1 and s2["sharpe_needed"] > 0 and s2["years"] > 7
    assert s2["psr_if_n_is_1"] >= s2["dsr"]                                      # fewer attempts can only help
    assert ("PASS" if s2["dsr"] >= 0.95 else "FAIL") == s2["result"]


def test_results_are_internally_consistent(evaluated):
    res, _, _ = evaluated
    it = res["items"]
    assert it["S18"]["round_trips"] > 0 and it["S18"]["result"] == ("PASS" if it["S18"]["round_trips"] >= 100 else "FAIL")
    assert it["S11"]["p95"] >= it["S11"]["observed"] - 0.2 and 0 <= it["S11"]["p95"] <= 1
    assert it["S12"]["result"] in {"PASS", "FAIL", "INFO"} and "2008" in it["S12"]["slices"]
    assert it["S13"]["max_share"] > 0 and it["S5"]["stress"][3]["cagr"] <= it["S5"]["stress"][2]["cagr"] + 1e-9
    assert res["rebalances"] > 80 and res["baseline"]["days"] > 1500


def test_the_evaluation_does_not_write_to_the_trial_database(evaluated):
    _, _, db = evaluated
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM research_trials").fetchone()[0] == 4
    assert "research_runs" not in [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    con.close()


def test_render_lists_s1_s14_and_every_item(evaluated):
    res, _, _ = evaluated
    text = st.render(res, {"ok": True, "detail": "tag committed"}, {"result": "PASS", "detail": "ok"})
    for tag in ("S1 ", "S14", "S2 ", "S3 ", "S19", "NOT RUN"):
        assert tag in text


# ---------------------------------------------------------------- the individual verdicts, checked independently
import us_momentum_backtest as bt

REAL_SIM = bt.simulate
_SIM_CACHE = {}


@pytest.fixture
def fast_sim(monkeypatch):
    """Serve each distinct simulation from a cache: the verdict logic is under test here, not the simulator."""
    def sim(opens, closes, equity0=5000.0, slip_bps=2.0):
        key = (id(opens), id(closes), equity0, slip_bps)
        if key not in _SIM_CACHE:
            _SIM_CACHE[key] = REAL_SIM(opens, closes, equity0, slip_bps)
        eq, log = _SIM_CACHE[key]
        return eq.copy(), list(log)
    monkeypatch.setattr(bt, "simulate", sim)


@pytest.fixture(scope="module")
def small_world(tmp_path_factory):
    d = tmp_path_factory.mktemp("small")
    db = d / "r.sqlite"
    make_db(db)
    opens, closes = synthetic(n=1100, start="2016-01-04")
    return opens, closes, db


def run_small(small_world, **kw):
    opens, closes, db = small_world
    return st.evaluate(opens, closes, db, boot_draws=200, mc_draws=200, **kw)


def test_s10_s11_s13_match_an_independent_computation(small_world, fast_sim):
    opens, closes, db = small_world
    res = run_small(small_world)
    eq, _ = bt.simulate(opens, closes, 5000.0, 2.0)
    r = eq.pct_change().dropna().to_numpy()
    assert res["items"]["S10"]["p5"] == pytest.approx(np.percentile(rg.block_bootstrap_sharpes(r, 21, 200, st.SEED), 5))
    monthly = eq.resample("ME").last().pct_change().dropna().to_numpy()
    assert res["items"]["S11"]["p95"] == pytest.approx(np.percentile(rg.mc_max_drawdown(monthly, 200, st.SEED), 95))
    assert res["items"]["S13"]["max_share"] == pytest.approx(rg.max_year_share(pd.Series(r, index=eq.index[1:])))


def test_a_short_sample_cannot_claim_the_regime_slices(small_world, fast_sim):
    s12 = run_small(small_world)["items"]["S12"]
    assert s12["result"] == "INFO" and s12["slices"]["2008"]["testable"] is False and "not testable" in s12["detail"]


def test_s16_catches_a_non_reproducible_simulation(small_world, monkeypatch):
    calls = {"n": 0}

    def flaky(*a, **k):
        eq, log = REAL_SIM(*a, **k)
        calls["n"] += 1
        return eq * (1 + 1e-9 * calls["n"]), log
    monkeypatch.setattr(bt, "simulate", flaky)
    assert run_small(small_world)["items"]["S16"]["result"] == "FAIL"


@pytest.mark.parametrize("trades,expect", [(99, "FAIL"), (100, "PASS")])
def test_s18_threshold(small_world, fast_sim, monkeypatch, trades, expect):
    monkeypatch.setattr(st, "round_trip_returns", lambda *a, **k: [0.01] * trades)
    assert run_small(small_world)["items"]["S18"]["result"] == expect


@pytest.mark.parametrize("freq,expect,amend", [(0.20, "PASS", False), (0.21, "FAIL", True)])
def test_s19_flags_the_profit_factor_rule_for_amendment(small_world, fast_sim, monkeypatch, freq, expect, amend):
    monkeypatch.setattr(st, "round_trip_returns", lambda *a, **k: [0.01, -0.01] * 50)
    monkeypatch.setattr(rg, "pf_below_one_frequency", lambda *a, **k: freq)
    it = run_small(small_world)["items"]["S19"]
    assert it["result"] == expect and ("AMEND" in it["detail"]) is amend


def test_s19_with_too_few_trades_is_not_a_verdict(small_world, fast_sim, monkeypatch):
    monkeypatch.setattr(st, "round_trip_returns", lambda *a, **k: [0.01] * 5)
    assert run_small(small_world)["items"]["S19"]["result"] == "INFO"


@pytest.mark.parametrize("answers,expect", [([True, True], "PASS"), ([True, False], "FAIL"), ([False, True], "FAIL")])
def test_s17_must_beat_both_benchmarks(small_world, fast_sim, monkeypatch, answers, expect):
    it = iter(answers)
    monkeypatch.setattr(st, "beats", lambda a, b: next(it))
    assert run_small(small_world)["items"]["S17"]["result"] == expect


@pytest.mark.parametrize("dsr,expect", [(0.95, "PASS"), (0.9499, "FAIL")])
def test_s2_follows_the_deflated_sharpe_threshold(small_world, fast_sim, monkeypatch, dsr, expect):
    monkeypatch.setattr(rg, "deflated_sharpe", lambda r, n, v, periods=252: {"dsr": dsr, "psr": 0.99, "sr_annual": 1.0,
                                                                         "sr0_annual": 0.5, "t": 10})
    assert run_small(small_world)["items"]["S2"]["result"] == expect


@pytest.mark.parametrize("share,expect", [(0.40, "PASS"), (0.41, "FAIL"), (float("nan"), "FAIL")])
def test_s13_threshold(small_world, fast_sim, monkeypatch, share, expect):
    monkeypatch.setattr(rg, "max_year_share", lambda r: share)
    assert run_small(small_world)["items"]["S13"]["result"] == expect


@pytest.mark.parametrize("dd,expect", [(0.27, "PASS"), (0.28, "FAIL")])
def test_s11_threshold(small_world, fast_sim, monkeypatch, dd, expect):
    monkeypatch.setattr(rg, "mc_max_drawdown", lambda r, draws, seed: np.full(50, dd))
    assert run_small(small_world)["items"]["S11"]["result"] == expect


@pytest.mark.parametrize("sharpes,expect", [(1.0, "PASS"), (0.0, "FAIL"), (-1.0, "FAIL")])
def test_s10_needs_a_positive_lower_tail(small_world, fast_sim, monkeypatch, sharpes, expect):
    monkeypatch.setattr(rg, "block_bootstrap_sharpes", lambda r, block, draws, seed: np.full(50, sharpes))
    assert run_small(small_world)["items"]["S10"]["result"] == expect


def test_s12_fails_when_a_testable_slice_breaches_the_limit(small_world, fast_sim, monkeypatch):
    ok = {k: {"testable": True, "return": 0.1, "max_dd": -0.1, "spy_max_dd": -0.2, "within_limit": True} for k in st.SLICES}
    monkeypatch.setattr(st, "slice_report", lambda eq, spy, slices=st.SLICES, dd_limit=0.27: ok)
    assert run_small(small_world)["items"]["S12"]["result"] == "PASS"
    bad = {**ok, "2020": {**ok["2020"], "max_dd": -0.4, "within_limit": False}}
    monkeypatch.setattr(st, "slice_report", lambda eq, spy, slices=st.SLICES, dd_limit=0.27: bad)
    it = run_small(small_world)["items"]["S12"]
    assert it["result"] == "FAIL" and "2020" in it["detail"]


def test_s5_needs_a_positive_return_at_double_the_costs(small_world, monkeypatch):
    def costly(opens, closes, equity0=5000.0, slip_bps=2.0):
        eq, log = REAL_SIM(opens, closes, equity0, slip_bps)
        if slip_bps > 3.5:                                   # the x2 run (4 bps) and the x3 run (6 bps)
            eq = pd.Series(eq.iloc[0] * np.linspace(1.0, 0.8, len(eq)), index=eq.index)   # a steady loss
        return eq, log
    monkeypatch.setattr(bt, "simulate", costly)
    assert run_small(small_world)["items"]["S5"]["result"] == "FAIL"


def test_s9_close_really_trades_at_the_close(small_world, fast_sim):
    opens, closes, db = small_world
    it = run_small(small_world)["items"]["S9-close"]
    assert it["close_metrics"] == bt.metrics(bt.simulate(closes, closes, 5000.0, 2.0)[0])
    assert it["close_metrics"] != bt.metrics(bt.simulate(opens, closes, 5000.0, 2.0)[0])


def test_s5_applies_doubled_and_tripled_costs(small_world, fast_sim):
    opens, closes, db = small_world
    st_ = run_small(small_world)["items"]["S5"]["stress"]
    base = bt.metrics(bt.simulate(opens, closes, 5000.0, 2.0)[0])["cagr"]
    assert st_[3]["cagr"] < st_[2]["cagr"] < base
    assert st_[2]["cagr"] == bt.metrics(bt.simulate(opens, closes, 5000.0, 4.0)[0])["cagr"]
    assert st_[3]["cagr"] == bt.metrics(bt.simulate(opens, closes, 5000.0, 6.0)[0])["cagr"]


def test_s2_value_is_the_deflated_sharpe_for_all_28_attempts(small_world, fast_sim):
    opens, closes, db = small_world
    s2 = run_small(small_world)["items"]["S2"]
    eq, _ = bt.simulate(opens, closes, 5000.0, 2.0)
    r = eq.pct_change().dropna().to_numpy()
    ts = st.trial_stats(db)
    expected = rg.deflated_sharpe(r, 28, ts["var_per_period"])
    assert s2["dsr"] == pytest.approx(expected["dsr"]) and s2["sr0"] == pytest.approx(expected["sr0_annual"])
    assert s2["dsr"] < s2["psr_if_n_is_1"]                      # 28 attempts must lower it, strictly
    assert s2["psr_if_n_is_1"] == pytest.approx(rg.deflated_sharpe(r, 1, ts["var_per_period"])["psr"])
