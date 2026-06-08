"""
Global equity data via yfinance.
Used for ASX, LSE, DAX, Asia Pacific, and Emerging Market tickers.
Returns the same OHLCV DataFrame format as alpaca_collector.py.
"""
import logging
import os
import pickle
import time

import pandas as pd
import yfinance as yf

_WEEKLY_CACHE_DIR = "/opt/trading-agent/data/cache/weekly/"
_WEEKLY_CACHE_TTL = 86400  # 24 hours

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


def get_yfinance_bars_weekly(symbol: str, lookback_weeks: int = 52) -> pd.DataFrame | None:
    """
    Fetch weekly OHLCV bars via yfinance — used for multi-timeframe analysis.
    Results are cached for 24 hours. On fetch failure, a stale cache is returned
    rather than None so MTF bias is preserved across transient outages.
    """
    cache_path = os.path.join(_WEEKLY_CACHE_DIR, f"{symbol}_yfinance_weekly.pkl")

    # ── Cache read ────────────────────────────────────────────────
    stale_df = None
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "rb") as f:
                cached_df = pickle.load(f)
            if time.time() - os.path.getmtime(cache_path) < _WEEKLY_CACHE_TTL:
                logger.debug("Weekly cache hit for %s", symbol)
                return cached_df
            stale_df = cached_df  # expired but keep as fallback
        except Exception as cache_exc:
            logger.debug("Weekly cache read failed for %s: %s", symbol, cache_exc)

    # ── Fetch from yfinance ───────────────────────────────────────
    try:
        period = f"{min(lookback_weeks * 7, 730)}d"
        ticker = yf.Ticker(symbol)
        df = ticker.history(period=period, interval="1wk", auto_adjust=True)
        if df is None or df.empty:
            if stale_df is not None:
                logger.warning("yfinance returned no weekly data for %s — returning stale cache", symbol)
                return stale_df
            return None
        df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
        df.index = pd.to_datetime(df.index)
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        logger.debug("yfinance weekly bars fetched for %s: %d rows", symbol, len(df))

        # ── Cache write ───────────────────────────────────────────
        try:
            os.makedirs(_WEEKLY_CACHE_DIR, exist_ok=True)
            with open(cache_path, "wb") as f:
                pickle.dump(df, f)
        except Exception as cache_exc:
            logger.debug("Weekly cache write failed for %s: %s", symbol, cache_exc)

        return df

    except Exception as e:
        if stale_df is not None:
            logger.warning("Weekly yfinance bars fetch failed for %s: %s — returning stale cache", symbol, e)
            return stale_df
        logger.warning("Weekly yfinance bars failed for %s: %s", symbol, e)
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
