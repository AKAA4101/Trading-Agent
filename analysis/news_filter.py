"""
News and macro sentiment filter.
Step 1: Pull headlines from NewsAPI for the instrument.
Step 2: Scrape Forex Factory RSS for high-impact calendar events.
Step 3: Ask Claude claude-sonnet-4-20250514 for a risk verdict.
"""
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup
import anthropic

from config import config

logger = logging.getLogger(__name__)

CLAUDE_MODEL = "claude-sonnet-4-5"

# ── International ticker → meaningful NewsAPI search term ──────────────────
# Without this mapping, searching for "7203.T" or "0700.HK" returns nothing.
TICKER_NEWS_QUERIES: dict[str, str] = {
    # ── Forex additions ──────────────────────────────────────────────────────
    "EUR_JPY": "EUR/JPY euro yen exchange rate",
    "USD_CHF": "USD/CHF dollar swiss franc",
    "AUD_JPY": "AUD/JPY australian dollar yen",
    "USD_SGD": "USD/SGD dollar singapore",
    "USD_HKD": "USD/HKD dollar hong kong",
    "USD_CNH": "USD/CNH dollar chinese yuan offshore",
    "USD_ZAR": "USD/ZAR dollar south african rand",
    "USD_MXN": "USD/MXN dollar mexican peso",
    "USD_BRL": "USD/BRL dollar brazilian real",
    "EUR_AUD": "EUR/AUD euro australian dollar",
    "GBP_AUD": "GBP/AUD pound australian dollar",
    "AUD_NZD": "AUD/NZD australian new zealand dollar",
    # ── Crypto additions ─────────────────────────────────────────────────────
    "BNB/USD":  "Binance Coin BNB price",
    "XRP/USD":  "XRP Ripple price crypto",
    "ADA/USD":  "Cardano ADA price",
    "AVAX/USD": "Avalanche AVAX price",
    "DOT/USD":  "Polkadot DOT price",
    "MATIC/USD":"Polygon MATIC price",
    "LINK/USD": "Chainlink LINK price",
    # ── US-listed ADRs ───────────────────────────────────────────────────────
    "SHEL":  "Shell oil energy",
    "BP":    "BP oil energy",
    "AZN":   "AstraZeneca pharma",
    "ULVR":  "Unilever consumer goods",
    "SAP":   "SAP software Germany",
    "ASML":  "ASML semiconductor chips",
    "NVO":   "Novo Nordisk diabetes pharma",
    "TTE":   "TotalEnergies oil energy France",
    "SIEGY": "Siemens industrial Germany",
    "TM":    "Toyota automobile Japan",
    "SONY":  "Sony electronics Japan",
    "HMC":   "Honda automobile Japan",
    "TOYOF": "Toyota Japan automobile",
    "BABA":  "Alibaba China ecommerce",
    "BIDU":  "Baidu China search internet",
    "JD":    "JD.com China ecommerce",
    "TCEHY": "Tencent China technology",
    "NIO":   "NIO electric vehicle China",
    "BHP":   "BHP mining iron ore",
    "RIO":   "Rio Tinto mining",
    "VALE":  "Vale mining iron ore Brazil",
    # ── ASX ──────────────────────────────────────────────────────────────────
    "BHP.AX": "BHP mining Australia",
    "CBA.AX": "Commonwealth Bank Australia",
    "CSL.AX": "CSL biopharmaceuticals Australia",
    "NAB.AX": "National Australia Bank",
    "WBC.AX": "Westpac Banking Australia",
    "ANZ.AX": "ANZ Bank Australia",
    "WES.AX": "Wesfarmers Australia",
    "MQG.AX": "Macquarie Bank Australia",
    "RIO.AX": "Rio Tinto mining Australia",
    "TLS.AX": "Telstra Australia telecom",
    "WOW.AX": "Woolworths Australia retail",
    "FMG.AX": "Fortescue Metals iron ore Australia",
    "NCM.AX": "Newcrest Mining gold Australia",
    "S32.AX": "South32 mining Australia",
    "ALL.AX": "Aristocrat gaming Australia",
    # ── LSE London ───────────────────────────────────────────────────────────
    "SHEL.L": "Shell oil energy London",
    "HSBA.L": "HSBC bank London",
    "AZN.L":  "AstraZeneca pharma London",
    "ULVR.L": "Unilever consumer goods London",
    "BP.L":   "BP oil energy London",
    "GSK.L":  "GSK GlaxoSmithKline pharma London",
    "RIO.L":  "Rio Tinto mining London",
    "BATS.L": "British American Tobacco London",
    "DGE.L":  "Diageo beverages London",
    "VOD.L":  "Vodafone telecom London",
    "LLOY.L": "Lloyds Bank London",
    "BARC.L": "Barclays Bank London",
    "NWG.L":  "NatWest Bank London",
    "REL.L":  "RELX information services London",
    "CPG.L":  "Compass Group catering London",
    # ── DAX Germany ──────────────────────────────────────────────────────────
    "SAP.DE":  "SAP software Germany",
    "SIE.DE":  "Siemens industrial Germany",
    "ALV.DE":  "Allianz insurance Germany",
    "MRK.DE":  "Merck pharmaceutical Germany",
    "BMW.DE":  "BMW automobiles Germany",
    "DTE.DE":  "Deutsche Telekom Germany",
    "BAYN.DE": "Bayer pharmaceutical Germany",
    "MUV2.DE": "Munich Re insurance Germany",
    "BAS.DE":  "BASF chemicals Germany",
    "RWE.DE":  "RWE energy Germany",
    # ── Japan TSE ────────────────────────────────────────────────────────────
    "7203.T": "Toyota Japan automobile",
    "6758.T": "Sony Japan electronics",
    "9984.T": "SoftBank Japan technology",
    "6861.T": "Keyence Japan sensors automation",
    "8306.T": "Mitsubishi UFJ Bank Japan",
    # ── Hong Kong HKEX ───────────────────────────────────────────────────────
    "0700.HK": "Tencent Hong Kong technology",
    "0941.HK": "China Mobile Hong Kong telecom",
    "1299.HK": "AIA insurance Hong Kong",
    "0005.HK": "HSBC Holdings Hong Kong",
    "2318.HK": "Ping An Insurance Hong Kong",
    # ── South Korea KRX ──────────────────────────────────────────────────────
    "005930.KS": "Samsung Electronics Korea",
    "000660.KS": "SK Hynix semiconductor Korea",
    "035420.KS": "NAVER Korea internet technology",
    # ── India NSE ────────────────────────────────────────────────────────────
    "RELIANCE.NS":  "Reliance Industries India oil petrochemicals",
    "TCS.NS":       "Tata Consultancy Services India IT",
    "INFY.NS":      "Infosys India technology outsourcing",
    "HDFCBANK.NS":  "HDFC Bank India",
    "ICICIBANK.NS": "ICICI Bank India",
    # ── Emerging Markets ─────────────────────────────────────────────────────
    "PETR4.SA":    "Petrobras oil Brazil",
    "VALE3.SA":    "Vale mining iron ore Brazil",
    "ITUB4.SA":    "Itaú Unibanco Bank Brazil",
    "NPN.JO":      "Naspers technology South Africa",
    "BTI.JO":      "British American Tobacco South Africa",
    "CFR.JO":      "Richemont luxury goods South Africa",
    "AMXL.MX":     "América Móvil telecom Mexico",
    "FEMSAUBD.MX": "FEMSA beverages Mexico",
}

FF_RSS_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"
CALENDAR_CACHE_PATH = "/opt/trading-agent/data/calendar_cache.json"
CALENDAR_CACHE_TTL_SECONDS = 6 * 3600

SYSTEM_PROMPT = (
    "You are a financial risk assessment engine for a swing trading system. "
    "Analyse the provided news headlines and upcoming economic events for the "
    "given instrument. Return ONLY a JSON response in this exact format:\n"
    "{\n"
    '  "verdict": "GREEN" | "AMBER" | "RED",\n'
    '  "confidence_impact": -30 to +10 (integer),\n'
    '  "reasoning": "one sentence max",\n'
    '  "key_risk": "the single biggest risk or null if GREEN"\n'
    "}"
)


@dataclass
class NewsResult:
    verdict: str          # GREEN | AMBER | RED
    confidence_impact: int
    reasoning: str
    key_risk: str | None
    headlines_count: int
    calendar_events: list[str]


def _fetch_headlines(query: str) -> list[str]:
    """Fetch last-24h headlines from NewsAPI."""
    try:
        since = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S")
        url = "https://newsapi.org/v2/everything"
        params = {
            "q": query,
            "from": since,
            "sortBy": "publishedAt",
            "pageSize": 20,
            "language": "en",
            "apiKey": config.NEWS_API_KEY,
        }
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        articles = resp.json().get("articles", [])
        return [f"{a['title']} — {a.get('description', '')}" for a in articles if a.get("title")]
    except Exception as exc:
        logger.warning("NewsAPI fetch failed: %s", exc)
        return []


def _fetch_calendar_events(instrument: str) -> list[str]:
    """Scrape Forex Factory RSS for high-impact events in next 48h.

    Caches the raw response to CALENDAR_CACHE_PATH and only re-fetches
    when the cache is older than CALENDAR_CACHE_TTL_SECONDS (6 h).
    On any fetch failure, falls back to stale cache; if no cache exists,
    returns an empty list (low calendar risk).
    """
    content: str | None = None

    # Load cache if it exists
    cached_content: str | None = None
    if os.path.exists(CALENDAR_CACHE_PATH):
        try:
            with open(CALENDAR_CACHE_PATH) as f:
                cached = json.load(f)
            cached_content = cached.get("content")
            age = time.time() - cached.get("timestamp", 0)
            if age < CALENDAR_CACHE_TTL_SECONDS:
                content = cached_content  # cache is fresh — use it
        except Exception as exc:
            logger.warning("Failed to read calendar cache: %s", exc)

    # Re-fetch only when cache is missing or stale
    if content is None:
        try:
            resp = requests.get(FF_RSS_URL, timeout=10)
            resp.raise_for_status()
            content = resp.text
            os.makedirs(os.path.dirname(CALENDAR_CACHE_PATH), exist_ok=True)
            try:
                with open(CALENDAR_CACHE_PATH, "w") as f:
                    json.dump({"timestamp": time.time(), "content": content}, f)
            except Exception as exc:
                logger.warning("Failed to write calendar cache: %s", exc)
        except Exception as exc:
            logger.warning("Forex Factory calendar fetch failed: %s", exc)
            if cached_content is not None:
                logger.info("Using stale calendar cache as fallback")
                content = cached_content
            else:
                logger.warning("No calendar cache available; returning LOW calendar risk")
                return []

    events = []
    try:
        soup = BeautifulSoup(content, "lxml-xml")
        items = soup.find_all("event")

        now = datetime.now(timezone.utc)
        cutoff = now + timedelta(hours=48)

        # Currency codes relevant to this instrument (e.g. EUR_USD → EUR, USD)
        currencies = _instrument_currencies(instrument)

        for item in items:
            try:
                impact = (item.find("impact") or item.find("impactClass") or item.find("level"))
                impact_text = impact.get_text().strip().upper() if impact else ""
                if "HIGH" not in impact_text and "3" not in impact_text:
                    continue

                currency_tag = item.find("country") or item.find("currency")
                currency = currency_tag.get_text().strip().upper() if currency_tag else ""
                if currencies and currency not in currencies:
                    continue

                date_tag = item.find("date") or item.find("pubDate")
                title_tag = item.find("title") or item.find("name")
                title = title_tag.get_text().strip() if title_tag else "Unknown event"

                if date_tag:
                    try:
                        event_dt = datetime.fromisoformat(date_tag.get_text().strip().replace("Z", "+00:00"))
                        if now <= event_dt <= cutoff:
                            events.append(f"{event_dt.strftime('%Y-%m-%d %H:%M UTC')} [{currency}] {title} (HIGH IMPACT)")
                    except ValueError:
                        events.append(f"[{currency}] {title} (HIGH IMPACT, date unclear)")
            except Exception:
                continue
    except Exception as exc:
        logger.warning("Failed to parse calendar content: %s", exc)

    return events


def _instrument_currencies(instrument: str) -> set[str]:
    """Extract currency codes from instrument name for calendar filtering."""
    upper = instrument.upper().replace("/", "_").replace("-", "_")
    parts = re.split(r"[_/\-]", upper)
    forex_codes = {
        "USD", "EUR", "GBP", "JPY", "AUD", "CAD", "NZD", "CHF",
        "SEK", "NOK", "SGD", "HKD", "CNH", "ZAR", "MXN", "BRL",
    }
    return {p for p in parts if p in forex_codes}


def _instrument_news_query(instrument: str) -> str:
    """Convert instrument symbol to a suitable NewsAPI query string."""
    # Explicit mapping covers all major forex, crypto, ADRs, and international tickers.
    base_mapping = {
        "EUR_USD": "EUR/USD euro dollar exchange rate",
        "GBP_USD": "GBP/USD pound dollar",
        "USD_JPY": "USD/JPY dollar yen",
        "AUD_USD": "AUD/USD australian dollar",
        "USD_CAD": "USD/CAD dollar canadian",
        "NZD_USD": "NZD/USD new zealand dollar",
        "EUR_GBP": "EUR/GBP euro pound",
        "GBP_JPY": "GBP/JPY pound yen",
        "BTC/USD": "bitcoin BTC price",
        "ETH/USD": "ethereum ETH price",
        "SOL/USD": "solana SOL price",
    }
    # Merge with the comprehensive international mapping defined above
    combined = {**base_mapping, **TICKER_NEWS_QUERIES}
    if instrument in combined:
        return combined[instrument]

    # Fallback: strip exchange suffixes and use the cleaned symbol
    clean = instrument
    for suffix in (".AX", ".L", ".DE", ".F", ".T", ".HK", ".NS", ".BO",
                   ".KS", ".KQ", ".JO", ".MX", ".SA"):
        clean = clean.replace(suffix, "")
    clean = clean.replace("_", " ").replace("/", " ")
    return clean


def analyse(instrument: str) -> NewsResult:
    """Run the full news + calendar analysis for an instrument."""
    query = _instrument_news_query(instrument)
    headlines = _fetch_headlines(query)
    calendar_events = _fetch_calendar_events(instrument)

    user_content = (
        f"Instrument: {instrument}\n\n"
        f"Recent headlines (last 24h):\n"
        + ("\n".join(f"- {h}" for h in headlines) if headlines else "- No recent headlines found")
        + "\n\nUpcoming high-impact economic events (next 48h):\n"
        + ("\n".join(f"- {e}" for e in calendar_events) if calendar_events else "- None identified")
    )

    try:
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        message = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=256,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
        raw = message.content[0].text.strip()

        # Extract JSON even if model wraps it in ```
        json_match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not json_match:
            raise ValueError(f"No JSON in Claude response: {raw}")
        parsed = json.loads(json_match.group())

        return NewsResult(
            verdict=parsed.get("verdict", "AMBER").upper(),
            confidence_impact=int(parsed.get("confidence_impact", 0)),
            reasoning=parsed.get("reasoning", ""),
            key_risk=parsed.get("key_risk"),
            headlines_count=len(headlines),
            calendar_events=calendar_events,
        )

    except Exception as exc:
        logger.error("News filter Claude call failed for %s: %s", instrument, exc)
        # Conservative fallback
        return NewsResult(
            verdict="AMBER",
            confidence_impact=-5,
            reasoning="News filter unavailable; defaulting to AMBER.",
            key_risk="API error during news analysis",
            headlines_count=len(headlines),
            calendar_events=calendar_events,
        )
