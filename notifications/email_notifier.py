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


# ── 2b. Position monitor close alert ──────────────────────────────────────────

def send_position_closed(trade: dict, exit_reason: str) -> bool:
    """
    Dedicated alert from the 30-minute position monitor.
    exit_reason: 'TAKE_PROFIT' or 'STOP_LOSS' or 'ALPACA_CLOSED'
    """
    pnl_pct   = trade.get("pnl_pct", 0) or 0
    symbol    = trade.get("instrument", "?")
    direction = trade.get("direction", "?")
    entry     = trade.get("entry_price", 0) or 0
    exit_p    = trade.get("exit_price", 0) or 0
    broker    = trade.get("broker", "").upper()
    pnl       = trade.get("pnl", 0) or 0

    if exit_reason == "TAKE_PROFIT":
        emoji = "✅"
        label = "TAKE PROFIT"
    elif exit_reason == "STOP_LOSS":
        emoji = "🛑"
        label = "STOP LOSS"
    else:
        emoji = "🔔"
        label = "POSITION CLOSED"

    subject = f"[TRADING AGENT] {emoji} {label} — {symbol} {pnl_pct:+.1f}%"

    body = f"""
TRADING AGENT — POSITION CLOSED
{'='*50}
{emoji} {label}

Instrument  : {symbol}
Direction   : {direction}
Broker      : {broker}
Exit Reason : {exit_reason}

Entry Price : {entry}
Exit Price  : {exit_p}
P&L         : {pnl:+.4f}
P&L %       : {pnl_pct:+.2f}%

Exit Time   : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}
{'='*50}
This is a paper trading system. No real money is at risk.
""".strip()

    logger.info(
        "Position closed alert: %s %s %s%.1f%%", emoji, symbol,
        "+" if pnl_pct >= 0 else "", pnl_pct,
    )
    return _send(subject, body)


# ── 3. Daily summary ───────────────────────────────────────────────────────

def send_daily_summary(
    signals_today: list,
    closed_today: list,
    snapshot: dict | None,
    instruments_scanned: int = 0,
    open_trades: list | None = None,
) -> bool:
    subject = f"[TRADING AGENT] Daily Summary — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}"

    # ── Signal stats ──────────────────────────────────────────────────────
    directional = [s for s in signals_today if s.get("direction", "NEUTRAL") != "NEUTRAL"]
    total_signals = len(directional)

    executed_signals = [
        s for s in directional
        if s.get("action_taken", "") in ("EXECUTED", "PAPER_SIGNAL", "PAPER_SIM_OPEN", "QUEUED_GTC", "QUEUED")
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
            str(s.get("action_taken", "")).removeprefix("REJECTED: ").split(":")[0].strip()
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

    # ── Open positions block ──────────────────────────────────────────────
    open_block = ""
    if open_trades:
        from execution.position_monitor import _fetch_price
        lines = []
        total_unrealised = 0.0
        for t in open_trades:
            sym       = t.get("instrument", "?")
            direction = t.get("direction", "?")
            entry     = float(t.get("entry_price") or 0)
            broker    = t.get("broker", "")
            # Use stored unrealised_pnl if fresh, else try to fetch live
            unrealised_pct = float(t.get("unrealised_pnl") or 0)
            if entry:
                price, _ = _fetch_price(sym)
                if price:
                    if direction == "LONG":
                        unrealised_pct = (price - entry) / entry * 100
                    else:
                        unrealised_pct = (entry - price) / entry * 100
                trend = "📈" if unrealised_pct >= 0 else "📉"
                lines.append(
                    f"  {sym:<12s} {direction:<5s} Entry: {entry:<10g} "
                    f"Current: {price or 0:<10g} {unrealised_pct:+.1f}% {trend}"
                )
                total_unrealised += unrealised_pct
            else:
                lines.append(f"  {sym:<12s} {direction:<5s} (no entry price)")
        avg_unrealised = total_unrealised / len(open_trades) if open_trades else 0
        open_block = (
            "\n── OPEN POSITIONS ───────────────────────────────\n"
            + "\n".join(lines)
            + f"\n\n  Total unrealised P&L (avg): {avg_unrealised:+.1f}%"
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
{open_block}
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
    liquidity_stats: dict | None = None,
    inception_stats: dict | None = None,
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
        if s.get("action_taken", "") in ("EXECUTED", "PAPER_SIGNAL", "PAPER_SIM_OPEN", "QUEUED_GTC", "QUEUED")
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
    current_value = snapshot.get("total_value", 0) if snapshot else 0

    # Prefer agent_config baselines (accurate) over snapshot-derived (stale)
    if inception_stats and inception_stats.get("week_start_value", 0) > 0:
        week_start_value = inception_stats["week_start_value"]
        week_start_date  = inception_stats["week_start_date"][:10]
    elif week_start_snapshot and week_start_snapshot.get("total_value", 0):
        week_start_value = float(week_start_snapshot["total_value"])
        week_start_date  = "~7d ago"
    else:
        week_start_value = 0.0
        week_start_date  = "unknown"

    if week_start_value > 0:
        weekly_return_pct = (current_value - week_start_value) / week_start_value * 100
        vs_benchmark      = weekly_return_pct - WEEKLY_BENCHMARK_PCT
        vs_benchmark_str  = f"{vs_benchmark:+.3f}%"
    else:
        weekly_return_pct = 0.0
        vs_benchmark_str  = "N/A"

    # Inception return
    if inception_stats and inception_stats.get("total_start_value", 0) > 0:
        inception_value   = inception_stats["total_start_value"]
        inception_date    = inception_stats["total_start_date"][:10]
        inception_return  = (current_value - inception_value) / inception_value * 100
        days_live         = (datetime.now(timezone.utc).date() -
                             datetime.fromisoformat(inception_stats["total_start_date"]).date()
                             ).days
        # Annualised benchmark for same period
        annual_bench_pct  = 6.19 / 365 * days_live
        vs_inception_bench = inception_return - annual_bench_pct
        inception_block   = (
            f"Inception Value : {inception_value:.2f}  ({inception_date})\n"
            f"Total Return    : {inception_return:+.3f}%  ({days_live}d)\n"
            f"Benchmark (same): {annual_bench_pct:+.3f}%\n"
            f"vs Benchmark    : {vs_inception_bench:+.3f}%"
        )
    else:
        inception_block = "Inception data  : not yet available"

    portfolio_block = (
        f"Current Value   : {current_value:.2f}\n"
        f"Week-start Value: {week_start_value:.2f}  ({week_start_date})\n"
        f"Weekly Return   : {weekly_return_pct:+.3f}%\n"
        f"Benchmark (wk)  : {WEEKLY_BENCHMARK_PCT:.3f}%  (6.19% ann.)\n"
        f"vs Benchmark    : {vs_benchmark_str}\n\n"
        f"{inception_block}"
    )

    # ── Market signal activity ────────────────────────────────────────────
    market_counts: Counter = Counter(s.get("market_type", "unknown") for s in directional)
    market_lines = "\n".join(
        f"  {mkt:20s}: {cnt} signals"
        for mkt, cnt in market_counts.most_common()
    ) or "  None"

    # ── Liquidity filter summary ──────────────────────────────────────────
    if liquidity_stats:
        liq_pass     = liquidity_stats.get("pass", 0)
        liq_fail     = liquidity_stats.get("fail", 0)
        liq_total    = liq_pass + liq_fail
        liq_pct      = liq_pass / liq_total * 100 if liq_total else 0
        top_markets  = liquidity_stats.get("top_fail_markets", {})
        top10_failed = liquidity_stats.get("top10_failed", [])
        market_fail_lines = "\n".join(
            f"  {mkt:20s}: {cnt} instruments filtered"
            for mkt, cnt in sorted(top_markets.items(), key=lambda x: -x[1])
        ) or "  None"
        top10_lines = "\n".join(
            f"  {sym:<15s} avgVol: {vol:>12,}"
            for sym, vol in top10_failed
        ) or "  None"
        liquidity_block = f"""
── LIQUIDITY FILTER SUMMARY ─────────────────────
Instruments passing : {liq_pass} of {liq_total} ({liq_pct:.1f}%)
Instruments filtered: {liq_fail}
Markets most affected:
{market_fail_lines}
Top 10 filtered instruments:
{top10_lines}"""
    else:
        liquidity_block = ""

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
{liquidity_block}
{'='*50}
""".strip()

    return _send(subject, body)
