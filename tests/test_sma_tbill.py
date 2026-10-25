"""200-day filter with T-bills registration (aether-research/sma_tbill): offline, synthetic data only."""
import json
import random
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for sub in ("sma_tbill", "etf_breakout", "risk_balanced", ""):
    sys.path.insert(0, str(ROOT / "aether-research" / sub) if sub else str(ROOT / "aether-research"))
import eb_rules  # noqa: E402
import fetch_bil  # noqa: E402
import st_hist as hist  # noqa: E402
import st_rules as rules  # noqa: E402


def _dates(start=date(2005, 1, 3), end=date(2026, 10, 6)):
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _walk(n, seed, drift=0.0002, vol=0.01):
    rnd, px, o, c = random.Random(seed), 100.0, [], []
    for _ in range(n):
        op = px * (1 + rnd.gauss(0, vol / 4))
        cl = op * (1 + rnd.gauss(drift, vol))
        o.append(op)
        c.append(cl)
        px = cl
    return o, c


def _bil(n, daily=0.00005):
    c = [50.0 * (1 + daily) ** i for i in range(n)]
    return c[:], c                                       # opens = closes: T-bills barely move intraday


def test_frozen_definitions():
    assert rules.SYMBOLS == ("SPY", "QQQ", "IWM", "GLD", "TLT") and rules.IDLE == "BIL"
    assert rules.VARIANTS == {"st_200": 200, "st_150": 150, "st_250": 250} and rules.BASE == "st_200"
    assert (rules.COST_BP_PER_SIDE, rules.STRESS_COST_BP_PER_SIDE) == (5.0, 10.0)
    assert hist.N_FLOOR == 55 and hist.START == date(2007, 6, 1) and hist.END == date(2026, 10, 6)
    assert fetch_bil.FIRST == date(2007, 5, 30) and fetch_bil.LAST == date(2026, 10, 6)


def test_idle_cash_version_equals_the_registered_200_day_comparator():
    """With idle cash the rule must reproduce etf-breakout-v1's 'sma200' comparator exactly."""
    n = 1200
    data = {s: _walk(n, i) for i, s in enumerate(rules.SYMBOLS)}
    bil = _bil(n)
    r_cash, _ = rules.run(data, bil, 200, 400, idle_cash=True)
    r_eb, _ = eb_rules.run_comparator(data, "sma200", 400)
    assert r_cash == pytest.approx(r_eb, rel=1e-12, abs=1e-15)


def test_states_use_closes_up_to_that_day_only():
    _, c = _walk(800, 3)
    base = rules.states(c, 200, 400)
    changed = c[:600] + [x * 4 for x in c[600:]]
    assert rules.states(changed, 200, 400)[:600 - 399] == base[:600 - 399]


def test_switch_accounting_exact():
    eo = [10.0] * 3 + [10, 10, 11, 12, 12]
    ec = [10.0] * 3 + [10, 11, 12, 12, 11]
    io = [50.0, 50, 50, 50, 50.1, 50.2, 50.3, 50.4]
    ic = [50.0, 50, 50, 50.05, 50.15, 50.25, 50.35, 50.45]
    # n = 2: day 3 (close 10 vs avg 10) OFF -> start in BIL; close 4 (11 > 10.5) ON -> buy ETF at open 5
    path, sw = rules.sleeve((eo, ec), (io, ic), 2, 4, cost_bp_per_side=10.0)
    k = 1 - 0.001
    e = k * ic[4] / io[4]                               # day 4: buy BIL at the open
    e *= io[5] / ic[4] * k * k * ec[5] / eo[5]          # day 5: BIL -> ETF
    assert path[0] == pytest.approx(k * ic[4] / io[4]) and path[1] == pytest.approx(e)
    assert sw >= 1


def test_tbills_beat_cash_when_bills_pay_and_never_add_market_risk():
    n = 1500
    data = {s: _walk(n, 10 + i) for i, s in enumerate(rules.SYMBOLS)}
    bil = _bil(n, daily=0.0001)
    r_bil, _ = rules.run(data, bil, 200, 400)
    r_cash, _ = rules.run(data, bil, 200, 400, idle_cash=True)
    import eb_hist
    assert eb_hist.cagr(r_bil) > eb_hist.cagr(r_cash)


def test_load_bil_refuses_gaps(tmp_path):
    import csv
    import gzip
    dates = _dates(date(2007, 5, 25), date(2007, 7, 31))
    p = tmp_path / "b.csv.gz"
    with gzip.open(p, "wt", newline="") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "date", "open", "close"])
        for d in dates:
            if d >= "2007-05-30" and d != "2007-06-15":
                w.writerow(["BIL", d, 50, 50])
    with pytest.raises(hist.Refused):
        hist.load_bil(dates, p)
    with gzip.open(p, "at", newline="") as f:
        csv.writer(f).writerow(["BIL", "2007-06-15", 50, 50])
    o, c = hist.load_bil(dates, p)
    assert o[dates.index("2007-06-01")] == 50 and c[dates.index("2007-05-31")] is None   # before START: unused


def test_fetch_writes_once_deterministic_and_manifest_matches(tmp_path):
    import pandas as pd
    idx = pd.to_datetime(["2007-05-29", "2007-05-30", "2007-05-31", "2026-10-06", "2026-10-07"])
    df = pd.DataFrame({"Open": [1, 50.0, 50.1, 91.0, 91.1], "Close": [1, 50.05, 50.15, 91.5, 91.6]}, index=idx)
    rows = fetch_bil.rows_from_frame(df)
    assert [r[1] for r in rows] == ["2007-05-30", "2007-05-31", "2026-10-06"]   # only the registered period
    a = fetch_bil.write(rows, tmp_path / "a.csv.gz", tmp_path / "a.json")
    with pytest.raises(fetch_bil.Refused):
        fetch_bil.write(rows, tmp_path / "a.csv.gz", tmp_path / "a.json")
    b = fetch_bil.write(rows, tmp_path / "b.csv.gz", tmp_path / "b.json")
    assert a["sha256"] == b["sha256"] and a["rows"] == 3                          # same rows -> same bytes
    assert hist.check_bil((tmp_path / "a.json").read_text(), (tmp_path / "a.csv.gz").read_bytes()) == a["sha256"]
    with pytest.raises(hist.Refused):
        hist.check_bil(json.dumps({"sha256": "0" * 64}), (tmp_path / "a.csv.gz").read_bytes())
    bad = pd.DataFrame({"Open": [float("nan")], "Close": [50.0]}, index=pd.to_datetime(["2008-01-02"]))
    with pytest.raises(fetch_bil.Refused):
        fetch_bil.rows_from_frame(bad)


def _db(tmp, logged=21, back=28):
    import trials
    db = tmp / "r.sqlite"
    con = trials._connect(str(db))
    for i in range(logged):
        con.execute("INSERT INTO research_trials (strategy, batch) VALUES (?, ?)", (f"x{i}", "other"))
    con.execute("INSERT INTO untracked_trials (family, count) VALUES ('f', ?)", (back,))
    con.commit()
    con.close()
    return db


def test_trial_n(tmp_path):
    assert hist.trial_n(_db(tmp_path))["n"] == 55              # 21 + 28 + 3 + 3


def test_end_to_end_synthetic_and_log_once(tmp_path):
    dates = _dates()
    data = {s: _walk(len(dates), 20 + i) for i, s in enumerate(rules.SYMBOLS)}
    st = dates.index("2007-06-01")
    bc = _bil(len(dates))[1]
    bil = ([None] * st + bc[st:], [None] * st + bc[st:])
    db = _db(tmp_path)
    res = hist.evaluate(dates, data, bil, hist.trial_n(db))
    assert res["window"] == ["2007-06-01", "2026-10-06"] and len(res["criteria"]) == 11
    assert res["verdict"] in {"historically supported", "historically unsupported", "mixed"}
    assert set(res["dollars"]) == {"100", "1000", "5000", "capital_for_target"}
    text = hist.render(res)
    assert "st_200 (judged)" in text and "capital needed for $2" in text and "S2 NO-GO" in text
    assert len(hist.log_trials(res, "h", db)) == 3
    with pytest.raises(hist.Refused):
        hist.log_trials({**res, "n_info": hist.trial_n(db)}, "h", db)
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM research_trials WHERE batch=?", (hist.BATCH,)).fetchone()[0] == 3
    con.close()


def test_research_modules_place_no_orders():
    for f in (ROOT / "aether-research" / "sma_tbill").glob("*.py"):
        src = f.read_text()
        for bad in ("submit_order", "OrderRequest", "TradingClient", "cancel_order", "close_position",
                    "alpaca_executor", "APCA_"):
            assert bad not in src, (f.name, bad)
