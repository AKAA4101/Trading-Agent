"""
Combines technical score + news verdict + calendar risk into a
final confidence score (0–100) and a trade recommendation.
"""
import logging
from dataclasses import dataclass

from analysis.technical import TechnicalResult
from analysis.news_filter import NewsResult

logger = logging.getLogger(__name__)

# Weights
W_TECHNICAL = 0.40
W_NEWS      = 0.35
W_CALENDAR  = 0.25

# News verdict → base news component (out of 35 pts before impact adj)
NEWS_BASE = {"GREEN": 35.0, "AMBER": 17.5, "RED": 0.0}

# Calendar impact mapping (events_count + max_impact → calendar pts out of 25)
CALENDAR_PTS = {
    "none":   25.0,
    "low":    20.0,
    "medium": 10.0,
    "high":    0.0,
}


@dataclass
class SignalResult:
    instrument: str
    direction: str            # LONG | SHORT | NEUTRAL
    confidence: float         # 0-100
    technical_score: float
    rsi: float
    news_verdict: str
    news_impact: int
    calendar_risk: str
    entry_price: float | None
    stop_loss: float | None
    take_profit: float | None
    reasoning: str
    actionable: bool


def _calendar_risk_level(calendar_events: list[str]) -> str:
    if not calendar_events:
        return "none"
    joined = " ".join(calendar_events).upper()
    if "HIGH" in joined:
        return "high"
    if "MEDIUM" in joined or "MED" in joined:
        return "medium"
    return "low"


def score(
    instrument: str,
    tech: TechnicalResult,
    news: NewsResult,
    current_price: float,
    threshold: int = 70,
) -> SignalResult:

    # ── Technical component (0–40) ───────────────────────────────
    tech_component = tech.score * W_TECHNICAL  # 0–40

    # ── News component (0–35) ────────────────────────────────────
    news_base = NEWS_BASE.get(news.verdict, 17.5)
    # Apply confidence_impact (range -30..+10) scaled to news weight
    news_adj = news.confidence_impact * (W_NEWS / 10)
    news_component = max(0.0, min(35.0, news_base + news_adj))

    # ── Calendar component (0–25) ────────────────────────────────
    cal_risk = _calendar_risk_level(news.calendar_events)
    cal_component = CALENDAR_PTS.get(cal_risk, 10.0)

    raw_score = tech_component + news_component + cal_component
    confidence = round(max(0.0, min(100.0, raw_score)), 1)

    # ── Prices (entry / SL / TP via ATR) ─────────────────────────
    stop_loss = take_profit = None
    if tech.atr > 0 and current_price:
        if tech.direction == "LONG":
            stop_loss   = round(current_price - 1.5 * tech.atr, 6)
            take_profit = round(current_price + 3.0 * tech.atr, 6)
        elif tech.direction == "SHORT":
            stop_loss   = round(current_price + 1.5 * tech.atr, 6)
            take_profit = round(current_price - 3.0 * tech.atr, 6)

    actionable = (
        confidence >= threshold
        and tech.direction != "NEUTRAL"
        and news.verdict != "RED"
    )

    reasoning = (
        f"Tech score {tech.score}/100 (trend={tech.breakdown['trend']}, "
        f"RSI={tech.rsi:.1f}, MACD_hist={tech.macd_hist:+.5f}). "
        f"News: {news.verdict} — {news.reasoning}. "
        f"Calendar risk: {cal_risk}. "
        f"Combined confidence: {confidence}%."
    )

    logger.info(
        "%s | dir=%s conf=%.1f tech=%.1f news=%s cal=%s actionable=%s",
        instrument, tech.direction, confidence, tech.score, news.verdict, cal_risk, actionable,
    )

    return SignalResult(
        instrument=instrument,
        direction=tech.direction,
        confidence=confidence,
        technical_score=tech.score,
        rsi=tech.rsi,
        news_verdict=news.verdict,
        news_impact=news.confidence_impact,
        calendar_risk=cal_risk,
        entry_price=current_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        reasoning=reasoning,
        actionable=actionable,
    )
