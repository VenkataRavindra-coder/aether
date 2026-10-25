"""The board at / (2026-10-21): one screen of panels, every value from the engine's read-only APIs."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGE = (ROOT / "board.html").read_text()


def _engine_paths():
    import aether_engine as eng
    return {getattr(r, "path", None) for r in eng.app.routes}


def test_root_serves_board_with_csrf_and_old_pages_redirect():
    import dashboard_v2
    import us_dashboard
    html = dashboard_v2.board().body.decode()
    assert "US trading desk" in html and us_dashboard.CSRF_TOKEN in html and "__CSRF__" not in html
    r = dashboard_v2.retired_page()
    assert r.status_code == 307 and r.headers["location"] == "/"
    paths = _engine_paths()
    assert {"/", "/console", "/dashboard/classic", "/us", "/map"} <= paths
    for f in ("console.html", "map_view.html", "dashboard_v2.html", "dashboard_classic.html", "us_dashboard.html"):
        assert not (ROOT / f).exists(), f


def test_every_api_the_board_calls_exists():
    used = set(re.findall(r'"(/api/[a-z/_]+)"', PAGE))
    assert used == {"/api/map/status", "/api/overview", "/api/money", "/api/us", "/api/us/summary", "/api/us/extras",
                    "/api/safety", "/api/connectivity", "/api/us/pause"}
    assert not used - _engine_paths()


def test_only_write_is_pause_and_it_needs_two_clicks():
    assert len(re.findall(r'method:\s*"POST"', PAGE)) == 1
    assert 'fetch("/api/us/pause"' in PAGE and '"x-aether-csrf": CSRF' in PAGE
    assert "Click again to confirm" in PAGE and "/api/settings" not in PAGE


def test_no_links_to_retired_pages():
    assert "/console" not in PAGE and 'href="/map"' not in PAGE


def test_no_sample_or_fake_data():
    low = PAGE.lower()
    for bad in ("math.random", "sample", "lorem", "demo", "placeholder", "fake"):
        assert bad not in low, bad
    # no hard-coded money or prices: every $ amount is formatted from an API value at runtime
    assert not re.search(r"\$\d", PAGE)


def test_wires_only_move_on_a_fresh_heartbeat():
    assert "hbAge() < 180" in PAGE and "prefers-reduced-motion" in PAGE


def test_money_panel_reads_each_account_with_its_own_keys(monkeypatch):
    """Panel 06: balance, holdings and gain/loss per account, straight from Alpaca, never mixed."""
    import json as _json
    import dashboard_v2 as d
    monkeypatch.setenv("APCA_LIVE_API_KEY_ID", "LK"); monkeypatch.setenv("APCA_LIVE_API_SECRET_KEY", "LS")
    monkeypatch.setenv("APCA_API_KEY_ID", "PK"); monkeypatch.setenv("APCA_API_SECRET_KEY", "PS")
    monkeypatch.delenv("STRESS_ALPACA_API_KEY", raising=False)
    monkeypatch.setenv("CANARY_AUTO", "0")
    seen = []
    data = {
        ("LK", "/v2/account"): {"equity": "100", "last_equity": "99.99924932", "cash": "9.98"},
        ("LK", "/v2/positions"): [{"symbol": "VTI", "asset_class": "us_equity", "qty": "0.241332",
                                   "avg_entry_price": "372.904", "current_price": "373.01", "cost_basis": "89.995",
                                   "market_value": "90.019249", "unrealized_pl": "0.024", "unrealized_plpc": "0.0003"}],
        ("LK", "/v2/account/portfolio/history"): {"timestamp": [1, 1760918400, 1761004800], "equity": [0, 100.0, 100.0]},
        ("PK", "/v2/account"): {"equity": "99427.54", "last_equity": "99427.54", "cash": "99427.54"},
        ("PK", "/v2/positions"): [],
        ("PK", "/v2/account/portfolio/history"): {"timestamp": [1760918400], "equity": [100000.0]},
    }

    class Resp:
        def __init__(self, body): self.body = body
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return _json.dumps(self.body).encode()

    def fake_urlopen(req, timeout=None):
        key = req.headers.get("Apca-api-key-id")
        path = req.full_url.split(".markets", 1)[1].split("?")[0]
        host = req.full_url.split("/v2")[0]
        seen.append((key, host))
        return Resp(data[(key, path)])

    monkeypatch.setattr(d.urllib.request, "urlopen", fake_urlopen)
    d._MONEY_CACHE.update(t=0.0, data=None)
    out = d.money()
    live, paper, stress = out["accounts"]
    assert live["real_money"] and not paper["real_money"] and not stress["real_money"]
    assert live["balance"] == 100 and live["cash"] == 9.98 and live["holdings"][0]["symbol"] == "VTI"
    assert abs(live["invested"] - 89.995) < 1e-9 and live["since_start"] == 0
    assert paper["holdings"] == [] and round(paper["since_start"], 2) == -572.46
    assert stress == {**stress, "configured": False} and "balance" not in stress   # no keys -> nothing shown
    assert out["canary_auto"] is False
    # live keys only ever go to the live host, paper keys only to the paper host
    assert all((k == "LK") == (h == "https://api.alpaca.markets") for k, h in seen)


def test_money_error_is_shown_not_filled(monkeypatch):
    import dashboard_v2 as d
    monkeypatch.setenv("APCA_LIVE_API_KEY_ID", "LK"); monkeypatch.setenv("APCA_LIVE_API_SECRET_KEY", "LS")

    def boom(req, timeout=None):
        raise OSError("network down")

    monkeypatch.setattr(d.urllib.request, "urlopen", boom)
    d._MONEY_CACHE.update(t=0.0, data=None)
    live = d.money()["accounts"][0]
    assert "network down" in live["error"] and "balance" not in live
