"""The board at / (2026-10-21): one screen of panels, every value from the engine's read-only APIs."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGE = (ROOT / "board.html").read_text()


def _engine_paths():
    import aether_engine as eng
    return {getattr(r, "path", None) for r in eng.app.routes}


def test_root_serves_board_and_console_moved():
    import dashboard_v2
    assert "US trading desk" in dashboard_v2.board().body.decode()
    assert 'id="v-overview"' in dashboard_v2.page().body.decode()
    paths = _engine_paths()
    assert "/" in paths and "/console" in paths


def test_every_api_the_board_calls_exists():
    used = set(re.findall(r'"(/api/[a-z/_]+)"', PAGE))
    assert used == {"/api/map/status", "/api/overview", "/api/us", "/api/us/summary", "/api/us/extras",
                    "/api/safety", "/api/connectivity"}
    assert not used - _engine_paths()


def test_board_is_read_only():
    assert "POST" not in PAGE and "method:" not in PAGE and "__CSRF__" not in PAGE


def test_every_panel_links_to_a_console_section():
    console = (ROOT / "console.html").read_text()
    for sec in set(re.findall(r'href="/console#([a-z]+)"', PAGE)):
        assert f'id="v-{sec}"' in console, sec


def test_no_sample_or_fake_data():
    low = PAGE.lower()
    for bad in ("math.random", "sample", "lorem", "demo", "placeholder", "fake"):
        assert bad not in low, bad
    # no hard-coded money or prices: every $ amount is formatted from an API value at runtime
    assert not re.search(r"\$\d", PAGE)


def test_wires_only_move_on_a_fresh_heartbeat():
    assert "hbAge() < 180" in PAGE and "prefers-reduced-motion" in PAGE
