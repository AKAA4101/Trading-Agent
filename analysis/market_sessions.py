"""
Market session awareness.
Determines whether an instrument's primary exchange has recently closed,
so the 4-hourly analysis cycle only processes instruments with fresh EOD data.

Forex and crypto are always considered active (24/5 and 24/7 respectively).
For all other instruments, we check whether the exchange has had a close
within the last 24 hours.
"""
from datetime import datetime, timedelta, time as dtime, timezone

# Close times in UTC for each exchange (hour, minute).
# These are approximate — DST offsets are not modelled here.
EXCHANGE_CLOSE_TIMES: dict[str, dtime] = {
    "US":   dtime(20, 0),   # NYSE / NASDAQ  (09:30-16:00 ET = 13:30-20:00 UTC)
    "LSE":  dtime(16, 30),  # London Stock Exchange
    "DAX":  dtime(16, 30),  # XETRA Frankfurt
    "ASX":  dtime(6,  0),   # ASX Sydney  (10:00-16:00 AEST = 00:00-06:00 UTC)
    "TSE":  dtime(6,  30),  # Tokyo Stock Exchange
    "HKEX": dtime(8,  0),   # Hong Kong Exchanges  (09:30-16:00 HKT = 01:30-08:00 UTC)
    "BSE":  dtime(10, 0),   # NSE/BSE India  (09:15-15:30 IST = 03:45-10:00 UTC)
    "KRX":  dtime(6,  30),  # Korea Exchange
    "JSE":  dtime(15, 0),   # Johannesburg Stock Exchange  (09:00-17:00 SAST = 07:00-15:00 UTC)
    "BMV":  dtime(21, 0),   # Mexico BMV  (08:30-15:00 CST = 14:30-21:00 UTC)
    "B3":   dtime(20, 0),   # B3 Brazil  (10:00-17:00 BRT = 13:00-20:00 UTC)
}

# Ticker suffix → exchange key
_SUFFIX_MAP: list[tuple[str, str]] = [
    (".AX",  "ASX"),
    (".L",   "LSE"),
    (".DE",  "DAX"),
    (".F",   "DAX"),
    (".T",   "TSE"),
    (".HK",  "HKEX"),
    (".NS",  "BSE"),
    (".BO",  "BSE"),
    (".KS",  "KRX"),
    (".KQ",  "KRX"),
    (".JO",  "JSE"),
    (".MX",  "BMV"),
    (".SA",  "B3"),
]


def get_exchange(ticker: str) -> str:
    """Return the exchange key for a ticker symbol."""
    upper = ticker.upper()
    for suffix, exchange in _SUFFIX_MAP:
        if upper.endswith(suffix.upper()):
            return exchange
    if "/" in upper or "_" in upper:
        return "FOREX"
    return "US"


def is_market_open(ticker: str) -> bool:
    """
    Return True if the instrument's primary market is currently open.

    Market hours (UTC, approximate):
      US Equities  Mon-Fri 13:30-20:00
      LSE London   Mon-Fri 08:00-16:30
      DAX Frankfurt Mon-Fri 08:00-16:30
      ASX Sydney   Mon-Fri 00:00-06:00
      TSE Tokyo    Mon-Fri 00:00-06:30
      HKEX         Mon-Fri 01:30-08:00
      BSE India    Mon-Fri 03:45-10:00
      KRX Korea    Mon-Fri 00:00-06:30
      JSE S.Africa Mon-Fri 07:00-15:00
      BMV Mexico   Mon-Fri 14:30-21:00
      B3 Brazil    Mon-Fri 13:00-20:00
      Forex        24/5 (Mon 00:00 – Fri 22:00 UTC)
      Crypto       24/7
    """
    exchange = get_exchange(ticker)
    now = datetime.now(timezone.utc)
    weekday = now.weekday()  # 0=Mon, 6=Sun

    if exchange == "CRYPTO":
        return True
    if exchange == "FOREX":
        return weekday < 5  # Mon-Fri only (simplified)

    if weekday >= 5:
        return False  # Weekend

    open_close: dict[str, tuple[dtime, dtime]] = {
        "US":   (dtime(13, 30), dtime(20, 0)),
        "LSE":  (dtime(8,  0),  dtime(16, 30)),
        "DAX":  (dtime(8,  0),  dtime(16, 30)),
        "ASX":  (dtime(0,  0),  dtime(6,  0)),
        "TSE":  (dtime(0,  0),  dtime(6,  30)),
        "HKEX": (dtime(1,  30), dtime(8,  0)),
        "BSE":  (dtime(3,  45), dtime(10, 0)),
        "KRX":  (dtime(0,  0),  dtime(6,  30)),
        "JSE":  (dtime(7,  0),  dtime(15, 0)),
        "BMV":  (dtime(14, 30), dtime(21, 0)),
        "B3":   (dtime(13, 0),  dtime(20, 0)),
    }
    bounds = open_close.get(exchange)
    if not bounds:
        return False
    open_t, close_t = bounds
    current_t = now.time().replace(tzinfo=None)
    return open_t <= current_t < close_t


def has_market_recently_closed(ticker: str, within_hours: int = 24) -> bool:
    """
    Return True if the instrument's exchange has had a market close
    within the last `within_hours` hours.

    Used to gate yfinance EOD analysis: only analyse an instrument
    when a fresh daily bar is available.  Forex and crypto always return True.
    """
    exchange = get_exchange(ticker)

    if exchange in ("FOREX", "CRYPTO"):
        return True

    close_time = EXCHANGE_CLOSE_TIMES.get(exchange)
    if close_time is None:
        return True  # Unknown exchange — don't skip

    now = datetime.now(timezone.utc)

    # Scan back up to 4 calendar days to find the most recent weekday close.
    for days_ago in range(4):
        candidate_date = (now - timedelta(days=days_ago)).date()
        candidate_dt = datetime.combine(candidate_date, close_time, tzinfo=timezone.utc)

        if candidate_dt > now:
            continue  # This close hasn't happened yet today
        if candidate_dt.weekday() >= 5:
            continue  # Weekend — no close

        age_hours = (now - candidate_dt).total_seconds() / 3600
        if age_hours <= within_hours:
            return True

    return False
