"""
Global equity data via yfinance.
Used for ASX, LSE, DAX, Asia Pacific, and Emerging Market tickers.
Returns the same OHLCV DataFrame format as alpaca_collector.py.
"""
import logging

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)


def get_yfinance_bars(symbol: str, lookback_days: int = 60) -> pd.DataFrame:
    """
    Fetch daily OHLCV bars for any exchange-suffix equity symbol.
    Examples: BHP.AX, SHEL.L, SAP.DE, 7203.T, 0700.HK, RELIANCE.NS
    Returns DataFrame with columns Open, High, Low, Close, Volume
    matching the format of alpaca_collector.get_equity_bars().
    """
    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(period=f"{lookback_days}d", interval="1d", auto_adjust=True)

        if df is None or df.empty:
            logger.warning("yfinance returned no data for %s", symbol)
            return pd.DataFrame()

        df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
        df.index = pd.to_datetime(df.index)
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)

        logger.debug("yfinance bars fetched for %s: %d rows", symbol, len(df))
        return df

    except Exception as exc:
        logger.error("Failed to fetch yfinance bars for %s: %s", symbol, exc)
        return pd.DataFrame()


def get_latest_price(symbol: str) -> float | None:
    try:
        df = get_yfinance_bars(symbol, lookback_days=5)
        if df.empty:
            return None
        return float(df["Close"].iloc[-1])
    except Exception as exc:
        logger.error("Failed to get yfinance latest price for %s: %s", symbol, exc)
        return None


def check_connectivity(test_symbols: list[str] | None = None) -> dict[str, bool]:
    """
    Verify yfinance can fetch data for a representative set of symbols.
    Returns {symbol: ok} for each test symbol.
    """
    if test_symbols is None:
        test_symbols = ["BHP.AX", "SHEL.L", "SAP.DE", "7203.T", "0700.HK"]
    results: dict[str, bool] = {}
    for sym in test_symbols:
        try:
            df = get_yfinance_bars(sym, lookback_days=10)
            results[sym] = not df.empty
        except Exception:
            results[sym] = False
    return results
