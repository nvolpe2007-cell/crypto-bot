import numpy as np
import pytest

from scripts.polymarket_5m_features import Tape, features, fit_logistic, predict


def _tape(n_sec=4000, start=1_000_000, drift=0.0):
    t = np.arange(start, start + n_sec, 1.0)
    rng = np.random.default_rng(0)
    p = 100.0 * np.exp(np.cumsum(rng.normal(drift, 1e-4, n_sec)))
    sv = np.where(np.arange(n_sec) % 2 == 0, 1.0, -1.0)
    return Tape(np.column_stack([t, p, sv]))


def test_features_ignore_trades_at_or_after_the_decision_instant():
    tape = _tape()
    ts = tape.t[3800]
    before = features(tape, int(tape.t[3700]), ts)
    # Mutate every trade from ts onward: a lookahead-free feature set cannot change.
    tape.p[3800:] *= 2.0
    tape.sv[3800:] = 50.0
    tape.cum_sv = np.cumsum(tape.sv)
    tape.cum_av = np.cumsum(np.abs(tape.sv))
    assert features(tape, int(tape.t[3700]), ts) == before


def test_ofi_is_signed_imbalance():
    t = np.arange(0, 100, 1.0)
    tape = Tape(np.column_stack([t, np.full(100, 100.0), np.ones(100)]))
    assert tape.ofi(90, 60) == pytest.approx(1.0)
    tape = Tape(np.column_stack([t, np.full(100, 100.0), -np.ones(100)]))
    assert tape.ofi(90, 60) == pytest.approx(-1.0)


def test_logistic_recovers_a_real_signal():
    rng = np.random.default_rng(1)
    x = rng.normal(size=(4000, 1))
    y = (rng.random(4000) < 1 / (1 + np.exp(-2 * x[:, 0]))).astype(float)
    w = fit_logistic(x, y, l2=0.0)
    assert w[1] == pytest.approx(2.0, abs=0.25)
    assert predict(w, np.array([[3.0]]))[0] > 0.95
