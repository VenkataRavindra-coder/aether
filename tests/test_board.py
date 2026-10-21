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
    assert used == {"/api/map/status", "/api/overview", "/api/us", "/api/us/summary", "/api/us/extras",
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
