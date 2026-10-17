"""The PBO gate (research_gate.pbo_cscv) must agree with an independent skfolio-based computation.

Includes the planted-overfit case from build-list item 3: many noise variants plus one that only looks
good in-sample. The gate must flag the batch (PBO well above 0.05), and both implementations must agree.
"""
import sys
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("skfolio")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "aether-research"))
import pbo_crosscheck as pc  # noqa: E402
import research_gate as rg  # noqa: E402


@pytest.mark.parametrize("seed,t,n,S", [(0, 640, 10, 16), (1, 403, 7, 8), (2, 500, 25, 10), (3, 96, 3, 4)])
def test_gate_matches_skfolio_on_random_batches(seed, t, n, S):
    m = np.random.default_rng(seed).normal(0.0003, 0.01, size=(t, n))
    r = pc.crosscheck(m, n_blocks=S)
    assert r["agree"], r


def test_planted_overfit_is_rejected_by_both():
    """50 pure-noise variants: the in-sample winner is luck, so PBO should be near 0.5, far above 0.05."""
    m = np.random.default_rng(42).normal(0.0, 0.01, size=(16 * 40, 50))
    r = pc.crosscheck(m, n_blocks=16)
    assert r["agree"], r
    assert r["gate"]["pbo"] > 0.05 and r["skfolio"]["pbo"] > 0.05


def test_a_real_edge_passes_both():
    """One variant with a persistent edge in every period should win out of sample too: PBO near 0."""
    rng = np.random.default_rng(7)
    m = rng.normal(0.0, 0.01, size=(16 * 40, 20))
    m[:, 0] += 0.004
    r = pc.crosscheck(m, n_blocks=16)
    assert r["agree"], r
    assert r["gate"]["pbo"] <= 0.05


def test_ties_and_flat_variants_agree():
    m = np.random.default_rng(5).normal(0, 0.01, size=(160, 6))
    m[:, 2] = 0.0            # flat variant (Sharpe floored to 0)
    m[:, 4] = m[:, 3]        # exact duplicate (rank ties)
    assert pc.crosscheck(m, n_blocks=8)["agree"]


def test_refuses_when_gate_would_sample():
    with pytest.raises(ValueError):
        pc.crosscheck(np.zeros((400, 3)), n_blocks=20)


def test_mismatch_would_be_detected(monkeypatch):
    """Sanity: if the gate's ranking were off by one, the cross-check must say DISAGREE."""
    m = np.random.default_rng(9).normal(0, 0.01, size=(320, 12))
    real = rg.pbo_cscv

    def skewed(*a, **k):
        out = real(*a, **k)
        return {**out, "pbo": min(1.0, out["pbo"] + 0.1)}
    monkeypatch.setattr(pc.rg, "pbo_cscv", skewed)
    assert not pc.crosscheck(m, n_blocks=8)["agree"]
