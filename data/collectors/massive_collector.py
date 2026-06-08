"""
Global equities data via Alpha Vantage API.
Used for Tier 2 instruments (ASX, LSE, DAX) and general equities fallback.
MASSIVE_API_KEY in .env holds the Alpha Vantage API key.
"""
import logging
import os
import pickle
import time

import pandas as pd
import requests

from config import config

_WEEKLY_CACHE_DIR = "/opt/trading-agent/data/cache/weekly/"
_WEEKLY_CACHE_TTL = 86400  # 24 hours

logger = logging.getLogger(__name__)

BASE_URL = "https://www.alphavantage.co/query"


def _params(extra: dict) -> dict:
    return {"apikey": config.MASSIVE_API_KEY, **extra}


def get_global_equity_bars(symbol: str, lookback_days: int = 60) -> pd.DataFrame:
    """
    Fetch daily OHLCV bars for any equity symbol via Alpha Vantage.
    symbol examples: BHP.AX, SHEL.L, SAP.DE, AAPL
    """
    try:
        resp = requests.get(
            BASE_URL,
            params=_params({
                "function": "TIME_SERIES_DAILY_ADJUSTED",
                "symbol": symbol,
                "outputsize": "compact",   # last 100 data points
            }),
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

        if "Error Message" in data:
            logger.warning("Alpha Vantage error for %s: %s", symbol, data["Error Message"])
            return pd.DataFrame()

        if "Note" in data:
            logger.warning("Alpha Vantage rate limit hit for %s", symbol)
            return pd.DataFrame()

        ts = data.get("Time Series (Daily)", {})
        if not ts:
            logger.warning("No time series data for %s", symbol)
            return pd.DataFrame()

        rows = []
        for date_str, bar in ts.items():
            rows.append({
                "timestamp": pd.to_datetime(date_str),
                "Open":   float(bar["1. open"]),
                "High":   float(bar["2. high"]),
                "Low":    float(bar["3. low"]),
                "Close":  float(bar["5. adjusted close"]),
                "Volume": int(bar["6. volume"]),
            })

        df = (
            pd.DataFrame(rows)
            .set_index("timestamp")
            .sort_index()
            .tail(lookback_days)
        )
        logger.debug("Alpha Vantage bars fetched for %s: %d rows", symbol, len(df))
        return df

    except Exception as exc:
        logger.error("Failed to fetch Alpha Vantage bars for %s: %s", symbol, exc)
        return pd.DataFrame()


def get_latest_price(symbol: str) -> float | None:
    try:
        resp = requests.get(
            BASE_URL,
            params=_params({"function": "GLOBAL_QUOTE", "symbol": symbol}),
            timeout=10,
        )
        resp.raise_for_status()
        quote = resp.json().get("Global Quote", {})
        price_str = quote.get("05. price")
        return float(price_str) if price_str else None
    except Exception as exc:
        logger.error("Failed to get Alpha Vantage latest price for %s: %s", symbol, exc)
        return None


def get_global_equity_bars_weekly(symbol: str, lookback_weeks: int = 52) -> pd.DataFrame | None:
    """
    Fetch weekly OHLCV bars via Alpha Vantage — used for multi-timeframe analysis.
    Uses TIME_SERIES_WEEKLY_ADJUSTED endpoint. Results are cached for 24 hours.
    On fetch failure, a stale cache is returned rather than None so MTF bias
    is preserved across transient API outages.
    """
    cache_path = os.path.join(_WEEKLY_CACHE_DIR, f"{symbol}_weekly.pkl")

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

    # ── Fetch from Alpha Vantage ──────────────────────────────────
    try:
        resp = requests.get(
            BASE_URL,
            params=_params({
                "function":   "TIME_SERIES_WEEKLY_ADJUSTED",
                "symbol":     symbol,
                "outputsize": "compact",
            }),
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        weekly = data.get("Weekly Adjusted Time Series", {})
        if not weekly:
            if stale_df is not None:
                logger.warning("Alpha Vantage returned no weekly data for %s — returning stale cache", symbol)
                return stale_df
            return None
        rows = []
        for date_str, vals in weekly.items():
            rows.append({
                "Open":   float(vals.get("1. open", 0)),
                "High":   float(vals.get("2. high", 0)),
                "Low":    float(vals.get("3. low", 0)),
                "Close":  float(vals.get("5. adjusted close", vals.get("4. close", 0))),
                "Volume": float(vals.get("6. volume", 0)),
                "time":   date_str,
            })
        if not rows:
            if stale_df is not None:
                logger.warning("Alpha Vantage returned empty rows for %s — returning stale cache", symbol)
                return stale_df
            return None
        df = pd.DataFrame(rows)
        df["time"] = pd.to_datetime(df["time"], utc=True)
        df = df.set_index("time").sort_index()
        df = df.tail(lookback_weeks)[["Open", "High", "Low", "Close", "Volume"]].copy()

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
            logger.warning("Weekly global equity bars fetch failed for %s: %s — returning stale cache", symbol, e)
            return stale_df
        logger.warning("Weekly global equity bars failed for %s: %s", symbol, e)
        return None


def check_connectivity() -> bool:
    """Return True if Alpha Vantage API key is valid."""
    try:
        resp = requests.get(
            BASE_URL,
            params=_params({"function": "GLOBAL_QUOTE", "symbol": "AAPL"}),
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        return "Global Quote" in data and bool(data["Global Quote"])
    except Exception:
        return False
