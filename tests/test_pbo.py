import datetime
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "aether-research"))
import pbo  # noqa: E402
import trials  # noqa: E402

OOS = {"sharpe": 0.5, "days": 300, "daily_skew": 0.0, "daily_kurtosis": 3.0}


def _days(n):
    d0 = datetime.date(2015, 1, 1)
    return [(d0 + datetime.timedelta(days=i)).isoformat() for i in range(n)]


def _log(db, rets, batch="b1", **kw):
    d = _days(len(rets))
    return trials.log_trial("s", "SPY", "daily", {"k": 1}, d[0], d[-1], d[len(d) // 2],
                            {"sharpe": 0.1}, OOS, db=db,
                            daily_returns=dict(zip(d, rets)), batch=batch, **kw)


def _count(db, table):
    if not Path(db).exists():
        return 0
    return sqlite3.connect(db).execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_legacy_call_still_works(tmp_path):
    db = str(tmp_path / "r.sqlite")
    _, n = trials.log_trial("s", "SPY", "daily", {}, "2015-01-01", "2020-01-01",
                            "2018-01-01", {"sharpe": 0.1}, OOS, db=db)
    assert n == 1 and _count(db, "trial_returns") == 0


def test_migrates_old_schema_keeps_rows(tmp_path):
    db = str(tmp_path / "old.sqlite")
    con = sqlite3.connect(db)
    con.execute(trials._TRIALS)
    con.execute("INSERT INTO research_trials (strategy) VALUES ('old')")
    con.commit(); con.close()
    _log(db, np.random.default_rng(0).normal(0, 0.01, 100), data_hash="abc")
    con = sqlite3.connect(db)
    cols = {r[1] for r in con.execute("PRAGMA table_info(research_trials)")}
    assert {"batch", "code_commit", "data_hash", "n_returns"} <= cols
    assert _count(db, "research_trials") == 2
    row = con.execute("SELECT data_hash, code_commit, n_returns FROM research_trials "
                      "WHERE strategy='s'").fetchone()
    assert row[0] == "abc" and row[1] and row[2] == 100


def test_bad_returns_write_nothing(tmp_path):
    db = str(tmp_path / "r.sqlite")
    rets = list(np.random.default_rng(1).normal(0, 0.01, 100))
    rets[50] = float("nan")
    with pytest.raises(ValueError):
        _log(db, rets)
    with pytest.raises(ValueError):
        _log(db, [0.001] * 10)  # too short
    assert _count(db, "research_trials") == 0


def test_matrix_by_batch(tmp_path):
    db = str(tmp_path / "r.sqlite")
    rng = np.random.default_rng(2)
    for _ in range(3):
        _log(db, rng.normal(0, 0.01, 400), batch="b1")
    _log(db, rng.normal(0, 0.01, 400), batch="b2")
    ids, days, M = pbo.returns_matrix(db, batch="b1")
    assert len(ids) == 3 and M.shape == (400, 3)


def test_pure_noise_fails_gate():  # planted overfit: best-of-noise must be rejected
    M = np.random.default_rng(3).normal(0, 0.01, (1600, 20))
    r = pbo.cscv_pbo(M)
    assert r["pbo"] > 0.5  # complementary halves: best-of-noise does worse than a coin flip OOS


def test_real_edge_passes_gate():
    M = np.random.default_rng(4).normal(0, 0.01, (1600, 20))
    M[:, 0] += 0.002
    assert pbo.cscv_pbo(M)["pbo"] <= 0.05


def test_rejects_bad_input():
    rng = np.random.default_rng(5)
    with pytest.raises(ValueError):
        pbo.cscv_pbo(rng.normal(0, 0.01, (1600, 1)))
    with pytest.raises(ValueError):
        pbo.cscv_pbo(rng.normal(0, 0.01, (1600, 5)), S=15)
    with pytest.raises(ValueError):
        pbo.cscv_pbo(rng.normal(0, 0.01, (100, 5)))


def test_wrapper_matches_research_gate_s3():  # one implementation: no drift possible
    import research_gate as rg
    M = np.random.default_rng(6).normal(0, 0.01, (1600, 12))
    M[:, 3] += 0.0005
    assert pbo.cscv_pbo(M)["pbo"] == rg.pbo_cscv(M, n_blocks=16)["pbo"]
