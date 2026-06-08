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
MAX_OPEN_POSITIONS     = 5     # hard cap — mirrors risk_manager.MAX_OPEN_POSITIONS
LIGHTWEIGHT_TECH_THRESHOLD = 65.0  # technical score above this goes into next_entry_queue


def run_analysis_cycle(db: DBManager) -> None:
    """
    4-hourly analysis cycle — position-aware.

    FULL cycle (open positions < MAX_OPEN_POSITIONS):
      technical + news filter + confidence score + execution + signal emails

    LIGHTWEIGHT cycle (at MAX_OPEN_POSITIONS):
      technical only — no news/Claude/NewsAPI calls, no execution, no emails.
      Instruments scoring above LIGHTWEIGHT_TECH_THRESHOLD are queued in
      next_entry_queue as candidates for instant entry when a slot opens.
    """
    from data.watchlist import get_active
    from risk.risk_manager import RiskManager
    from execution.alpaca_executor import get_account as alpaca_account
    from execution.oanda_executor import get_account as oanda_account, execute_queued_signals
    from execution.paper_broker import manage_positions as paper_manage

    logger.info("──── Analysis cycle started ────")
    risk_mgr = RiskManager(db)

    if risk_mgr.check_drawdown_halt():
        logger.warning("Drawdown halt active — skipping analysis cycle")
        return

    # ── Check scan mode ───────────────────────────────────────────────────
    open_count = db.count_open_trades()
    lightweight = open_count >= MAX_OPEN_POSITIONS
    mode_label = "LIGHTWEIGHT (at max positions)" if lightweight else "FULL"
    logger.info("Scan mode: %s — open positions: %d/%d", mode_label, open_count, MAX_OPEN_POSITIONS)

    if lightweight:
        # Clear stale queue entries before rebuilding it
        removed = db.clear_stale_next_entry_queue()
        if removed:
            logger.info("Cleared %d stale next_entry_queue entries", removed)
        _run_lightweight_cycle(db, risk_mgr)
        return

    # ── Pre-cycle tasks (full cycle only) ────────────────────────────────
    queued_exec = execute_queued_signals(db)
    if queued_exec:
        logger.info("Executed %d queued OANDA signals", queued_exec)

    paper_summary = paper_manage(db)
    logger.info("paper_sim position check: %s", paper_summary)

    # Portfolio value for position sizing
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
    from notifications.email_notifier import send_signal_alert, send_red_alert, send_market_amber_alert

    BATCH_SIZE  = 50
    MAX_WORKERS = 4

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
            batch_num     = i // BATCH_SIZE + 1
            total_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE
            logger.info("Batch %d/%d — %d instruments", batch_num, total_batches, len(batch))
            futures = [pool.submit(_safe_analyse, inst) for inst in batch]
            for f in as_completed(futures):
                f.result()

    logger.info("──── Analysis cycle complete — %d instruments ────", total)

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


def _run_lightweight_cycle(db: DBManager, risk_mgr) -> None:
    """
    Technical-only scan when at maximum open positions.
    No news/Claude/NewsAPI calls. Queues strong setups for instant entry
    when a position closes.
    """
    from data.watchlist import get_active
    from concurrent.futures import ThreadPoolExecutor, as_completed

    active = get_active()
    logger.info("Lightweight scan: %d instruments (tech-only, no API calls)", len(active))

    queued_count = 0
    queued_lock  = Lock()

    def _safe_lightweight(inst):
        nonlocal queued_count
        try:
            result = _analyse_instrument_lightweight(inst, db, risk_mgr)
            if result:
                with queued_lock:
                    queued_count += 1
        except Exception as exc:
            logger.error("Lightweight error for %s: %s", inst.symbol, exc)

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(_safe_lightweight, inst) for inst in active]
        for f in as_completed(futures):
            f.result()

    queue = db.get_next_entry_queue()
    logger.info(
        "──── Lightweight cycle complete — %d queued for next slot, queue size=%d ────",
        queued_count, len(queue),
    )


def _analyse_instrument_lightweight(inst, db: DBManager, risk_mgr) -> bool:
    """
    Technical-only analysis for use when at max positions.
    Returns True if instrument was added to next_entry_queue.
    """
    from data.collectors.alpaca_collector import get_equity_bars, get_crypto_bars
    from data.collectors.oanda_collector import get_forex_bars
    from data.collectors.yfinance_collector import get_yfinance_bars
    from analysis.technical import compute as tech_compute

    symbol = inst.symbol

    # Market session gate — yfinance EOD instruments only
    if inst.market_type == "yfinance":
        from analysis.market_sessions import has_market_recently_closed
        if not has_market_recently_closed(symbol):
            return False

    # Liquidity filter
    liq_ok, _ = risk_mgr.check_liquidity(symbol, inst.market_type)
    if not liq_ok:
        return False

    # Fetch price data
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
        return False

    # Higher timeframe (best-effort, non-fatal)
    df_higher = None
    try:
        if inst.market_type == "forex":
            from data.collectors.oanda_collector import get_forex_bars_weekly
            df_higher = get_forex_bars_weekly(symbol)
        elif inst.market_type == "crypto":
            from data.collectors.alpaca_collector import get_crypto_bars_weekly
            df_higher = get_crypto_bars_weekly(symbol)
        elif inst.market_type == "us_equity":
            from data.collectors.alpaca_collector import get_equity_bars_weekly
            df_higher = get_equity_bars_weekly(symbol)
        elif inst.market_type == "yfinance":
            from data.collectors.yfinance_collector import get_yfinance_bars_weekly
            df_higher = get_yfinance_bars_weekly(symbol)
        else:
            from data.collectors.massive_collector import get_global_equity_bars_weekly
            df_higher = get_global_equity_bars_weekly(symbol)
    except Exception:
        pass

    tech = tech_compute(df, df_higher)
    if tech is None:
        return False

    if tech.direction == "NEUTRAL" or tech.score < LIGHTWEIGHT_TECH_THRESHOLD:
        logger.debug("Lightweight skip %s: dir=%s score=%.1f", symbol, tech.direction, tech.score)
        return False

    current_price = float(df["Close"].iloc[-1])
    db.upsert_next_entry_queue(
        instrument=symbol,
        market_type=inst.market_type,
        technical_score=tech.score,
        direction=tech.direction,
        entry_price=current_price,
    )
    logger.info(
        "WATCHING %s | dir=%s tech=%.1f — added to next_entry_queue",
        symbol, tech.direction, tech.score,
    )
    return True


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

    # ── 1b. Liquidity filter — runs before any API calls ─────────
    liq_ok, liq_reason = risk_mgr.check_liquidity(symbol, inst.market_type)
    if not liq_ok:
        db.insert_signal(
            instrument=symbol, market_type=inst.market_type,
            direction="NEUTRAL", technical_score=0.0,
            news_verdict="GREEN", confidence_score=0.0,
            action_taken="REJECTED: LIQUIDITY",
            reasoning=liq_reason,
            entry_price=0.0, stop_loss=None, take_profit=None,
        )
        return None

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

    # ── 1b. Higher timeframe data for multi-timeframe analysis ────
    # Fetch weekly bars as the higher timeframe. Failures are non-fatal —
    # if df_higher is None, tech_compute() defaults MTF multiplier to 1.0.
    df_higher = None
    try:
        if inst.market_type == "forex":
            from data.collectors.oanda_collector import get_forex_bars_weekly
            df_higher = get_forex_bars_weekly(symbol)
        elif inst.market_type == "crypto":
            from data.collectors.alpaca_collector import get_crypto_bars_weekly
            df_higher = get_crypto_bars_weekly(symbol)
        elif inst.market_type == "us_equity":
            from data.collectors.alpaca_collector import get_equity_bars_weekly
            df_higher = get_equity_bars_weekly(symbol)
        elif inst.market_type == "yfinance":
            from data.collectors.yfinance_collector import get_yfinance_bars_weekly
            df_higher = get_yfinance_bars_weekly(symbol)
        else:
            from data.collectors.massive_collector import get_global_equity_bars_weekly
            df_higher = get_global_equity_bars_weekly(symbol)
        if df_higher is not None:
            logger.debug("HTF weekly bars fetched for %s: %d rows", symbol, len(df_higher))
        else:
            logger.debug("HTF weekly bars unavailable for %s — MTF bias neutral", symbol)
    except Exception as htf_exc:
        logger.warning("HTF fetch failed for %s: %s — continuing without MTF bias", symbol, htf_exc)
        df_higher = None

    # ── 2. Technical analysis ─────────────────────────────────────
    tech = tech_compute(df, df_higher)
    if tech is None:
        logger.warning("Insufficient data for technical analysis: %s", symbol)
        return None

    current_price = float(df["Close"].iloc[-1])

    # ── 3. News / sentiment filter ────────────────────────────────
    news = news_analyse(symbol, technical_score=tech.score)

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

    alpaca_value       = alpaca.get("portfolio_value", 0)
    oanda_value        = oanda.get("nav", 0)
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

    now_iso = datetime.now(timezone.utc).isoformat()

    # ── Inception baseline (set once, never overwritten) ─────────────────
    if db.get_config("total_start_value") is None:
        db.set_config("total_start_value", str(total_value))
        db.set_config("total_start_date", now_iso)
        logger.info("Inception baseline set: %.2f on %s", total_value, now_iso)

    # ── Week-start baseline (reset every Monday) ──────────────────────────
    today_weekday = datetime.now(timezone.utc).weekday()  # 0 = Monday
    week_start_str = db.get_config("week_start_value")
    week_start_date = db.get_config("week_start_date")

    # Set on first run or on Monday if date has moved to new week
    need_new_week_baseline = week_start_str is None or (
        today_weekday == 0 and (
            week_start_date is None or
            week_start_date[:10] < datetime.now(timezone.utc).date().isoformat()
        )
    )
    if need_new_week_baseline:
        db.set_config("week_start_value", str(total_value))
        db.set_config("week_start_date", now_iso)
        logger.info("Week-start baseline set/reset: %.2f on %s", total_value, now_iso)

    week_start_value  = float(db.get_config("week_start_value") or total_value)
    weekly_return_pct = round(
        (total_value - week_start_value) / week_start_value * 100, 4
    ) if week_start_value > 0 else 0.0

    # Total return since inception
    inception_value = float(db.get_config("total_start_value") or total_value)
    total_return_pct = round(
        (total_value - inception_value) / inception_value * 100, 4
    ) if inception_value > 0 else 0.0

    db.insert_snapshot(
        total_value=total_value,
        cash=cash,
        open_positions=open_pos,
        daily_pnl=daily_pnl,
        total_pnl=round(total_value - inception_value, 2),
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
        "Portfolio snapshot: alpaca=%.2f oanda=%.2f paper_sim=%.2f total=%.2f "
        "week_return=%.3f%% total_return=%.3f%% drawdown=%.2f%%",
        alpaca_value, oanda_value, paper_sim_value, total_value,
        weekly_return_pct, total_return_pct, drawdown_pct,
    )


def run_position_monitor(db: DBManager) -> None:
    """Lightweight 30-minute position check — no watchlist scan."""
    from execution.position_monitor import run_position_check
    summary = run_position_check(db)
    logger.info("Position monitor: %s", summary)


def run_daily_summary(db: DBManager) -> None:
    from data.watchlist import get_active
    from notifications.email_notifier import send_daily_summary
    from analysis.cost_tracker import get_stats as get_cost_stats
    signals             = db.get_signals_today()
    closed              = db.get_closed_trades_today()
    snap                = db.latest_portfolio_snapshot()
    open_trades         = db.get_open_trades()
    instruments_scanned = len(get_active())
    api_costs           = get_cost_stats()
    send_daily_summary(
        signals, closed, snap, instruments_scanned,
        open_trades=open_trades, api_costs=api_costs,
    )
    logger.info("Daily summary email sent")


def run_weekly_summary(db: DBManager) -> None:
    from notifications.email_notifier import send_weekly_summary
    signals_week    = db.get_signals_this_week()
    closed_week     = db.get_closed_trades_this_week()
    snap            = db.latest_portfolio_snapshot()
    week_start_snap = db.get_snapshot_days_ago(7)

    # Pull inception and week baseline from agent_config
    inception_stats = {
        "total_start_value": float(db.get_config("total_start_value") or 0),
        "total_start_date":  db.get_config("total_start_date") or "unknown",
        "week_start_value":  float(db.get_config("week_start_value") or 0),
        "week_start_date":   db.get_config("week_start_date") or "unknown",
    }
    send_weekly_summary(signals_week, closed_week, snap, week_start_snap,
                        inception_stats=inception_stats)
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
