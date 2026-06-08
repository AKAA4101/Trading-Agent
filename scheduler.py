"""
APScheduler configuration.
Jobs:
  - Full analysis cycle   : every 4 hours
  - Portfolio snapshot    : daily 07:00 Brisbane (UTC+10)
  - Daily summary email   : daily 07:05 Brisbane (UTC+10)
  - Weekly summary email  : Monday 07:00 Brisbane (UTC+10)
  - Tier 2 weekly screen  : Monday 06:00 Brisbane (UTC+10)
"""
import logging
from collections import defaultdict
from datetime import datetime, timezone
from threading import Lock

import pytz
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from config import config
from database.db_manager import DBManager

logger = logging.getLogger(__name__)

BRISBANE = pytz.timezone(config.BRISBANE_TZ)

MARKET_AMBER_THRESHOLD = 0.30  # 30% of a market returning RED triggers AMBER alert


def run_analysis_cycle(db: DBManager) -> None:
    """Full 4-hourly analysis cycle across all active instruments."""
    from data.watchlist import get_active
    from analysis.technical import compute as tech_compute
    from analysis.news_filter import analyse as news_analyse
    from analysis.confidence_scorer import score as conf_score
    from risk.risk_manager import RiskManager
    from execution.alpaca_executor import get_account as alpaca_account
    from execution.oanda_executor import get_account as oanda_account, execute_queued_signals
    from execution.paper_broker import manage_positions as paper_manage
    from notifications.email_notifier import send_signal_alert, send_red_alert

    logger.info("──── Analysis cycle started ────")
    risk_mgr = RiskManager(db)

    if risk_mgr.check_drawdown_halt():
        logger.warning("Drawdown halt active — skipping analysis cycle")
        return

    # ── Pre-cycle tasks ───────────────────────────────────────────────────
    # Execute any queued forex signals if market is now open
    queued_exec = execute_queued_signals(db)
    if queued_exec:
        logger.info("Executed %d queued OANDA signals", queued_exec)

    # Check open paper_sim positions for SL/TP hits
    paper_summary = paper_manage(db)
    logger.info("paper_sim position check: %s", paper_summary)

    # Portfolio value for position sizing (all three sources)
    alpaca_acct = alpaca_account()
    oanda_acct  = oanda_account()
    from execution.paper_broker import PAPER_SIM_STARTING_VALUE
    paper_sim_realized = db.get_paper_sim_realized_pnl()
    paper_sim_value    = PAPER_SIM_STARTING_VALUE + paper_sim_realized
    portfolio_value = (
        alpaca_acct.get("portfolio_value", 100_000)
        + oanda_acct.get("nav", 0)
        + paper_sim_value
    ) or 100_000
    logger.info(
        "Portfolio values — Alpaca=%.2f OANDA=%.2f PaperSim=%.2f Total=%.2f",
        alpaca_acct.get("portfolio_value", 0), oanda_acct.get("nav", 0),
        paper_sim_value, portfolio_value,
    )

    active = get_active()
    by_type: dict[str, int] = {}
    for inst in active:
        by_type[inst.market_type] = by_type.get(inst.market_type, 0) + 1
    logger.info(
        "Scanning %d active instruments — %s",
        len(active),
        " | ".join(f"{k}={v}" for k, v in sorted(by_type.items())),
    )

    from concurrent.futures import ThreadPoolExecutor, as_completed
    from notifications.email_notifier import send_market_amber_alert

    BATCH_SIZE  = 50
    MAX_WORKERS = 4

    # Track RED verdicts per market for the AMBER alert check
    market_verdicts: dict[str, list[str]] = defaultdict(list)
    mv_lock = Lock()

    def _safe_analyse(inst):
        try:
            verdict = _analyse_instrument(
                inst, db, risk_mgr, portfolio_value,
                send_signal_alert, send_red_alert,
            )
            if verdict is not None:
                with mv_lock:
                    market_verdicts[inst.market_type].append(verdict)
        except Exception as exc:
            logger.error("Error analysing %s: %s", inst.symbol, exc)

    total = len(active)
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for i in range(0, total, BATCH_SIZE):
            batch = active[i : i + BATCH_SIZE]
            batch_num   = i // BATCH_SIZE + 1
            total_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE
            logger.info("Batch %d/%d — %d instruments", batch_num, total_batches, len(batch))
            futures = [pool.submit(_safe_analyse, inst) for inst in batch]
            for f in as_completed(futures):
                f.result()

    logger.info("──── Analysis cycle complete — %d instruments ────", total)

    # ── Market-wide AMBER alert check ────────────────────────────────────
    for market, verdicts in market_verdicts.items():
        if not verdicts:
            continue
        red_count = sum(1 for v in verdicts if v == "RED")
        red_pct   = red_count / len(verdicts)
        if red_pct > MARKET_AMBER_THRESHOLD:
            logger.warning(
                "Market AMBER alert: %s — %d/%d instruments RED (%.1f%%)",
                market, red_count, len(verdicts), red_pct * 100,
            )
            send_market_amber_alert(market, red_count, len(verdicts), red_pct * 100)


def _analyse_instrument(inst, db, risk_mgr, portfolio_value,
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
        return None

    # ── 2. Technical analysis ─────────────────────────────────────
    tech = tech_compute(df)
    if tech is None:
        logger.warning("Insufficient data for technical analysis: %s", symbol)
        return None

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
        db.insert_signal(
            instrument=symbol, market_type=inst.market_type,
            direction=signal.direction, technical_score=signal.technical_score,
            news_verdict=signal.news_verdict, confidence_score=signal.confidence,
            action_taken="NO_SIGNAL", reasoning=signal.reasoning,
            entry_price=current_price, stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
        )
    elif risk_decision.approved:
        # ── 7. Insert signal as PENDING, route to execution ───────
        from execution.execution_router import route as exec_route

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

        route_result = exec_route(
            symbol=symbol,
            market_type=inst.market_type,
            direction=signal.direction,
            units=risk_decision.position_size_units,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            signal_id=signal_id,
            db=db,
            entry_price=current_price,
        )

        action = route_result.action
        executed = route_result.success
        logger.info(
            "Execution route result | %s → broker=%s action=%s trade_id=%s",
            symbol, route_result.broker, action, route_result.trade_db_id,
        )
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
    return news.verdict


def run_portfolio_snapshot(db: DBManager) -> None:
    from execution.alpaca_executor import get_account as alpaca_account
    from execution.oanda_executor import get_account as oanda_account
    from execution.paper_broker import PAPER_SIM_STARTING_VALUE

    alpaca = alpaca_account()
    oanda  = oanda_account()

    alpaca_value   = alpaca.get("portfolio_value", 0)
    oanda_value    = oanda.get("nav", 0)
    paper_sim_realized = db.get_paper_sim_realized_pnl()
    paper_sim_value    = PAPER_SIM_STARTING_VALUE + paper_sim_realized

    total_value = alpaca_value + oanda_value + paper_sim_value
    cash        = alpaca.get("cash", 0) + oanda.get("balance", 0)
    open_pos    = db.count_open_trades()

    closed_today = db.get_trades_today()
    daily_pnl = sum((t.get("pnl") or 0) for t in closed_today if t.get("status") == "CLOSED")

    snap = db.latest_portfolio_snapshot()
    initial = snap.get("total_value", total_value) if snap else total_value
    drawdown_pct = max(0.0, (initial - total_value) / initial * 100) if initial > 0 else 0.0

    # Week-start baseline: earliest snapshot from this Monday onwards
    week_start_value = None
    weekly_return_pct = None
    week_snap = db.get_week_start_snapshot()
    if week_snap:
        wv = week_snap.get("total_value")
        if wv and float(wv) > 0:
            week_start_value = float(wv)
            weekly_return_pct = round((total_value - week_start_value) / week_start_value * 100, 4)

    db.insert_snapshot(
        total_value=total_value,
        cash=cash,
        open_positions=open_pos,
        daily_pnl=daily_pnl,
        total_pnl=0,
        drawdown_pct=drawdown_pct,
        alpaca_value=alpaca_value,
        oanda_value=oanda_value,
        paper_sim_value=paper_sim_value,
        week_start_value=week_start_value,
        weekly_return_pct=weekly_return_pct,
    )

    if drawdown_pct >= config.DAILY_DRAWDOWN_LIMIT_PCT:
        from notifications.email_notifier import send_drawdown_alert
        send_drawdown_alert(drawdown_pct)

    logger.info(
        "Portfolio snapshot: alpaca=%.2f oanda=%.2f paper_sim=%.2f total=%.2f drawdown=%.2f%%",
        alpaca_value, oanda_value, paper_sim_value, total_value, drawdown_pct,
    )


def run_position_monitor(db: DBManager) -> None:
    """Lightweight 30-minute position check — no watchlist scan."""
    from execution.position_monitor import run_position_check
    summary = run_position_check(db)
    logger.info("Position monitor: %s", summary)


def run_daily_summary(db: DBManager) -> None:
    from data.watchlist import get_active
    from notifications.email_notifier import send_daily_summary
    signals             = db.get_signals_today()
    closed              = db.get_closed_trades_today()
    snap                = db.latest_portfolio_snapshot()
    open_trades         = db.get_open_trades()
    instruments_scanned = len(get_active())
    send_daily_summary(signals, closed, snap, instruments_scanned, open_trades=open_trades)
    logger.info("Daily summary email sent")


def run_weekly_summary(db: DBManager) -> None:
    from notifications.email_notifier import send_weekly_summary
    signals_week      = db.get_signals_this_week()
    closed_week       = db.get_closed_trades_this_week()
    snap              = db.latest_portfolio_snapshot()
    week_start_snap   = db.get_snapshot_days_ago(7)
    send_weekly_summary(signals_week, closed_week, snap, week_start_snap)
    logger.info("Weekly summary email sent")


def run_tier2_screen(db: DBManager) -> None:
    from data.watchlist import activate_tier2
    from data.index_loader import refresh_all_indices
    logger.info("Weekly Tier 2 screen — refreshing all index constituents")
    summary = refresh_all_indices()
    logger.info("Index refresh complete: %s", summary)
    activate_tier2()
    run_analysis_cycle(db)


def build_scheduler(db: DBManager) -> BlockingScheduler:
    scheduler = BlockingScheduler(timezone=pytz.utc)

    # Lightweight position monitor every 30 minutes
    scheduler.add_job(
        run_position_monitor,
        trigger=IntervalTrigger(minutes=30),
        args=[db],
        id="position_monitor",
        name="Position Monitor",
        misfire_grace_time=60,
    )

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

    # Weekly performance summary email: Monday 07:00 Brisbane
    scheduler.add_job(
        run_weekly_summary,
        trigger=CronTrigger(day_of_week="mon", hour=7, minute=0, timezone=BRISBANE),
        args=[db],
        id="weekly_summary",
        name="Weekly Performance Summary Email",
        misfire_grace_time=300,
    )

    return scheduler
