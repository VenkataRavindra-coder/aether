"""The detailed console at /console (2026-10-18, moved from / on 2026-10-21 when the board took /)."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGE = (ROOT / "console.html").read_text()


def _engine_paths():
    import aether_engine as eng
    return {getattr(r, "path", None) for r in eng.app.routes}


def test_root_serves_console_with_csrf():
    import dashboard_v2
    import us_dashboard
    html = dashboard_v2.page().body.decode()
    assert "<title>AETHER</title>" in html
    assert us_dashboard.CSRF_TOKEN in html and "__CSRF__" not in html


def test_every_api_the_console_calls_exists():
    used = set(re.findall(r'"(/api/[a-z/_]+)', PAGE))
    assert used >= {"/api/map/status", "/api/overview", "/api/us", "/api/us/summary", "/api/us/extras",
                    "/api/strategy", "/api/safety", "/api/us/pause"}
    paths = _engine_paths()
    missing = {u.rstrip("?") for u in used} - paths - {"/api/market"}
    assert not missing, missing
    assert "/api/market" in paths


def test_old_pages_redirect_and_files_are_gone():
    import dashboard_v2
    r = dashboard_v2.classic_page()
    assert r.status_code == 307 and r.headers["location"] == "/console#holdings"
    for f in ("dashboard_v2.html", "dashboard_classic.html", "us_dashboard.html"):
        assert not (ROOT / f).exists(), f


def test_menu_has_every_section_and_no_mock_data():
    for v in ("overview", "holdings", "us", "orders", "market", "crypto", "safety", "map", "health"):
        assert f'id="v-{v}"' in PAGE, v
    for mock in ("Math.random", "NVDA", "248,430", "lorem"):
        assert mock not in PAGE


def test_only_write_is_pause_and_it_needs_two_clicks():
    posts = re.findall(r'method:\s*"POST"', PAGE)
    assert len(posts) == 2  # /api/us/pause and /logout
    assert "Click again to confirm" in PAGE
    assert "/api/settings" not in PAGE


def test_system_map_is_native_not_an_iframe():
    assert "<iframe" not in PAGE and 'id="arch"' in PAGE and "function renderArch" in PAGE
    for tier in ("Safety gate", "Engines", "Brokers", "Records &amp; jobs"):
        assert tier.replace("&amp;", "&") in PAGE or tier in PAGE


def test_no_example_data_anywhere():
    """Everything shown is live: no sample/demo data paths in the console or the map."""
    m = (ROOT / "map_view.html").read_text()
    for page in (PAGE, m):
        low = page.lower()
        for bad in ("function sample", "samplerows", "sample_events", "sample data", "math.random"):
            assert bad not in low, bad
    for hard in ("23:30 GST", "BTC, ETH, SOL", "verdict for us_etf_momentum_v1"):
        assert hard not in PAGE, hard
