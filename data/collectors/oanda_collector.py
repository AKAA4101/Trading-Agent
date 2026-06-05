import logging
from datetime import datetime, timedelta, timezone

import pandas as pd
import oandapyV20
import oandapyV20.endpoints.instruments as instruments

from config import config

logger = logging.getLogger(__name__)


def _client() -> oandapyV20.API:
    return oandapyV20.API(access_token=config.OANDA_API_TOKEN,
                          environment="practice")


def get_forex_bars(instrument: str, lookback_days: int = 60,
                   granularity: str = "D") -> pd.DataFrame:
    """
    Fetch OHLCV candles for an OANDA instrument (e.g. EUR_USD).
    Returns a DataFrame with columns Open, High, Low, Close, Volume.
    """
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
        req = instruments.InstrumentsCandles(instrument=instrument, params=params)
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
