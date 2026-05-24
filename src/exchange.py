"""
Kraken Exchange Connection Wrapper
Handles market data fetching and order execution via ccxt
"""

import ccxt.async_support as ccxt
import asyncio
import random
import time
from datetime import datetime
from typing import Optional, List, Dict
import logging

logger = logging.getLogger(__name__)

# Transient errors worth retrying: network blips, timeouts, rate-limit back-off.
# Non-transient errors (AuthenticationError, InsufficientFunds, etc.) propagate
# immediately — retrying them would not help and could mask real problems.
_RETRYABLE = (ccxt.NetworkError, ccxt.RequestTimeout, ccxt.RateLimitExceeded)


class CircuitBreakerOpen(Exception):
    """Raised when too many consecutive API call failures have tripped the circuit breaker.

    The bot should treat this as a signal to pause all trading until the exchange
    recovers. The circuit resets automatically after `cooldown_seconds`, or
    immediately when the next call succeeds.
    """


class CircuitBreaker:
    """
    Tracks consecutive total-failure calls and opens a cooldown window once
    the failure count reaches the configured threshold.

    Usage inside _retry:
      - Call check() at the top — raises CircuitBreakerOpen if the circuit is open.
      - Call record_success() when a call succeeds — resets the failure counter.
      - Call record_failure() when all retries are exhausted — increments counter
        and opens the circuit if threshold is reached.

    The circuit is "half-open" after the cooldown expires: it allows one call
    through. If that call succeeds, record_success() fully resets the state.
    If it fails again, record_failure() re-opens the circuit.

    Cooldown escalates on repeated trips (×1 → ×2 → ×5) so a persistently
    down exchange doesn't get hammered every 60 seconds.
    """

    # Cooldown multipliers for successive trips: 1st=×1, 2nd=×2, 3rd+=×5
    _COOLDOWN_MULTIPLIERS = (1, 2, 5)

    def __init__(self, threshold: int = 5, cooldown_seconds: float = 60.0):
        self.threshold = threshold
        self.cooldown_seconds = cooldown_seconds
        self._failures = 0
        self._open_until: Optional[float] = None  # time.monotonic() timestamp
        self._consecutive_opens: int = 0  # tracks escalation across trips

    @property
    def failure_count(self) -> int:
        return self._failures

    @property
    def consecutive_open_count(self) -> int:
        return self._consecutive_opens

    @property
    def is_open(self) -> bool:
        if self._open_until is None:
            return False
        if time.monotonic() < self._open_until:
            return True
        # Cooldown expired — half-open; let next call through
        self._open_until = None
        return False

    def check(self) -> None:
        """Raise CircuitBreakerOpen if the circuit is currently open."""
        if self._open_until is not None and time.monotonic() < self._open_until:
            remaining = self._open_until - time.monotonic()
            raise CircuitBreakerOpen(
                f"Exchange circuit breaker open — pausing for {remaining:.0f}s more "
                f"after {self._failures} consecutive failures"
            )
        if self._open_until is not None:
            # Cooldown just expired — half-open, allow call through
            self._open_until = None

    def record_success(self) -> None:
        """Reset the breaker after any successful API call."""
        if self._failures > 0:
            logger.info(
                f"[CircuitBreaker] Reset — exchange recovered after "
                f"{self._failures} consecutive failure(s)"
            )
        self._failures = 0
        self._open_until = None
        self._consecutive_opens = 0

    def record_failure(self) -> None:
        """Increment failure count; open circuit when threshold is reached.

        Cooldown escalates on repeated trips so a persistently-down exchange
        is not retried every 60s: trip 1=60s, trip 2=120s, trip 3+=300s.
        """
        self._failures += 1
        if self._failures >= self.threshold:
            self._consecutive_opens += 1
            idx = min(self._consecutive_opens - 1, len(self._COOLDOWN_MULTIPLIERS) - 1)
            cooldown = self.cooldown_seconds * self._COOLDOWN_MULTIPLIERS[idx]
            self._open_until = time.monotonic() + cooldown
            logger.warning(
                f"[CircuitBreaker] OPEN (trip #{self._consecutive_opens}) — "
                f"{self._failures} consecutive total-failures. "
                f"Pausing all exchange calls for {cooldown:.0f}s."
            )


class ExchangeConnection:
    """Async wrapper around Kraken exchange"""

    def __init__(self, api_key: str = None, secret: str = None, sandbox: bool = True,
                 circuit_threshold: int = 5, circuit_cooldown: float = 60.0):
        self.sandbox = sandbox
        self.exchange = ccxt.kraken({
            'apiKey': api_key or '',
            'secret': secret or '',
            'enableRateLimit': True,
            'options': {
                'defaultType': 'spot',
            }
        })
        if sandbox:
            self.exchange.set_sandbox_mode(True)
        self._circuit = CircuitBreaker(threshold=circuit_threshold,
                                       cooldown_seconds=circuit_cooldown)
        logger.info(f"Exchange initialized (sandbox={sandbox})")

    async def _retry(self, coro_fn, *args, retries: int = 3,
                     label: str = '?', **kwargs):
        """Call coro_fn(*args, **kwargs) with exponential-backoff retry.

        Retries only on _RETRYABLE (NetworkError, RequestTimeout,
        RateLimitExceeded).  Any other exception propagates immediately so that
        programming errors and permanent exchange rejections are never hidden.

        RateLimitExceeded uses a 30s minimum wait because Kraken's rate-limit
        window is typically 30+ seconds — hammering it sooner wastes retries.

        Raises CircuitBreakerOpen if the circuit is currently open (too many
        consecutive total-failures across all calls on this connection).

        Raises the last seen _RETRYABLE exception when all attempts are exhausted.
        """
        self._circuit.check()  # raises CircuitBreakerOpen if open
        last_exc: Exception = RuntimeError("_retry: no attempts made")
        for attempt in range(1, retries + 1):
            try:
                result = await coro_fn(*args, **kwargs)
                self._circuit.record_success()
                return result
            except _RETRYABLE as exc:
                last_exc = exc
                logger.warning(
                    f"{label} attempt {attempt}/{retries} failed "
                    f"({type(exc).__name__}): {exc}"
                )
                if attempt < retries:
                    if isinstance(exc, ccxt.RateLimitExceeded):
                        # Kraken's rate-limit window is ≥30s; respect it.
                        wait = max(30.0, 2 ** attempt) + random.uniform(0, 2)
                    else:
                        # Jitter spreads retries from concurrent instances so
                        # they don't all hammer the exchange at the same moment.
                        wait = 2 ** attempt + random.uniform(0, 1)
                    await asyncio.sleep(wait)
        self._circuit.record_failure()
        logger.error(f"{label} failed after {retries} attempts: {last_exc}")
        raise last_exc

    async def connect(self, retries: int = 3):
        """Initialize exchange connection with retry."""
        await self._retry(self.exchange.load_markets,
                          retries=retries, label='connect')
        logger.info("Exchange connection established")

    async def disconnect(self):
        """Close exchange connection"""
        await self.exchange.close()

    async def fetch_ohlcv(self, symbol: str, timeframe: str = '1m',
                          limit: int = 1000, since: Optional[int] = None,
                          retries: int = 3) -> List:
        """Fetch candlestick data with automatic retry on transient errors.

        Returns [] when all retries are exhausted so that callers in a polling
        loop can skip the symbol for this tick.  CircuitBreakerOpen propagates
        to the caller unchanged so the bot can detect an outage and pause.
        """
        try:
            return await self._retry(
                self.exchange.fetch_ohlcv, symbol,
                timeframe=timeframe, limit=limit, since=since,
                retries=retries, label=f'fetch_ohlcv({symbol})',
            )
        except CircuitBreakerOpen:
            raise
        except Exception as exc:
            logger.error(f"fetch_ohlcv({symbol}) exhausted retries: {exc}")
            return []

    async def fetch_ohlcv_between(self, symbol: str, timeframe: str,
                                   start_date: str, end_date: str) -> List:
        """Fetch historical data between two dates.

        Args:
            symbol: Trading pair
            timeframe: Candle timeframe
            start_date: ISO format date string (e.g., '2024-01-01')
            end_date: ISO format date string

        Returns:
            List of OHLCV data
        """
        start_ms = int(datetime.fromisoformat(start_date).timestamp() * 1000)
        end_ms = int(datetime.fromisoformat(end_date).timestamp() * 1000)

        all_data = []
        current_ms = start_ms

        while current_ms < end_ms:
            logger.debug(f"Fetching data for {symbol} from "
                         f"{datetime.fromtimestamp(current_ms / 1000)}")
            # Use the wrapper (with retry) instead of self.exchange directly
            batch = await self.fetch_ohlcv(symbol, timeframe=timeframe,
                                           limit=1000, since=current_ms)
            if not batch:
                break

            all_data.extend(batch)
            next_ms = batch[-1][0] + 1  # move past last candle
            if next_ms <= current_ms:
                # Exchange returned data that doesn't advance our cursor —
                # bail out to avoid an infinite loop.
                logger.warning(
                    f"fetch_ohlcv_between({symbol}): timestamp stall "
                    f"(batch[-1]={batch[-1][0]}, cursor={current_ms}), stopping early"
                )
                break
            current_ms = next_ms

        logger.info(f"Fetched {len(all_data)} total candles for {symbol}")
        return all_data

    async def get_ticker(self, symbol: str, retries: int = 3) -> Dict:
        """Get current ticker price with retry."""
        return await self._retry(
            self.exchange.fetch_ticker, symbol,
            retries=retries, label=f'get_ticker({symbol})',
        )

    async def get_balance(self, retries: int = 3) -> Dict:
        """Get account balance with retry."""
        return await self._retry(
            self.exchange.fetch_balance,
            retries=retries, label='get_balance',
        )

    async def create_order(self, symbol: str, order_type: str, side: str,
                           amount: float, price: Optional[float] = None) -> Dict:
        """Place an order.

        No automatic retry: order creation is NOT idempotent.  A
        RequestTimeout could mean the exchange already accepted the order —
        retrying blindly would create a duplicate position.  Callers must
        handle exceptions explicitly and reconcile via get_open_orders().
        """
        params = {'symbol': symbol, 'type': order_type,
                  'side': side, 'amount': amount}
        if price and order_type == 'limit':
            params['price'] = price

        logger.info(f"Placing {order_type} {side} order for "
                    f"{amount} {symbol} at {price or 'market'}")
        return await self.exchange.create_order(**params)

    async def cancel_order(self, order_id: str, symbol: str,
                           retries: int = 3) -> Dict:
        """Cancel an order with retry.

        Cancellation is idempotent: attempting to cancel an already-cancelled
        order raises an exchange error (non-retryable), not a network error, so
        retry here is safe.
        """
        return await self._retry(
            self.exchange.cancel_order, order_id, symbol,
            retries=retries, label=f'cancel_order({order_id})',
        )

    async def get_open_orders(self, symbol: Optional[str] = None,
                              retries: int = 3) -> List:
        """Get open orders with retry."""
        return await self._retry(
            self.exchange.fetch_open_orders, symbol,
            retries=retries, label='get_open_orders',
        )

    async def get_trades(self, symbol: Optional[str] = None,
                         since: Optional[int] = None,
                         retries: int = 3) -> List:
        """Get trade history with retry."""
        return await self._retry(
            self.exchange.fetch_trades, symbol, since=since,
            retries=retries, label=f'get_trades({symbol})',
        )


class KrakenFuturesConnection:
    """
    Async wrapper around Kraken Futures (perpetual swaps) via ccxt.krakenfutures.

    Requires separate API keys from futures.kraken.com — not the same keys as
    spot Kraken. Set KRAKEN_FUTURES_API_KEY and KRAKEN_FUTURES_API_SECRET in .env.

    Unified CCXT symbols for perps:
        BTC/USD:USD   ETH/USD:USD   SOL/USD:USD
    """

    # Map spot symbol → perp unified symbol
    SPOT_TO_PERP = {
        'BTC/USD': 'BTC/USD:USD',
        'ETH/USD': 'ETH/USD:USD',
        'SOL/USD': 'SOL/USD:USD',
    }

    def __init__(self, api_key: str = None, secret: str = None, sandbox: bool = True,
                 circuit_threshold: int = 5, circuit_cooldown: float = 60.0):
        self.sandbox = sandbox
        self.exchange = ccxt.krakenfutures({
            'apiKey': api_key or '',
            'secret': secret or '',
            'enableRateLimit': True,
        })
        if sandbox:
            self.exchange.set_sandbox_mode(True)
        self._circuit = CircuitBreaker(threshold=circuit_threshold,
                                       cooldown_seconds=circuit_cooldown)
        logger.info(f"Kraken Futures initialized (sandbox={sandbox})")

    def perp_symbol(self, spot_symbol: str) -> str:
        """Convert spot symbol to Kraken Futures perp symbol."""
        return self.SPOT_TO_PERP.get(spot_symbol, spot_symbol)

    async def _retry(self, coro_fn, *args, retries: int = 3,
                     label: str = '?', **kwargs):
        """Identical retry + circuit-breaker semantics to ExchangeConnection._retry.

        Retries only _RETRYABLE errors with exponential backoff.
        RateLimitExceeded uses a 30s minimum wait (Kraken's reset window).
        Non-transient errors (auth, bad params) propagate immediately.
        Raises CircuitBreakerOpen if the circuit is open.
        """
        self._circuit.check()
        last_exc: Exception = RuntimeError("_retry: no attempts made")
        for attempt in range(1, retries + 1):
            try:
                result = await coro_fn(*args, **kwargs)
                self._circuit.record_success()
                return result
            except _RETRYABLE as exc:
                last_exc = exc
                logger.warning(
                    f"{label} attempt {attempt}/{retries} failed "
                    f"({type(exc).__name__}): {exc}"
                )
                if attempt < retries:
                    if isinstance(exc, ccxt.RateLimitExceeded):
                        wait = max(30.0, 2 ** attempt) + random.uniform(0, 2)
                    else:
                        wait = 2 ** attempt + random.uniform(0, 1)
                    await asyncio.sleep(wait)
        self._circuit.record_failure()
        logger.error(f"{label} failed after {retries} attempts: {last_exc}")
        raise last_exc

    async def connect(self, retries: int = 3):
        """Initialize futures connection with retry."""
        await self._retry(self.exchange.load_markets,
                          retries=retries, label='futures.connect')
        logger.info("Kraken Futures connection established")

    async def disconnect(self):
        await self.exchange.close()

    async def fetch_ohlcv(self, symbol: str, timeframe: str = '1m',
                          limit: int = 1000, since: Optional[int] = None,
                          retries: int = 3) -> List:
        """Fetch futures OHLCV with retry. Returns [] when all retries fail.
        CircuitBreakerOpen propagates unchanged.
        """
        perp = self.perp_symbol(symbol)
        try:
            return await self._retry(
                self.exchange.fetch_ohlcv, perp,
                timeframe=timeframe, limit=limit, since=since,
                retries=retries, label=f'futures.fetch_ohlcv({perp})',
            )
        except CircuitBreakerOpen:
            raise
        except Exception as exc:
            logger.error(f"futures.fetch_ohlcv({perp}) exhausted retries: {exc}")
            return []

    async def get_ticker(self, symbol: str, retries: int = 3) -> Dict:
        """Get futures ticker with retry."""
        return await self._retry(
            self.exchange.fetch_ticker, self.perp_symbol(symbol),
            retries=retries, label=f'futures.get_ticker({symbol})',
        )

    async def fetch_funding_rate(self, symbol: str,
                                  retries: int = 3) -> Optional[float]:
        """Current funding rate as a fraction (e.g. 0.0001 = 0.01% per 8h).

        Returns None on permanent failure so callers can skip gracefully.
        CircuitBreakerOpen propagates unchanged.
        """
        try:
            data = await self._retry(
                self.exchange.fetch_funding_rate, self.perp_symbol(symbol),
                retries=retries, label=f'futures.fetch_funding_rate({symbol})',
            )
            return data.get('fundingRate')
        except CircuitBreakerOpen:
            raise
        except Exception as e:
            logger.warning(f"Funding rate fetch failed for {symbol}: {e}")
            return None

    async def fetch_funding_rate_history(self, symbol: str, limit: int = 3,
                                          retries: int = 3) -> List[Dict]:
        """Recent funding rate history. Returns [] on permanent failure.
        CircuitBreakerOpen propagates unchanged.
        """
        try:
            return await self._retry(
                self.exchange.fetch_funding_rate_history,
                self.perp_symbol(symbol),
                limit=limit,
                retries=retries,
                label=f'futures.fetch_funding_rate_history({symbol})',
            )
        except CircuitBreakerOpen:
            raise
        except Exception as e:
            logger.warning(f"Funding history fetch failed for {symbol}: {e}")
            return []

    async def get_balance(self, retries: int = 3) -> Dict:
        """Get futures account balance with retry."""
        return await self._retry(
            self.exchange.fetch_balance,
            retries=retries, label='futures.get_balance',
        )

    async def create_order(self, symbol: str, order_type: str, side: str,
                           amount: float, price: Optional[float] = None,
                           leverage: int = 1) -> Dict:
        """Place a futures order. No retry — order creation is not idempotent."""
        perp = self.perp_symbol(symbol)
        params = {}
        if leverage > 1:
            params['leverage'] = leverage
        logger.info(f"Placing {order_type} {side} order: {amount} {perp} @ {price or 'market'} (lev={leverage}x)")
        return await self.exchange.create_order(perp, order_type, side, amount, price, params)

    async def cancel_order(self, order_id: str, symbol: str,
                           retries: int = 3) -> Dict:
        """Cancel a futures order with retry (cancellation is idempotent)."""
        return await self._retry(
            self.exchange.cancel_order, order_id, self.perp_symbol(symbol),
            retries=retries, label=f'futures.cancel_order({order_id})',
        )

    async def get_open_positions(self, retries: int = 3) -> List:
        """Get all open perp positions with retry. Returns [] on permanent failure.
        CircuitBreakerOpen propagates unchanged.
        """
        try:
            return await self._retry(
                self.exchange.fetch_positions,
                retries=retries, label='futures.get_open_positions',
            )
        except CircuitBreakerOpen:
            raise
        except Exception as e:
            logger.warning(f"Fetch positions failed: {e}")
            return []

    @classmethod
    def from_env(cls) -> 'KrakenFuturesConnection':
        import os
        from dotenv import load_dotenv
        load_dotenv()
        return cls(
            api_key=os.getenv('KRAKEN_FUTURES_API_KEY', ''),
            secret=os.getenv('KRAKEN_FUTURES_API_SECRET', ''),
            sandbox=os.getenv('KRAKEN_FUTURES_SANDBOX', 'true').lower() == 'true',
        )


async def test_connection():
    """Test exchange connection"""
    exchange = ExchangeConnection(sandbox=False)  # Public data doesn't need auth
    await exchange.connect()

    # Fetch recent BTC data
    ohlcv = await exchange.fetch_ohlcv('BTC/USD', timeframe='1m', limit=10)
    print(f"Latest BTC candle: {ohlcv[-1]}")

    await exchange.disconnect()


if __name__ == '__main__':
    asyncio.run(test_connection())
