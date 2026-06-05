"""
Global equities data via Alpha Vantage API.
Used for Tier 2 instruments (ASX, LSE, DAX) and general equities fallback.
MASSIVE_API_KEY in .env holds the Alpha Vantage API key.
"""
import logging

import pandas as pd
import requests

from config import config

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
