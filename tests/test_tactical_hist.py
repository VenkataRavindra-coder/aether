"""Historical tactical runner (aether-research/tactical_hist): offline, synthetic bars, temp files only."""
import csv
import gzip
import math
import random
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "aether-research" / "tactical_hist"))
sys.path.insert(0, str(ROOT / "aether-research"))
import tactical_hist as th  # noqa: E402
from aether_tactical import engulfing_engine, orb_engine  # noqa: E402
from aether_tactical.market_time import NY  # noqa: E402


def _write(tmp, n_days=260, seed=1, start=date(2016, 1, 4)):
    rnd = random.Random(seed)
    days, d = [], start
    while len(days) < n_days:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    with (tmp / th.CAL_FILE.name).open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["day", "open", "close"])
        for d in days:
            w.writerow([d.isoformat(), "09:30", "13:00" if d.month == 11 and d.day == 25 else "16:00"])
    px = 200.0
    with gzip.open(tmp / th.BARS_FILE.name, "wt", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t", "o", "h", "l", "c", "v"])
        for d in days:
            for i in range(390):
                t = datetime(d.year, d.month, d.day, 9, 30, tzinfo=NY) + timedelta(minutes=i)
                o = px
                c = o * (1 + rnd.gauss(0.00001, 0.0007))
                h, l = max(o, c) * (1 + abs(rnd.gauss(0, 0.0002))), min(o, c) * (1 - abs(rnd.gauss(0, 0.0002)))
                w.writerow([t.isoformat(), o, h, l, c, rnd.randint(50_000, 400_000)])
                px = c
            w.writerow([(datetime(d.year, d.month, d.day, 8, 0, tzinfo=NY)).isoformat(), px, px, px, px, 1])  # pre-market: dropped
    return days


@pytest.fixture
def data(tmp_path):
    days = _write(tmp_path)
    return tmp_path, th.load(tmp_path), days


def test_load_keeps_regular_hours_and_window(data):
    _, sessions, days = data
    assert len(sessions) == len(days)
    s, bars = sessions[0]
    assert all(s.open <= b.t < s.close for b in bars) and len(bars) == 390


def test_baseline_is_the_frozen_engine_exactly(data):
    _, sessions, _ = data
    rows = th.score_variant(sessions, orb_engine, "RANGE", timedelta(minutes=15))
    for (s, bars), r in zip(sessions, rows):
        e = orb_engine.evaluate(bars, s)
        assert r["trade"] == (e["trade"] if e["state"] == "traded" else None)


def test_neighbour_patch_is_restored_even_on_error(data):
    _, sessions, _ = data
    th.score_variant(sessions[:5], engulfing_engine, "VOL_MULT", 2.0)
    assert engulfing_engine.VOL_MULT == 1.5 and orb_engine.RANGE == timedelta(minutes=15)
    with pytest.raises(RuntimeError):
        with th.patched(orb_engine, "RANGE", timedelta(minutes=30)):
            raise RuntimeError
    assert orb_engine.RANGE == timedelta(minutes=15)


def test_orb_range_length_changes_the_range(data):
    _, sessions, _ = data
    s, bars = sessions[0]
    with th.patched(orb_engine, "RANGE", timedelta(minutes=30)):
        r30 = orb_engine.evaluate(bars, s)
    r15 = orb_engine.evaluate(bars, s)
    assert r30["range_high"] >= r15["range_high"] and r30["range_low"] <= r15["range_low"]


def test_metrics_formulas():
    rows = [{"day": "2016-01-04", "ok": True, "trade": {"gross_bp": 14.0}},
            {"day": "2016-01-05", "ok": True, "trade": None},
            {"day": "2017-01-03", "ok": True, "trade": {"gross_bp": -6.0}}]
    m = th.metrics(rows)
    assert m["trades"] == 2 and m["mean_net_bp"] == pytest.approx(0.0)       # nets +10 and -10
    assert m["profit_factor"] == pytest.approx(1.0) and m["wins"] == 1
    assert [x for _, x in m["daily"]] == [0.001, 0.0, -0.001]
    assert th.metrics(rows, 4.0)["mean_net_bp"] == pytest.approx(-4.0)


def test_no_trades_is_mixed_not_unsupported():
    rows = [{"day": "2016-01-04", "ok": True, "trade": None}]
    assert th.metrics(rows)["mean_net_bp"] is None


def test_full_run_logs_six_trials_once(data, tmp_path):
    _, sessions, _ = data
    db = tmp_path / "research.sqlite"
    import trials
    trials._connect(str(db)).close()
    res = th.evaluate_all(sessions, th.trial_n(db))
    assert set(res["strategies"]) == {"A", "B"} and len(res["variants"]) == 6
    for s in res["strategies"].values():
        assert s["verdict"] in ("historically supported", "historically unsupported", "mixed")
        assert len(s["criteria"]) == 6
    assert res["n_info"]["n"] == th.N_FLOOR
    assert "Strategy A" in th.render(res)
    done = th.log_trials(res, "deadbeef", db)
    assert len(done) == 6 and done[-1][2] == 6
    with pytest.raises(th.Refused):
        th.log_trials(th.evaluate_all(sessions, th.trial_n(db)), "deadbeef", db)


def test_too_many_bad_sessions_refuses_the_run(data):
    _, sessions, _ = data
    def spoil(bars):   # a high below the close is a malformed bar: bars.verify rejects the session
        b = bars[5]
        return bars[:5] + [type(b)(b.t, b.o, min(b.o, b.c) * 0.99, b.l * 0.98, b.c, b.v)] + bars[6:]
    broken = [(s, spoil(bars) if i % 10 == 0 else bars) for i, (s, bars) in enumerate(sessions)]
    with pytest.raises(th.Refused, match="data problem"):
        th.evaluate_all(broken, {"n": 37, "logged": 0, "backfilled": 0, "pending": 6, "already_logged": 0})


def test_fetch_refuses_when_data_exists(data):
    tmp, _, _ = data
    with pytest.raises(th.Refused, match="one fetch only"):
        th.fetch(tmp)


def test_missing_trial_db_refuses(tmp_path):
    with pytest.raises(th.Refused):
        th.trial_n(tmp_path / "nope.sqlite")


def test_window_never_reads_the_forward_period():
    assert th.END < date(2026, 10, 21)
