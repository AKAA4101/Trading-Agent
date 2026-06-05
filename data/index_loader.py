"""
Dynamic index constituent loader.
Fetches full index membership from Wikipedia, caches results to disk for
one week, and returns Instrument objects for the analysis pipeline.

refresh_all_indices() is called weekly from run_tier2_screen() every Monday.
get_dynamic_instruments() reads from cache and is called by watchlist.get_active().
"""
import io
import json
import logging
import os
import re
import time

import pandas as pd
import requests

logger = logging.getLogger(__name__)

CACHE_PATH = os.path.join(os.path.dirname(__file__), "index_cache.json")
CACHE_TTL_SECONDS = 7 * 24 * 3600  # 1 week


# ── Wikipedia HTTP helper ─────────────────────────────────────────────────

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def _wiki_tables(url: str) -> list[pd.DataFrame]:
    """Fetch a Wikipedia page with a browser User-Agent and parse all tables."""
    resp = requests.get(url, headers=_HEADERS, timeout=20)
    resp.raise_for_status()
    return pd.read_html(io.StringIO(resp.text))


# ── Per-index Wikipedia scrapers ──────────────────────────────────────────

def _fetch_sp500() -> list[str]:
    """~503 US equities from the S&P 500."""
    try:
        df = _wiki_tables("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")[0]
        syms = df["Symbol"].dropna().astype(str).str.strip().tolist()
        return [s for s in syms if 1 <= len(s) <= 10]
    except Exception as exc:
        logger.warning("S&P 500 fetch failed: %s", exc)
        return []


def _fetch_nasdaq100() -> list[str]:
    """~100 Nasdaq-listed equities."""
    url = "https://en.wikipedia.org/wiki/Nasdaq-100"
    col_candidates = ["Ticker", "Symbol", "Ticker symbol"]
    try:
        tables = _wiki_tables(url)
        for i, df in enumerate(tables):
            for col in col_candidates:
                if col in df.columns:
                    syms = df[col].dropna().astype(str).str.strip().tolist()
                    syms = [s for s in syms if re.match(r"^[A-Z]{1,5}$", s)]
                    if len(syms) >= 50:
                        logger.debug("NASDAQ 100: %d symbols at table %d col '%s'", len(syms), i, col)
                        return syms
    except Exception as exc:
        logger.warning("NASDAQ 100 fetch failed: %s", exc)
    return []


def _fetch_ftse100() -> list[str]:
    """~100 LSE equities (with .L suffix)."""
    url = "https://en.wikipedia.org/wiki/FTSE_100_Index"
    col_candidates = ["EPIC", "Ticker", "Symbol", "Epic", "Ticker symbol", "Code"]
    try:
        tables = _wiki_tables(url)
        for i, df in enumerate(tables):
            for col in col_candidates:
                if col in df.columns:
                    syms = df[col].dropna().astype(str).str.strip().tolist()
                    syms = [s for s in syms if re.match(r"^[A-Z0-9]{1,8}$", s)]
                    if len(syms) >= 80:
                        result = [s + ".L" if not s.endswith(".L") else s for s in syms]
                        logger.debug("FTSE 100: %d at table %d col '%s'", len(result), i, col)
                        return result
    except Exception as exc:
        logger.warning("FTSE 100 fetch failed: %s", exc)
    return []


def _fetch_dax40() -> list[str]:
    """~40 XETRA equities (with .DE suffix)."""
    url = "https://en.wikipedia.org/wiki/DAX"
    col_candidates = ["Ticker symbol", "Ticker", "Symbol", "Xetra code", "Kürzel"]
    try:
        tables = _wiki_tables(url)
        for i, df in enumerate(tables):
            for col in col_candidates:
                if col in df.columns:
                    syms = df[col].dropna().astype(str).str.strip().tolist()
                    syms = [s.replace(".DE", "") for s in syms]
                    syms = [s for s in syms if re.match(r"^[A-Z0-9]{1,8}$", s)]
                    if len(syms) >= 30:
                        result = [s + ".DE" for s in syms]
                        logger.debug("DAX 40: %d at table %d col '%s'", len(result), i, col)
                        return result
    except Exception as exc:
        logger.warning("DAX 40 fetch failed: %s", exc)
    return []


def _fetch_asx200() -> list[str]:
    """~200 ASX equities (with .AX suffix)."""
    url = "https://en.wikipedia.org/wiki/S%26P/ASX_200"
    col_candidates = ["Code", "ASX code", "ASX Code", "Ticker", "Symbol", "ASX ticker"]
    try:
        tables = _wiki_tables(url)
        for i, df in enumerate(tables):
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = [
                    " ".join(str(c) for c in col if "Unnamed" not in str(c) and str(c) != "nan").strip()
                    for col in df.columns
                ]
            for col in col_candidates:
                if col in df.columns:
                    syms = df[col].dropna().astype(str).str.strip().tolist()
                    syms = [s for s in syms if re.match(r"^[A-Z0-9]{1,8}$", s)]
                    if len(syms) >= 150:
                        result = [s + ".AX" if not s.endswith(".AX") else s for s in syms]
                        logger.debug("ASX 200: %d at table %d col '%s'", len(result), i, col)
                        return result
    except Exception as exc:
        logger.warning("ASX 200 fetch failed: %s", exc)
    return []


def _fetch_nikkei225() -> list[str]:
    """
    ~225 TSE equities (with .T suffix).
    The Wikipedia Nikkei 225 page does not have a structured table of stock
    codes — constituents appear only as a concatenated company-name string in
    a navigation template.  This fetcher therefore returns an empty list;
    the 5 key TSE stocks in CORE_WATCHLIST (7203.T, 6758.T, 9984.T, 6861.T,
    8306.T) provide baseline TSE coverage until a structured source is added.
    """
    logger.info(
        "Nikkei 225: Wikipedia page contains no structured code table — "
        "skipping dynamic fetch (core TSE stocks still analysed)"
    )
    return []


def _fetch_hangseng() -> list[str]:
    """~80 HKEX equities (with .HK suffix)."""
    url = "https://en.wikipedia.org/wiki/Hang_Seng_Index"
    col_candidates = ["Code", "Stock code", "Symbol", "Ticker", "HKEX code", "Constituent code"]
    try:
        tables = _wiki_tables(url)
        for i, df in enumerate(tables):
            for col in col_candidates:
                if col in df.columns:
                    vals = df[col].dropna().astype(str).str.strip().tolist()
                    syms = []
                    for v in vals:
                        v_clean = v.replace(".HK", "").strip()
                        if re.match(r"^\d{1,5}$", v_clean):
                            syms.append(v_clean.zfill(4))
                    if len(syms) >= 50:
                        result = [s + ".HK" for s in syms]
                        logger.debug("Hang Seng: %d at table %d col '%s'", len(result), i, col)
                        return result
            # Auto-detect: any column with 50+ short numeric codes
            for col in df.columns:
                vals = df[col].dropna().astype(str).str.strip().tolist()
                syms = [v.zfill(4) for v in vals if re.match(r"^\d{1,5}$", v) and int(v) < 10000]
                if len(syms) >= 50:
                    result = [s + ".HK" for s in syms]
                    logger.debug("Hang Seng: %d at table %d col '%s' (auto)", len(result), i, col)
                    return result
    except Exception as exc:
        logger.warning("Hang Seng fetch failed: %s", exc)
    return []


def _fetch_nifty50() -> list[str]:
    """50 NSE India equities (with .NS suffix)."""
    url = "https://en.wikipedia.org/wiki/NIFTY_50"
    col_candidates = ["Symbol", "Ticker", "NSE symbol", "NSE Symbol", "Ticker symbol"]
    try:
        tables = _wiki_tables(url)
        for i, df in enumerate(tables):
            for col in col_candidates:
                if col in df.columns:
                    syms = df[col].dropna().astype(str).str.strip().tolist()
                    syms = [s for s in syms if re.match(r"^[A-Z&]{1,25}$", s) and s != "NSE"]
                    if len(syms) >= 40:
                        result = [s + ".NS" if not s.endswith(".NS") else s for s in syms]
                        logger.debug("Nifty 50: %d at table %d col '%s'", len(result), i, col)
                        return result
    except Exception as exc:
        logger.warning("Nifty 50 fetch failed: %s", exc)
    return []


# ── Index registry ────────────────────────────────────────────────────────

# (cache_key, fetcher, market_type, exchange_label)
INDEX_REGISTRY: list[tuple[str, object, str, str]] = [
    ("sp500",     _fetch_sp500,     "us_equity", ""),
    ("nasdaq100", _fetch_nasdaq100, "us_equity", ""),
    ("ftse100",   _fetch_ftse100,   "yfinance",  "LSE"),
    ("dax40",     _fetch_dax40,     "yfinance",  "DAX"),
    ("asx200",    _fetch_asx200,    "yfinance",  "ASX"),
    ("nikkei225", _fetch_nikkei225, "yfinance",  "TSE"),
    ("hangseng",  _fetch_hangseng,  "yfinance",  "HKEX"),
    ("nifty50",   _fetch_nifty50,   "yfinance",  "BSE"),
]


# ── Public API ────────────────────────────────────────────────────────────

def refresh_all_indices() -> dict[str, int]:
    """
    Fetch all index constituents from Wikipedia and write to the cache file.
    Called weekly from run_tier2_screen() every Monday.
    Returns {index_name: symbol_count}.
    """
    results: dict[str, list[str]] = {}
    summary: dict[str, int] = {}

    for name, fetcher, _, _ in INDEX_REGISTRY:
        logger.info("Refreshing index: %s", name)
        try:
            syms = fetcher()
            results[name] = syms
            summary[name] = len(syms)
            logger.info("  %s → %d symbols", name, len(syms))
        except Exception as exc:
            logger.error("Failed to refresh %s: %s", name, exc)
            results[name] = []
            summary[name] = 0

    try:
        with open(CACHE_PATH, "w") as f:
            json.dump({"timestamp": time.time(), "indices": results}, f)
        logger.info("Index cache written to %s", CACHE_PATH)
    except Exception as exc:
        logger.error("Failed to write index cache: %s", exc)

    return summary


def get_dynamic_instruments(core_symbols: set[str]) -> list:
    """
    Read cached index constituents and return as Instrument objects,
    excluding symbols already present in core_symbols.
    Returns an empty list if the cache has not been populated yet.
    """
    from data.watchlist import Instrument

    if not os.path.exists(CACHE_PATH):
        logger.info("Index cache not found — will populate on next Monday screen")
        return []

    try:
        with open(CACHE_PATH) as f:
            data = json.load(f)
    except Exception as exc:
        logger.warning("Failed to read index cache: %s", exc)
        return []

    age_days = (time.time() - data.get("timestamp", 0)) / 86400
    if age_days > 8:
        logger.warning("Index cache is %.1f days old — refresh due on next Monday screen", age_days)

    instruments = []
    seen = set(core_symbols)

    for name, _, market_type, exchange in INDEX_REGISTRY:
        syms = data.get("indices", {}).get(name, [])
        added = 0
        for sym in syms:
            if sym in seen:
                continue
            seen.add(sym)
            instruments.append(Instrument(
                symbol=sym,
                market_type=market_type,
                tier=1,
                active=True,
                exchange=exchange,
            ))
            added += 1
        if added:
            logger.debug("Dynamic: +%d from %s", added, name)

    return instruments


def cache_summary() -> dict[str, int]:
    """Return {index_name: count} from the on-disk cache, or empty dict if missing."""
    if not os.path.exists(CACHE_PATH):
        return {}
    try:
        with open(CACHE_PATH) as f:
            data = json.load(f)
        return {name: len(syms) for name, syms in data.get("indices", {}).items()}
    except Exception:
        return {}
