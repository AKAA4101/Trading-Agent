"""
Email alert system via Gmail SMTP.
Sends alerts for: new signals, closed trades, daily summary,
RED news alerts, and drawdown limit hits.
"""
import logging
import smtplib
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from config import config

logger = logging.getLogger(__name__)

VERDICT_EMOJI = {"GREEN": "🟢", "AMBER": "🟡", "RED": "🔴"}
DIR_EMOJI     = {"LONG": "📈", "SHORT": "📉", "NEUTRAL": "⚪"}


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
    """signal: SignalResult, risk_decision: RiskDecision"""
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

def send_daily_summary(signals_today: list, closed_today: list, snapshot: dict | None) -> bool:
    subject = f"[TRADING AGENT] Daily Summary — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}"

    total_pnl = sum((t.get("pnl") or 0) for t in closed_today)
    pnl_str = f"{total_pnl:+.4f}"

    signal_lines = "\n".join(
        f"  {s.get('direction','?'):5s} {s.get('instrument','?'):12s} "
        f"conf={s.get('confidence_score',0):.0f}% news={s.get('news_verdict','?')} → {s.get('action_taken','?')}"
        for s in signals_today
    ) or "  None"

    trade_lines = "\n".join(
        f"  {t.get('direction','?'):5s} {t.get('instrument','?'):12s} "
        f"pnl={t.get('pnl',0):+.4f} ({t.get('pnl_pct',0):+.2f}%)"
        for t in closed_today
    ) or "  None"

    snap_str = ""
    if snapshot:
        snap_str = (
            f"\nPortfolio Value : {snapshot.get('total_value', 'N/A')}\n"
            f"Daily P&L       : {snapshot.get('daily_pnl', 'N/A')}\n"
            f"Drawdown        : {snapshot.get('drawdown_pct', 0):.2f}%\n"
            f"Open Positions  : {snapshot.get('open_positions', 0)}"
        )

    body = f"""
TRADING AGENT — DAILY SUMMARY
{'='*50}
Date        : {datetime.now(timezone.utc).strftime('%Y-%m-%d')}
Closed P&L  : {pnl_str}
{snap_str}

── SIGNALS GENERATED ────────────────────────────
{signal_lines}

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
