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
_ccxt_async.kraken = lambda *args, **kw: _FakeExchange()
_ccxt_async.krakenfutures = lambda *args, **kw: _FakeExchange()

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
    upper = sma + std * rolling_std
    lower = sma - std * rolling_std
    bandwidth = (upper - lower) / sma * 100
    percent = (series - lower) / (upper - lower)
    p = f"{length}_{float(std)}"
    return pd.DataFrame({
        f"BBL_{p}": lower,
        f"BBM_{p}": sma,
        f"BBU_{p}": upper,
        f"BBB_{p}": bandwidth,
        f"BBP_{p}": percent,
    }, index=series.index)


_stub.ema = _ema
_stub.rsi = _rsi
_stub.atr = _atr
_stub.macd = _macd
_stub.adx = _adx
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

# ── Attribution ledger: never let a test write to the REAL P&L database ───────
# src/attribution.record() writes through a PROCESS-WIDE SINGLETON whose db_path
# defaults to data/attribution.db -- the live ledger behind the dashboard and the
# daily Telegram P&L scorecard. regime_arm.py and arbitrage/funding_arb_paper.py
# call record() directly, so merely exercising those code paths in a test appended
# rows to production. Measured 2026-09-06: one `pytest tests/` run added 6 rows,
# and 94 accumulated rows carried reason='test' with a hardcoded net_pnl of 10.0,
# which made the regime_intraday arm read as a 100%-win-rate, +$942 strategy.
#
# The singleton is "first call wins the db_path", so claiming it HERE -- before any
# test imports a module that records -- redirects every write to a throwaway file.
# This changes no production behaviour; it only binds the singleton inside pytest.
import atexit as _atexit
import shutil as _shutil
import tempfile as _tempfile

_attrib_tmpdir = _tempfile.mkdtemp(prefix="pytest-attribution-")
_atexit.register(_shutil.rmtree, _attrib_tmpdir, True)

import src.attribution as _attribution  # noqa: E402

_attribution.get_ledger(_os.path.join(_attrib_tmpdir, "attribution.db"))
assert _attribution.get_ledger().db_path.startswith(_attrib_tmpdir), (
    "attribution ledger singleton was claimed before conftest could redirect it; "
    "a test would write to the production data/attribution.db"
)

# ── Trade journal: same bug class as the attribution ledger above ────────────
# src/trade_journal.py exposes TWO module-level paths, JOURNAL_FILE (.json) and
# CSV_FILE (.csv). tests/test_trade_journal.py monkeypatches only JOURNAL_FILE,
# so append_csv() kept writing to the PRODUCTION data/trade_journal.csv --
# measured 2026-09-07 at +15,838 bytes per suite run. That file is what
# src/session_filter.py rated trading sessions from, and it had accumulated 3665
# rows of which 3290 were synthetic, EVERY one stamped hour_utc=12.
#
# Redirect both here so no test can reach the real files, whatever it patches.
import src.trade_journal as _trade_journal  # noqa: E402

_trade_journal.JOURNAL_FILE = _os.path.join(_attrib_tmpdir, "trade_journal.json")
_trade_journal.CSV_FILE = _os.path.join(_attrib_tmpdir, "trade_journal.csv")

# Pre-import modules that test_live_trading.py would otherwise replace with
# MagicMock stubs (via _ensure_stub).  Importing here — after all stubs above
# are installed — caches the real implementations in sys.modules so that
# _ensure_stub's "not in sys.modules" guard leaves them intact.
import src.regime_detector as _  # noqa: F401, E402
import src.notifications as _  # noqa: F401, E402
import src.order_flow as _  # noqa: F401, E402
import src.multi_timeframe as _  # noqa: F401, E402
import src.portfolio_optimizer as _  # noqa: F401, E402
import src.market_sentiment as _  # noqa: F401, E402
import src.ml_scorer as _  # noqa: F401, E402

# ── General guard: the suite must not touch ANYTHING under data/ ─────────────
# Two separate production files were found being written by tests on 2026-09-07
# (data/attribution.db and data/trade_journal.csv), each through a different
# mechanism. Rather than keep patching one path at a time, snapshot the whole
# directory and fail the run if any file changes. A test that legitimately needs
# to write must use tmp_path.
import hashlib as _hashlib

_DATA_DIR = _os.path.join(_project_root, "data")
_data_snapshot: dict = {}


def _snapshot_data() -> dict:
    snap = {}
    for root, _dirs, files in _os.walk(_DATA_DIR):
        for name in files:
            fp = _os.path.join(root, name)
            try:
                with open(fp, "rb") as fh:
                    snap[fp] = _hashlib.md5(fh.read()).hexdigest()
            except OSError:
                pass
    return snap


def pytest_sessionstart(session):
    global _data_snapshot
    if _os.path.isdir(_DATA_DIR):
        _data_snapshot = _snapshot_data()


def pytest_sessionfinish(session, exitstatus):
    if not _data_snapshot:
        return
    after = _snapshot_data()
    changed = sorted(
        [p for p in after if p in _data_snapshot and after[p] != _data_snapshot[p]]
        + [p for p in after if p not in _data_snapshot]
        + [p for p in _data_snapshot if p not in after]
    )
    if changed:
        rel = [_os.path.relpath(p, _project_root) for p in changed]
        print()
        print("=" * 70)
        print("TEST SUITE MODIFIED PRODUCTION DATA FILES -- this is a bug:")
        for r in rel:
            print("   " + r)
        print("Tests must write to tmp_path. See tests/test_attribution_isolation.py")
        print("=" * 70)
        session.exitstatus = 1
