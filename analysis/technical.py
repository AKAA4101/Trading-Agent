"""
Technical analysis module.
Computes indicators via pandas-ta and returns a composite score 0-100.

Scoring breakdown (100 pts total):
  Trend (EMA alignment)     20 pts  — reduced from 25 to accommodate ADX
  ADX (trend strength)      15 pts  — NEW: filters ranging vs trending markets
  Momentum/RSI              18 pts  — reduced from 20
  MACD                      17 pts  — reduced from 20
  Bollinger                 10 pts  — reduced from 15
  Volume                    10 pts  — reduced from 20
  Support/Resistance        10 pts  — NEW: structural market context
  Total                    100 pts

Multi-timeframe:
  Optional df_higher (e.g. daily when primary is 4H) provides a trend bias
  multiplier applied to the final score:
    - With-trend signal:     score * 1.15 (capped at 100)
    - Counter-trend signal:  score * 0.75
    - No higher TF data:     score unchanged
"""
import logging
from dataclasses import dataclass, field

import numpy as np
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
    adx: float                     # NEW: trend strength 0-100
    volume_ratio: float            # current vol / 20-period avg
    nearest_support: float         # NEW: nearest support level
    nearest_resistance: float      # NEW: nearest resistance level
    sr_context: str                # NEW: NEAR_SUPPORT | NEAR_RESISTANCE | NEUTRAL
    htf_bias: str                  # NEW: BULLISH | BEARISH | NEUTRAL (higher timeframe)
    mtf_multiplier: float          # NEW: multiplier applied (0.75, 1.0, or 1.15)
    breakdown: dict = field(default_factory=dict)


def _find_support_resistance(df: pd.DataFrame, window: int = 5, lookback: int = 50) -> tuple[list, list]:
    """
    Identify swing highs (resistance) and swing lows (support) over the
    last `lookback` bars using a rolling window approach.

    A swing high is a bar whose High is the highest in a window of
    `window` bars on each side. Swing low is the mirror.

    Returns (support_levels, resistance_levels) as lists of floats.
    """
    recent = df.tail(lookback).copy().reset_index(drop=True)
    highs = recent["High"].values
    lows  = recent["Low"].values
    n     = len(recent)

    support_levels    = []
    resistance_levels = []

    for i in range(window, n - window):
        # Swing high: highest point in surrounding window
        if highs[i] == max(highs[i - window: i + window + 1]):
            resistance_levels.append(float(highs[i]))
        # Swing low: lowest point in surrounding window
        if lows[i] == min(lows[i - window: i + window + 1]):
            support_levels.append(float(lows[i]))

    return support_levels, resistance_levels


def _score_sr(close: float, support_levels: list, resistance_levels: list) -> tuple[float, float, float, str]:
    """
    Score price position relative to support/resistance levels.

    Returns (sr_score, nearest_support, nearest_resistance, context_str).

    Scoring logic (10 pts max):
      - Price within 0.5% of support  → 10 pts (strong entry context)
      - Price within 1.0% of support  →  7 pts
      - Price within 2.0% of support  →  5 pts
      - Price within 0.5% of resistance →  2 pts (poor entry, near ceiling)
      - Price within 1.0% of resistance →  4 pts
      - No significant level nearby   →  5 pts (neutral)
    """
    if not support_levels and not resistance_levels:
        return 5.0, 0.0, 0.0, "NEUTRAL"

    # Find nearest levels
    nearest_sup = min(support_levels, key=lambda x: abs(close - x)) if support_levels else 0.0
    nearest_res = min(resistance_levels, key=lambda x: abs(close - x)) if resistance_levels else 0.0

    # Distance as % of price
    sup_dist = abs(close - nearest_sup) / close if nearest_sup > 0 else 1.0
    res_dist = abs(close - nearest_res) / close if nearest_res > 0 else 1.0

    # Only score levels that are actually below (support) or above (resistance)
    sup_valid = nearest_sup < close
    res_valid = nearest_res > close

    sr_score = 5.0  # default neutral
    context  = "NEUTRAL"

    if sup_valid and sup_dist < 0.005:
        sr_score = 10.0
        context  = "NEAR_SUPPORT"
    elif sup_valid and sup_dist < 0.01:
        sr_score = 7.0
        context  = "NEAR_SUPPORT"
    elif sup_valid and sup_dist < 0.02:
        sr_score = 5.0
        context  = "NEAR_SUPPORT"
    elif res_valid and res_dist < 0.005:
        sr_score = 2.0
        context  = "NEAR_RESISTANCE"
    elif res_valid and res_dist < 0.01:
        sr_score = 4.0
        context  = "NEAR_RESISTANCE"

    return sr_score, round(nearest_sup, 6), round(nearest_res, 6), context


def _compute_htf_bias(df_higher: pd.DataFrame | None) -> tuple[str, float]:
    """
    Derive a trend bias from the higher timeframe DataFrame.

    Uses EMA20 vs EMA50 alignment + RSI on the higher timeframe.
    Returns (bias_str, multiplier):
      BULLISH  → 1.15  (with-trend long or counter-trend short penalised later)
      BEARISH  → 0.75 or 1.15 depending on signal direction (applied in compute())
      NEUTRAL  → 1.0
    """
    if df_higher is None or len(df_higher) < 50:
        return "NEUTRAL", 1.0

    df_h = df_higher.copy()
    df_h.ta.ema(length=20, append=True)
    df_h.ta.ema(length=50, append=True)
    df_h.ta.rsi(length=14, append=True)

    last_h = df_h.iloc[-1]

    def safe_h(col: str, default: float = 0.0) -> float:
        val = last_h.get(col, default)
        return float(val) if pd.notna(val) else default

    h_ema20 = safe_h("EMA_20")
    h_ema50 = safe_h("EMA_50")
    h_rsi   = safe_h("RSI_14")
    h_close = float(last_h["Close"])

    if h_ema20 <= 0 or h_ema50 <= 0:
        return "NEUTRAL", 1.0

    bullish = h_close > h_ema20 > h_ema50 and h_rsi > 50
    bearish = h_close < h_ema20 < h_ema50 and h_rsi < 50

    if bullish:
        return "BULLISH", 1.15
    elif bearish:
        return "BEARISH", 1.15   # raw multiplier — direction-aware logic in compute()
    else:
        return "NEUTRAL", 1.0


def compute(df: pd.DataFrame, df_higher: pd.DataFrame | None = None) -> TechnicalResult | None:
    """
    Compute technical score for the primary timeframe DataFrame.

    Args:
        df:         OHLCV DataFrame for the primary timeframe.
                    Must have columns: Open, High, Low, Close, Volume.
                    Minimum 50 rows required.
        df_higher:  Optional OHLCV DataFrame for a higher timeframe
                    (e.g. daily bars when primary is 4H, or weekly when
                    primary is daily). Used for multi-timeframe bias only.
                    Minimum 50 rows recommended.

    Returns:
        TechnicalResult or None if insufficient data.
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
    df.ta.adx(length=14, append=True)   # NEW: ADX

    last  = df.iloc[-1]
    close = float(last["Close"])

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

    # ADX — pandas-ta appends ADX_14, DMP_14, DMN_14
    adx    = safe("ADX_14")

    # Bollinger — dynamic column lookup (version-safe)
    cols   = df.columns.tolist()
    bb_u_col = next((c for c in cols if c.startswith("BBU_")), "BBU_20_2.0")
    bb_m_col = next((c for c in cols if c.startswith("BBM_")), "BBM_20_2.0")
    bb_l_col = next((c for c in cols if c.startswith("BBL_")), "BBL_20_2.0")
    bb_u   = safe(bb_u_col)
    bb_m   = safe(bb_m_col)
    bb_l   = safe(bb_l_col)

    vol_avg   = df["Volume"].tail(20).mean()
    vol_ratio = float(last["Volume"]) / vol_avg if vol_avg > 0 else 1.0

    # ── Support / Resistance ─────────────────────────────────────
    support_levels, resistance_levels = _find_support_resistance(df)
    sr_score, nearest_sup, nearest_res, sr_context = _score_sr(
        close, support_levels, resistance_levels
    )

    # ── Higher timeframe bias ────────────────────────────────────
    htf_bias, htf_raw_mult = _compute_htf_bias(df_higher)

    # ── Trend score (20 pts, reduced from 25) ────────────────────
    trend_score = 0.0
    if ema20 > 0 and ema50 > 0:
        above_ema20  = close > ema20
        above_ema50  = close > ema50
        above_ema200 = ema200 > 0 and close > ema200
        below_ema20  = close < ema20
        below_ema50  = close < ema50
        below_ema200 = ema200 > 0 and close < ema200

        if above_ema20 and above_ema50 and above_ema200:
            bull_trend = 20.0
        elif above_ema20 and above_ema50:
            bull_trend = 14.0
        elif above_ema20:
            bull_trend = 8.0
        elif not above_ema20 and above_ema50:
            bull_trend = 4.0
        else:
            bull_trend = 0.0

        if below_ema20 and below_ema50 and below_ema200:
            bear_trend = 20.0
        elif below_ema20 and below_ema50:
            bear_trend = 14.0
        elif below_ema20:
            bear_trend = 8.0
        elif not below_ema20 and below_ema50:
            bear_trend = 4.0
        else:
            bear_trend = 0.0

        trend_score = max(bull_trend, bear_trend)

    # ── ADX score (15 pts) — NEW ─────────────────────────────────
    # ADX measures trend STRENGTH regardless of direction.
    # We also use it to gate the trend score — a strong ADX reading
    # validates the EMA alignment; a weak ADX (ranging market) reduces it.
    #
    #   ADX < 20  → ranging/weak trend:  adx_score = 0  + trend gated to 30%
    #   ADX 20-25 → developing trend:    adx_score = 5  + trend at 60%
    #   ADX 25-35 → confirmed trend:     adx_score = 10 + trend at 85%
    #   ADX > 35  → strong trend:        adx_score = 15 + trend at 100%
    #
    adx_score    = 0.0
    trend_gate   = 1.0   # multiplier on trend_score
    if adx > 0:
        if adx >= 35:
            adx_score  = 15.0
            trend_gate = 1.0
        elif adx >= 25:
            adx_score  = 10.0
            trend_gate = 0.85
        elif adx >= 20:
            adx_score  = 5.0
            trend_gate = 0.60
        else:
            adx_score  = 0.0
            trend_gate = 0.30   # ranging — EMA alignment unreliable

    trend_score = trend_score * trend_gate
    trend_score = max(0.0, min(20.0, trend_score))
    adx_score   = max(0.0, min(15.0, adx_score))

    # ── Momentum / RSI score (18 pts, reduced from 20) ───────────
    rsi_score = 0.0
    if rsi > 0:
        if 50 <= rsi <= 70:   rsi_score = 18.0
        elif rsi > 70:        rsi_score = 11.0
        elif 40 <= rsi < 50:  rsi_score = 9.0
        elif 30 <= rsi < 40:  rsi_score = 4.5
        else:                 rsi_score = 1.8

        rsi_series = df["RSI_14"].dropna().tail(3)
        if len(rsi_series) >= 2:
            if rsi_series.iloc[-1] > rsi_series.iloc[-2]:
                rsi_score = min(18.0, rsi_score + 2.5)

    # ── MACD score (17 pts, reduced from 20) ─────────────────────
    macd_score = 0.0
    if macd != 0 or macd_s != 0:
        if macd > macd_s:   macd_score += 8.5
        if macd_h > 0:      macd_score += 4.25
        hist_series = df["MACDh_12_26_9"].dropna().tail(3)
        if len(hist_series) >= 2 and hist_series.iloc[-1] > hist_series.iloc[-2]:
            macd_score += 4.25

    macd_score = max(0.0, min(17.0, macd_score))

    # ── Bollinger score (10 pts, reduced from 15) ────────────────
    bb_score = 0.0
    if bb_u > bb_l and bb_l > 0:
        bb_range = bb_u - bb_l
        bb_pos   = (close - bb_l) / bb_range
        if 0.5 <= bb_pos < 0.8:    bb_score = 10.0
        elif 0.8 <= bb_pos <= 1.0: bb_score = 5.5
        elif bb_pos > 1.0:         bb_score = 3.5
        elif 0.3 <= bb_pos < 0.5:  bb_score = 5.0
        else:                      bb_score = 2.0

    # ── Volume score (10 pts, reduced from 20) ───────────────────
    vol_score = 0.0
    if vol_ratio >= 2.0:    vol_score = 10.0
    elif vol_ratio >= 1.5:  vol_score = 7.5
    elif vol_ratio >= 1.2:  vol_score = 5.0
    elif vol_ratio >= 0.8:  vol_score = 2.5
    else:                   vol_score = 1.0

    # ── Total (before MTF adjustment) ────────────────────────────
    total = trend_score + adx_score + rsi_score + macd_score + bb_score + vol_score + sr_score
    total = max(0.0, min(100.0, total))

    # ── Determine primary direction ───────────────────────────────
    if total >= 55 and close > ema50:
        direction = "LONG"
    elif total <= 35 or (close < ema50 and rsi < 45):
        direction = "SHORT"
    else:
        direction = "NEUTRAL"

    # ── Multi-timeframe multiplier ────────────────────────────────
    # Apply AFTER direction is determined so we can check alignment.
    # With-trend = HTF bias matches signal direction  → boost
    # Counter-trend = HTF bias opposes signal direction → penalise
    mtf_multiplier = 1.0
    if htf_bias != "NEUTRAL" and direction != "NEUTRAL":
        with_trend = (
            (htf_bias == "BULLISH" and direction == "LONG") or
            (htf_bias == "BEARISH" and direction == "SHORT")
        )
        mtf_multiplier = 1.15 if with_trend else 0.75

    total = round(max(0.0, min(100.0, total * mtf_multiplier)), 1)

    breakdown = {
        "trend":         round(trend_score, 1),
        "adx":           round(adx_score, 1),
        "momentum_rsi":  round(rsi_score, 1),
        "macd":          round(macd_score, 1),
        "bollinger":     round(bb_score, 1),
        "volume":        round(vol_score, 1),
        "support_resistance": round(sr_score, 1),
        "htf_bias":      htf_bias,
        "mtf_multiplier": round(mtf_multiplier, 2),
    }

    return TechnicalResult(
        score=total,
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
        adx=round(adx, 2),
        volume_ratio=round(vol_ratio, 2),
        nearest_support=nearest_sup,
        nearest_resistance=nearest_res,
        sr_context=sr_context,
        htf_bias=htf_bias,
        mtf_multiplier=round(mtf_multiplier, 2),
        breakdown=breakdown,
    )
