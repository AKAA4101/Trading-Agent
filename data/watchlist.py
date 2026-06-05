from dataclasses import dataclass, field
from typing import Literal

MarketType = Literal["forex", "crypto", "us_equity", "yfinance"]


@dataclass
class Instrument:
    symbol: str
    market_type: MarketType
    tier: int
    active: bool = True
    exchange: str = ""


CORE_WATCHLIST: list[Instrument] = [

    # ── TIER 1: FOREX (OANDA) ────────────────────────────────────────────────
    # Major pairs
    Instrument("EUR_USD", "forex", 1),
    Instrument("GBP_USD", "forex", 1),
    Instrument("USD_JPY", "forex", 1),
    Instrument("AUD_USD", "forex", 1),
    Instrument("USD_CAD", "forex", 1),
    Instrument("NZD_USD", "forex", 1),
    Instrument("EUR_GBP", "forex", 1),
    Instrument("GBP_JPY", "forex", 1),
    # Minor/cross pairs
    Instrument("EUR_JPY", "forex", 1),
    Instrument("USD_CHF", "forex", 1),
    Instrument("AUD_JPY", "forex", 1),
    # Exotic pairs
    Instrument("USD_SGD", "forex", 1),
    Instrument("USD_HKD", "forex", 1),
    Instrument("USD_CNH", "forex", 1),
    Instrument("USD_ZAR", "forex", 1),
    Instrument("USD_MXN", "forex", 1),
    Instrument("USD_BRL", "forex", 1),
    Instrument("EUR_AUD", "forex", 1),
    Instrument("GBP_AUD", "forex", 1),
    Instrument("AUD_NZD", "forex", 1),

    # ── TIER 1: CRYPTO (Alpaca) ──────────────────────────────────────────────
    Instrument("BTC/USD",  "crypto", 1),
    Instrument("ETH/USD",  "crypto", 1),
    Instrument("SOL/USD",  "crypto", 1),
    Instrument("BNB/USD",  "crypto", 1),
    Instrument("XRP/USD",  "crypto", 1),
    Instrument("ADA/USD",  "crypto", 1),
    Instrument("AVAX/USD", "crypto", 1),
    Instrument("DOT/USD",  "crypto", 1),
    Instrument("MATIC/USD","crypto", 1),
    Instrument("LINK/USD", "crypto", 1),

    # ── TIER 1: US EQUITIES (Alpaca/IEX) ────────────────────────────────────
    Instrument("AAPL",  "us_equity", 1),
    Instrument("MSFT",  "us_equity", 1),
    Instrument("GOOGL", "us_equity", 1),
    Instrument("AMZN",  "us_equity", 1),
    Instrument("NVDA",  "us_equity", 1),
    Instrument("META",  "us_equity", 1),
    Instrument("TSLA",  "us_equity", 1),
    Instrument("JPM",   "us_equity", 1),
    Instrument("V",     "us_equity", 1),
    Instrument("JNJ",   "us_equity", 1),
    Instrument("WMT",   "us_equity", 1),
    Instrument("XOM",   "us_equity", 1),
    Instrument("BAC",   "us_equity", 1),
    Instrument("PG",    "us_equity", 1),
    Instrument("MA",    "us_equity", 1),
    Instrument("BRK.B", "us_equity", 1),
    Instrument("HD",    "us_equity", 1),
    Instrument("CVX",   "us_equity", 1),
    Instrument("ABBV",  "us_equity", 1),
    Instrument("MRK",   "us_equity", 1),

    # ── TIER 1: INTERNATIONAL ADRs (Alpaca/IEX — US-listed) ─────────────────
    # UK / Europe ADRs
    Instrument("SHEL",  "us_equity", 1, exchange="ADR"),  # Shell
    Instrument("BP",    "us_equity", 1, exchange="ADR"),  # BP
    Instrument("AZN",   "us_equity", 1, exchange="ADR"),  # AstraZeneca
    Instrument("ULVR",  "us_equity", 1, exchange="ADR"),  # Unilever
    Instrument("SAP",   "us_equity", 1, exchange="ADR"),  # SAP SE
    Instrument("ASML",  "us_equity", 1, exchange="ADR"),  # ASML
    Instrument("NVO",   "us_equity", 1, exchange="ADR"),  # Novo Nordisk
    Instrument("TTE",   "us_equity", 1, exchange="ADR"),  # TotalEnergies
    Instrument("SIEGY", "us_equity", 1, exchange="ADR"),  # Siemens
    # Japan ADRs
    Instrument("TM",    "us_equity", 1, exchange="ADR"),  # Toyota
    Instrument("SONY",  "us_equity", 1, exchange="ADR"),  # Sony
    Instrument("HMC",   "us_equity", 1, exchange="ADR"),  # Honda
    Instrument("TOYOF", "us_equity", 1, exchange="ADR"),  # Toyota (OTC)
    # China / HK ADRs
    Instrument("BABA",  "us_equity", 1, exchange="ADR"),  # Alibaba
    Instrument("BIDU",  "us_equity", 1, exchange="ADR"),  # Baidu
    Instrument("JD",    "us_equity", 1, exchange="ADR"),  # JD.com
    Instrument("TCEHY", "us_equity", 1, exchange="ADR"),  # Tencent
    Instrument("NIO",   "us_equity", 1, exchange="ADR"),  # NIO
    # Australia / Mining on US markets
    Instrument("BHP",   "us_equity", 1, exchange="ADR"),  # BHP Group
    Instrument("RIO",   "us_equity", 1, exchange="ADR"),  # Rio Tinto
    Instrument("VALE",  "us_equity", 1, exchange="ADR"),  # Vale SA

    # ── TIER 1: ASX (yfinance) ───────────────────────────────────────────────
    Instrument("BHP.AX", "yfinance", 1, exchange="ASX"),
    Instrument("CBA.AX", "yfinance", 1, exchange="ASX"),
    Instrument("CSL.AX", "yfinance", 1, exchange="ASX"),
    Instrument("NAB.AX", "yfinance", 1, exchange="ASX"),
    Instrument("WBC.AX", "yfinance", 1, exchange="ASX"),
    Instrument("ANZ.AX", "yfinance", 1, exchange="ASX"),
    Instrument("WES.AX", "yfinance", 1, exchange="ASX"),
    Instrument("MQG.AX", "yfinance", 1, exchange="ASX"),
    Instrument("RIO.AX", "yfinance", 1, exchange="ASX"),
    Instrument("TLS.AX", "yfinance", 1, exchange="ASX"),
    Instrument("WOW.AX", "yfinance", 1, exchange="ASX"),
    Instrument("FMG.AX", "yfinance", 1, exchange="ASX"),
    Instrument("NCM.AX", "yfinance", 1, exchange="ASX"),
    Instrument("S32.AX", "yfinance", 1, exchange="ASX"),
    Instrument("ALL.AX", "yfinance", 1, exchange="ASX"),

    # ── TIER 1: LSE London (yfinance) ────────────────────────────────────────
    Instrument("SHEL.L", "yfinance", 1, exchange="LSE"),
    Instrument("HSBA.L", "yfinance", 1, exchange="LSE"),
    Instrument("AZN.L",  "yfinance", 1, exchange="LSE"),
    Instrument("ULVR.L", "yfinance", 1, exchange="LSE"),
    Instrument("BP.L",   "yfinance", 1, exchange="LSE"),
    Instrument("GSK.L",  "yfinance", 1, exchange="LSE"),
    Instrument("RIO.L",  "yfinance", 1, exchange="LSE"),
    Instrument("BATS.L", "yfinance", 1, exchange="LSE"),
    Instrument("DGE.L",  "yfinance", 1, exchange="LSE"),
    Instrument("VOD.L",  "yfinance", 1, exchange="LSE"),
    Instrument("LLOY.L", "yfinance", 1, exchange="LSE"),
    Instrument("BARC.L", "yfinance", 1, exchange="LSE"),
    Instrument("NWG.L",  "yfinance", 1, exchange="LSE"),
    Instrument("REL.L",  "yfinance", 1, exchange="LSE"),
    Instrument("CPG.L",  "yfinance", 1, exchange="LSE"),

    # ── TIER 1: DAX Germany (yfinance) ───────────────────────────────────────
    Instrument("SAP.DE",  "yfinance", 1, exchange="DAX"),
    Instrument("SIE.DE",  "yfinance", 1, exchange="DAX"),
    Instrument("ALV.DE",  "yfinance", 1, exchange="DAX"),
    Instrument("MRK.DE",  "yfinance", 1, exchange="DAX"),
    Instrument("BMW.DE",  "yfinance", 1, exchange="DAX"),
    Instrument("DTE.DE",  "yfinance", 1, exchange="DAX"),
    Instrument("BAYN.DE", "yfinance", 1, exchange="DAX"),
    Instrument("MUV2.DE", "yfinance", 1, exchange="DAX"),
    Instrument("BAS.DE",  "yfinance", 1, exchange="DAX"),
    Instrument("RWE.DE",  "yfinance", 1, exchange="DAX"),

    # ── TIER 1: Asia Pacific — Japan (yfinance) ───────────────────────────────
    Instrument("7203.T", "yfinance", 1, exchange="TSE"),   # Toyota
    Instrument("6758.T", "yfinance", 1, exchange="TSE"),   # Sony
    Instrument("9984.T", "yfinance", 1, exchange="TSE"),   # SoftBank
    Instrument("6861.T", "yfinance", 1, exchange="TSE"),   # Keyence
    Instrument("8306.T", "yfinance", 1, exchange="TSE"),   # Mitsubishi UFJ

    # ── TIER 1: Asia Pacific — Hong Kong (yfinance) ───────────────────────────
    Instrument("0700.HK", "yfinance", 1, exchange="HKEX"),  # Tencent
    Instrument("0941.HK", "yfinance", 1, exchange="HKEX"),  # China Mobile
    Instrument("1299.HK", "yfinance", 1, exchange="HKEX"),  # AIA
    Instrument("0005.HK", "yfinance", 1, exchange="HKEX"),  # HSBC HK
    Instrument("2318.HK", "yfinance", 1, exchange="HKEX"),  # Ping An

    # ── TIER 1: Asia Pacific — South Korea (yfinance) ────────────────────────
    Instrument("005930.KS", "yfinance", 1, exchange="KRX"),  # Samsung
    Instrument("000660.KS", "yfinance", 1, exchange="KRX"),  # SK Hynix
    Instrument("035420.KS", "yfinance", 1, exchange="KRX"),  # NAVER

    # ── TIER 1: Asia Pacific — India NSE (yfinance) ───────────────────────────
    Instrument("RELIANCE.NS",  "yfinance", 1, exchange="BSE"),
    Instrument("TCS.NS",       "yfinance", 1, exchange="BSE"),
    Instrument("INFY.NS",      "yfinance", 1, exchange="BSE"),
    Instrument("HDFCBANK.NS",  "yfinance", 1, exchange="BSE"),
    Instrument("ICICIBANK.NS", "yfinance", 1, exchange="BSE"),

    # ── TIER 2: Emerging Markets (yfinance, weekly screen only) ──────────────
    # Brazil
    Instrument("PETR4.SA",    "yfinance", 2, active=False, exchange="B3"),
    Instrument("VALE3.SA",    "yfinance", 2, active=False, exchange="B3"),
    Instrument("ITUB4.SA",    "yfinance", 2, active=False, exchange="B3"),
    # South Africa
    Instrument("NPN.JO",      "yfinance", 2, active=False, exchange="JSE"),
    Instrument("BTI.JO",      "yfinance", 2, active=False, exchange="JSE"),
    Instrument("CFR.JO",      "yfinance", 2, active=False, exchange="JSE"),
    # Mexico
    Instrument("AMXL.MX",     "yfinance", 2, active=False, exchange="BMV"),
    Instrument("FEMSAUBD.MX", "yfinance", 2, active=False, exchange="BMV"),
]


def get_active(tier: int | None = None) -> list[Instrument]:
    """Return all active instruments — core watchlist merged with dynamic index constituents."""
    from data.index_loader import get_dynamic_instruments
    core = [i for i in CORE_WATCHLIST if i.active and (tier is None or i.tier == tier)]
    core_symbols = {i.symbol for i in CORE_WATCHLIST}
    dynamic = get_dynamic_instruments(core_symbols)
    if tier is not None:
        dynamic = [i for i in dynamic if i.tier == tier]
    return core + dynamic


def get_by_market(market_type: MarketType, active_only: bool = True) -> list[Instrument]:
    return [i for i in CORE_WATCHLIST if i.market_type == market_type and (not active_only or i.active)]


def activate_tier2() -> None:
    for i in CORE_WATCHLIST:
        if i.tier == 2:
            i.active = True


def coverage_summary() -> dict[str, int]:
    """Return instrument counts by market type and exchange for all active instruments."""
    summary: dict[str, int] = {}
    for inst in get_active():
        key = f"{inst.market_type}/{inst.exchange}" if inst.exchange else inst.market_type
        summary[key] = summary.get(key, 0) + 1
    return summary
