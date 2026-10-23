"""Automatic paper execution (tactical_paper_exec.py): offline, fake broker, temp state."""
from datetime import datetime, timedelta

import pytest

import tactical_paper_exec as tpx
from aether_tactical.bars import Bar
from aether_tactical.market_time import NY, Session


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(tpx, "STATE", tmp_path / "s.json")
    monkeypatch.setattr(tpx, "_tell", lambda text: None)
    monkeypatch.setattr(tpx, "_log", lambda *a, **k: None)


S = Session("2026-10-23", datetime(2026, 10, 23, 9, 30, tzinfo=NY), datetime(2026, 10, 23, 16, 0, tzinfo=NY))


def bars_until(minutes, breakout_at=None):
    """Flat 100.0-100.5 range for 15 minutes, then a drift; a close above 100.5 at `breakout_at`."""
    out = []
    for i in range(minutes):
        t = S.open + timedelta(minutes=i)
        c = 100.2
        if breakout_at is not None and i >= breakout_at:
            c = 100.8
        h = 100.5 if i < 15 else max(c, 100.4) + 0.05
        out.append(Bar(t, 100.2, h, 100.0 if i < 15 else 100.1, c, 1000))
    return out


class FakeOrder:
    def __init__(self, i): self.id = i


class FakeTC:
    def __init__(self): self.sent = []
    def submit_order(self, req):
        self.sent.append(req)
        return FakeOrder(f"o{len(self.sent)}")


def test_orb_signal_on_the_last_bar_places_one_oto_buy_with_the_range_low_stop():
    b = bars_until(20, breakout_at=19)
    tc = FakeTC()
    now = b[-1].t + timedelta(minutes=1, seconds=5)
    out = tpx.tick(now=now, tc=tc, fetch=lambda *a: b, session_lookup=lambda d: S)
    assert "orb15: entered" in out and len(tc.sent) == 1
    req = tc.sent[0]
    assert req.symbol == "QQQ" and req.side.value == "buy" and req.order_class.value == "oto"
    assert req.stop_loss.stop_price == 100.0 and req.qty == int(50 // (100.8 - 100.0))
    # a second tick the same day never re-enters
    tpx.tick(now=now + timedelta(minutes=1), tc=tc, fetch=lambda *a: b + bars_until(21, 19)[-1:], session_lookup=lambda d: S)
    assert len(tc.sent) == 1


def test_a_signal_already_past_is_recorded_missed_never_chased():
    b = bars_until(40, breakout_at=19)       # breakout long ago; the engine reports it as traded
    tc = FakeTC()
    tpx.tick(now=b[-1].t + timedelta(minutes=1), tc=tc, fetch=lambda *a: b, session_lookup=lambda d: S)
    assert tc.sent == [] and tpx.load_state()["2026-10-23"]["orb15"]["status"] == "missed"


def test_plan_entry_refuses_stale_signals_and_price_at_or_below_stop():
    t = S.open + timedelta(minutes=20)
    r = {"strategy": "orb15", "state": "signal", "range_low": 100.0}
    assert tpx.plan_entry(r, t, t + timedelta(minutes=5), 101.0)[0] is None              # stale
    assert tpx.plan_entry(r, t, t + timedelta(minutes=1), 100.0)[0] is None              # at the stop
    o, _ = tpx.plan_entry(r, t, t + timedelta(minutes=1), 101.0)
    assert o["qty"] == 50 and o["stop"] == 100.0 and "target" not in o


def test_engulfing_gets_a_bracket_with_a_1_5r_target():
    t = S.open + timedelta(minutes=90)
    o, _ = tpx.plan_entry({"strategy": "engulf15", "state": "signal", "stop": 99.0}, t, t + timedelta(minutes=1), 100.0)
    assert o["target"] == 101.5 and o["qty"] == 50


def test_outside_the_session_does_nothing():
    tc = FakeTC()
    assert tpx.tick(now=S.close - timedelta(minutes=3), tc=tc, fetch=lambda *a: [], session_lookup=lambda d: S) == "outside session"
    assert tc.sent == []


def test_off_unless_enabled_and_paper_keys_required(monkeypatch, capsys):
    monkeypatch.setenv("TACTICAL_PAPER_EXEC", "0")
    assert tpx.main(["tick"]) == 0 and "off" in capsys.readouterr().out
    monkeypatch.setenv("STRESS_ALPACA_API_KEY", "")
    with pytest.raises(SystemExit):
        tpx.client()


def test_client_is_hard_wired_to_paper():
    import inspect
    assert "paper=True" in inspect.getsource(tpx.client) and "APCA_LIVE" not in inspect.getsource(tpx)
