#!/usr/bin/env python3
"""AETHER research: runner for the three daily-frequency candidates registered in docs/PREREG_daily_v1.md
(tag `prereg-daily-v1`). Research only: nothing here places orders or is imported by the trading engine.

  python daily_candidates.py count                # print N (trial count) and stop; the first step of any run
  python daily_candidates.py run                  # all three candidates, all four criteria, falsifiers, verdicts
  python daily_candidates.py run --log-trials     # additionally log each candidate once via trials.log_trial
  python daily_candidates.py holdout              # always refused until the soak is complete

Fail-closed guards (a run is refused, never degraded): the registration tag must exist and be an ancestor of HEAD;
tracked files must be committed; the data file must have the registered SHA-256; the trial database must be
readable (N cannot be known without it). Data after 2026-10-06 is never read. Every rule and number below is the
registered one; nothing is tuned.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import math
import re
import sqlite3
import statistics
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import research_gate as rg  # noqa: E402

DATA_FILE = HERE / "data" / "us_daily_yf.csv.gz"
RESULTS = HERE / "results"
TRIAL_DB = HERE.parent / "research.sqlite"
REPO = HERE.parent.parent
INVENTORY_FILE = REPO / "docs" / "TRIAL_INVENTORY_daily_v1.md"   # must contain a line "N_inventory: <int>"
INTRADAY_FILE = HERE / "data" / "intraday_0945_sip.csv.gz"         # written once by fetch_0945.py (amendments 1, 3)
MANIFEST_FILE = REPO / "docs" / "INTRADAY_MANIFEST_daily_v1.md"    # its SHA-256 and overlap start, committed first

TAG = "prereg-daily-v1"
DATA_SHA256 = "e9524a2a60e1c609af72c991d1be8cc8d42f36d95bcafcf1b1b81dd1971ed9b6"
START, END = "2007-03-01", "2026-10-06"       # evaluation window; nothing after END is ever read
SEED = 20261007
SLIP, FEE = 5e-4, 0.3e-4                       # 5 bp per side on traded notional; 0.3 bp sell fee
DSR_MIN, PBO_MAX, DD_LIMIT, RANDOM_P = 0.95, 0.05, 0.27, 0.05
MIN_ROUND_TRIPS, MIN_DAYS, MIN_MONTHS, MIN_PBO_DAYS, MIN_WINDOWS = 30, 756, 60, 320, 60
# Per-period variance of the trial Sharpes recorded in the stage-1 evidence (tag evidence-s1-stage1,
# results/strategy_truth_yf_20261007_074638.json, "trials.var_per_period"; annual SD 0.286). The database that held the
# individual Sharpes was lost with the Azure VM, so this number is the surviving record. Amendment 2.
HIST_VAR_PER_PERIOD = 0.0003242764550264549
DEFAULT_VAR_PER_PERIOD = (0.5 / math.sqrt(252)) ** 2   # the old placeholder (0.5 annual SD), reported as a sensitivity
N_FLOOR = 31                                    # 28 recorded for S2 + the three candidates
MC_DRAWS, RANDOM_DRAWS, PBO_BLOCKS, PBO_COMBOS = 10000, 10000, 16, 20000
SA_UNIVERSE = ["SPY", "QQQ", "IWM", "EFA", "TLT", "GLD"]
CASH = "SHY"
NAMES = ("S-A", "S-B", "S-C")
BASE = {"S-A": {"sma": 200, "confirm": 1}, "S-B": {"rsi_below": 10, "exit_sma": 5},
        "S-C": {"entry_offset": 0, "exit_day": 4}}
FAMILY = {"S-A": [{"sma": s, "confirm": c} for s in (150, 200, 250) for c in (1, 3, 5)],
          "S-B": [{"rsi_below": t, "exit_sma": e} for t in (5, 10, 15) for e in (3, 5, 10)],
          "S-C": [{"entry_offset": o, "exit_day": j} for o in (-1, 0, 1) for j in (3, 4, 5)]}
PASS, FAIL, INSUFFICIENT = "PASS", "FAIL", "INSUFFICIENT"
# Criterion 5 (amendment 1): 09:45 ET fills.
C5_SYMBOLS = ["SPY", "QQQ", "IWM", "EFA", "TLT", "GLD", "IEF", "SHY"]
C5_MIN_COVERAGE, C5_MAX_SUBSTITUTED, C5_SHARPE_RATIO = 0.98, 0.02, 0.75
FILL_ORDER = ("09:45", "09:50", "09:55", "daily")   # 09:45 is on time; every other kind is a substituted fill
# `run` was locked until (1) the execution-price question was settled by a registered amendment and (2) the trial
# inventory was reviewed by hand. Lifted 2026-10-22 in its own reviewed change (Amendment 1 section 5 step 5), after:
# amendments 1-3 tagged (prereg-daily-v1.1/.2/.3), the SIP intraday manifest committed (98a84a1), criterion 5
# implemented with tests, and docs/TRIAL_INVENTORY_daily_v1.md committed (N_inventory 37). The other guards still
# apply: registration tag ancestry, clean tree, data and intraday fingerprints, the inventory line.
RUN_LOCKED = False


class Refused(Exception):
    """A guard stopped the run. The message says why."""


# ------------------------------------------------------------------ guards
def sha256_file(p) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def _git(*args, repo=REPO):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=20)


def registration_commit(repo=REPO) -> str:
    """Commit the registration tag points to; refuses if the tag is missing or not an ancestor of HEAD."""
    t = _git("rev-parse", "--verify", f"{TAG}^{{commit}}", repo=repo)
    if t.returncode:
        raise Refused(f"registration tag {TAG} does not exist: nothing may be run before the pre-registration is locked")
    c = t.stdout.strip()
    if _git("merge-base", "--is-ancestor", c, "HEAD", repo=repo).returncode:
        raise Refused(f"tag {TAG} ({c[:12]}) is not an ancestor of HEAD: run from code that contains the registration")
    return c


def require_clean(repo=REPO):
    d = _git("status", "--porcelain", "--untracked-files=no", repo=repo).stdout.strip()
    if d:
        raise Refused("tracked files have uncommitted changes; commit them first so the result names a real commit")


def check_data(path=DATA_FILE, expected=DATA_SHA256) -> str:
    sha = sha256_file(path)
    if sha != expected:
        raise Refused(f"data fingerprint {sha[:16]} differs from the registered {expected[:16]}")
    return sha


# ------------------------------------------------------------------ data
def load_data(path=DATA_FILE, end=END):
    """Opens and closes on the NYSE calendar (SPY), cut at `end`. A bar missing after the first valid one is a
    data defect, never filled."""
    df = pd.read_csv(path)
    opens = df.pivot(index="date", columns="symbol", values="open").sort_index()
    closes = df.pivot(index="date", columns="symbol", values="close").sort_index()
    cal = closes.index[closes["SPY"].notna()]
    cal = cal[cal <= end]
    opens, closes = opens.reindex(cal), closes.reindex(cal)
    opens.index = closes.index = pd.to_datetime(cal)
    return opens, closes


def check_window(opens, closes, symbols, start):
    """Raise on any missing open or close for the symbols from `start` onward."""
    i0 = int(np.searchsorted(closes.index, pd.Timestamp(start)))
    for s in symbols:
        if s not in closes.columns:
            raise Refused(f"symbol {s} missing from the data")
        if closes[s].iloc[i0:].isna().any() or opens[s].iloc[i0:].isna().any():
            raise Refused(f"data defect: missing bar for {s} inside the evaluation window; not filled")
    return i0


# ------------------------------------------------------------------ signals
def sma(close: pd.Series, n: int) -> np.ndarray:
    return close.rolling(n).mean().to_numpy()


def trend_state(close: pd.Series, n: int, confirm: int) -> np.ndarray:
    """State at each close: ON if close > SMA(n). With confirm c, the state flips only after c consecutive closes on
    the other side. Equal to the average counts as not above."""
    c, m = close.to_numpy(float), sma(close, n)
    raw = np.where(np.isnan(m), False, c > m)
    valid = ~np.isnan(m)
    out = np.zeros(len(c), bool)
    cur, run = False, 0
    for t in range(len(c)):
        if not valid[t]:
            out[t] = False
            continue
        if confirm <= 1:
            cur = bool(raw[t])
        else:
            if bool(raw[t]) != cur:
                run += 1
                if run >= confirm:
                    cur, run = bool(raw[t]), 0
            else:
                run = 0
        out[t] = cur
    return out


def wilder_rsi(close: pd.Series, n: int = 2) -> np.ndarray:
    """Wilder's RSI: first average is the simple mean of the first n changes, then avg = (prev*(n-1)+x)/n.
    If the average loss is zero the RSI is 100 (50 if the average gain is also zero)."""
    c = close.to_numpy(float)
    out = np.full(len(c), np.nan)
    if len(c) <= n:
        return out
    d = np.diff(c)
    gain, loss = np.where(d > 0, d, 0.0), np.where(d < 0, -d, 0.0)
    ag, al = gain[:n].mean(), loss[:n].mean()

    def rsi(g, l):
        if l == 0:
            return 50.0 if g == 0 else 100.0
        return 100.0 - 100.0 / (1.0 + g / l)
    out[n] = rsi(ag, al)
    for t in range(n, len(d)):
        ag = (ag * (n - 1) + gain[t]) / n
        al = (al * (n - 1) + loss[t]) / n
        out[t + 1] = rsi(ag, al)
    return out


def sb_target_on(close: pd.Series, i0: int, rsi_below: float, exit_sma: int, max_hold: int = 5) -> np.ndarray:
    """S-B: decided at the close of day t, executed at the open of t+1. Entry: RSI(2) < threshold and close > SMA200.
    Session 1 of the hold is the entry session. Exit once close > SMA(exit_sma) or the 5th session has closed,
    executed at the next open. A new entry can be decided no earlier than the close after an exit was decided."""
    c = close.to_numpy(float)
    r, m200, mx = wilder_rsi(close, 2), sma(close, 200), sma(close, exit_sma)
    T = len(c)
    on = np.zeros(T, bool)
    entry = None
    for t in range(i0, T - 1):
        if entry is None:
            if not (np.isnan(r[t]) or np.isnan(m200[t])) and r[t] < rsi_below and c[t] > m200[t]:
                entry = t + 1
                on[t + 1] = True
        else:
            held = t - entry + 1                      # sessions completed at this close
            if (not np.isnan(mx[t]) and c[t] > mx[t]) or held >= max_hold:
                entry = None
            else:
                on[t + 1] = True
    return on


def month_groups(index: pd.DatetimeIndex):
    """List of arrays of row positions, one per calendar month, in order."""
    ym = index.year * 100 + index.month
    out, start = [], 0
    for i in range(1, len(ym) + 1):
        if i == len(ym) or ym[i] != ym[i - 1]:
            out.append(np.arange(start, i))
            start = i
    return out


def sc_windows(index: pd.DatetimeIndex, i0: int, entry_offset: int, exit_day: int):
    """S-C windows as (entry_row, exit_row). Entry: last trading day of the month + offset (-1 the day before, +1
    the first day of the next month). Exit: trading day `exit_day` of the next month (1 = its first trading day).
    The position is held from the open of the entry row to the open of the exit row. Windows must start at or after
    row i0 and have both rows inside the data."""
    groups, T = month_groups(index), len(index)
    out = []
    for g in groups[:-1]:
        last = int(g[-1])
        entry, exit_ = last + entry_offset, last + exit_day
        if entry >= i0 and exit_ < T and entry < exit_:
            out.append((entry, exit_))
    return out


def sc_target_on(index, i0, entry_offset, exit_day):
    on = np.zeros(len(index), bool)
    for e, x in sc_windows(index, i0, entry_offset, exit_day):
        on[e:x] = True
    return on


# ------------------------------------------------------------------ simulator
def simulate(op, col, sleeves, weights, target_on, reset, i0, initial):
    """Sleeve simulator. sleeves[k] = (on_symbol, off_symbol or None). target_on[i, k]: sleeve k wants its ON asset
    at the open of row i. reset[i]: at that open, sleeve values are reset to `weights`. initial[k]: -1 nothing held
    (a purchase at the first open), 0 off asset, 1 on asset. A position decided at open i earns open(i)->open(i+1).
    Costs: SLIP on every purchase, SLIP+FEE on every sale. Returns a DataFrame of net/gross returns, costs, holds."""
    K, T = len(sleeves), op.shape[0]
    w = np.asarray(weights, float)
    v = w.copy()
    hold = np.array(initial, int)
    n = T - 1 - i0
    net, cost, gross = np.empty(n), np.empty(n), np.empty(n)
    holds = np.zeros((n, K), bool)
    for j, i in enumerate(range(i0, T - 1)):
        before = v.sum()
        for k in range(K):
            want = 1 if target_on[i, k] else 0
            if hold[k] != want:
                if want == 0 and sleeves[k][1] is None:
                    raise ValueError("sleeve has no off asset")
                if hold[k] != -1:
                    v[k] *= 1.0 - (SLIP + FEE)
                v[k] *= 1.0 - SLIP
                hold[k] = want
        if reset[i]:
            tot = v.sum()
            tgt = w * tot
            d = tgt - v
            c = np.where(d < 0, -d, 0.0).sum() * (SLIP + FEE) + np.where(d > 0, d, 0.0).sum() * SLIP
            v = tgt * (1.0 - c / tot)
        after = v.sum()
        for k in range(K):
            sym = sleeves[k][0] if hold[k] == 1 else sleeves[k][1]
            v[k] *= op[i + 1, col[sym]] / op[i, col[sym]]
        holds[j] = hold == 1
        net[j] = v.sum() / before - 1.0
        cost[j] = 1.0 - after / before
        gross[j] = v.sum() / after - 1.0
    return pd.DataFrame({"net": net, "cost": cost, "gross": gross}), holds


def reset_days(index, i0):
    """True on the first trading day of each month after the first row of the window."""
    ym = index.year * 100 + index.month
    r = np.zeros(len(index), bool)
    r[1:] = ym[1:] != ym[:-1]
    r[: i0 + 1] = False
    return r


def _prep(opens, closes, symbols, start):
    syms = list(dict.fromkeys(symbols))
    i0 = check_window(opens, closes, syms, start)
    col = {s: j for j, s in enumerate(syms)}
    return opens[syms].to_numpy(float), col, i0


def run_sa(opens, closes, sma_n, confirm, start=START):
    syms = SA_UNIVERSE + [CASH]
    op, col, i0 = _prep(opens, closes, syms, start)
    T = len(closes)
    on = np.column_stack([np.concatenate(([False], trend_state(closes[s], sma_n, confirm)[:-1])) for s in SA_UNIVERSE])
    if np.isnan(sma(closes[SA_UNIVERSE[0]], sma_n)[i0 - 1]):
        raise Refused("not enough warm-up history before the window start for the SMA")
    sleeves = [(s, CASH) for s in SA_UNIVERSE]
    sim, holds = simulate(op, col, sleeves, [1 / 6] * 6, on, reset_days(closes.index, i0), i0, [0] * 6)
    sim.index = closes.index[i0:T - 1]
    return {"sim": sim, "holds": holds, "round_trips": count_runs(holds), "trips": None, "target": on[i0:T - 1]}


def run_sb(opens, closes, rsi_below, exit_sma, start=START):
    syms = ["SPY", CASH]
    op, col, i0 = _prep(opens, closes, syms, start)
    T = len(closes)
    on = sb_target_on(closes["SPY"], i0, rsi_below, exit_sma)[:, None]
    sim, holds = simulate(op, col, [("SPY", CASH)], [1.0], on, np.zeros(T, bool), i0, [0])
    sim.index = closes.index[i0:T - 1]
    return {"sim": sim, "holds": holds, "round_trips": count_runs(holds), "trips": trip_returns(sim, holds[:, 0]),
            "target": on[i0:T - 1]}


def run_sc(opens, closes, entry_offset, exit_day, start=START):
    syms = ["TLT", CASH]
    op, col, i0 = _prep(opens, closes, syms, start)
    T = len(closes)
    on = sc_target_on(closes.index, i0, entry_offset, exit_day)[:, None]
    sim, holds = simulate(op, col, [("TLT", CASH)], [1.0], on, np.zeros(T, bool), i0, [0])
    sim.index = closes.index[i0:T - 1]
    return {"sim": sim, "holds": holds, "round_trips": count_runs(holds), "trips": trip_returns(sim, holds[:, 0]),
            "target": on[i0:T - 1]}


def run_bench(opens, closes, which, start=START):
    if which == "P":
        syms, weights, sl = ["SPY", "IEF"], [0.6, 0.4], [("SPY", None), ("IEF", None)]
        reset = True
    elif which == "SPY":
        syms, weights, sl = ["SPY"], [1.0], [("SPY", None)]
        reset = False
    elif which == "EW6":
        syms, weights, sl = SA_UNIVERSE, [1 / 6] * 6, [(s, None) for s in SA_UNIVERSE]
        reset = True
    else:
        raise ValueError(which)
    op, col, i0 = _prep(opens, closes, syms, start)
    T = len(closes)
    sim, holds = simulate(op, col, sl, weights, np.ones((T, len(sl)), bool),
                          reset_days(closes.index, i0) if reset else np.zeros(T, bool), i0, [-1] * len(sl))
    sim.index = closes.index[i0:T - 1]
    return sim


def count_runs(holds: np.ndarray) -> int:
    """Number of COMPLETED ON runs (round trips), summed over sleeves: the exit was executed inside the sample. A
    position still open at the last row is not a completed round trip and is not counted."""
    n = 0
    for k in range(holds.shape[1]):
        h = holds[:, k].astype(int)
        n += int((np.diff(h) == -1).sum())
    return n


def trip_returns(sim: pd.DataFrame, on: np.ndarray) -> list:
    """Net return of each ON run of a single-sleeve strategy: compounded gross return while ON, minus the cost paid
    at entry and at exit (the first OFF session)."""
    out, i, n = [], 0, len(on)
    g, c = sim["gross"].to_numpy(), sim["cost"].to_numpy()
    while i < n:
        if on[i]:
            j = i
            while j < n and on[j]:
                j += 1
            if j >= n:                       # still open at the end: not a completed round trip
                break
            out.append(float(np.prod(1.0 + g[i:j]) - 1.0 - c[i] - c[j]))
            i = j
        else:
            i += 1
    return out


# ------------------------------------------------------------------ metrics and tests
def perf(r: pd.Series) -> dict:
    a = r.to_numpy(float)
    years = (r.index[-1] - r.index[0]).days / 365.25
    eq = np.cumprod(1.0 + a)
    cagr = float(eq[-1] ** (1 / years) - 1) if years > 0 and eq[-1] > 0 else float("nan")
    dd = rg.max_drawdown(a)
    return {"sharpe": rg.sharpe(a), "cagr": cagr, "max_dd": dd,
            "calmar": cagr / dd if dd > 0 and math.isfinite(cagr) else float("nan"), "days": int(len(a))}


def monthly_returns(r: pd.Series) -> np.ndarray:
    return (1.0 + r).groupby([r.index.year, r.index.month]).prod().to_numpy() - 1.0


def sc_random_day_test(opens, closes, entry_offset, exit_day, start=START, draws=RANDOM_DRAWS, seed=SEED):
    """Null: the holding window starts on a random trading day. One start per calendar month (the same months that
    have a registered window), uniform over that month's trading days with a complete window in the sample.
    Statistic: mean over months of TLT's compounded open-to-open return minus SHY's over the window, gross of costs."""
    i0 = check_window(opens, closes, ["TLT", CASH], start)
    idx, T = closes.index, len(closes)
    length = exit_day - entry_offset
    tlt, shy = opens["TLT"].to_numpy(float), opens[CASH].to_numpy(float)

    def wret(a):
        return a[length:] / a[:-length] - 1.0
    wex = np.full(T, np.nan)
    wex[: T - length] = wret(tlt) - wret(shy)                       # window starting at row d
    wins = sc_windows(idx, i0, entry_offset, exit_day)
    ym = idx.year * 100 + idx.month
    months = [ym[e] for e, _ in wins]
    groups = {ym[g[0]]: g for g in month_groups(idx)}
    real = float(np.mean([wex[e] for e, _ in wins])) if wins else float("nan")
    cands = [np.array([d for d in groups[m] if d >= i0 and d + length < T]) for m in months]
    if len(wins) == 0 or any(len(c) == 0 for c in cands):
        return {"n_windows": len(wins), "real": real, "p": float("nan"), "draws": 0}
    rng = np.random.default_rng(seed)
    pick = np.column_stack([c[rng.integers(0, len(c), size=draws)] for c in cands])
    null = wex[pick].mean(axis=1)
    return {"n_windows": len(wins), "real": real, "p": rg.pvalue_vs_null(real, null),
            "null_mean": float(null.mean()), "draws": draws}


# ------------------------------------------------------------------ trial counting (section 7)
def trial_inventory(db) -> dict:
    """Everything the count is made of, for a manual check against what was actually attempted."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        logged = [{"id": r[0], "strategy": r[1], "symbol": r[2], "created_at": r[3], "oos_sharpe": r[4]}
                  for r in con.execute("SELECT id, strategy, symbol, created_at, oos_sharpe FROM research_trials ORDER BY id")]
        try:
            back = [{"family": r[0], "count": r[1], "note": r[2]}
                    for r in con.execute("SELECT family, count, note FROM untracked_trials ORDER BY id")]
        except sqlite3.OperationalError:
            back = []
    finally:
        con.close()
    return {"logged": logged, "backfilled": back}


def read_inventory(path=INVENTORY_FILE) -> int:
    """N_inventory from the committed manual inventory. A missing file or line refuses the run."""
    p = Path(path)
    if not p.exists():
        raise Refused(f"trial inventory {p.name} is missing: commit it before any run")
    m = re.search(r"^N_inventory:\s*(\d+)\s*$", p.read_text(), re.M)
    if not m:
        raise Refused(f"{p.name} has no 'N_inventory: <int>' line")
    return int(m.group(1))


def trial_counts(db, pending_sharpes: dict, inventory=None) -> dict:
    """N = research_trials rows + untracked_trials count + candidates not yet logged, floor 31. The variance is the
    variance of per-period trial Sharpes, as dsr_report.py computes it, including the new candidates."""
    if not Path(db).exists():
        raise Refused(f"trial database {db} not found: N cannot be known, run refused")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT strategy, oos_sharpe FROM research_trials").fetchall()
        try:
            n_back = int(con.execute("SELECT COALESCE(SUM(count), 0) FROM untracked_trials").fetchone()[0])
        except sqlite3.OperationalError:
            n_back = 0
    except sqlite3.OperationalError as e:
        raise Refused(f"trial database unreadable ({e}): run refused")
    finally:
        con.close()
    logged = {r[0] for r in rows}
    pending = {k: v for k, v in pending_sharpes.items() if f"daily_v1_{k}" not in logged}
    sharpes = [r[1] for r in rows if r[1] is not None] + list(pending.values())
    per = [x / math.sqrt(rg.PERIODS) for x in sharpes]
    # Amendment 2: never below the registered history; the logged trials (incl. the candidates) can only raise it.
    var = max(HIST_VAR_PER_PERIOD, statistics.variance(per)) if len(per) >= 2 else HIST_VAR_PER_PERIOD
    # N_inventory lists every attempt OTHER than the three candidates (by definition), so the candidates are added
    # as a constant: logging them moves them from `pending` into `rows` and can never change N.
    n = max(N_FLOOR, len(rows) + n_back + len(pending),
            (inventory + len(NAMES)) if inventory is not None else 0)
    return {"n_total": n, "n_inventory": inventory, "n_logged": len(rows), "n_backfilled": n_back, "n_pending": len(pending),
            "var_per_period": var}


# ------------------------------------------------------------------ criterion 5: 09:45 ET fills (amendment 1)
def read_manifest(path=MANIFEST_FILE) -> dict:
    """sha256 and overlap_start from the committed intraday manifest. Missing file or lines refuse the run."""
    p = Path(path)
    if not p.exists():
        raise Refused(f"{p.name} is missing: run fetch_0945.py and commit the manifest before any run")
    t = p.read_text()
    m1, m2 = re.search(r"^sha256:\s*([0-9a-f]{64})\s*$", t, re.M), re.search(r"^overlap_start:\s*(\d{4}-\d{2}-\d{2})\s*$", t, re.M)
    if not m1 or not m2:
        raise Refused(f"{p.name} lacks a 'sha256:' or 'overlap_start:' line")
    return {"sha256": m1.group(1), "overlap_start": m2.group(1)}


def load_intraday(index, path=INTRADAY_FILE, manifest=None):
    """(fills, kinds): DataFrames on the daily calendar `index` x C5_SYMBOLS. fills holds the 09:45 open or, if that bar
    is missing, the 09:50 open, the 09:55 open, then the daily open (FILL_ORDER); kinds says which. NaN/None = no price.
    With a manifest, a file whose SHA-256 differs is refused."""
    if not Path(path).exists():
        raise Refused(f"intraday file {Path(path).name} is missing: run fetch_0945.py first")
    if manifest is not None and sha256_file(path) != manifest["sha256"]:
        raise Refused(f"intraday data fingerprint differs from the committed manifest ({manifest['sha256'][:16]})")
    raw = pd.read_csv(path, dtype={"date": str, "slot": str})
    raw = raw[raw["date"] <= END]
    days = [d.strftime("%Y-%m-%d") for d in index]
    f = np.full((len(days), len(C5_SYMBOLS)), np.nan)
    k = np.full((len(days), len(C5_SYMBOLS)), None, dtype=object)
    for kind in reversed(FILL_ORDER):                    # later kinds are overwritten by better ones
        sub = raw[raw["slot"] == kind].pivot_table(index="date", columns="symbol", values="open", aggfunc="first")
        sub = sub.reindex(index=days, columns=C5_SYMBOLS).to_numpy(float)
        mask = ~np.isnan(sub)
        f[mask] = sub[mask]
        k[mask] = kind
    return (pd.DataFrame(f, index=index, columns=C5_SYMBOLS),
            pd.DataFrame(k, index=index, columns=C5_SYMBOLS, dtype=object))


def with_fills(opens, fills, start):
    """The open-proxy prices with every C5 symbol replaced by its 09:45 fill price from `start` on."""
    out = opens.copy()
    rows = out.index >= pd.Timestamp(start)
    for s in C5_SYMBOLS:
        if s in out.columns:
            out.loc[rows, s] = fills.loc[rows, s]
    return out


def used_prices(holds, sleeves, i0, index):
    """Every (date, symbol) price a simulated run uses: the asset each sleeve holds over row j is valued at the fill
    price of day i0+j and of day i0+j+1 (which is also the sale price when it is sold that morning)."""
    used = set()
    for j in range(holds.shape[0]):
        for k, (on, off) in enumerate(sleeves):
            sym = on if holds[j, k] else off
            if sym is None:
                continue
            used.add((index[i0 + j], sym))
            used.add((index[i0 + j + 1], sym))
    return used


def substituted_share(used, kinds) -> dict:
    n = len(used)
    k = [kinds.at[d, s] for d, s in used]
    by = {x: sum(1 for v in k if v == x) for x in FILL_ORDER}
    missing = sum(1 for v in k if v is None or (isinstance(v, float) and math.isnan(v)))
    sub = n - by["09:45"]
    return {"required": n, "on_time": by["09:45"], "at_0950": by["09:50"], "at_0955": by["09:55"],
            "daily_open": by["daily"], "missing": missing, "share": sub / n if n else float("nan")}


C5_SLEEVES = {"S-A": [(s, CASH) for s in SA_UNIVERSE], "S-B": [("SPY", CASH)], "S-C": [("TLT", CASH)]}
BENCH_SLEEVES = {"P": [("SPY", None), ("IEF", None)], "SPY": [("SPY", None)], "EW6": [(s, None) for s in SA_UNIVERSE]}


def criterion5(name, opens, closes, fills, kinds, start):
    """Amendment 1, criterion 5. Re-run the candidate and the benchmarks with every fill at the 09:45 ET price over the
    window where all eight symbols have 09:45 bars, and compare with the open-proxy run over the identical window."""
    params = BASE[name]
    runner = {"S-A": lambda o: run_sa(o, closes, params["sma"], params["confirm"], start),
              "S-B": lambda o: run_sb(o, closes, params["rsi_below"], params["exit_sma"], start),
              "S-C": lambda o: run_sc(o, closes, params["entry_offset"], params["exit_day"], start)}[name]
    o945 = with_fills(opens, fills, start)
    try:
        proxy, c945 = runner(opens), runner(o945)
        benches = {b: run_bench(o945, closes, b, start) for b in (("P", "SPY", "EW6") if name == "S-A" else ("P", "SPY"))}
    except Refused as e:                                 # a price still missing after every substitute
        return crit(False, f"09:45 run not possible: {e}", sufficient=False), {}
    i0 = int(np.searchsorted(closes.index, pd.Timestamp(start)))
    idx = closes.index
    needed = sorted({s for sl in C5_SLEEVES[name] for s in sl if s} | {s for b in benches for sl in BENCH_SLEEVES[b] for s in sl if s})
    cov = {s: float((kinds.loc[idx >= pd.Timestamp(start), s] == "09:45").mean()) for s in needed}
    subs = {name: substituted_share(used_prices(c945["holds"], C5_SLEEVES[name], i0, idx), kinds)}
    for b in benches:
        h = np.ones((len(benches[b]), len(BENCH_SLEEVES[b])), bool)
        subs[b] = substituted_share(used_prices(h, BENCH_SLEEVES[b], i0, idx), kinds)
    m_open, m945 = perf(proxy["sim"]["net"]), perf(c945["sim"]["net"])
    bp, bs = perf(benches["P"]["net"]), perf(benches["SPY"]["net"])
    trips = c945["round_trips"]
    low_cov = [s for s, v in cov.items() if v < C5_MIN_COVERAGE]
    over = [k for k, v in subs.items() if not v["share"] <= C5_MAX_SUBSTITUTED]
    sufficient = (m945["days"] >= MIN_DAYS and trips >= MIN_ROUND_TRIPS and not low_cov and not over
                  and all(math.isfinite(x["calmar"]) for x in (m945, bp, bs)))
    ratio_ok = m945["sharpe"] >= C5_SHARPE_RATIO * m_open["sharpe"] if m_open["sharpe"] > 0 else True
    c4 = (m945["sharpe"] > bp["sharpe"] and m945["sharpe"] > bs["sharpe"]
          and m945["calmar"] > bp["calmar"] and m945["calmar"] > bs["calmar"])
    ok = m945["sharpe"] > 0 and ratio_ok and m945["cagr"] > 0 and c4
    why = []
    if low_cov:
        why.append("09:45 coverage below 98% for " + ", ".join(f"{s} {cov[s]:.1%}" for s in low_cov))
    if over:
        why.append("substituted fills above 2% in " + ", ".join(f"{k} {subs[k]['share']:.1%}" for k in over))
    if m945["days"] < MIN_DAYS or trips < MIN_ROUND_TRIPS:
        why.append(f"sample {m945['days']} days / {trips} completed round trips (need {MIN_DAYS} / {MIN_ROUND_TRIPS})")
    detail = (f"window {start}..{END}: 09:45 Sharpe {m945['sharpe']:.2f} vs open-proxy {m_open['sharpe']:.2f} "
              f"(need > 0 and >= {C5_SHARPE_RATIO:g}x if positive), CAGR {m945['cagr']:.1%}, Sharpe/Calmar "
              f"{m945['sharpe']:.2f}/{m945['calmar']:.2f} vs 60/40 {bp['sharpe']:.2f}/{bp['calmar']:.2f} vs SPY "
              f"{bs['sharpe']:.2f}/{bs['calmar']:.2f} (benchmarks at 09:45); substituted "
              + ", ".join(f"{k} {v['share']:.2%}" for k, v in subs.items())
              + ("; INSUFFICIENT: " + "; ".join(why) if why else ""))
    info = {"window_start": start, "coverage": cov, "substituted": subs, "open_proxy": m_open, "fills_0945": m945,
            "bench_0945": {"P": bp, "SPY": bs}, "round_trips": trips}
    return crit(ok, detail, sufficient, float(m945["sharpe"])), info


# ------------------------------------------------------------------ criteria and verdicts
def crit(passed, detail, sufficient=True, value=None):
    if not sufficient:
        return {"result": INSUFFICIENT, "detail": detail, "value": value}
    if value is not None and isinstance(value, float) and not math.isfinite(value):
        return {"result": INSUFFICIENT, "detail": detail + " (not finite)", "value": value}
    return {"result": PASS if passed else FAIL, "detail": detail, "value": value}


REQUIRED_CRITERIA = ("1", "2", "3", "4", "5")   # criterion 5 (09:45 ET fills) is mandatory, amendment 1


def verdict(criteria: dict) -> str:
    """Fails closed. A FAIL is final. WORTH PAPER-TESTING needs all five criteria present and every result equal to
    PASS: INSUFFICIENT, an unknown status, a missing result or an absent criterion is never a pass."""
    res = [c.get("result") for c in criteria.values()]
    if FAIL in res:
        return "UNSUPPORTED"
    have = {k.split()[0] for k in criteria}
    if not set(REQUIRED_CRITERIA) <= have or any(r != PASS for r in res):
        return "INCONCLUSIVE"
    return "WORTH PAPER-TESTING"


def sensitivity_flags(criteria: dict, sens: dict) -> dict:
    """For each reported sensitivity, what criterion 1 and the verdict would be if its DSR were used instead of the
    registered one. Reported only, never gating. `sensitive` is True if criterion 1 or the verdict would change."""
    base = verdict(criteria)
    k1 = next(k for k in criteria if k.split()[0] == "1")
    out = {}
    for name, dsr in sens.items():
        alt = {k: dict(v) for k, v in criteria.items()}
        if alt[k1]["result"] in (PASS, FAIL):                 # an INSUFFICIENT criterion 1 stays INSUFFICIENT
            alt[k1]["result"] = (INSUFFICIENT if not math.isfinite(dsr) else PASS if dsr >= DSR_MIN else FAIL)
        v = verdict(alt)
        c1 = alt[k1]["result"] != criteria[k1]["result"]
        out[name] = {"dsr": dsr, "criterion_1": alt[k1]["result"], "verdict": v, "criterion_1_changes": c1,
                     "verdict_changes": v != base, "sensitive": c1 or v != base}
    return out


def evaluate_candidate(name, opens, closes, n_info, start=START, mc_draws=MC_DRAWS, rand_draws=RANDOM_DRAWS,
                       intraday=None):
    runner = {"S-A": lambda p: run_sa(opens, closes, p["sma"], p["confirm"], start),
              "S-B": lambda p: run_sb(opens, closes, p["rsi_below"], p["exit_sma"], start),
              "S-C": lambda p: run_sc(opens, closes, p["entry_offset"], p["exit_day"], start)}[name]
    base = runner(BASE[name])
    r = base["sim"]["net"]
    m, trips = perf(r), base["round_trips"]
    enough_trips = trips >= MIN_ROUND_TRIPS
    d = rg.deflated_sharpe(r.to_numpy(), n_info["n_total"], n_info["var_per_period"])
    # Reported only, never gating (Amendment 2): how much the DSR depends on the registered variance (old default) and
    # under a stress scenario in which N is doubled. Doubling N is a stress test, not a guarantee that N is complete.
    sens = {"old_default_variance_0.5_annual_sd": rg.deflated_sharpe(r.to_numpy(), n_info["n_total"], DEFAULT_VAR_PER_PERIOD)["dsr"],
            "n_doubled": rg.deflated_sharpe(r.to_numpy(), 2 * n_info["n_total"], n_info["var_per_period"])["dsr"]}
    c = {}
    c["1 DSR >= 0.95"] = crit(d["dsr"] >= DSR_MIN, f"DSR {d['dsr']:.3f} (N={n_info['n_total']}, SR {m['sharpe']:.2f}, "
                              f"luck threshold {d['sr0_annual']:.2f}; reported only: DSR {sens['old_default_variance_0.5_annual_sd']:.3f} "
                              f"with the old 0.5 default variance, {sens['n_doubled']:.3f} in the N-doubled stress scenario); {trips} round trips",
                              len(r) >= MIN_DAYS and enough_trips, float(d["dsr"]))
    fam = pd.DataFrame({i: runner(p)["sim"]["net"] for i, p in enumerate(FAMILY[name])})
    try:
        pbo = rg.pbo_cscv(fam.to_numpy(), n_blocks=PBO_BLOCKS, max_combos=PBO_COMBOS, seed=SEED)["pbo"]
        pbo_ok = len(fam) >= MIN_PBO_DAYS
    except ValueError as e:
        pbo, pbo_ok = float("nan"), False
    c["2 PBO <= 0.05"] = crit(pbo <= PBO_MAX, f"PBO {pbo:.3f} over {len(FAMILY[name])} variants, {len(fam)} days",
                              pbo_ok and enough_trips, float(pbo))
    mon = monthly_returns(r)
    if len(mon) >= 2:
        p95 = float(np.percentile(rg.mc_max_drawdown(mon, mc_draws, SEED), 95))
    else:
        p95 = float("nan")
    c["3 MC drawdown <= 27%"] = crit(p95 <= DD_LIMIT, f"95th percentile {p95:.1%} (limit {DD_LIMIT:.0%}); observed "
                                     f"daily {m['max_dd']:.1%}", len(mon) >= MIN_MONTHS and enough_trips, p95)
    bp, bs, be = (perf(run_bench(opens, closes, k, start)["net"]) for k in ("P", "SPY", "EW6"))
    cal_ok = all(math.isfinite(x["calmar"]) for x in (m, bp, bs))
    both = m["sharpe"] > bp["sharpe"] and m["sharpe"] > bs["sharpe"] and m["calmar"] > bp["calmar"] and m["calmar"] > bs["calmar"]
    either = ((m["sharpe"] > bp["sharpe"] or m["calmar"] > bp["calmar"])
              and (m["sharpe"] > bs["sharpe"] or m["calmar"] > bs["calmar"]))
    exposure = float(base["holds"].any(axis=1).mean())
    c["4 beats benchmarks (Sharpe AND Calmar)"] = crit(
        both, f"Sharpe/Calmar {m['sharpe']:.2f}/{m['calmar']:.2f} vs 60/40 {bp['sharpe']:.2f}/{bp['calmar']:.2f} vs SPY "
              f"{bs['sharpe']:.2f}/{bs['calmar']:.2f}; S17-style OR reading: {'beats' if either else 'does not beat'}; "
              f"CAGR {m['cagr']:.1%} vs {bp['cagr']:.1%}/{bs['cagr']:.1%}; exposure {exposure:.0%}",
        cal_ok and enough_trips and len(r) >= MIN_DAYS, float(m["sharpe"] - max(bp["sharpe"], bs["sharpe"])))
    extra = {}
    if name == "S-A":
        ok = m["max_dd"] < be["max_dd"] and m["sharpe"] > be["sharpe"]
        c["F-A drawdown and Sharpe vs own comparator"] = crit(
            ok, f"max DD {m['max_dd']:.1%} vs EW6 {be['max_dd']:.1%}; Sharpe {m['sharpe']:.2f} vs {be['sharpe']:.2f}",
            len(r) >= MIN_DAYS and enough_trips)
    elif name == "S-B":
        t = base["trips"] or []
        e = float(np.mean(t)) if t else float("nan")
        c["F-B expectancy per round trip > 0"] = crit(e > 0, f"{len(t)} completed round trips, mean {e:.3%} net",
                                                      len(t) >= MIN_ROUND_TRIPS, e)
        extra["round_trip_returns"] = len(t)
    else:
        t = sc_random_day_test(opens, closes, BASE[name]["entry_offset"], BASE[name]["exit_day"], start, rand_draws)
        c["F-C random-day test p <= 0.05"] = crit(
            t["p"] <= RANDOM_P, f"p {t['p']:.4f} over {t['draws']} draws, {t['n_windows']} windows; real mean excess "
                                f"{t['real']:.3%}", t["n_windows"] >= MIN_WINDOWS and enough_trips, float(t["p"]))
        extra["random_day"] = t
    if intraday is None:
        c["5 09:45 ET fills"] = crit(False, "09:45 data not supplied: criterion 5 cannot pass", sufficient=False)
    else:
        fills, kinds, c5_start = intraday
        c["5 09:45 ET fills"], extra["criterion_5"] = criterion5(name, opens, closes, fills, kinds, c5_start)
    flags = sensitivity_flags(c, sens)
    return {"name": name, "params": BASE[name], "metrics": m, "round_trips": trips, "criteria": c,
            "verdict": verdict(c), "dsr_sensitivity": sens, "sensitivity_flags": flags,
            "verdict_sensitive": any(f["sensitive"] for f in flags.values()), "returns": r, "benchmarks": {"P": bp, "SPY": bs, "EW6": be}, **extra}


def evaluate_all(opens, closes, db, names=NAMES, start=START, mc_draws=MC_DRAWS, rand_draws=RANDOM_DRAWS,
                 inventory=None, intraday=None):
    """Print N first, then evaluate. N is computed from the database plus the candidates' own Sharpes."""
    quick = {}
    for n in names:
        run = {"S-A": lambda: run_sa(opens, closes, BASE["S-A"]["sma"], BASE["S-A"]["confirm"], start),
               "S-B": lambda: run_sb(opens, closes, **BASE["S-B"], start=start),
               "S-C": lambda: run_sc(opens, closes, **BASE["S-C"], start=start)}[n]()
        quick[n] = perf(run["sim"]["net"])["sharpe"]
    n_info = trial_counts(db, quick, inventory)
    out = {"n_info": n_info, "candidates": {}}
    for n in names:
        out["candidates"][n] = evaluate_candidate(n, opens, closes, n_info, start, mc_draws, rand_draws, intraday)
    return out


# ------------------------------------------------------------------ logging and output
def log_candidates(res, db, data_sha):
    import trials
    done = []
    con = sqlite3.connect(db)
    try:
        have = {(r[0], r[1]) for r in con.execute("SELECT strategy, data_hash FROM research_trials")}
    except sqlite3.OperationalError:
        have = set()
    con.close()
    for n, c in res["candidates"].items():
        if (f"daily_v1_{n}", data_sha) in have:        # an identical configuration is not a new trial
            done.append((n, None, None))
            continue
        r = c["returns"]
        m = c["metrics"]
        skew, kurt = rg.moments(r.to_numpy())
        oos = {"sharpe": m["sharpe"], "days": m["days"], "daily_skew": skew, "daily_kurtosis": kurt}   # kurtosis is RAW
        tid, total = trials.log_trial(f"daily_v1_{n}", {"S-A": "SA6", "S-B": "SPY", "S-C": "TLT"}[n], "backtest",
                                      {**c["params"], "prereg": TAG, "full_window_sharpe_used_as_oos": True},
                                      START, END, "none", m, oos, oos_trades=c["round_trips"], db=db,
                                      daily_returns=r, batch="daily-v1", data_hash=data_sha)
        done.append((n, tid, total))
    return done


def render(res) -> str:
    ni = res["n_info"]
    lines = [f"N = {ni['n_total']} ({ni['n_logged']} logged + {ni['n_backfilled']} backfilled + {ni['n_pending']} "
             f"candidates not yet logged; floor {N_FLOOR})", ""]
    for n, c in res["candidates"].items():
        lines.append(f"== {n}: {c['verdict']}   Sharpe {c['metrics']['sharpe']:.2f}  CAGR {c['metrics']['cagr']:.1%}  "
                     f"maxDD {c['metrics']['max_dd']:.1%}  round trips {c['round_trips']}")
        for k, v in c["criteria"].items():
            lines.append(f"   {v['result']:12s} {k}: {v['detail']}")
        for sn, f in c["sensitivity_flags"].items():
            if f["sensitive"]:
                lines.append(f"   SENSITIVE    criterion 1 under '{sn}': DSR {f['dsr']:.3f} -> {f['criterion_1']}, "
                             f"verdict would be {f['verdict']}")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("count")
    r = sub.add_parser("run")
    r.add_argument("--db", default=str(TRIAL_DB))
    r.add_argument("--log-trials", action="store_true")
    sub.add_parser("holdout")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "holdout":
            raise Refused("the holdout stays closed: it needs the soak to be complete and a docs/holdout.lock entry")
        if a.cmd == "run" and RUN_LOCKED:
            raise Refused("run is locked: the 09:45 ET execution-price amendment and the trial inventory review are "
                          "pending. Nothing has been run on the real history.")
        commit = registration_commit()
        if a.cmd == "count":
            print(json.dumps({"counts": trial_counts(str(TRIAL_DB), {}), "inventory": trial_inventory(str(TRIAL_DB))},
                             indent=2, default=str))
            return 0
        require_clean()
        data_sha = check_data()
        opens, closes = load_data()
        man = read_manifest()
        fills, kinds = load_intraday(closes.index, manifest=man)
        res = evaluate_all(opens, closes, a.db, inventory=read_inventory(),
                           intraday=(fills, kinds, man["overlap_start"]))
        print(render(res))
        RESULTS.mkdir(exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        head = _git("rev-parse", "HEAD").stdout.strip()
        record = {"prereg_tag": TAG, "prereg_commit": commit, "head_commit": head, "runner_sha256": sha256_file(__file__),
                  "data_sha256": data_sha, "window": [START, END], "n_info": res["n_info"],
                  "intraday_sha256": man["sha256"], "criterion5_window_start": man["overlap_start"],
                  "research_gate_sha256": sha256_file(HERE.parent / "research_gate.py"),
                  "candidates": {n: {k: v for k, v in c.items() if k != "returns"} for n, c in res["candidates"].items()}}
        out = RESULTS / f"daily_v1_{stamp}.json"
        out.write_text(json.dumps(record, indent=2, default=str))
        print(f"\nwritten {out}\nprereg {TAG} {commit[:12]}  head {head[:12]}  runner {record['runner_sha256'][:16]}")
        if a.log_trials:
            for n, tid, total in log_candidates(res, a.db, data_sha):
                print(f"{n}: already logged, not counted again" if tid is None else
                      f"logged trial {n}: id {tid}, research_trials now {total}")
        return 0
    except Refused as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
