"""
pytest configuration — runs before any test collection.

Injects lightweight stubs for optional/heavy dependencies that may
not be installed in every environment (CI, developer laptops, etc.).
Production environments with the real packages installed are unaffected.
"""
import sys
import os
import types

# Make the project root importable so `from src.xxx import` works.
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


# ── pandas_ta stub ────────────────────────────────────────────────────────────
try:
    import pandas_ta  # noqa: F401
except ImportError:
    import pandas as pd

    _ta = types.ModuleType("pandas_ta")

    def _ema(series, length=20, **_kw):
        return series.ewm(span=length, adjust=False).mean()

    def _rsi(series, length=14, **_kw):
        delta = series.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.ewm(alpha=1.0 / length, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1.0 / length, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, 1e-10)
        return 100.0 - (100.0 / (1.0 + rs))

    def _atr(high, low, close, length=14, **_kw):
        prev = close.shift(1)
        tr = pd.concat(
            [high - low, (high - prev).abs(), (low - prev).abs()], axis=1
        ).max(axis=1)
        return tr.ewm(alpha=1.0 / length, adjust=False).mean()

    def _macd(series, fast=12, slow=26, signal=9, **_kw):
        ema_fast  = series.ewm(span=fast,   adjust=False).mean()
        ema_slow  = series.ewm(span=slow,   adjust=False).mean()
        macd_line = ema_fast - ema_slow
        sig_line  = macd_line.ewm(span=signal, adjust=False).mean()
        hist      = macd_line - sig_line
        # Column order must mirror real pandas_ta: [MACD, MACDh, MACDs]
        return pd.DataFrame({"MACD": macd_line, "MACDh": hist, "MACDs": sig_line})

    def _adx(high, low, close, length=14, **_kw):
        prev = close.shift(1)
        tr   = pd.concat(
            [high - low, (high - prev).abs(), (low - prev).abs()], axis=1
        ).max(axis=1)
        up   = high.diff()
        down = -low.diff()
        dm_p = up.where((up > down) & (up > 0), 0.0)
        dm_m = down.where((down > up) & (down > 0), 0.0)
        atr  = tr.ewm(alpha=1.0 / length, adjust=False).mean().replace(0, 1e-10)
        di_p = 100.0 * dm_p.ewm(alpha=1.0 / length, adjust=False).mean() / atr
        di_m = 100.0 * dm_m.ewm(alpha=1.0 / length, adjust=False).mean() / atr
        dx   = 100.0 * (di_p - di_m).abs() / (di_p + di_m).replace(0, 1e-10)
        adx  = dx.ewm(alpha=1.0 / length, adjust=False).mean()
        return pd.DataFrame(
            {f"ADX_{length}": adx, f"DMP_{length}": di_p, f"DMN_{length}": di_m}
        )

    _ta.ema  = _ema
    _ta.rsi  = _rsi
    _ta.atr  = _atr
    _ta.macd = _macd
    _ta.adx  = _adx
    sys.modules["pandas_ta"] = _ta


# ── ccxt stub (exchange is not used in unit tests) ────────────────────────────
try:
    import ccxt  # noqa: F401
except ImportError:
    _ccxt_async = types.ModuleType("ccxt.async_support")

    class _FakeExchange:
        def __init__(self, *_a, **_kw):
            pass
        def set_sandbox_mode(self, *_a, **_kw):
            pass

    _ccxt_async.kraken = _FakeExchange

    _ccxt_root = types.ModuleType("ccxt")
    _ccxt_root.async_support = _ccxt_async
    sys.modules["ccxt"]               = _ccxt_root
    sys.modules["ccxt.async_support"] = _ccxt_async
