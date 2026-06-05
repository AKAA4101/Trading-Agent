"""
APScheduler configuration.
Jobs:
  - Full analysis cycle   : every 4 hours
  - Portfolio snapshot    : daily 07:00 Brisbane (UTC+10)
  - Daily summary email   : daily 07:00 Brisbane (UTC+10)
  - Tier 2 weekly screen  : Monday 06:00 Brisbane (UTC+10)
"""
import logging
from datetime import datetime, timezone

import pytz
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from config import config
from database.db_manager import DBManager

logger = logging.getLogger(__name__)

BRISBANE = pytz.timezone(config.BRISBANE_TZ)


def run_analysis_cycle(db: DBManager) -> None:
    """Full 4-hourly analysis cycle across all active instruments."""
    from data.watchlist import get_active
    from data.collectors.alpaca_collector import get_equity_bars, get_crypto_bars
    from data.collectors.oanda_collector import get_forex_bars
    from data.collectors.massive_collector import get_global_equity_bars
    from analysis.technical import compute as tech_compute
    from analysis.news_filter import analyse as news_analyse
    from analysis.confidence_scorer import score as conf_score
    from risk.risk_manager import RiskManager
    from execution.alpaca_executor import submit_bracket_order as alpaca_exec, get_account as alpaca_account
    from execution.oanda_executor import submit_bracket_order as oanda_exec, get_account as oanda_account
    from notifications.email_notifier import send_signal_alert, send_red_alert

    logger.info("──── Analysis cycle started ────")
    risk_mgr = RiskManager(db)

    if risk_mgr.check_drawdown_halt():
        logger.warning("Drawdown halt active — skipping analysis cycle")
        return

    # Portfolio value for position sizing
    alpaca_acct = alpaca_account()
    oanda_acct  = oanda_account()
    portfolio_value = (
        alpaca_acct.get("portfolio_value", 100_000)
        + oanda_acct.get("nav", 0)
    ) or 100_000

    active = get_active()
    by_type: dict[str, int] = {}
    for inst in active:
        by_type[inst.market_type] = by_type.get(inst.market_type, 0) + 1
    logger.info(
        "Scanning %d active instruments — %s",
        len(active),
        " | ".join(f"{k}={v}" for k, v in sorted(by_type.items())),
    )

    for inst in active:
        try:
            _analyse_instrument(
                inst, db, risk_mgr, portfolio_value,
                alpaca_exec, oanda_exec,
                send_signal_alert, send_red_alert,
            )
        except Exception as exc:
            logger.error("Error analysing %s: %s", inst.symbol, exc)

    logger.info("──── Analysis cycle complete ────")


def _analyse_instrument(inst, db, risk_mgr, portfolio_value,
                         alpaca_exec, oanda_exec,
                         send_signal_alert, send_red_alert):
    from data.collectors.alpaca_collector import get_equity_bars, get_crypto_bars
    from data.collectors.oanda_collector import get_forex_bars
    from data.collectors.yfinance_collector import get_yfinance_bars
    from analysis.technical import compute as tech_compute
    from analysis.news_filter import analyse as news_analyse
    from analysis.confidence_scorer import score as conf_score
    from risk.risk_manager import RiskDecision

    symbol = inst.symbol
    logger.info("Analysing %s (%s)", symbol, inst.market_type)

    # ── Market session gate — yfinance EOD instruments only ───────
    # Only analyse when the instrument's exchange has closed in the last 24h.
    # Forex and crypto are always eligible regardless of time.
    if inst.market_type == "yfinance":
        from analysis.market_sessions import has_market_recently_closed
        if not has_market_recently_closed(symbol):
            logger.info("Skipping %s — exchange has not closed in last 24h", symbol)
            return

    # ── 1. Fetch price data ───────────────────────────────────────
    if inst.market_type == "forex":
        df = get_forex_bars(symbol)
    elif inst.market_type == "crypto":
        df = get_crypto_bars(symbol)
    elif inst.market_type == "us_equity":
        df = get_equity_bars(symbol)
    elif inst.market_type == "yfinance":
        df = get_yfinance_bars(symbol)
    else:
        from data.collectors.massive_collector import get_global_equity_bars
        df = get_global_equity_bars(symbol)

    if df is None or df.empty:
        logger.warning("No price data for %s — skipping", symbol)
        return

    # ── 2. Technical analysis ─────────────────────────────────────
    tech = tech_compute(df)
    if tech is None:
        logger.warning("Insufficient data for technical analysis: %s", symbol)
        return

    current_price = float(df["Close"].iloc[-1])

    # ── 3. News / sentiment filter ────────────────────────────────
    news = news_analyse(symbol)

    if news.verdict == "RED":
        logger.warning("RED verdict for %s — %s", symbol, news.reasoning)
        send_red_alert(symbol, news.reasoning, news.key_risk)

    # ── 4. Confidence score ───────────────────────────────────────
    signal = conf_score(symbol, tech, news, current_price, threshold=config.CONFIDENCE_THRESHOLD)

    # ── 5. ATR history for volatility filter ─────────────────────
    atr_col = "ATRr_14"
    import pandas_ta as ta
    atr_series = df["Close"].copy()
    df_copy = df.copy()
    df_copy.ta.atr(length=14, append=True)
    atr_avg = float(df_copy[atr_col].dropna().tail(20).mean()) if atr_col in df_copy.columns else None

    # ── 6. Risk evaluation ────────────────────────────────────────
    risk_decision = risk_mgr.evaluate(signal, tech, portfolio_value, current_price, atr_avg)

    action = "REJECTED"
    executed = False

    if signal.direction == "NEUTRAL":
        action = "NO_SIGNAL"
    elif risk_decision.approved:
        # ── 7. Execute ────────────────────────────────────────────
        if inst.market_type == "yfinance":
            # International exchange tickers are not executable via Alpaca/OANDA.
            # Record as PAPER_SIGNAL so the signal is visible in the DB and email.
            db.insert_signal(
                instrument=symbol,
                market_type=inst.market_type,
                direction=signal.direction,
                technical_score=signal.technical_score,
                news_verdict=signal.news_verdict,
                confidence_score=signal.confidence,
                action_taken="PAPER_SIGNAL",
                reasoning=signal.reasoning,
                entry_price=current_price,
                stop_loss=signal.stop_loss,
                take_profit=signal.take_profit,
            )
            action = "PAPER_SIGNAL"
            executed = True
        else:
            signal_id = db.insert_signal(
                instrument=symbol,
                market_type=inst.market_type,
                direction=signal.direction,
                technical_score=signal.technical_score,
                news_verdict=signal.news_verdict,
                confidence_score=signal.confidence,
                action_taken="PENDING",
                reasoning=signal.reasoning,
                entry_price=current_price,
                stop_loss=signal.stop_loss,
                take_profit=signal.take_profit,
            )

            if inst.market_type == "forex":
                result = oanda_exec(
                    instrument=symbol,
                    direction=signal.direction,
                    units=risk_decision.position_size_units,
                    stop_loss=signal.stop_loss,
                    take_profit=signal.take_profit,
                    signal_id=signal_id,
                    db=db,
                )
            else:
                result = alpaca_exec(
                    symbol=symbol,
                    direction=signal.direction,
                    units=risk_decision.position_size_units,
                    stop_loss=signal.stop_loss,
                    take_profit=signal.take_profit,
                    signal_id=signal_id,
                    db=db,
                )

            if result.success:
                action = "EXECUTED"
                executed = True
                db.insert_signal(
                    instrument=symbol,
                    market_type=inst.market_type,
                    direction=signal.direction,
                    technical_score=signal.technical_score,
                    news_verdict=signal.news_verdict,
                    confidence_score=signal.confidence,
                    action_taken=action,
                    reasoning=signal.reasoning,
                    entry_price=current_price,
                    stop_loss=signal.stop_loss,
                    take_profit=signal.take_profit,
                )
            else:
                action = f"EXECUTION_FAILED: {result.message}"
    else:
        db.insert_signal(
            instrument=symbol,
            market_type=inst.market_type,
            direction=signal.direction,
            technical_score=signal.technical_score,
            news_verdict=signal.news_verdict,
            confidence_score=signal.confidence,
            action_taken=f"REJECTED: {risk_decision.reason}",
            reasoning=signal.reasoning,
            entry_price=current_price,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
        )

    send_signal_alert(signal, risk_decision, executed)
    logger.info("Done: %s | conf=%.1f | action=%s", symbol, signal.confidence, action)


def run_portfolio_snapshot(db: DBManager) -> None:
    from execution.alpaca_executor import get_account as alpaca_account
    from execution.oanda_executor import get_account as oanda_account

    alpaca = alpaca_account()
    oanda  = oanda_account()

    total_value = alpaca.get("portfolio_value", 0) + oanda.get("nav", 0)
    cash        = alpaca.get("cash", 0) + oanda.get("balance", 0)
    open_pos    = db.count_open_trades()

    closed_today = db.get_trades_today()
    daily_pnl = sum((t.get("pnl") or 0) for t in closed_today if t.get("status") == "CLOSED")

    snap = db.latest_portfolio_snapshot()
    initial = snap.get("total_value", total_value) if snap else total_value
    drawdown_pct = max(0.0, (initial - total_value) / initial * 100) if initial > 0 else 0.0

    db.insert_snapshot(
        total_value=total_value,
        cash=cash,
        open_positions=open_pos,
        daily_pnl=daily_pnl,
        total_pnl=0,
        drawdown_pct=drawdown_pct,
    )

    if drawdown_pct >= config.DAILY_DRAWDOWN_LIMIT_PCT:
        from notifications.email_notifier import send_drawdown_alert
        send_drawdown_alert(drawdown_pct)

    logger.info("Portfolio snapshot: value=%.2f drawdown=%.2f%%", total_value, drawdown_pct)


def run_daily_summary(db: DBManager) -> None:
    from notifications.email_notifier import send_daily_summary
    signals = db.get_recent_signals(50)
    closed  = db.get_recent_closed_trades(20)
    snap    = db.latest_portfolio_snapshot()
    send_daily_summary(signals, closed, snap)
    logger.info("Daily summary email sent")


def run_tier2_screen(db: DBManager) -> None:
    from data.watchlist import activate_tier2
    logger.info("Weekly Tier 2 screen — activating global equities")
    activate_tier2()
    run_analysis_cycle(db)


def build_scheduler(db: DBManager) -> BlockingScheduler:
    scheduler = BlockingScheduler(timezone=pytz.utc)

    # Full analysis every 4 hours
    scheduler.add_job(
        run_analysis_cycle,
        trigger=IntervalTrigger(hours=4),
        args=[db],
        id="analysis_cycle",
        name="Full Analysis Cycle",
        misfire_grace_time=300,
    )

    # Portfolio snapshot daily at 07:00 Brisbane
    scheduler.add_job(
        run_portfolio_snapshot,
        trigger=CronTrigger(hour=7, minute=0, timezone=BRISBANE),
        args=[db],
        id="portfolio_snapshot",
        name="Daily Portfolio Snapshot",
        misfire_grace_time=300,
    )

    # Daily summary email at 07:00 Brisbane
    scheduler.add_job(
        run_daily_summary,
        trigger=CronTrigger(hour=7, minute=5, timezone=BRISBANE),
        args=[db],
        id="daily_summary",
        name="Daily Summary Email",
        misfire_grace_time=300,
    )

    # Tier 2 weekly screen: Monday 06:00 Brisbane
    scheduler.add_job(
        run_tier2_screen,
        trigger=CronTrigger(day_of_week="mon", hour=6, minute=0, timezone=BRISBANE),
        args=[db],
        id="tier2_screen",
        name="Weekly Tier 2 Screen",
        misfire_grace_time=600,
    )

    return scheduler
