"""
Hard-coded risk management rules.
These cannot be overridden by AI signals.
"""
import logging
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
