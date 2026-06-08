"""
Hard-coded risk management rules.
These cannot be overridden by AI signals.
"""
import json
import logging
import os
import time
from dataclasses import dataclass

from analysis.confidence_scorer import SignalResult
from analysis.technical import TechnicalResult
from config import config
from database.db_manager import DBManager

logger = logging.getLogger(__name__)

MAX_POSITION_PCT = config.MAX_POSITION_SIZE_PCT / 100.0   # e.g. 0.20
MAX_OPEN_POSITIONS = 5
DAILY_DRAWDOWN_LIMIT = config.DAILY_DRAWDOWN_LIMIT_PCT / 100.0
VOLATILITY_MULTIPLIER = 2.0    # skip if ATR > 2x its 20-period average
SL_ATR_MULT = 1.5
TP_ATR_MULT = 3.0

# ── Liquidity filter constants ─────────────────────────────────────────────
_LIQUIDITY_CACHE_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "liquidity_cache.json")
_LIQUIDITY_CACHE_TTL  = 7 * 24 * 3600   # 7 days — liquidity is slow-moving
_MIN_AVG_VOLUME       = 500_000          # 500k shares/day
_MIN_MARKET_CAP       = 500_000_000      # USD $500M — fallback when volume unavailable

# Instruments known to be liquid but failing yfinance fast_info data gap
LIQUIDITY_ALLOWLIST = {
    "BRK.B",  # Berkshire Hathaway B — highly liquid, yfinance fast_info gap
    "BF.B",   # Brown-Forman B — liquid NYSE stock, yfinance fast_info gap
}


def _load_liquidity_cache() -> dict:
    try:
        with open(_LIQUIDITY_CACHE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_liquidity_cache(cache: dict) -> None:
    os.makedirs(os.path.dirname(_LIQUIDITY_CACHE_PATH), exist_ok=True)
    with open(_LIQUIDITY_CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2)


@dataclass
class RiskDecision:
    approved: bool
    reason: str
    position_size_pct: float   # fraction of portfolio, 0-MAX_POSITION_PCT
    position_size_units: float # actual units/shares/lots
    adjusted: bool             # True if AMBER news caused size reduction


class RiskManager:
    def __init__(self, db: DBManager):
        self.db = db

    # ── Public API ────────────────────────────────────────────────

    def check_liquidity(self, symbol: str, market_type: str) -> tuple[bool, str]:
        """
        Pre-filter: returns (passes, reason).
        Forex and crypto are always exempt — major pairs/coins are always liquid.
        For equities: require averageVolume >= 500k OR marketCap >= $500M.
        Results cached 7 days since liquidity doesn't change rapidly.
        """
        if market_type in ("forex", "crypto"):
            return True, "EXEMPT: forex/crypto always liquid"

        if symbol in LIQUIDITY_ALLOWLIST:
            return True, "PASS: ALLOWLIST — known liquid, yfinance data gap"

        cache = _load_liquidity_cache()
        now = time.time()
        entry = cache.get(symbol)
        if entry and (now - entry.get("timestamp", 0)) < _LIQUIDITY_CACHE_TTL:
            return entry["passes"], entry["reason"]

        try:
            import yfinance as yf
            info = yf.Ticker(symbol).fast_info
            avg_vol = getattr(info, "three_month_average_volume", None) or 0
            mkt_cap = getattr(info, "market_cap", None) or 0

            if avg_vol >= _MIN_AVG_VOLUME:
                passes = True
                reason = f"PASS: avgVol {avg_vol:,.0f} >= {_MIN_AVG_VOLUME:,}"
            elif avg_vol == 0 and mkt_cap >= _MIN_MARKET_CAP:
                # marketCap fallback only when volume data is genuinely unavailable
                passes = True
                reason = f"PASS: marketCap ${mkt_cap/1e9:.1f}B >= $500M (vol data unavailable)"
            elif avg_vol > 0:
                passes = False
                reason = (
                    f"REJECTED: LIQUIDITY — avgVol {avg_vol:,.0f} "
                    f"below minimum {_MIN_AVG_VOLUME:,}"
                )
            else:
                passes = False
                reason = (
                    f"REJECTED: LIQUIDITY — no volume data, "
                    f"marketCap ${mkt_cap/1e6:.0f}M below $500M minimum"
                )

        except Exception as exc:
            logger.warning("Liquidity check error for %s: %s — defaulting PASS", symbol, exc)
            passes = True
            reason = "PASS: liquidity check error — defaulting to pass"

        cache[symbol] = {"passes": passes, "reason": reason, "timestamp": now}
        _save_liquidity_cache(cache)

        if not passes:
            logger.warning("LIQUIDITY REJECTED: %s — %s", symbol, reason)

        return passes, reason

    def evaluate(
        self,
        signal: SignalResult,
        tech: TechnicalResult,
        portfolio_value: float,
        current_price: float,
        atr_history_avg: float | None = None,
    ) -> RiskDecision:
        """
        Evaluate a signal against all hard risk rules.
        Returns RiskDecision with approved=False and reason if any rule fires.
        """
        # 1. RED news filter
        if signal.news_verdict == "RED":
            return RiskDecision(False, "NEWS_RED: no new positions when news filter returns RED", 0.0, 0.0, False)

        # 2. RSI extremes — bounce/reversal risk
        if signal.direction == "SHORT" and tech.rsi < 25:
            logger.warning(
                "RSI_OVERSOLD rejection: %s RSI=%.1f < 25", signal.instrument, tech.rsi
            )
            return RiskDecision(
                False,
                f"RSI_OVERSOLD: Dangerous to short extremely oversold asset (RSI={tech.rsi:.1f} < 25), high bounce risk",
                0.0, 0.0, False,
            )
        if signal.direction == "LONG" and tech.rsi > 75:
            logger.warning(
                "RSI_OVERBOUGHT rejection: %s RSI=%.1f > 75", signal.instrument, tech.rsi
            )
            return RiskDecision(
                False,
                f"RSI_OVERBOUGHT: Dangerous to go long extremely overbought asset (RSI={tech.rsi:.1f} > 75), high reversal risk",
                0.0, 0.0, False,
            )

        # 3. Confidence threshold
        if signal.confidence < config.CONFIDENCE_THRESHOLD:
            return RiskDecision(
                False,
                f"CONFIDENCE_LOW: {signal.confidence:.1f}% < threshold {config.CONFIDENCE_THRESHOLD}%",
                0.0, 0.0, False,
            )

        # 4. Daily drawdown limit
        snapshot = self.db.latest_portfolio_snapshot()
        if snapshot and snapshot.get("drawdown_pct", 0) >= DAILY_DRAWDOWN_LIMIT * 100:
            return RiskDecision(False, f"DRAWDOWN_LIMIT: daily drawdown ≥ {config.DAILY_DRAWDOWN_LIMIT_PCT}%, trading halted", 0.0, 0.0, False)

        # 5. Maximum open positions
        open_count = self.db.count_open_trades()
        if open_count >= MAX_OPEN_POSITIONS:
            return RiskDecision(False, f"MAX_POSITIONS: already {open_count} open positions (limit {MAX_OPEN_POSITIONS})", 0.0, 0.0, False)

        # 6. Volatility filter
        if atr_history_avg and tech.atr > 0:
            if tech.atr > VOLATILITY_MULTIPLIER * atr_history_avg:
                return RiskDecision(
                    False,
                    f"VOLATILITY_FILTER: ATR {tech.atr:.5f} > {VOLATILITY_MULTIPLIER}x avg {atr_history_avg:.5f}",
                    0.0, 0.0, False,
                )

        # ── All checks passed: size the position ─────────────────
        adjusted = False
        size_pct = MAX_POSITION_PCT

        # AMBER news → reduce 50%
        if signal.news_verdict == "AMBER":
            size_pct = size_pct * 0.5
            adjusted = True

        size_pct = min(size_pct, MAX_POSITION_PCT)
        position_value = portfolio_value * size_pct
        units = position_value / current_price if current_price > 0 else 0.0

        logger.info(
            "Risk APPROVED: %s | size_pct=%.1f%% | units=%.4f | adjusted=%s",
            signal.instrument, size_pct * 100, units, adjusted,
        )

        return RiskDecision(
            approved=True,
            reason="All risk checks passed",
            position_size_pct=size_pct,
            position_size_units=units,
            adjusted=adjusted,
        )

    def check_drawdown_halt(self) -> bool:
        """Return True if daily drawdown limit is hit → halt all trading."""
        snapshot = self.db.latest_portfolio_snapshot()
        if not snapshot:
            return False
        return snapshot.get("drawdown_pct", 0) >= DAILY_DRAWDOWN_LIMIT * 100
