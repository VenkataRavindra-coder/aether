"""The daily live risk mark must be scheduled (safety_guard blocks live entries on a mark > 4 days old)."""
from pathlib import Path

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"


def test_riskmark_units_exist_and_point_at_risk_mark():
    svc = (DEPLOY / "aether-riskmark.service").read_text()
    tmr = (DEPLOY / "aether-riskmark.timer").read_text()
    assert "risk_mark.py" in svc and "User=ratelark" in svc
    assert "EnvironmentFile=-/home/ratelark/ratelark/.env.live" in svc  # live keys live there now
    assert "Mon..Fri" in tmr and "America/New_York" in tmr and "Persistent=true" in tmr


def test_weekday_marks_beat_the_staleness_limit():
    """Mon-Fri marks leave at most a 3-day gap (Fri -> Mon), inside the 4-day limit."""
    import safety_guard
    assert safety_guard.limits().max_mark_age_days >= 3
