"""The single console at / (2026-10-18): replaces dashboard_v2, dashboard_classic and /us."""
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
    assert "/api/market" in paths and "/map" in paths


def test_old_pages_redirect_and_files_are_gone():
    import dashboard_v2
    r = dashboard_v2.classic_page()
    assert r.status_code == 307 and r.headers["location"] == "/#holdings"
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


def test_map_embed_mode():
    m = (ROOT / "map_view.html").read_text()
    assert 'get("embed") === "1"' in m and "/map?embed=1" in PAGE
