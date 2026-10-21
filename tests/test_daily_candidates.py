"""Tests for aether-research/us_momentum/daily_candidates.py (runner for docs/PREREG_daily_v1.md).
Synthetic data, throwaway databases and throwaway git repos only: nothing real is read or written.

Run from the repo root:  .venv/bin/python -m pytest -q tests/test_daily_candidates.py
"""
import math
import pathlib
import sqlite3
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "aether-research"))
sys.path.insert(0, str(ROOT / "aether-research" / "us_momentum"))

import numpy as np
import pandas as pd
import pytest

import daily_candidates as dc
import trials

SYMS = ["SPY", "QQQ", "IWM", "EFA", "TLT", "GLD", "IEF", "SHY"]


def synth(start="2005-01-03", end="2012-12-31", seed=1):
    idx = pd.bdate_range(start, end)
    rng = np.random.default_rng(seed)
    rows = []
    for k, s in enumerate(SYMS):
        drift, vol = (0.0001 if s != "SHY" else 0.00002), (0.01 if s != "SHY" else 0.0005)
        close = 100 * np.cumprod(1 + rng.normal(drift, vol, len(idx)))
        open_ = np.concatenate(([close[0]], close[:-1])) * (1 + rng.normal(0, 0.002, len(idx)))
        rows += [(s, d.strftime("%Y-%m-%d"), o, c) for d, o, c in zip(idx, open_, close)]
    return pd.DataFrame(rows, columns=["symbol", "date", "open", "close"])


@pytest.fixture
def data(tmp_path):
    p = tmp_path / "d.csv.gz"
    synth().to_csv(p, index=False, compression="gzip")
    o, c = dc.load_data(p, end="2012-12-31")
    return p, o, c


# ------------------------------------------------------------------ signals
def test_trend_state_above_equal_and_confirmation():
    up = pd.Series(np.arange(1.0, 301.0))
    st = dc.trend_state(up, 200, 1)
    assert not st[:199].any() and st[199:].all()                       # warm-up OFF, then ON
    flat = pd.Series(np.full(300, 50.0))
    assert not dc.trend_state(flat, 200, 1).any()                      # equal to the average is not above
    # a one-day dip below the average does not flip a 3-day-confirmation state, but flips a 1-day one
    s = up.copy()
    s.iloc[250] = 1.0
    assert not dc.trend_state(s, 200, 1)[250] and dc.trend_state(s, 200, 3)[250]
    for k in (251, 252):                                               # three closes below: flips on the third
        s.iloc[k] = 1.0
    st3 = dc.trend_state(s, 200, 3)
    assert st3[251] and not st3[252]


def test_wilder_rsi_hand_values():
    r = dc.wilder_rsi(pd.Series([10.0, 11.0, 10.0, 9.0]), 2)
    assert np.isnan(r[0]) and np.isnan(r[1])
    assert r[2] == pytest.approx(50.0)                                 # avg gain .5, avg loss .5
    assert r[3] == pytest.approx(25.0)                                 # avg gain .25, avg loss .75
    assert dc.wilder_rsi(pd.Series([1.0, 2.0, 3.0, 4.0]), 2)[3] == 100.0


def _sb_series(tail):
    base = np.linspace(100, 400, 320)
    return pd.Series(np.concatenate([base, tail]))


def test_sb_time_stop_holds_exactly_five_sessions():
    close = _sb_series(400 * 0.99 ** np.arange(1, 16))               # steady fall, stays above SMA200
    on = dc.sb_target_on(close, 250, rsi_below=10, exit_sma=5)
    first = int(np.argmax(on))
    run = 0
    while on[first + run]:
        run += 1
    assert run == 5 and not on[first + 5]
    assert first == 320 + 2                                            # RSI(2)<10 after 2 down closes: decided at close 321


def test_sb_exits_when_close_above_exit_average():
    close = _sb_series(np.array([396.0, 392.0, 388.0, 450.0, 451.0, 452.0, 453.0]))
    on = dc.sb_target_on(close, 250, rsi_below=10, exit_sma=5)
    first = int(np.argmax(on))
    assert first == 322 and on[322] and on[323]                        # close 323 is above SMA5: exit at the next open
    assert not on[324] and not on[325:].any()


def test_sc_windows_last_day_to_fourth_trading_day():
    idx = pd.bdate_range("2020-01-01", "2020-04-30")
    w = dc.sc_windows(idx, 0, 0, 4)
    e, x = w[0]
    assert idx[e] == pd.Timestamp("2020-01-31") and idx[x] == pd.Timestamp("2020-02-06")
    on = dc.sc_target_on(idx, 0, 0, 4)
    held = [d.strftime("%m-%d") for d in idx[on]][:4]
    assert held == ["01-31", "02-03", "02-04", "02-05"]               # four intervals; exit session is OFF
    assert not on[x]
    assert [idx[a] for a, _ in dc.sc_windows(idx, 0, -1, 3)][0] == pd.Timestamp("2020-01-30")
    assert [idx[a] for a, _ in dc.sc_windows(idx, 0, 1, 3)][0] == pd.Timestamp("2020-02-03")


# ------------------------------------------------------------------ simulator
def _flat_arrays(T=6, K=1):
    rng = np.random.default_rng(3)
    op = 100 * np.cumprod(1 + rng.normal(0, 0.01, (T, 2 + K)), axis=0)
    return op


def test_simulate_costs_and_zero_cost_identity(monkeypatch):
    op = _flat_arrays(6, 0)
    col = {"A": 0, "B": 1}
    on = np.ones((6, 1), bool)
    sim, _ = dc.simulate(op, col, [("A", "B")], [1.0], on, np.zeros(6, bool), 0, [0])
    first = (1 - (dc.SLIP + dc.FEE)) * (1 - dc.SLIP) * op[1, 0] / op[0, 0] - 1     # B -> A flip on day 0
    assert sim["net"].iloc[0] == pytest.approx(first)
    assert sim["cost"].iloc[0] == pytest.approx(1 - (1 - (dc.SLIP + dc.FEE)) * (1 - dc.SLIP))
    assert sim["cost"].iloc[1:].eq(0).all()
    monkeypatch.setattr(dc, "SLIP", 0.0)
    monkeypatch.setattr(dc, "FEE", 0.0)
    sim0, _ = dc.simulate(op, col, [("A", None)], [1.0], on, np.zeros(6, bool), 0, [-1])
    assert np.prod(1 + sim0["net"]) == pytest.approx(op[5, 0] / op[0, 0])


def test_simulate_daily_reset_equals_weighted_returns(monkeypatch):
    monkeypatch.setattr(dc, "SLIP", 0.0)
    monkeypatch.setattr(dc, "FEE", 0.0)
    op = _flat_arrays(8, 0)
    col = {"A": 0, "B": 1}
    sim, _ = dc.simulate(op, col, [("A", None), ("B", None)], [0.6, 0.4], np.ones((8, 2), bool),
                         np.ones(8, bool), 0, [-1, -1])
    ra, rb = op[1:, 0] / op[:-1, 0] - 1, op[1:, 1] / op[:-1, 1] - 1
    assert sim["net"].to_numpy() == pytest.approx(0.6 * ra + 0.4 * rb)


def test_reset_costs_only_the_amount_moved():
    op = _flat_arrays(5, 0)
    col = {"A": 0, "B": 1}
    reset = np.array([False, True, False, False, False])
    sim, _ = dc.simulate(op, col, [("A", None), ("B", None)], [0.5, 0.5], np.ones((5, 2), bool), reset, 0, [1, 1])
    assert sim["cost"].iloc[0] == 0 and sim["cost"].iloc[1] > 0 and sim["cost"].iloc[2] == 0


# ------------------------------------------------------------------ data guards
def test_load_data_never_reads_past_the_end(tmp_path):
    p = tmp_path / "d.csv.gz"
    synth("2005-01-03", "2013-06-28").to_csv(p, index=False, compression="gzip")
    o, c = dc.load_data(p, end="2012-12-31")
    assert c.index.max() <= pd.Timestamp("2012-12-31") and o.index.max() == c.index.max()


def test_missing_bar_in_window_is_a_defect_not_filled(data):
    _, o, c = data
    c2 = c.copy()
    c2.loc[c2.index[500], "TLT"] = np.nan
    with pytest.raises(dc.Refused):
        dc.check_window(o, c2, ["TLT"], "2006-06-01")


def test_check_data_rejects_wrong_fingerprint(tmp_path):
    p = tmp_path / "d.csv.gz"
    p.write_bytes(b"not the registered data")
    with pytest.raises(dc.Refused):
        dc.check_data(p)


def _repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        subprocess.run(["git", "-C", str(tmp_path), "config", k, v], check=True)
    (tmp_path / "a").write_text("1")
    subprocess.run(["git", "-C", str(tmp_path), "add", "a"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "c1"], check=True)
    return tmp_path


def test_registration_guards(tmp_path):
    repo = _repo(tmp_path)
    with pytest.raises(dc.Refused):
        dc.registration_commit(repo)                                  # no tag yet
    subprocess.run(["git", "-C", str(repo), "tag", dc.TAG], check=True)
    c = dc.registration_commit(repo)
    assert len(c) == 40
    (repo / "a").write_text("2")                                      # dirty tracked file
    with pytest.raises(dc.Refused):
        dc.require_clean(repo)
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "--orphan", "other"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qam", "x"], check=True)
    with pytest.raises(dc.Refused):
        dc.registration_commit(repo)                                  # tag not an ancestor of HEAD


def test_run_is_locked_until_the_amendment(capsys):
    assert dc.RUN_LOCKED is True
    assert dc.main(["run"]) == 2
    assert "locked" in capsys.readouterr().err


def test_inventory_lists_every_logged_and_backfilled_attempt(tmp_path):
    inv = dc.trial_inventory(_db(tmp_path))
    assert len(inv["logged"]) == 4 and sum(b["count"] for b in inv["backfilled"]) == 24


def test_holdout_is_always_refused(capsys):
    assert dc.main(["holdout"]) == 2
    assert "stays closed" in capsys.readouterr().err


# ------------------------------------------------------------------ trial counting
def _db(tmp_path, logged=4, back=24):
    db = str(tmp_path / "r.sqlite")
    trials._connect(db).close()
    for i in range(logged):
        trials.log_trial(f"old{i}", "X", "bt", {}, "a", "b", "c", {"sharpe": 0.5 + i * 0.1},
                         {"sharpe": 0.5 + i * 0.1, "days": 500, "daily_skew": 0.0, "daily_kurtosis": 3.0}, db=db)
    if back:
        trials.log_untracked("past", back, "n", db=db)
    return db


def test_trial_count_does_not_reset_history(tmp_path):
    db = _db(tmp_path)
    n = dc.trial_counts(db, {"S-A": 0.5, "S-B": 0.4, "S-C": 0.3})
    assert n["n_total"] == 31 and n["n_pending"] == 3 and n["n_logged"] == 4 and n["n_backfilled"] == 24
    assert n["var_per_period"] > 0


def test_trial_count_floor_and_extra_history(tmp_path):
    assert dc.trial_counts(_db(tmp_path, logged=1, back=0), {})["n_total"] == 31
    sub = tmp_path / "sub"
    sub.mkdir()
    assert dc.trial_counts(_db(sub, logged=4, back=30), {"S-A": 0.5})["n_total"] == 35   # 4 + 30 + 1 pending


def test_trial_count_refuses_without_database(tmp_path):
    with pytest.raises(dc.Refused):
        dc.trial_counts(str(tmp_path / "missing.sqlite"), {})


# ------------------------------------------------------------------ criteria and verdicts
def test_insufficient_is_not_pass_or_fail():
    assert dc.crit(True, "x", sufficient=False)["result"] == dc.INSUFFICIENT
    assert dc.crit(False, "x", sufficient=False)["result"] == dc.INSUFFICIENT
    assert dc.crit(True, "x", True, float("nan"))["result"] == dc.INSUFFICIENT
    assert dc.crit(False, "x")["result"] == dc.FAIL and dc.crit(True, "x")["result"] == dc.PASS


def test_verdict_rules():
    P, F, I = dc.PASS, dc.FAIL, dc.INSUFFICIENT
    mk = lambda *r: {f"{i + 1} c": {"result": x} for i, x in enumerate(r)}
    assert dc.verdict(mk(P, P, P, P, P)) == "WORTH PAPER-TESTING"
    assert dc.verdict(mk(P, I, P, P, P)) == "INCONCLUSIVE"
    assert dc.verdict(mk(P, P, P, P, I)) == "INCONCLUSIVE"            # criterion 5 insufficient: never a pass
    assert dc.verdict(mk(P, F, P, P, P)) == "UNSUPPORTED"
    assert dc.verdict(mk(I, F, P, P, P)) == "UNSUPPORTED"             # a FAIL is never rescued by INSUFFICIENT


def test_unknown_or_missing_status_fails_closed():
    ok = {f"{i} c": {"result": dc.PASS} for i in (1, 2, 3, 4, 5)}
    assert dc.verdict(ok) == "WORTH PAPER-TESTING"
    assert dc.verdict({**ok, "2 c": {"result": "MAYBE"}}) == "INCONCLUSIVE"
    assert dc.verdict({**ok, "2 c": {"result": None}}) == "INCONCLUSIVE"
    assert dc.verdict({**ok, "2 c": {}}) == "INCONCLUSIVE"
    assert dc.verdict({**ok, "F-A x": {"result": "pass"}}) == "INCONCLUSIVE"      # lower case is not PASS


def test_open_position_at_the_end_is_not_a_completed_round_trip():
    h = np.array([[0], [1], [1], [0], [1], [1]], bool)       # one completed run, one still open at the end
    assert dc.count_runs(h) == 1
    assert dc.count_runs(np.array([[1], [1], [1]], bool)) == 0
    assert dc.count_runs(np.array([[1, 0], [0, 1], [1, 1], [0, 0]], bool)) == 3   # sleeves counted separately


def test_inventory_is_enforced_in_the_count(tmp_path):
    f = tmp_path / "inv.md"
    with pytest.raises(dc.Refused):
        dc.read_inventory(f)                                  # file missing
    f.write_text("no number here")
    with pytest.raises(dc.Refused):
        dc.read_inventory(f)                                  # line missing
    f.write_text("# inventory\nN_inventory: 40\n")
    assert dc.read_inventory(f) == 40
    db = _db(tmp_path)
    assert dc.trial_counts(db, {"S-A": .5, "S-B": .4, "S-C": .3})["n_total"] == 31
    n = dc.trial_counts(db, {"S-A": .5, "S-B": .4, "S-C": .3}, inventory=40)
    assert n["n_total"] == 43 and n["n_inventory"] == 40       # inventory + the three candidates
    assert dc.trial_counts(db, {}, inventory=10)["n_total"] == 31   # a small inventory never lowers N


def test_trial_variance_is_never_below_the_registered_history(tmp_path):
    """Amendment 2: the database that held the old Sharpes is gone; the stage-1 variance is the floor."""
    empty = str(tmp_path / "e.sqlite")
    trials._connect(empty).close()
    assert dc.trial_counts(empty, {})["var_per_period"] == dc.HIST_VAR_PER_PERIOD      # no rows: not the 0.5 default
    tight = dc.trial_counts(empty, {"S-A": 0.50, "S-B": 0.50, "S-C": 0.50})["var_per_period"]
    assert tight == dc.HIST_VAR_PER_PERIOD                                              # a tight cluster cannot lower it
    wide = dc.trial_counts(empty, {"S-A": -1.0, "S-B": 0.5, "S-C": 2.0})["var_per_period"]
    assert wide > dc.HIST_VAR_PER_PERIOD                                                # a wide spread can raise it
    assert abs(math.sqrt(dc.HIST_VAR_PER_PERIOD) * math.sqrt(252) - 0.286) < 0.001      # annual SD 0.29, as recorded


def test_stored_variance_reproduces_the_recorded_s2_threshold():
    """Provenance check against the committed stage-1 evidence: V_hist with N=28 gives the recorded luck threshold and
    DSR; the old 0.5 default does not (it is much harsher)."""
    import json
    ev = json.loads((ROOT / "aether-research/us_momentum/results/strategy_truth_yf_20261007_074638.json").read_text())
    assert ev["trials"]["var_per_period"] == dc.HIST_VAR_PER_PERIOD and ev["trials"]["n_total"] == 28
    b = ev["baseline"]
    sr = b["sharpe"] / math.sqrt(252)
    sr0 = dc.rg.expected_max_sr(28, dc.HIST_VAR_PER_PERIOD)
    assert sr0 * math.sqrt(252) == pytest.approx(ev["items"]["S2"]["sr0"], abs=1e-6)     # recorded threshold is annualised
    dsr = dc.rg.prob_sr_above(sr, sr0, b["days"], b["daily_skew"], b["daily_kurtosis"])
    assert dsr == pytest.approx(ev["items"]["S2"]["dsr"], abs=2e-3)
    sr0_default = dc.rg.expected_max_sr(28, dc.DEFAULT_VAR_PER_PERIOD)
    assert dc.rg.prob_sr_above(sr, sr0_default, b["days"], b["daily_skew"], b["daily_kurtosis"]) < 0.3


def _full(c1=dc.PASS, others=dc.PASS):
    d = {f"{i} c": {"result": others} for i in (2, 3, 4, 5)}
    return {"1 DSR >= 0.95": {"result": c1}, **d, "F x": {"result": others}}


def test_sensitivity_flags_a_pass_that_a_stress_scenario_would_fail():
    crit = _full(dc.PASS)
    f = dc.sensitivity_flags(crit, {"old": 0.40, "n_doubled": 0.97})
    assert dc.verdict(crit) == "WORTH PAPER-TESTING"
    assert f["old"]["sensitive"] and f["old"]["criterion_1"] == dc.FAIL and f["old"]["verdict"] == "UNSUPPORTED"
    assert f["old"]["criterion_1_changes"] and f["old"]["verdict_changes"]
    assert not f["n_doubled"]["sensitive"]                      # still passes under the stress scenario


def test_sensitivity_flags_a_fail_that_would_pass_and_keeps_insufficient():
    f = dc.sensitivity_flags(_full(dc.FAIL), {"old": 0.99})
    assert f["old"]["criterion_1"] == dc.PASS and f["old"]["criterion_1_changes"] and f["old"]["verdict_changes"]
    g = dc.sensitivity_flags(_full(dc.INSUFFICIENT), {"old": 0.99, "bad": float("nan")})
    assert g["old"]["criterion_1"] == dc.INSUFFICIENT and not g["old"]["sensitive"]
    h = dc.sensitivity_flags(_full(dc.PASS), {"nan": float("nan")})
    assert h["nan"]["criterion_1"] == dc.INSUFFICIENT and h["nan"]["sensitive"]


def test_flag_shows_even_while_criterion_5_is_missing():
    four = {f"{i} c": {"result": dc.PASS} for i in (1, 2, 3, 4)}
    f = dc.sensitivity_flags(four, {"old": 0.2})
    assert f["old"]["criterion_1_changes"] and f["old"]["sensitive"]      # criterion 1 flips even though the verdict cannot


def test_logging_the_candidates_never_changes_n(tmp_path):
    """Regression: with N_inventory = 40, N was 43 before the candidates were logged and 40 after."""
    db = _db(tmp_path)
    pend = {"S-A": .5, "S-B": .4, "S-C": .3}
    before = dc.trial_counts(db, pend, inventory=40)["n_total"]
    for k, n in enumerate(dc.NAMES):
        trials.log_trial(f"daily_v1_{n}", "X", "bt", {}, "a", "b", "c", {"sharpe": .5},
                         {"sharpe": .5, "days": 500, "daily_skew": 0.0, "daily_kurtosis": 3.0}, db=db)
        mid = dc.trial_counts(db, {m: pend[m] for m in dc.NAMES[k + 1:]}, inventory=40)["n_total"]
        assert mid == before == 43                              # one, two, three candidates logged: unchanged
    assert dc.trial_counts(db, {}, inventory=40)["n_total"] == 43
    for inv in (0, 10, 28, 40, 60):                             # same invariant without the inventory term
        a = dc.trial_counts(db, {}, inventory=inv)["n_total"]
        assert a == max(31, 4 + 24 + 3, inv + 3)


def test_missing_criterion_five_can_never_be_worth_paper_testing():
    P = dc.PASS
    four = {f"{i} c": {"result": P} for i in (1, 2, 3, 4)}
    assert dc.verdict(four) == "INCONCLUSIVE"
    assert dc.verdict({**four, "F-A falsifier": {"result": P}}) == "INCONCLUSIVE"


# ------------------------------------------------------------------ S-C random-day test
def test_random_day_test_is_deterministic_and_detects_a_real_effect(data):
    _, o, c = data
    o2 = o.copy()
    on = dc.sc_target_on(o2.index, 0, 0, 4)
    shift = np.where(on, 1.01, 1.0)
    o2["TLT"] = 100 * np.cumprod(shift)                               # TLT only rises inside the registered windows
    o2["SHY"] = 100.0
    a = dc.sc_random_day_test(o2, c, 0, 4, "2006-06-01", draws=2000)
    b = dc.sc_random_day_test(o2, c, 0, 4, "2006-06-01", draws=2000)
    assert a == b and a["p"] <= 0.05 and a["n_windows"] > 60
    noise = dc.sc_random_day_test(o, c, 0, 4, "2006-06-01", draws=2000)
    assert 0 < noise["p"] <= 1


# ------------------------------------------------------------------ end to end on synthetic data
def test_evaluate_all_structure_and_logging(data, tmp_path):
    _, o, c = data
    db = _db(tmp_path)
    res = dc.evaluate_all(o, c, db, start="2006-06-01", mc_draws=200, rand_draws=200)
    assert res["n_info"]["n_total"] >= 31
    for name, cand in res["candidates"].items():
        keys = list(cand["criteria"])
        assert [k[0] for k in keys[:5]] == list("12345") and len(keys) == 6
        assert cand["criteria"]["5 09:45 fill robustness"]["result"] == dc.INSUFFICIENT
        assert all(v["result"] in (dc.PASS, dc.FAIL, dc.INSUFFICIENT) for v in cand["criteria"].values())
        assert cand["verdict"] in ("WORTH PAPER-TESTING", "UNSUPPORTED", "INCONCLUSIVE")
        assert math.isfinite(cand["metrics"]["sharpe"])
        sens = cand["dsr_sensitivity"]
        assert set(sens) == {"old_default_variance_0.5_annual_sd", "n_doubled"}
        assert cand["criteria"]["1 DSR >= 0.95"]["detail"].count("reported only") == 1    # shown, never gating
        assert set(cand["sensitivity_flags"]) == set(sens) and isinstance(cand["verdict_sensitive"], bool)
    out1 = dc.log_candidates(res, db, "abc")
    out2 = dc.log_candidates(res, db, "abc")
    assert all(t is not None for _, t, _ in out1) and all(t is None for _, t, _ in out2)   # never logged twice
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM research_trials WHERE strategy LIKE 'daily_v1_%'").fetchone()[0] == 3
    n_ret = con.execute("SELECT COUNT(*) FROM trial_returns").fetchone()[0]
    con.close()
    assert n_ret > 1000
    assert dc.trial_counts(db, {})["n_total"] == 31                    # 28 old + the 3 now logged: no double count

import intraday_0945 as i945


def _bars_for_slot(symbol, local_date, slot, price):
    from zoneinfo import ZoneInfo
    import datetime as dt
    local_day = dt.date.fromisoformat(local_date)
    hour, minute = map(int, slot.split(":"))
    stamp = dt.datetime.combine(local_day, dt.time(hour, minute), ZoneInfo("America/New_York"))
    return {"symbol": symbol, "timestamp": stamp.astimezone(dt.timezone.utc).isoformat(), "open": price}


def test_intraday_fill_priority_and_coverage_are_per_symbol():
    idx = pd.bdate_range("2020-03-06", periods=5)
    daily = pd.DataFrame({s: np.arange(100, 105, dtype=float) for s in i945.SYMBOLS}, index=idx)
    rows = []
    for s in i945.SYMBOLS:
        for d in idx:
            day = d.strftime("%Y-%m-%d")
            if day == "2020-03-09" and s == "SPY":
                rows.append(_bars_for_slot(s, day, "09:50", 111.0))
            elif day == "2020-03-10" and s == "SPY":
                rows.append(_bars_for_slot(s, day, "09:55", 112.0))
            elif day == "2020-03-11" and s == "SPY":
                continue
            else:
                rows.append(_bars_for_slot(s, day, "09:45", 110.0))
    bars = pd.DataFrame(rows)
    prices, sources, report = i945.select_fills(bars, daily)
    assert prices.loc[pd.Timestamp("2020-03-09"), "SPY"] == 111
    assert sources.loc[pd.Timestamp("2020-03-09"), "SPY"] == "09:50"
    assert prices.loc[pd.Timestamp("2020-03-10"), "SPY"] == 112
    assert sources.loc[pd.Timestamp("2020-03-10"), "SPY"] == "09:55"
    assert prices.loc[pd.Timestamp("2020-03-11"), "SPY"] == daily.loc[pd.Timestamp("2020-03-11"), "SPY"]
    assert sources.loc[pd.Timestamp("2020-03-11"), "SPY"] == "daily_open"
    assert report["overlap_start"] == "2020-03-06"
    assert report["coverage"]["SPY"]["on_time_0945"] == 2
    assert report["coverage"]["SPY"]["fallback_0950"] == 1
    assert report["coverage"]["SPY"]["fallback_0955"] == 1
    assert report["coverage"]["SPY"]["fallback_daily_open"] == 1


def test_intraday_rejects_duplicate_bars_and_unknown_overlap():
    idx = pd.bdate_range("2020-01-02", periods=3)
    daily = pd.DataFrame({s: 100.0 for s in i945.SYMBOLS}, index=idx)
    rows = [_bars_for_slot(s, d.strftime("%Y-%m-%d"), "09:45", 100.0)
            for s in i945.SYMBOLS for d in idx]
    with pytest.raises(i945.IntradayDataError, match="duplicate"):
        i945.select_fills(pd.DataFrame(rows + [rows[0]]), daily)
    with pytest.raises(i945.IntradayDataError, match="no 09:45"):
        i945.select_fills(pd.DataFrame(rows[:-3]), daily)


def test_intraday_accepts_dst_correct_local_slots():
    idx = pd.DatetimeIndex(["2020-03-06", "2020-03-09"])
    daily = pd.DataFrame({s: [100.0, 101.0] for s in i945.SYMBOLS}, index=idx)
    rows = [_bars_for_slot(s, d, "09:45", 110.0) for s in i945.SYMBOLS for d in idx.strftime("%Y-%m-%d")]
    prices, sources, report = i945.select_fills(pd.DataFrame(rows), daily)
    assert report["overlap_start"] == "2020-03-06"
    assert sources.eq("09:45").all().all()
    assert prices.loc[pd.Timestamp("2020-03-09"), "SPY"] == 110.0



def test_criterion5_uses_fill_events_and_enforces_substitution_budget(data):
    _, opens, closes = data
    sources = pd.DataFrame("09:45", index=opens.index, columns=dc.SA_UNIVERSE + ["IEF", "SHY"])
    coverage = {s: {"coverage_pct": 100.0} for s in sources.columns}
    result, detail = dc.evaluate_criterion5("S-A", opens, closes, opens, sources, coverage, "2006-06-01")
    assert detail["actual"]["sharpe"] == pytest.approx(detail["open_proxy_same_window"]["sharpe"])
    assert detail["fills"]["candidate"]["total"] > 0
    assert detail["fills"]["candidate"]["substituted_pct"] == 0
    assert result["result"] in (dc.PASS, dc.FAIL)  # sample is sufficient; performance decides

    fallback = sources.copy()
    fallback.loc[:, :] = "09:50"
    result2, detail2 = dc.evaluate_criterion5("S-A", opens, closes, opens, fallback, coverage, "2006-06-01")
    assert result2["result"] == dc.INSUFFICIENT
    assert detail2["fills"]["candidate"]["substituted_pct"] == 1.0


def test_fill_quality_counts_each_buy_and_sell_fill():
    sources = pd.DataFrame({"SPY": ["09:45"], "SHY": ["09:50"]},
                           index=pd.DatetimeIndex(["2020-01-02"]))
    events = [(0, "SPY", "buy"), (0, "SHY", "sell"), (0, "SHY", "buy")]
    q = dc.fill_quality(events, sources)
    assert q["total"] == 3 and q["09:45"] == 1 and q["09:50"] == 2
    assert q["substituted_pct"] == pytest.approx(2 / 3)


def test_criterion5_missing_traded_fill_is_insufficient(data):
    _, opens, closes = data
    symbols = dc.SA_UNIVERSE + ["IEF", "SHY"]
    sources = pd.DataFrame("09:45", index=opens.index, columns=symbols)
    coverage = {s: {"coverage_pct": 100.0} for s in symbols}
    prices = opens.copy()
    sources.loc[:, :] = "missing"
    result, detail = dc.evaluate_criterion5("S-A", opens, closes, prices, sources, coverage, "2006-06-01")
    assert detail["fills"]["candidate"]["missing"] == detail["fills"]["candidate"]["total"]
    assert result["result"] == dc.INSUFFICIENT
