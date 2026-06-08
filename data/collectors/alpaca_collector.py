import logging
from datetime import datetime, timedelta, timezone

import pandas as pd
from alpaca.data import StockHistoricalDataClient, CryptoHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame

from config import config

logger = logging.getLogger(__name__)


def _stock_client() -> StockHistoricalDataClient:
    return StockHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY)


def _crypto_client() -> CryptoHistoricalDataClient:
    return CryptoHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY)


def get_equity_bars(symbol: str, lookback_days: int = 90) -> pd.DataFrame:
    """Return daily OHLCV bars for a US equity symbol."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=lookback_days)
    try:
        client = _stock_client()
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
            feed='iex',
        )
        bars = client.get_stock_bars(req)
        df = bars.df
        if isinstance(df.index, pd.MultiIndex):
            df = df.loc[symbol] if symbol in df.index.get_level_values(0) else df.droplevel(0)
        df = df.rename(columns={"open": "Open", "high": "High", "low": "Low",
                                 "close": "Close", "volume": "Volume"})
        df.index = pd.to_datetime(df.index)
        logger.debug("Alpaca equity bars fetched for %s: %d rows", symbol, len(df))
        return df
    except Exception as exc:
        logger.error("Failed to fetch equity bars for %s: %s", symbol, exc)
        return pd.DataFrame()


def get_crypto_bars(symbol: str, lookback_days: int = 90) -> pd.DataFrame:
    """Return daily OHLCV bars for a crypto pair (e.g. BTC/USD)."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=lookback_days)
    try:
        client = _crypto_client()
        req = CryptoBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
        )
        bars = client.get_crypto_bars(req)
        df = bars.df
        if isinstance(df.index, pd.MultiIndex):
            df = df.droplevel(0)
        df = df.rename(columns={"open": "Open", "high": "High", "low": "Low",
                                 "close": "Close", "volume": "Volume"})
        df.index = pd.to_datetime(df.index)
        logger.debug("Alpaca crypto bars fetched for %s: %d rows", symbol, len(df))
        return df
    except Exception as exc:
        logger.error("Failed to fetch crypto bars for %s: %s", symbol, exc)
        return pd.DataFrame()


def get_latest_price(symbol: str, market_type: str) -> float | None:
    try:
        if market_type == "crypto":
            df = get_crypto_bars(symbol, lookback_days=2)
        else:
            df = get_equity_bars(symbol, lookback_days=2)
        if df.empty:
            return None
        return float(df["Close"].iloc[-1])
    except Exception as exc:
        logger.error("Failed to get latest price for %s: %s", symbol, exc)
        return None
