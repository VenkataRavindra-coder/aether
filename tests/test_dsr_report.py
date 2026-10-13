import datetime
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "aether-research"))
import dsr_report  # noqa: E402
import trials  # noqa: E402


def _seed(db, batch, n, edge=False, seed=0):
    rng = np.random.default_rng(seed)
    d0 = datetime.date(2015, 1, 1)
    days = [(d0 + datetime.timedelta(days=i)).isoformat() for i in range(1600)]
    for i in range(n):
        r = rng.normal(0, 0.01, 1600)
        if edge and i == 0:
            r += 0.002
        oos = {"sharpe": float(r.mean() / r.std() * 252 ** 0.5), "days": 800,
               "daily_skew": 0.0, "daily_kurtosis": 3.0}
        trials.log_trial(f"v{i}", "SPY", "daily", {"i": i}, days[0], days[-1], days[800],
                         {"sharpe": 0.1}, oos, db=db, daily_returns=dict(zip(days, r)),
                         batch=batch)


def test_report_shows_pbo_per_batch(tmp_path, capsys):
    db = str(tmp_path / "r.sqlite")
    _seed(db, "noise", 10, seed=1)
    _seed(db, "edge", 10, edge=True, seed=2)
    dsr_report.main(["--db", db])
    out = capsys.readouterr().out
    assert "Trials: 20 logged" in out
    lines = {l.split()[0]: l for l in out.splitlines() if l.startswith("  ") and "PBO" in l}
    assert "FAIL" in lines["noise"] and "PASS" in lines["edge"]


def test_unknown_batch_is_reported_not_hidden(tmp_path, capsys):
    db = str(tmp_path / "r.sqlite")
    _seed(db, "b1", 3)
    dsr_report.main(["--db", db, "--batch", "nope"])
    assert "nope" in capsys.readouterr().out
