"""
Kraken Exchange Connection Wrapper
Handles market data fetching and order execution via ccxt
"""

import ccxt.async_support as ccxt
import asyncio
from datetime import datetime, timedelta
from typing import Optional, List, Dict
import logging

logger = logging.getLogger(__name__)


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

    async def connect(self):
        """Initialize exchange connection"""
        await self.exchange.load_markets()
        logger.info("Exchange connection established")

    async def disconnect(self):
        """Close exchange connection"""
        await self.exchange.close()

    async def fetch_ohlcv(self, symbol: str, timeframe: str = '1m',
                          limit: int = 1000, since: Optional[int] = None) -> List:
        """
        Fetch candlestick data

        Args:
            symbol: Trading pair (e.g., 'BTC/USD')
            timeframe: Candle timeframe
            limit: Number of candles to fetch
            since: Unix timestamp in milliseconds to start from

        Returns:
            List of [timestamp, open, high, low, close, volume]
        """
        try:
            ohlcv = await self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit, since=since)
            logger.debug(f"Fetched {len(ohlcv)} candles for {symbol}")
            return ohlcv
        except Exception as e:
            logger.error(f"Error fetching OHLCV for {symbol}: {e}")
            return []

    async def fetch_ohlcv_between(self, symbol: str, timeframe: str,
                                   start_date: str, end_date: str) -> List:
        """
        Fetch historical data between two dates

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
            logger.debug(f"Fetching data for {symbol} from {datetime.fromtimestamp(current_ms/1000)}")
            batch = await self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=1000, since=current_ms)

            if not batch:
                break

            all_data.extend(batch)
            current_ms = batch[-1][0] + 1  # Move past last candle

        logger.info(f"Fetched {len(all_data)} total candles for {symbol}")
        return all_data

    async def get_ticker(self, symbol: str) -> Dict:
        """Get current ticker price"""
        ticker = await self.exchange.fetch_ticker(symbol)
        return ticker

    async def get_balance(self) -> Dict:
        """Get account balance"""
        balance = await self.exchange.fetch_balance()
        return balance

    async def create_order(self, symbol: str, order_type: str, side: str,
                           amount: float, price: Optional[float] = None) -> Dict:
        """
        Place an order

        Args:
            symbol: Trading pair
            order_type: 'market' or 'limit'
            side: 'buy' or 'sell'
            amount: Amount to trade
            price: Price for limit orders

        Returns:
            Order response from exchange
        """
        params = {'symbol': symbol, 'type': order_type, 'side': side, 'amount': amount}
        if price and order_type == 'limit':
            params['price'] = price

        logger.info(f"Placing {order_type} {side} order for {amount} {symbol} at {price or 'market'}")
        order = await self.exchange.create_order(**params)
        return order

    async def cancel_order(self, order_id: str, symbol: str) -> Dict:
        """Cancel an order"""
        return await self.exchange.cancel_order(order_id, symbol)

    async def get_open_orders(self, symbol: Optional[str] = None) -> List:
        """Get open orders"""
        return await self.exchange.fetch_open_orders(symbol)

    async def get_trades(self, symbol: Optional[str] = None, since: Optional[int] = None) -> List:
        """Get trade history"""
        return await self.exchange.fetch_trades(symbol, since=since)


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
