"""
Technical analysis module.
Computes indicators via pandas-ta and returns a composite score 0-100.
"""
import logging
from dataclasses import dataclass

import pandas as pd
import pandas_ta as ta

logger = logging.getLogger(__name__)


@dataclass
class TechnicalResult:
    score: float                   # 0-100
    direction: str                 # LONG | SHORT | NEUTRAL
    ema20: float
    ema50: float
    ema200: float
    rsi: float
    macd: float
    macd_signal: float
    macd_hist: float
    bb_upper: float
    bb_mid: float
    bb_lower: float
    atr: float
    volume_ratio: float            # current vol / 20-period avg
    breakdown: dict                # sub-score breakdown


def compute(df: pd.DataFrame) -> TechnicalResult | None:
    """
    df must have columns: Open, High, Low, Close, Volume
    Returns None if insufficient data.
    """
    if df is None or len(df) < 50:
        logger.warning("Insufficient data for technical analysis (%d rows)", len(df) if df is not None else 0)
        return None

    df = df.copy()

    # ── Indicators ───────────────────────────────────────────────
    df.ta.ema(length=20, append=True)
    df.ta.ema(length=50, append=True)
    df.ta.ema(length=200, append=True)
    df.ta.rsi(length=14, append=True)
    df.ta.macd(fast=12, slow=26, signal=9, append=True)
    df.ta.bbands(length=20, std=2, append=True)
    df.ta.atr(length=14, append=True)

    last = df.iloc[-1]
    close = last["Close"]

    def safe(col: str, default: float = 0.0) -> float:
        val = last.get(col, default)
        return float(val) if pd.notna(val) else default

    ema20  = safe("EMA_20")
    ema50  = safe("EMA_50")
    ema200 = safe("EMA_200")
    rsi    = safe("RSI_14")
    macd   = safe("MACD_12_26_9")
    macd_s = safe("MACDs_12_26_9")
    macd_h = safe("MACDh_12_26_9")
    atr    = safe("ATRr_14")

    # pandas_ta column names for bbands vary by version (BBU_20_2.0 vs BBU_20_2.0_2.0)
    # Find the actual column names dynamically.
    cols = df.columns.tolist()
    bb_u_col = next((c for c in cols if c.startswith("BBU_")), "BBU_20_2.0")
    bb_m_col = next((c for c in cols if c.startswith("BBM_")), "BBM_20_2.0")
    bb_l_col = next((c for c in cols if c.startswith("BBL_")), "BBL_20_2.0")
    bb_u   = safe(bb_u_col)
    bb_m   = safe(bb_m_col)
    bb_l   = safe(bb_l_col)

    vol_avg = df["Volume"].tail(20).mean()
    vol_ratio = float(last["Volume"]) / vol_avg if vol_avg > 0 else 1.0

    # ── Trend score (25 pts) ──────────────────────────────────────
    # Graduated scoring for both LONG and SHORT directions.
    # EMA200 is optional — treated as unknown if data is insufficient.
    trend_score = 0.0
    if ema20 > 0 and ema50 > 0:
        above_ema20  = close > ema20
        above_ema50  = close > ema50
        above_ema200 = ema200 > 0 and close > ema200
        below_ema20  = close < ema20
        below_ema50  = close < ema50
        below_ema200 = ema200 > 0 and close < ema200

        # Bullish trend strength (LONG)
        if above_ema20 and above_ema50 and above_ema200:
            bull_trend = 25.0
        elif above_ema20 and above_ema50:
            bull_trend = 18.0
        elif above_ema20:
            bull_trend = 10.0
        elif not above_ema20 and above_ema50:
            bull_trend = 5.0
        else:
            bull_trend = 0.0

        # Bearish trend strength (SHORT) — mirror of bullish
        if below_ema20 and below_ema50 and below_ema200:
            bear_trend = 25.0
        elif below_ema20 and below_ema50:
            bear_trend = 18.0
        elif below_ema20:
            bear_trend = 10.0
        elif not below_ema20 and below_ema50:
            bear_trend = 5.0
        else:
            bear_trend = 0.0

        trend_score = max(bull_trend, bear_trend)

    trend_score = max(0.0, min(25.0, trend_score))

    # ── Momentum / RSI score (20 pts) ────────────────────────────
    rsi_score = 0.0
    if rsi > 0:
        if 50 <= rsi <= 70:   rsi_score = 20.0   # bullish momentum
        elif rsi > 70:        rsi_score = 12.0   # overbought, reduced
        elif 40 <= rsi < 50:  rsi_score = 10.0   # slightly bearish
        elif 30 <= rsi < 40:  rsi_score = 5.0    # bearish
        else:                 rsi_score = 2.0    # oversold

        # direction bonus: check RSI slope over last 3 bars
        rsi_series = df["RSI_14"].dropna().tail(3)
        if len(rsi_series) >= 2:
            if rsi_series.iloc[-1] > rsi_series.iloc[-2]:
                rsi_score = min(20.0, rsi_score + 3)

    # ── MACD score (20 pts) ──────────────────────────────────────
    macd_score = 0.0
    if macd != 0 or macd_s != 0:
        if macd > macd_s:     macd_score += 10.0  # bullish crossover
        if macd_h > 0:        macd_score += 5.0   # positive histogram
        hist_series = df["MACDh_12_26_9"].dropna().tail(3)
        if len(hist_series) >= 2 and hist_series.iloc[-1] > hist_series.iloc[-2]:
            macd_score += 5.0   # histogram expanding

    macd_score = max(0.0, min(20.0, macd_score))

    # ── Bollinger score (15 pts) ──────────────────────────────────
    bb_score = 0.0
    if bb_u > bb_l and bb_l > 0:
        bb_range = bb_u - bb_l
        bb_pos = (close - bb_l) / bb_range  # 0=bottom, 1=top
        if 0.5 <= bb_pos < 0.8:   bb_score = 15.0  # mid-to-upper: bullish
        elif 0.8 <= bb_pos <= 1.0: bb_score = 8.0   # near upper: near resistance
        elif bb_pos > 1.0:         bb_score = 5.0   # breakout above band
        elif 0.3 <= bb_pos < 0.5:  bb_score = 7.0
        else:                      bb_score = 3.0   # near lower band

    # ── Volume confirmation score (20 pts) ───────────────────────
    vol_score = 0.0
    if vol_ratio >= 2.0:    vol_score = 20.0
    elif vol_ratio >= 1.5:  vol_score = 15.0
    elif vol_ratio >= 1.2:  vol_score = 10.0
    elif vol_ratio >= 0.8:  vol_score = 5.0
    else:                   vol_score = 2.0   # below-average volume

    total = trend_score + rsi_score + macd_score + bb_score + vol_score
    total = max(0.0, min(100.0, total))

    # ── Determine direction ───────────────────────────────────────
    if total >= 55 and close > ema50:
        direction = "LONG"
    elif total <= 35 or (close < ema50 and rsi < 45):
        direction = "SHORT"
    else:
        direction = "NEUTRAL"

    breakdown = {
        "trend": round(trend_score, 1),
        "momentum_rsi": round(rsi_score, 1),
        "macd": round(macd_score, 1),
        "bollinger": round(bb_score, 1),
        "volume": round(vol_score, 1),
    }

    return TechnicalResult(
        score=round(total, 1),
        direction=direction,
        ema20=round(ema20, 5),
        ema50=round(ema50, 5),
        ema200=round(ema200, 5),
        rsi=round(rsi, 2),
        macd=round(macd, 6),
        macd_signal=round(macd_s, 6),
        macd_hist=round(macd_h, 6),
        bb_upper=round(bb_u, 5),
        bb_mid=round(bb_m, 5),
        bb_lower=round(bb_l, 5),
        atr=round(atr, 6),
        volume_ratio=round(vol_ratio, 2),
        breakdown=breakdown,
    )
