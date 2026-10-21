"""Auto test page: read-only, file-based, never touches the test-trading code."""
import json
import re
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

import autotest_view as av

ROOT = Path(__file__).resolve().parent.parent


def test_api_reads_status_file_switch_and_survives_missing_parts(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("X=1\nCANARY_AUTO=1\n")
    st = tmp_path / "auto-status.json"
    st.write_text(json.dumps({"action": "buy", "reason": "flat", "completed_round_trips": 2, "target_round_trips": 10}))
    monkeypatch.setattr(av, "STATUS", st)
    monkeypatch.setattr(av, "_switch_on", lambda: av.__dict__["_switch_on"].__wrapped__() if False else True)
    monkeypatch.setattr(av, "_log_rows", lambda: [])
    monkeypatch.setattr(av, "_timers", lambda: (_ for _ in ()).throw(RuntimeError("no systemd")))
    app = FastAPI(); app.include_router(av.build_router())
    d = TestClient(app).get("/api/autotest").json()
    assert d["switch_on"] is True and d["status"]["completed_round_trips"] == 2
    assert d["timers"] is None and any("timers" in e for e in d["errors"])


def test_switch_parsing(tmp_path):
    f = tmp_path / ".env"
    f.write_text("CANARY_AUTO=0\nCANARY_AUTO=1\n")
    assert av._switch_on(f) is True                     # last line wins, like the job's loader order
    f.write_text("CANARY_AUTO=0\n")
    assert av._switch_on(f) is False
    assert av._switch_on(tmp_path / "missing") is None


def test_isolated_from_trading_code_and_linked_everywhere():
    src = (ROOT / "autotest_view.py").read_text()
    assert not re.search(r"^\s*(import|from)\s+canary(_auto)?\b", src, re.M)
    for bad in ("submit_order", "TradingClient", "StockHistoricalDataClient", "cancel_order"):
        assert bad not in src
    eng = (ROOT / "aether_engine.py").read_text()
    assert "autotest_view" in eng and "canary" not in eng.lower()
    for f in ("board.html", "trend.html", "tactical.html", "autotest.html"):
        assert 'href="/autotest"' in (ROOT / f).read_text(), f
    page = (ROOT / "autotest.html").read_text()
    assert 'method: "POST"' not in page and "math.random" not in page.lower() and not re.search(r"\$\d", page.replace("$8", "").replace("$0.05", ""))
