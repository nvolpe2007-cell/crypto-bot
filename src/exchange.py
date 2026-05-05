"""
Kraken Exchange Connection Wrapper
Handles market data fetching and order execution via ccxt
"""

import ccxt.async_support as ccxt
import asyncio
from datetime import datetime
from typing import Optional, List, Dict
import logging

logger = logging.getLogger(__name__)

# Transient errors worth retrying: network blips, timeouts, rate-limit back-off.
# Non-transient errors (AuthenticationError, InsufficientFunds, etc.) propagate
# immediately — retrying them would not help and could mask real problems.
_RETRYABLE = (ccxt.NetworkError, ccxt.RequestTimeout, ccxt.RateLimitExceeded)


class ExchangeConnection:
    """Async wrapper around Kraken exchange"""

    def __init__(self, api_key: str = None, secret: str = None, sandbox: bool = True):
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
        logger.info(f"Exchange initialized (sandbox={sandbox})")

    async def _retry(self, coro_fn, *args, retries: int = 3,
                     label: str = '?', **kwargs):
        """Call coro_fn(*args, **kwargs) with exponential-backoff retry.

        Retries only on _RETRYABLE (NetworkError, RequestTimeout,
        RateLimitExceeded).  Any other exception propagates immediately so that
        programming errors and permanent exchange rejections are never hidden.

        Raises the last seen exception when all attempts are exhausted.
        """
        last_exc: Exception = RuntimeError("_retry: no attempts made")
        for attempt in range(1, retries + 1):
            try:
                return await coro_fn(*args, **kwargs)
            except _RETRYABLE as exc:
                last_exc = exc
                logger.warning(
                    f"{label} attempt {attempt}/{retries} failed "
                    f"({type(exc).__name__}): {exc}"
                )
                if attempt < retries:
                    await asyncio.sleep(2 ** attempt)   # 2 s, 4 s, …
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

        Returns [] (rather than raising) when all retries are exhausted so that
        callers that poll in a loop can simply skip the symbol for this tick.
        """
        try:
            return await self._retry(
                self.exchange.fetch_ohlcv, symbol,
                timeframe=timeframe, limit=limit, since=since,
                retries=retries, label=f'fetch_ohlcv({symbol})',
            )
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
            current_ms = batch[-1][0] + 1   # move past last candle

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
