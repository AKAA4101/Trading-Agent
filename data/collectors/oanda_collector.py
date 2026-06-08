import logging
import os
import pickle
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import oandapyV20
import oandapyV20.endpoints.instruments as instruments

from config import config

_WEEKLY_CACHE_DIR = "/opt/trading-agent/data/cache/weekly/"
_WEEKLY_CACHE_TTL = 86400  # 24 hours

logger = logging.getLogger(__name__)


def _client() -> oandapyV20.API:
    return oandapyV20.API(access_token=config.OANDA_API_TOKEN,
                          environment="practice")


def get_forex_bars(instrument: str, lookback_days: int = 90,
                   granularity: str = "D") -> pd.DataFrame:
    """
    Fetch OHLCV candles for an OANDA instrument (e.g. EUR_USD or EUR/USD).
    Returns a DataFrame with columns Open, High, Low, Close, Volume.
    """
    # OANDA requires underscore format (EUR_USD), not slash (EUR/USD)
    oanda_instrument = instrument.replace("/", "_")
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=lookback_days)
    params = {
        "from": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "to":   end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "granularity": granularity,
        "price": "M",  # mid prices
    }
    try:
        client = _client()
        req = instruments.InstrumentsCandles(instrument=oanda_instrument, params=params)
        client.request(req)
        candles = req.response.get("candles", [])
        rows = []
        for c in candles:
            if not c.get("complete", False):
                continue
            mid = c["mid"]
            rows.append({
                "timestamp": pd.to_datetime(c["time"]),
                "Open":   float(mid["o"]),
                "High":   float(mid["h"]),
                "Low":    float(mid["l"]),
                "Close":  float(mid["c"]),
                "Volume": int(c.get("volume", 0)),
            })
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows).set_index("timestamp")
        df.index = pd.to_datetime(df.index, utc=True)
        logger.debug("OANDA bars fetched for %s: %d rows", instrument, len(df))
        return df
    except Exception as exc:
        logger.error("Failed to fetch OANDA bars for %s: %s", instrument, exc)
        return pd.DataFrame()


def get_latest_price(instrument: str) -> float | None:
    try:
        df = get_forex_bars(instrument, lookback_days=2)
        if df.empty:
            return None
        return float(df["Close"].iloc[-1])
    except Exception as exc:
        logger.error("Failed to get latest OANDA price for %s: %s", instrument, exc)
        return None


def get_forex_bars_weekly(instrument: str, lookback_weeks: int = 52) -> pd.DataFrame | None:
    """
    Fetch weekly OHLCV bars for a forex pair — used for multi-timeframe analysis.
    Results are cached for 24 hours. On fetch failure, a stale cache is returned
    rather than None so MTF bias is preserved across transient API outages.
    """
    cache_path = os.path.join(_WEEKLY_CACHE_DIR, f"{instrument}_oanda_weekly.pkl")

    # ── Cache read ────────────────────────────────────────────────
    stale_df = None
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "rb") as f:
                cached_df = pickle.load(f)
            if time.time() - os.path.getmtime(cache_path) < _WEEKLY_CACHE_TTL:
                logger.debug("Weekly cache hit for %s", instrument)
                return cached_df
            stale_df = cached_df  # expired but keep as fallback
        except Exception as cache_exc:
            logger.debug("Weekly cache read failed for %s: %s", instrument, cache_exc)

    # ── Fetch from OANDA ──────────────────────────────────────────
    try:
        oanda_instrument = instrument.replace("/", "_")
        start = datetime.now(timezone.utc) - timedelta(weeks=lookback_weeks)
        params = {
            "from":        start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "granularity": "W",
            "price":       "M",
        }
        client = _client()
        req = instruments.InstrumentsCandles(instrument=oanda_instrument, params=params)
        client.request(req)
        raw = req.response.get("candles", [])
        if not raw:
            if stale_df is not None:
                logger.warning("OANDA returned no weekly candles for %s — returning stale cache", instrument)
                return stale_df
            return None
        rows = []
        for c in raw:
            if not c.get("complete", True):
                continue
            mid = c.get("mid", {})
            rows.append({
                "Open":   float(mid.get("o", 0)),
                "High":   float(mid.get("h", 0)),
                "Low":    float(mid.get("l", 0)),
                "Close":  float(mid.get("c", 0)),
                "Volume": float(c.get("volume", 0)),
                "time":   c["time"],
            })
        if not rows:
            if stale_df is not None:
                logger.warning("OANDA returned empty weekly rows for %s — returning stale cache", instrument)
                return stale_df
            return None
        df = pd.DataFrame(rows)
        df["time"] = pd.to_datetime(df["time"], utc=True)
        df = df.set_index("time").sort_index()
        df = df[["Open", "High", "Low", "Close", "Volume"]].copy()

        # ── Cache write ───────────────────────────────────────────
        try:
            os.makedirs(_WEEKLY_CACHE_DIR, exist_ok=True)
            with open(cache_path, "wb") as f:
                pickle.dump(df, f)
        except Exception as cache_exc:
            logger.debug("Weekly cache write failed for %s: %s", instrument, cache_exc)

        return df

    except Exception as e:
        if stale_df is not None:
            logger.warning("Weekly forex bars fetch failed for %s: %s — returning stale cache", instrument, e)
            return stale_df
        logger.warning("Weekly forex bars failed for %s: %s", instrument, e)
        return None


def get_account_summary() -> dict:
    try:
        import oandapyV20.endpoints.accounts as accounts
        client = _client()
        req = accounts.AccountSummary(accountID=config.OANDA_ACCOUNT_ID)
        client.request(req)
        return req.response.get("account", {})
    except Exception as exc:
        logger.error("Failed to get OANDA account summary: %s", exc)
        return {}
