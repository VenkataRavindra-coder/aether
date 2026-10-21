from datetime import date, datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from macro_trend.api import SignalService, build_router
from macro_trend.config import ConfigurationError, Settings
from macro_trend.data import DailyBars, MarketDataError, AlpacaReadOnlyData
from macro_trend.strategy import evaluate_trend


def _bars(closes, mode="paper"):
    return DailyBars("SPY", tuple(date(2025, 1, 1) for _ in closes), tuple(closes),
                     date(2026, 1, 1), datetime(2026, 1, 2, tzinfo=timezone.utc),
                     mode, "iex")


def test_trend_up_and_down_actions_are_distinct():
    up = evaluate_trend(_bars([100.0] * 199 + [101.0]))
    assert up.signal == "RISK_ON" and up.action_on_new_entry == "BUY"
    assert up.action_if_long == "HOLD_LONG" and up.latest_close == 101.0
    down = evaluate_trend(_bars([100.0] * 199 + [99.0]))
    assert down.signal == "DEFENSIVE_CASH" and down.action_on_new_entry == "DEFENSIVE_CASH"
    assert down.action_if_long == "EXIT"


def test_equal_to_sma_is_defensive():
    result = evaluate_trend(_bars([100.0] * 200))
    assert result.trend == "AT_OR_BELOW_200_SMA" and result.signal == "DEFENSIVE_CASH"


def test_insufficient_history_rejected():
    with pytest.raises(ValueError, match="200"):
        evaluate_trend(_bars([100.0] * 199))


@pytest.mark.parametrize("mode,key,secret,expected", [
    ("paper", "APCA_API_KEY_ID", "APCA_API_SECRET_KEY", "https://paper-api.alpaca.markets"),
    ("live", "APCA_LIVE_API_KEY_ID", "APCA_LIVE_API_SECRET_KEY", "https://api.alpaca.markets"),
])
def test_mode_uses_separate_credentials_and_fixed_hosts(mode,key,secret,expected):
    s=Settings.from_env({"ALPACA_MODE":mode,key:"k",secret:"s"})
    assert s.trading_base_url == expected and s.api_key == "k"


def test_config_rejects_unknown_mode_and_missing_live_credentials():
    with pytest.raises(ConfigurationError):
        Settings.from_env({"ALPACA_MODE":"production","APCA_API_KEY_ID":"k","APCA_API_SECRET_KEY":"s"})
    with pytest.raises(ConfigurationError):
        Settings.from_env({"ALPACA_MODE":"live","APCA_API_KEY_ID":"k","APCA_API_SECRET_KEY":"s"})


def test_service_error_gives_no_stale_cached_signal():
    def fail(_symbol):
        raise TimeoutError("network timed out")
    service=SignalService(fail)
    with pytest.raises(MarketDataError):
        service.signal("SPY")


def test_route_returns_503_no_signal_on_provider_failure():
    service=SignalService(lambda _s: (_ for _ in ()).throw(MarketDataError("timeout")))
    app=FastAPI(); app.include_router(build_router(service))
    r=TestClient(app).get("/signal/SPY")
    assert r.status_code == 503 and r.json()["detail"]["signal"] == "NO_SIGNAL"
    assert r.json()["detail"]["recommended_action"] == "NO_ACTION"


def test_unsupported_ticker_is_not_silently_traded():
    app=FastAPI(); app.include_router(build_router(SignalService(lambda s: _bars([100.0] * 200))))
    assert TestClient(app).get("/signal/QQQ").status_code == 404


class _Response:
    def __init__(self, payload): self.payload=payload
    def raise_for_status(self): return None
    def json(self): return self.payload


class _Http:
    def __init__(self, payloads): self.payloads=list(payloads); self.calls=[]
    def get(self, url, **kwargs):
        self.calls.append((url,kwargs))
        payload=self.payloads.pop(0)
        if isinstance(payload, Exception): raise payload
        return _Response(payload)


def test_broker_timeout_is_converted_to_safe_error():
    import requests
    s=Settings("paper","k","s")
    client=AlpacaReadOnlyData(s,http=_Http([requests.Timeout()]))
    with pytest.raises(MarketDataError, match="timed out"):
        client.fetch_verified_daily()


def test_feed_adjustment_and_timeout_are_explicit():
    s=Settings("paper","k","s")
    http=_Http([
        {"timestamp":"2026-10-21T21:00:00Z","is_open":False},
        [{"date":"2026-10-21","close":"16:00","open":"09:30"}],
        {"bars":[],"next_page_token":None},
    ])
    client=AlpacaReadOnlyData(s,http=http)
    with pytest.raises(MarketDataError):
        client.fetch_verified_daily()
    url,kwargs=http.calls[-1]
    assert url == "https://data.alpaca.markets/v2/stocks/bars"
    assert kwargs["params"]["symbols"] == "SPY" and kwargs["params"]["timeframe"] == "1Day"
    assert kwargs["params"]["adjustment"] == "all" and kwargs["params"]["feed"] == "iex"
    assert kwargs["timeout"][1] == 8.0



def test_service_cache_is_marked_and_failed_refresh_never_reuses_stale_signal(monkeypatch):
    import macro_trend.api as api
    state = {"fail": False}
    def fetch(_symbol):
        if state["fail"]:
            raise TimeoutError("network timed out")
        return _bars([100.0] * 199 + [101.0])
    service = SignalService(fetch, cache_seconds=30)
    first = service.signal("SPY")
    cached = service.signal("SPY")
    assert first["status"] == "available" and first["recommended_action"] == "BUY_OR_HOLD_LONG"
    assert first["cached"] is False and cached["cached"] is True
    state["fail"] = True
    service._cached["SPY"] = (api.time.monotonic() - 31, first)
    with pytest.raises(MarketDataError):
        service.signal("SPY")


def test_verified_fetch_uses_last_completed_close_and_validates_calendar():
    from datetime import timedelta
    start = date(2026, 10, 21) - timedelta(days=420)
    today = date(2026, 10, 21)
    sessions = []
    d = start
    while d <= today:
        if d.weekday() < 5:
            sessions.append(d)
        d += timedelta(days=1)
    completed = [d for d in sessions if d < today]
    calendar = [{"date": d.isoformat(), "open": "09:30", "close": "16:00"} for d in sessions]
    from zoneinfo import ZoneInfo
    bars = [{"t": datetime.combine(d, datetime.min.time(), ZoneInfo("America/New_York")).astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
             "c": 500 + i} for i, d in enumerate(completed)]
    class Http:
        def get(self, url, **kwargs):
            if url.endswith("/v2/clock"):
                return _Response({"timestamp": "2026-10-21T14:00:00Z", "is_open": True})
            if url.endswith("/v2/calendar"):
                return _Response(calendar)
            if url == "https://data.alpaca.markets/v2/stocks/bars":
                return _Response({"bars": bars, "next_page_token": None})
            raise AssertionError(url)
    result = AlpacaReadOnlyData(Settings("paper", "k", "s"), http=Http()).fetch_verified_daily()
    assert result.completed_session == date(2026, 10, 20)
    assert result.close == bars[-1]["c"] and len(result.closes) >= 200
    assert result.mode == "paper" and result.feed == "iex"
