"""
Email alert system via Gmail SMTP.
Sends alerts for: new signals, closed trades, daily summary,
RED news alerts, drawdown limit hits, weekly performance,
and market-wide AMBER alerts.
"""
import logging
import smtplib
from collections import Counter
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from config import config

logger = logging.getLogger(__name__)

VERDICT_EMOJI = {"GREEN": "🟢", "AMBER": "🟡", "RED": "🔴"}
DIR_EMOJI     = {"LONG": "📈", "SHORT": "📉", "NEUTRAL": "⚪"}

WEEKLY_BENCHMARK_PCT = 6.19 / 52  # annualised 6.19% → weekly


def _send(subject: str, body: str) -> bool:
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = config.SMTP_EMAIL
        msg["To"]      = config.SMTP_TO

        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.login(config.SMTP_EMAIL, config.SMTP_PASSWORD)
            server.sendmail(config.SMTP_EMAIL, config.SMTP_TO, msg.as_string())

        logger.info("Email sent: %s", subject)
        return True
    except Exception as exc:
        logger.error("Email send failed: %s", exc)
        return False


# ── 1. Trade signal alert ──────────────────────────────────────────────────

def send_signal_alert(signal, risk_decision, executed: bool) -> bool:
    """
    Only sends an email when one of these conditions is true:
      - confidence >= 70%  (actionable signal)
      - executed is True   (trade actually placed)
      - news_verdict is RED (immediate risk alert)
    Everything else is logged at INFO level and skipped.
    """
    should_email = (
        signal.confidence >= 70
        or executed
        or signal.news_verdict == "RED"
    )
    if not should_email:
        logger.info(
            "Signal %s conf=%.1f%% verdict=%s — logged to DB only, no email sent",
            signal.instrument, signal.confidence, signal.news_verdict,
        )
        return False

    verdict_emoji = VERDICT_EMOJI.get(signal.news_verdict, "⚪")
    dir_emoji     = DIR_EMOJI.get(signal.direction, "⚪")
    status        = "EXECUTED" if executed else "REJECTED"
    status_line   = f"✅ {status}" if executed else f"❌ {status}: {risk_decision.reason}"

    subject = (
        f"[TRADING AGENT] {verdict_emoji} {signal.direction} Signal — "
        f"{signal.instrument} ({signal.confidence:.0f}% confidence)"
    )

    body = f"""
TRADING AGENT — SIGNAL ALERT
{'='*50}
Instrument  : {signal.instrument}
Direction   : {dir_emoji} {signal.direction}
Confidence  : {signal.confidence:.1f}%
Status      : {status_line}
Timestamp   : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}

── PRICES ──────────────────────────────────────
Entry Zone  : {signal.entry_price}
Stop Loss   : {signal.stop_loss}
Take Profit : {signal.take_profit}

── POSITION SIZING ────────────────────────────
Position Sz : {risk_decision.position_size_pct*100:.1f}% of portfolio
Units       : {risk_decision.position_size_units:.4f}
AMBER Adj.  : {'Yes — size halved' if risk_decision.adjusted else 'No'}

── ANALYSIS ────────────────────────────────────
Technical   : {signal.technical_score:.1f}/100 (RSI: {signal.rsi:.1f})
News        : {verdict_emoji} {signal.news_verdict} (impact: {signal.news_impact:+d})
Calendar    : {signal.calendar_risk.upper()}

── REASONING ───────────────────────────────────
{signal.reasoning}
{'='*50}
This is a paper trading system. No real money is at risk.
""".strip()

    return _send(subject, body)


# ── 2. Trade closed ────────────────────────────────────────────────────────

def send_trade_closed(trade: dict) -> bool:
    pnl = trade.get("pnl", 0) or 0
    pnl_pct = trade.get("pnl_pct", 0) or 0
    emoji = "✅" if pnl >= 0 else "❌"
    subject = f"[TRADING AGENT] {emoji} Trade Closed — {trade.get('instrument')} ({pnl_pct:+.2f}%)"

    body = f"""
TRADING AGENT — TRADE CLOSED
{'='*50}
Instrument  : {trade.get('instrument')}
Direction   : {trade.get('direction')}
Broker      : {trade.get('broker', '').upper()}

Entry Price : {trade.get('entry_price')}
Exit Price  : {trade.get('exit_price')}
Entry Time  : {trade.get('entry_time')}
Exit Time   : {trade.get('exit_time')}

P&L         : {pnl:+.4f}
P&L %       : {pnl_pct:+.2f}%
{'='*50}
""".strip()

    return _send(subject, body)


# ── 3. Daily summary ───────────────────────────────────────────────────────

def send_daily_summary(
    signals_today: list,
    closed_today: list,
    snapshot: dict | None,
    instruments_scanned: int = 0,
) -> bool:
    subject = f"[TRADING AGENT] Daily Summary — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}"

    # ── Signal stats ──────────────────────────────────────────────────────
    directional = [s for s in signals_today if s.get("direction", "NEUTRAL") != "NEUTRAL"]
    total_signals = len(directional)

    executed_signals = [
        s for s in directional
        if s.get("action_taken", "") in ("EXECUTED", "PAPER_SIGNAL")
    ]
    rejected_signals = [
        s for s in directional
        if str(s.get("action_taken", "")).startswith("REJECTED")
    ]
    red_alerts = [
        s for s in directional
        if s.get("news_verdict") == "RED"
    ]

    # Top rejection reason
    top_reason = "N/A"
    if rejected_signals:
        reasons = [
            str(s.get("action_taken", "")).removeprefix("REJECTED: ").strip()
            for s in rejected_signals
        ]
        top_reason = Counter(reasons).most_common(1)[0][0]

    # ── Confidence distribution ───────────────────────────────────────────
    buckets = {"0-30": 0, "30-50": 0, "50-70": 0, "70+": 0}
    for s in directional:
        c = s.get("confidence_score", 0) or 0
        if c < 30:
            buckets["0-30"] += 1
        elif c < 50:
            buckets["30-50"] += 1
        elif c < 70:
            buckets["50-70"] += 1
        else:
            buckets["70+"] += 1

    # ── P&L ──────────────────────────────────────────────────────────────
    total_pnl = sum((t.get("pnl") or 0) for t in closed_today)
    pnl_str = f"{total_pnl:+.4f}"

    snap_str = ""
    if snapshot:
        snap_str = (
            f"\nPortfolio Value : {snapshot.get('total_value', 'N/A')}\n"
            f"Daily P&L       : {snapshot.get('daily_pnl', 'N/A')}\n"
            f"Drawdown        : {snapshot.get('drawdown_pct', 0):.2f}%\n"
            f"Open Positions  : {snapshot.get('open_positions', 0)}"
        )

    trade_lines = "\n".join(
        f"  {t.get('direction','?'):5s} {t.get('instrument','?'):12s} "
        f"pnl={t.get('pnl',0):+.4f} ({t.get('pnl_pct',0):+.2f}%)"
        for t in closed_today
    ) or "  None"

    red_line = (
        "  " + ", ".join(s.get("instrument", "?") for s in red_alerts)
        if red_alerts else "  None"
    )

    body = f"""
TRADING AGENT — DAILY SUMMARY
{'='*50}
Date                : {datetime.now(timezone.utc).strftime('%Y-%m-%d')}
Instruments Scanned : {instruments_scanned}
Closed P&L          : {pnl_str}
{snap_str}

── SIGNAL OVERVIEW ──────────────────────────────
Signals Generated   : {total_signals}
  Executed          : {len(executed_signals)}
  Rejected          : {len(rejected_signals)}  (top reason: {top_reason})
  RED News Alerts   : {len(red_alerts)}

── CONFIDENCE DISTRIBUTION ──────────────────────
  0–30%  : {buckets['0-30']} signals
  30–50% : {buckets['30-50']} signals
  50–70% : {buckets['50-70']} signals
  70%+   : {buckets['70+']} signals  ← these generated emails

── RED ALERTS FIRED ─────────────────────────────
{red_line}

── TRADES CLOSED ────────────────────────────────
{trade_lines}
{'='*50}
""".strip()

    return _send(subject, body)


# ── 4. RED news alert ──────────────────────────────────────────────────────

def send_red_alert(instrument: str, reasoning: str, key_risk: str | None) -> bool:
    subject = f"[TRADING AGENT] 🔴 RED ALERT — {instrument}"
    body = f"""
TRADING AGENT — NEWS RED ALERT
{'='*50}
Instrument  : {instrument}
Verdict     : 🔴 RED — No new positions
Reasoning   : {reasoning}
Key Risk    : {key_risk or 'N/A'}
Timestamp   : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}
{'='*50}
""".strip()
    return _send(subject, body)


# ── 5. Drawdown limit hit ──────────────────────────────────────────────────

def send_drawdown_alert(drawdown_pct: float) -> bool:
    subject = f"[TRADING AGENT] ⚠️ DRAWDOWN LIMIT HIT — {drawdown_pct:.2f}%"
    body = f"""
TRADING AGENT — DRAWDOWN LIMIT ALERT
{'='*50}
Daily drawdown has reached {drawdown_pct:.2f}%.
Limit is {config.DAILY_DRAWDOWN_LIMIT_PCT}%.
All trading has been HALTED for 24 hours.
Timestamp   : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}
{'='*50}
""".strip()
    return _send(subject, body)


# ── 6. Market-wide AMBER alert ─────────────────────────────────────────────

def send_market_amber_alert(
    market: str, red_count: int, total_count: int, red_pct: float
) -> bool:
    """Fires when >30% of instruments in a single market return RED in one cycle."""
    subject = (
        f"[TRADING AGENT] ⚠️ MARKET ALERT — "
        f"{market} showing broad RED signals"
    )
    body = f"""
TRADING AGENT — MARKET-WIDE RISK ALERT
{'='*50}
Market      : {market}
RED Verdicts: {red_count} of {total_count} instruments ({red_pct:.1f}%)
Threshold   : 30% — broad risk event detected

This is not a single-instrument alert.
A systemic risk condition may be unfolding in this market.
Review open positions and consider reducing exposure.

Timestamp   : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}
{'='*50}
""".strip()
    return _send(subject, body)


# ── 7. Weekly performance summary ─────────────────────────────────────────

def send_weekly_summary(
    signals_week: list,
    closed_week: list,
    snapshot: dict | None,
    week_start_snapshot: dict | None,
) -> bool:
    subject = (
        f"[TRADING AGENT] Weekly Performance — "
        f"w/e {datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
    )

    # ── Signal stats ──────────────────────────────────────────────────────
    directional = [s for s in signals_week if s.get("direction", "NEUTRAL") != "NEUTRAL"]
    total_signals = len(directional)
    executed_signals = [
        s for s in directional
        if s.get("action_taken", "") in ("EXECUTED", "PAPER_SIGNAL")
    ]
    rejected_signals = [
        s for s in directional
        if str(s.get("action_taken", "")).startswith("REJECTED")
    ]

    # ── Trade P&L ─────────────────────────────────────────────────────────
    total_pnl = sum((t.get("pnl") or 0) for t in closed_week)
    closed_lines = "\n".join(
        f"  {t.get('direction','?'):5s} {t.get('instrument','?'):12s} "
        f"pnl={t.get('pnl',0):+.4f} ({t.get('pnl_pct',0):+.2f}%)"
        for t in closed_week
    ) or "  None"

    # ── Best / worst signal (by confidence score) ─────────────────────────
    scored = [s for s in directional if s.get("confidence_score") is not None]
    if scored:
        best  = max(scored, key=lambda s: s.get("confidence_score", 0))
        worst = min(scored, key=lambda s: s.get("confidence_score", 0))
        best_line  = (
            f"{best.get('instrument','?')} "
            f"{best.get('direction','?')} "
            f"conf={best.get('confidence_score',0):.1f}% "
            f"→ {best.get('action_taken','?')}"
        )
        worst_line = (
            f"{worst.get('instrument','?')} "
            f"{worst.get('direction','?')} "
            f"conf={worst.get('confidence_score',0):.1f}% "
            f"→ {worst.get('action_taken','?')}"
        )
    else:
        best_line = worst_line = "N/A"

    # ── Portfolio performance ─────────────────────────────────────────────
    current_value    = snapshot.get("total_value", 0) if snapshot else 0
    week_start_value = week_start_snapshot.get("total_value", 0) if week_start_snapshot else 0

    if week_start_value and week_start_value > 0:
        weekly_return_pct = (current_value - week_start_value) / week_start_value * 100
        vs_benchmark      = weekly_return_pct - WEEKLY_BENCHMARK_PCT
        vs_benchmark_str  = f"{vs_benchmark:+.3f}% vs benchmark"
    else:
        weekly_return_pct = 0.0
        vs_benchmark_str  = "N/A (no prior snapshot)"

    portfolio_block = (
        f"Current Value   : {current_value:.2f}\n"
        f"Week-start Value: {week_start_value:.2f}\n"
        f"Weekly Return   : {weekly_return_pct:+.3f}%\n"
        f"Benchmark (wk)  : {WEEKLY_BENCHMARK_PCT:.3f}%  (6.19% ann.)\n"
        f"vs Benchmark    : {vs_benchmark_str}"
    )

    # ── Market signal activity ────────────────────────────────────────────
    market_counts: Counter = Counter(s.get("market_type", "unknown") for s in directional)
    market_lines = "\n".join(
        f"  {mkt:20s}: {cnt} signals"
        for mkt, cnt in market_counts.most_common()
    ) or "  None"

    body = f"""
TRADING AGENT — WEEKLY PERFORMANCE
{'='*50}
Week Ending : {datetime.now(timezone.utc).strftime('%Y-%m-%d')}

── SIGNAL SUMMARY ───────────────────────────────
Total Signals   : {total_signals}
  Executed      : {len(executed_signals)}
  Rejected      : {len(rejected_signals)}
Best Signal     : {best_line}
Worst Signal    : {worst_line}

── EXECUTED TRADES ──────────────────────────────
{closed_lines}
Total P&L       : {total_pnl:+.4f}

── PORTFOLIO PERFORMANCE ────────────────────────
{portfolio_block}

── SIGNAL ACTIVITY BY MARKET ────────────────────
{market_lines}
{'='*50}
""".strip()

    return _send(subject, body)
