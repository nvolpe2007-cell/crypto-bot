"""
pytest configuration — stubs heavy/unavailable dependencies so tests run
in any Python 3.11 environment without installing ccxt or pandas_ta.

Both packages are stubbed at module level so that they are present in
sys.modules before any src.* module is imported during test collection.
"""

import sys
import types
import numpy as np
import pandas as pd

# ── ccxt.async_support stub ───────────────────────────────────────────────────
# src/exchange.py does `import ccxt.async_support as ccxt`; provide a minimal
# no-op stub so the import succeeds without the real ccxt installed.

class _FakeExchange:
    async def set_sandbox_mode(self, *a, **kw): pass
    async def load_markets(self, *a, **kw): return {}
    async def close(self, *a, **kw): pass
    async def fetch_ohlcv(self, *a, **kw): return []
    async def fetch_ticker(self, *a, **kw): return {}
    async def fetch_balance(self, *a, **kw): return {}
    async def create_order(self, *a, **kw): return {}
    async def cancel_order(self, *a, **kw): return {}
    async def fetch_open_orders(self, *a, **kw): return []
    async def fetch_trades(self, *a, **kw): return []

    def set_sandbox_mode(self, *a, **kw): pass


_ccxt_async = types.ModuleType("ccxt.async_support")
_ccxt_async.kraken = lambda **kw: _FakeExchange()

# Exception hierarchy mirroring real ccxt so exchange.py's _RETRYABLE tuple
# resolves to the same classes that test code raises.
class _BaseError(Exception): pass
class _NetworkError(_BaseError): pass
class _RequestTimeout(_NetworkError): pass
class _RateLimitExceeded(_NetworkError): pass
class _ExchangeError(_BaseError): pass
class _AuthenticationError(_ExchangeError): pass

_ccxt_async.BaseError = _BaseError
_ccxt_async.NetworkError = _NetworkError
_ccxt_async.RequestTimeout = _RequestTimeout
_ccxt_async.RateLimitExceeded = _RateLimitExceeded
_ccxt_async.ExchangeError = _ExchangeError
_ccxt_async.AuthenticationError = _AuthenticationError

_ccxt_root = types.ModuleType("ccxt")
_ccxt_root.async_support = _ccxt_async
_ccxt_root.BaseError = _BaseError
_ccxt_root.NetworkError = _NetworkError
_ccxt_root.RequestTimeout = _RequestTimeout
_ccxt_root.RateLimitExceeded = _RateLimitExceeded
_ccxt_root.ExchangeError = _ExchangeError
_ccxt_root.AuthenticationError = _AuthenticationError

sys.modules["ccxt"] = _ccxt_root
sys.modules["ccxt.async_support"] = _ccxt_async

# ── pandas_ta stub ────────────────────────────────────────────────────────────

_stub = types.ModuleType("pandas_ta")


def _ema(series: pd.Series, length: int = 9, **_) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()


def _rsi(series: pd.Series, length: int = 14, **_) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(com=length - 1, min_periods=length).mean()
    avg_loss = loss.ewm(com=length - 1, min_periods=length).mean()
    # When avg_loss==0 and avg_gain>0 → RSI=100; both==0 → RSI=50 (neutral)
    rsi = pd.Series(np.where(
        avg_loss == 0,
        np.where(avg_gain == 0, 50.0, 100.0),
        100.0 - 100.0 / (1.0 + avg_gain / avg_loss),
    ), index=series.index)
    return rsi


def _atr(high: pd.Series, low: pd.Series, close: pd.Series,
         length: int = 14, **_) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low,
         (high - prev_close).abs(),
         (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(span=length, adjust=False).mean()


def _macd(series: pd.Series, fast: int = 12, slow: int = 26,
          signal: int = 9, **_) -> pd.DataFrame:
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    hist = macd_line - signal_line
    return pd.DataFrame({
        f"MACD_{fast}_{slow}_{signal}": macd_line,
        f"MACDs_{fast}_{slow}_{signal}": signal_line,
        f"MACDh_{fast}_{slow}_{signal}": hist,
    })


def _adx(high: pd.Series, low: pd.Series, close: pd.Series,
         length: int = 14, **_) -> pd.DataFrame:
    up = high.diff()
    down = -low.diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)

    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)

    tr_s = tr.rolling(length).sum().replace(0, np.nan)
    plus_di = 100 * plus_dm.rolling(length).sum() / tr_s
    minus_di = 100 * minus_dm.rolling(length).sum() / tr_s
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx_vals = dx.rolling(length).mean().fillna(25.0)

    return pd.DataFrame({
        f"ADX_{length}": adx_vals,
        f"DMP_{length}": plus_di.fillna(0),
        f"DMN_{length}": minus_di.fillna(0),
    })


def _bbands(series: pd.Series, length: int = 20, std: float = 2.0, **_) -> pd.DataFrame:
    sma = series.rolling(length).mean()
    rolling_std = series.rolling(length).std()
    upper  = sma + std * rolling_std
    lower  = sma - std * rolling_std
    col = f"BBU_{length}_{std}"
    col_m = f"BBM_{length}_{std}"
    col_l = f"BBL_{length}_{std}"
    return pd.DataFrame({col: upper, col_m: sma, col_l: lower})


_stub.ema    = _ema
_stub.rsi    = _rsi
_stub.atr    = _atr
_stub.macd   = _macd
_stub.adx    = _adx
_stub.bbands = _bbands

# Install before any src.* module is imported during test collection
sys.modules["pandas_ta"] = _stub

# ── aiohttp stub ──────────────────────────────────────────────────────────────
# market_sentiment, kraken_ws, and crypto_vol import aiohttp for HTTP/WS;
# unit tests never exercise those code paths so a minimal stub is enough.

import enum as _enum

class _WSMsgType(_enum.Enum):
    TEXT   = "text"
    CLOSED = "closed"
    ERROR  = "error"

class _ClientTimeout:
    def __init__(self, *_a, **_kw): pass

class _FakeResponse:
    async def json(self, *_a, **_kw): return {}
    async def text(self, *_a, **_kw): return ""
    async def __aenter__(self): return self
    async def __aexit__(self, *_a): pass

class _FakeSession:
    def get(self, *_a, **_kw):   return _FakeResponse()
    def post(self, *_a, **_kw):  return _FakeResponse()
    def ws_connect(self, *_a, **_kw): return _FakeResponse()
    async def __aenter__(self): return self
    async def __aexit__(self, *_a): pass

_aiohttp = types.ModuleType("aiohttp")
_aiohttp.ClientSession = _FakeSession
_aiohttp.ClientTimeout = _ClientTimeout
_aiohttp.WSMsgType     = _WSMsgType
sys.modules["aiohttp"] = _aiohttp

# Ensure the project root is on sys.path for `from src.xxx import` lookups
import os as _os
_project_root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
