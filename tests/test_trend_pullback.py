"""Trend-pullback registration (aether-research/trend_pullback): offline, synthetic bars, temp files only."""
import math
import random
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "aether-research" / "trend_pullback"))
sys.path.insert(0, str(ROOT / "aether-research" / "tactical_hist"))
sys.path.insert(0, str(ROOT / "aether-research"))
import hist  # noqa: E402
import rules  # noqa: E402
from aether_tactical.bars import Bar  # noqa: E402
from aether_tactical.market_time import NY, Session  # noqa: E402


# ------------------------------------------------------------------ helpers
def _days(n, start=date(2016, 1, 4)):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _session(d, close_h=16):
    return Session(d.isoformat(), datetime(d.year, d.month, d.day, 9, 30, tzinfo=NY),
                   datetime(d.year, d.month, d.day, close_h, 0, tzinfo=NY))


def _flat(s, px, n=None):
    n = n if n is not None else int((s.close - s.open) / timedelta(minutes=1))
    return [Bar(s.open + timedelta(minutes=i), px, px, px, px, 1000) for i in range(n)]


def _random_sessions(n, seed=1, drift=0.00002):
    rnd, px, out = random.Random(seed), 200.0, []
    for d in _days(n):
        s = _session(d)
        bars = []
        for i in range(390):
            o = px
            c = o * (1 + rnd.gauss(drift, 0.0008))
            h, l = max(o, c) * (1 + abs(rnd.gauss(0, 0.0003))), min(o, c) * (1 - abs(rnd.gauss(0, 0.0003)))
            bars.append(Bar(s.open + timedelta(minutes=i), o, h, l, c, 1000))
            px = c
        out.append((s, bars))
    return out


def C(o, h, l, c):
    return rules.Candle(0, None, None, o, h, l, c)


# ------------------------------------------------------------------ frozen definitions
def test_exactly_four_frozen_variants():
    assert set(rules.VARIANTS.values()) == {(tf, p) for tf in ("1h", "1d") for p in ("engulfing", "hammer")}
    assert len(rules.VARIANTS) == 4
    assert (rules.EMA_FAST, rules.EMA_SLOW, rules.WARMUP, rules.TARGET_R) == (20, 50, 150, 2.0)
    assert rules.MAX_HOLD_SLOTS == {"1h": 6, "1d": 10}
    assert (rules.COST_BP_PER_SIDE, rules.STRESS_COST_BP_PER_SIDE) == (5.0, 10.0)


def test_ema_seed_and_recursion():
    v = list(range(1, 61))
    e = rules.ema(v, 50)
    assert e[48] is None and e[49] == pytest.approx(25.5)
    k = 2 / 51
    assert e[50] == pytest.approx(51 * k + 25.5 * (1 - k))
    assert rules.ema([1, 2], 50) == [None, None]


def test_engulfing():
    assert rules.is_engulfing(C(10, 10.2, 9.4, 9.5), C(9.4, 10.3, 9.3, 10.1))
    assert not rules.is_engulfing(C(10, 10.2, 9.4, 9.5), C(9.6, 10.3, 9.5, 10.1))   # opens above prior close
    assert not rules.is_engulfing(C(10, 10.2, 9.4, 9.5), C(9.4, 9.9, 9.3, 9.9))     # closes below prior open
    assert not rules.is_engulfing(C(9.5, 10.2, 9.4, 10), C(9.4, 10.3, 9.3, 10.1))   # prior candle green
    assert not rules.is_engulfing(C(10, 10, 9.5, 9.5), C(9.5, 10, 9.5, 10))         # equal bodies: not strictly bigger


def test_hammer():
    assert rules.is_hammer(C(9.8, 10.0, 9.0, 9.95))      # body 0.15, lower 0.8, upper 0.05 of range 1.0
    assert rules.is_hammer(C(9.95, 10.0, 9.0, 9.8))      # red hammer counts
    assert not rules.is_hammer(C(9.5, 10.0, 9.0, 9.95))  # body 0.45 > 0.35
    assert not rules.is_hammer(C(9.6, 9.9, 9.0, 9.7))    # upper wick 0.2 > 0.10
    assert not rules.is_hammer(C(9.8, 10.0, 9.5, 9.95))  # lower wick 0.3 < 0.6 of range
    assert not rules.is_hammer(C(10, 10, 10, 10))        # no range


# ------------------------------------------------------------------ slots and candles
def test_hourly_slots_full_hours_only_and_early_close():
    d1, d2 = _days(2)
    s1, s2 = _session(d1), _session(d2, close_h=13)
    m = rules.Market([(s1, _flat(s1, 100)), (s2, _flat(s2, 100))], "1h")
    assert [x.start.strftime("%H:%M") for x in m.slots] == ["09:30", "10:30", "11:30", "12:30", "13:30", "14:30",
                                                            "09:30", "10:30", "11:30"]
    assert len(m.candles) == 9


def test_hour_needs_54_minutes_and_day_needs_90_percent():
    d = _days(1)[0]
    s = _session(d)
    bars = [b for i, b in enumerate(_flat(s, 100)) if not (0 < i < 8)]          # 7 minutes missing in hour 1
    m = rules.Market([(s, bars)], "1h")
    assert [c.slot for c in m.candles] == [1, 2, 3, 4, 5]
    assert len(rules.Market([(s, _flat(s, 100, 350))], "1d").candles) == 0     # 350 < ceil(0.9 * 390)
    assert len(rules.Market([(s, _flat(s, 100, 351))], "1d").candles) == 1


def test_failed_session_keeps_its_slot_and_breaks_engulfing_adjacency():
    days = _days(3)
    sess = [(_session(days[0]), _flat(_session(days[0]), 100)), (_session(days[1]), []),
            (_session(days[2]), _flat(_session(days[2]), 100))]
    m = rules.Market(sess, "1d")
    assert len(m.slots) == 3 and [c.slot for c in m.candles] == [0, 2]


# ------------------------------------------------------------------ trade simulation (daily timeframe)
def _daily_market(prices_per_day, n_days=None):
    """One session per entry; each entry is either a price (flat day) or a list of minute (o,h,l,c)."""
    days = _days(len(prices_per_day))
    out = []
    for d, spec in zip(days, prices_per_day):
        s = _session(d)
        if isinstance(spec, list):
            bars = [Bar(s.open + timedelta(minutes=i), *ohlc, 1000) for i, ohlc in enumerate(spec)]
            last = spec[-1][3]
            bars += [Bar(s.open + timedelta(minutes=i), last, last, last, last, 1000) for i in range(len(spec), 390)]
        else:
            bars = _flat(s, spec)
        out.append((s, bars))
    return rules.Market(out, "1d")


def _signal_day():
    return [(100, 101, 99, 100.5)]          # daily candle high 101, low 99 (rest of day flat at 100.5)


def test_gap_above_trigger_fills_at_open_then_target():
    m = _daily_market([_signal_day(), [(102, 102, 101.5, 102)], [(102, 107, 102, 107)]])
    t = rules.simulate(m, 0)
    assert t["entry_px"] == 102 and t["stop"] == 99 and t["target"] == pytest.approx(108)
    m = _daily_market([_signal_day(), [(102, 102, 101.5, 102)], [(102, 109, 102, 109)]])
    t = rules.simulate(m, 0)
    assert t["exit_reason"] == "target" and t["exit_px"] == pytest.approx(108)
    assert t["net_bp"] == pytest.approx((108 / 102 - 1) * 1e4 - 10)


def test_no_break_no_trade_and_stop_gap_fills_worse():
    assert rules.simulate(_daily_market([_signal_day(), 100.8, 100.8]), 0) is None   # never above 101
    m = _daily_market([_signal_day(), [(100.9, 101.2, 100.9, 101.2)], [(98, 98, 97, 97.5)]])
    t = rules.simulate(m, 0)
    assert t["entry_px"] == 101 and t["exit_reason"] == "stop" and t["exit_px"] == 98   # gap below stop 99


def test_entry_minute_checks_stop_only():
    m = _daily_market([_signal_day(), [(100.5, 108, 98.5, 107)], 107])
    t = rules.simulate(m, 0)
    assert t["exit_reason"] == "stop_entry_minute" and t["exit_px"] == 99


def test_stop_first_when_both_in_one_minute():
    m = _daily_market([_signal_day(), [(101.5, 101.5, 101.5, 101.5), (101.5, 110, 98, 100)], 100])
    assert rules.simulate(m, 0)["exit_reason"] == "stop"


def test_max_hold_ten_sessions_exits_at_last_close():
    m = _daily_market([_signal_day(), [(101.5, 101.5, 101.5, 101.5)]] + [101.6] * 9 + [101.7, 101.8, 101.9])
    t = rules.simulate(m, 0)
    assert t["exit_reason"] == "max_hold" and t["exit_px"] == 101.7
    assert t["exit_t"].date() == date.fromisoformat(m.sessions[11].day)


def test_data_end():
    m = _daily_market([_signal_day(), [(101.5, 101.5, 101.5, 101.5)], 101.6])
    assert rules.simulate(m, 0)["exit_reason"] == "data_end"


def test_one_position_at_a_time_and_no_signal_in_warmup():
    m = rules.Market(_random_sessions(40, seed=3), "1h")
    assert len(rules.run(m, "engulfing")) >= 2          # the check below is not vacuous
    for p in rules.PATTERNS:
        tr = rules.run(m, p)
        assert all(not rules.context_ok(m, i) for i in range(min(rules.WARMUP, len(m.candles))))
        for a, b in zip(tr, tr[1:]):
            assert b["signal_start"] > a["exit_t"] and b["entry_t"] > a["exit_t"]


def test_signal_needs_trend_and_pullback(monkeypatch):
    m = rules.Market(_random_sessions(40, seed=5), "1h")
    for i in range(rules.WARMUP, len(m.candles)):
        if rules.is_signal(m, i, "hammer") or rules.is_signal(m, i, "engulfing"):
            assert m.candles[i].c > m.ema_slow[i] and m.candles[i].l <= m.ema_fast[i]


# ------------------------------------------------------------------ runner
def test_daily_mark_to_market_compounds_to_trade_return():
    sess = _random_sessions(30, seed=7)
    cal = hist.Calendar(sess)
    t = {"entry_t": sess[3][1][100].t, "entry_px": sess[3][1][100].o,
         "exit_t": sess[8][1][200].t, "exit_px": sess[8][1][200].o}
    rets = hist.daily_returns(cal, [t], 0, 0.0)
    assert math.prod(1 + x for _, x in rets) - 1 == pytest.approx(t["exit_px"] / t["entry_px"] - 1)
    assert sum(1 for _, x in rets if x != 0) == 6


def test_random_entry_p_is_matched_and_deterministic():
    real = [{"signal_start": datetime(2017, 1, 3), "net_bp": 50.0}] * 5
    pool = [{"signal_start": datetime(2017, 2, 1), "net_bp": float(x)} for x in range(-20, 20)]
    a, b = hist.random_entry_p(real, pool), hist.random_entry_p(real, pool)
    assert a == b and a["p"] == pytest.approx(1 / 1001)
    worse = [{"signal_start": datetime(2017, 1, 3), "net_bp": -50.0}] * 5
    assert hist.random_entry_p(worse, pool)["p"] == pytest.approx(1.0)
    assert hist.random_entry_p(real * 10, pool[:3])["p"] is None


def _db(tmp, logged=0, back=0, batch_rows=0):
    import trials
    db = tmp / "r.sqlite"
    con = trials._connect(str(db))
    for i in range(logged):
        con.execute("INSERT INTO research_trials (strategy, batch) VALUES (?, ?)", (f"x{i}", "other"))
    for i in range(batch_rows):
        con.execute("INSERT INTO research_trials (strategy, batch) VALUES (?, ?)", (f"y{i}", hist.BATCH))
    if back:
        con.execute("INSERT INTO untracked_trials (family, count) VALUES ('f', ?)", (back,))
    con.commit()
    con.close()
    return db


def test_trial_n_counts_registry_and_floor(tmp_path):
    assert hist.trial_n(_db(tmp_path, logged=9, back=28))["n"] == 44        # 9 + 28 + 3 + 4
    (tmp_path / "r.sqlite").unlink()
    assert hist.trial_n(_db(tmp_path))["n"] == hist.N_FLOOR
    (tmp_path / "r.sqlite").unlink()
    assert hist.trial_n(_db(tmp_path, logged=20, back=28))["n"] == 55
    with pytest.raises(hist.Refused):
        hist.trial_n(tmp_path / "missing.sqlite")


def test_data_hash_pinned():
    hist.check_data_hash("1ca901b4" + "0" * 50 + "5ee745")
    with pytest.raises(hist.Refused):
        hist.check_data_hash("deadbeef" + "0" * 50 + "5ee745")


def test_too_many_bad_sessions_refused():
    def broken(n_bad, n):
        return [(s, [Bar(b.t, -1, b.h, b.l, b.c, b.v) for b in bars]) if i < n_bad else (s, bars)
                for i, (s, bars) in enumerate(_random_sessions(n))]
    with pytest.raises(hist.Refused):
        hist.verified_sessions(broken(2, 50))          # 4% > 2%
    ok, n_bad = hist.verified_sessions(broken(1, 100))
    assert n_bad == 1 and ok[0][1] == [] and len(ok) == 100


def test_end_to_end_synthetic_and_log_once(tmp_path):
    sess, _ = hist.verified_sessions(_random_sessions(240, seed=11, drift=0.00004))
    db = _db(tmp_path, logged=9, back=28)
    res = hist.evaluate_all(sess, hist.trial_n(db))
    assert set(res["variants"]) == set(rules.VARIANTS)
    for v in res["variants"].values():
        assert v["verdict"] in {"historically supported", "historically unsupported", "insufficient evidence", "mixed"}
        assert len(v["criteria"]) == 10
        assert v["trades"] < hist.MIN_TRADES or v["verdict"] != "insufficient evidence"
    text = hist.render(res)
    assert "PBO across the 4 variants (reported only" in text and "S2 NO-GO" in text
    done = hist.log_trials(res, "h", db)
    assert len(done) == 4
    with pytest.raises(hist.Refused):
        hist.log_trials({**res, "n_info": hist.trial_n(db)}, "h", db)
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM research_trials WHERE batch=?", (hist.BATCH,)).fetchone()[0] == 4
    con.close()


def test_research_modules_place_no_orders():
    for f in (ROOT / "aether-research" / "trend_pullback").glob("*.py"):
        src = f.read_text()
        for bad in ("submit_order", "MarketOrderRequest", "LimitOrderRequest", "cancel_order", "close_position",
                    "alpaca_executor", "APCA_LIVE"):
            assert bad not in src or (f.name == "forward.py" and bad == "APCA_LIVE"), (f.name, bad)


# ------------------------------------------------------------------ forward journal (no orders)
import forward  # noqa: E402


def test_forward_keys_must_be_the_second_paper_account():
    with pytest.raises(forward.Refused):
        forward.keys({})
    with pytest.raises(forward.Refused):
        forward.keys({"TP_APCA_API_KEY_ID": "A", "TP_APCA_API_SECRET_KEY": "s", "APCA_API_KEY_ID": "A"})
    with pytest.raises(forward.Refused):
        forward.keys({"TP_APCA_API_KEY_ID": "L", "TP_APCA_API_SECRET_KEY": "s", "APCA_LIVE_API_KEY_ID": "L"})
    assert forward.keys({"TP_APCA_API_KEY_ID": "T", "TP_APCA_API_SECRET_KEY": "s",
                         "APCA_API_KEY_ID": "A", "APCA_LIVE_API_KEY_ID": "L"}) == ("T", "s")


def _trade(start, net=12.0, reason="target"):
    t0 = datetime.fromisoformat(start)
    return {"signal_start": t0, "entry_t": t0 + timedelta(hours=1), "entry_px": 500.0, "exit_t": t0 + timedelta(hours=3),
            "exit_px": 501.0, "exit_reason": reason, "stop": 499.0, "target": 502.0, "net_bp": net}


def test_forward_journal_is_append_only_and_flags_mismatches(tmp_path, monkeypatch):
    monkeypatch.setattr(forward, "DATA", tmp_path)
    a = _trade("2026-10-27T10:30:00-04:00")
    assert forward.append_new({"tpb_engulf_1h": [a]}) == {"new": 1, "mismatches": 0}
    assert forward.append_new({"tpb_engulf_1h": [a]}) == {"new": 0, "mismatches": 0}
    changed = {**a, "net_bp": -3.0}
    assert forward.append_new({"tpb_engulf_1h": [changed]}) == {"new": 0, "mismatches": 1}
    rows = forward.journal()
    assert rows[0]["net_bp"] == 12.0 and rows[1]["kind"] == "mismatch"
    s = forward.summary()
    assert s["tpb_engulf_1h"]["trades"] == 1 and s["_mismatches"] == 1 and s["tpb_engulf_1h"]["state"] == "collecting"


def test_forward_cache_round_trip_and_replay_window(tmp_path, monkeypatch):
    monkeypatch.setattr(forward, "DATA", tmp_path)
    sess = _random_sessions(30, seed=4)
    for i, (s, bars) in enumerate(sess):
        forward.save_session(s, bars if i != 5 else [], i != 5)
    back = forward.load_sessions()
    assert len(back) == 30 and back[5][1] == [] and back[0][1] == sess[0][1]
    for trades in forward.replay(back).values():            # all synthetic data is before FORWARD_START
        assert trades == []


def test_forward_update_cache_uses_calendar_and_skips_unfinished(tmp_path, monkeypatch):
    monkeypatch.setattr(forward, "DATA", tmp_path)
    days = [date(2026, 10, 22), date(2026, 10, 23), date(2026, 10, 26), date(2026, 10, 27)]
    cal = [_session(d) for d in days]
    monkeypatch.setattr(forward, "calendar", lambda tc, a, b: cal)
    now = datetime(2026, 10, 27, 15, 0, tzinfo=NY)           # the 27th is still open
    calls = []

    def fetch(s):
        calls.append(s.day)
        return _flat(s, 100) if s.day != "2026-10-23" else []
    added, failed = forward.update_cache(None, None, now, fetch=fetch)
    assert calls == ["2026-10-22", "2026-10-23", "2026-10-26"] and (added, failed) == (3, 1)
    assert forward.update_cache(None, None, now, fetch=fetch) == (0, 0)   # cached sessions are not refetched


def test_forward_run_writes_status_for_health_checks(tmp_path, monkeypatch):
    """2026-10-25: each run leaves last_run.json (ok, failed forward sessions, duplicates) for research_health.py."""
    import json as _json
    monkeypatch.setattr(forward, "DATA", tmp_path)
    days = [date(2026, 10, 23), date(2026, 10, 26), date(2026, 10, 27)]
    cal = [_session(d) for d in days]
    monkeypatch.setattr(forward, "calendar", lambda tc, a, b: cal)
    monkeypatch.setattr(forward, "clients", lambda env=None: (None, None))
    from aether_tactical import bars as _bars
    monkeypatch.setattr(_bars, "fetch_session", lambda sym, s, now=None, client=None: [] if s.day == "2026-10-26" else _flat(s, 100))

    class _Now(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 27, 16, 25, tzinfo=NY)
    monkeypatch.setattr(forward, "datetime", _Now)
    assert forward.main(["run"]) == 0
    st = _json.loads((tmp_path / "last_run.json").read_text())
    assert st["ok"] is True and st["failed_forward_sessions"] == ["2026-10-26"] and st["duplicates"] == 0
    assert st["latest_session"] == "2026-10-27" and st["finished_at"]
    # a refused run is recorded as not ok, with the reason
    def refuse(env=None):
        raise forward.Refused("missing TP_APCA_API_KEY_ID / TP_APCA_API_SECRET_KEY (second paper account)")
    monkeypatch.setattr(forward, "clients", refuse)
    assert forward.main(["run"]) == 2
    st = _json.loads((tmp_path / "last_run.json").read_text())
    assert st["ok"] is False and "missing TP_APCA" in st["error"]


def test_forward_duplicate_rows_counted(tmp_path, monkeypatch):
    monkeypatch.setattr(forward, "DATA", tmp_path)
    a = _trade("2026-10-27T10:30:00-04:00")
    forward.append_new({"tpb_engulf_1h": [a]})
    with forward.journal_path().open("a") as f:
        f.write(forward.journal_path().read_text().splitlines()[0] + "\n")
    assert forward.duplicate_rows() == 1
