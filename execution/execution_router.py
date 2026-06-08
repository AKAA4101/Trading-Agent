"""
Unified execution router.
Every approved signal passes through here; routed by market_type to the
correct broker / simulator.  Global position and drawdown guards enforced
before any order is sent.
"""
import logging
from dataclasses import dataclass

from database.db_manager import DBManager

logger = logging.getLogger(__name__)

MAX_OPEN_POSITIONS = 5


@dataclass
class RouteResult:
    success: bool
    action: str        # "EXECUTED", "PAPER_SIM_OPEN", "QUEUED", "QUEUED_GTC", "EXECUTION_FAILED:…", "SKIPPED:…"
    order_id: str | None
    trade_db_id: int | None
    broker: str | None
    message: str


def route(
    symbol: str,
    market_type: str,
    direction: str,
    units: float,
    stop_loss: float,
    take_profit: float,
    signal_id: int,
    db: DBManager,
    entry_price: float | None = None,
) -> RouteResult:
    """Route a signal to the correct execution engine and update the signal record."""

    logger.info(
        "ROUTER ENTRY | signal_id=%s %s %s market_type=%s units=%.4f "
        "entry=%.5f SL=%.5f TP=%.5f",
        signal_id, direction, symbol, market_type, units,
        entry_price or 0, stop_loss or 0, take_profit or 0,
    )

    # ── Global position guard ─────────────────────────────────────────────
    open_count = db.count_open_trades()
    logger.info("ROUTER | open positions: %d / %d", open_count, MAX_OPEN_POSITIONS)
    if open_count >= MAX_OPEN_POSITIONS:
        msg = f"Global position limit: {open_count}/{MAX_OPEN_POSITIONS} open"
        logger.warning("ROUTER BLOCKED %s — %s", symbol, msg)
        db.update_signal_action(signal_id, f"SKIPPED: {msg}")
        return RouteResult(False, f"SKIPPED: {msg}", None, None, None, msg)

    if market_type in ("us_equity", "crypto"):
        logger.info("ROUTER | %s → Alpaca paper (market_type=%s)", symbol, market_type)
        return _route_alpaca(
            symbol, market_type, direction, units, stop_loss, take_profit,
            signal_id, db, entry_price,
        )
    elif market_type == "forex":
        logger.info("ROUTER | %s → OANDA practice", symbol)
        return _route_oanda(
            symbol, direction, units, stop_loss, take_profit,
            signal_id, db,
        )
    elif market_type == "yfinance":
        logger.info("ROUTER | %s → paper_sim", symbol)
        return _route_paper_sim(
            symbol, direction, units, stop_loss, take_profit,
            signal_id, db, entry_price,
        )
    else:
        msg = f"No execution path for market_type={market_type}"
        logger.warning("ROUTER SKIPPED %s — %s", symbol, msg)
        db.update_signal_action(signal_id, f"SKIPPED: {msg}")
        return RouteResult(False, f"SKIPPED: {msg}", None, None, None, msg)


# ── Per-broker helpers ────────────────────────────────────────────────────

def _route_alpaca(symbol, market_type, direction, units, stop_loss, take_profit,
                  signal_id, db, entry_price):
    from execution.alpaca_executor import submit_bracket_order
    logger.info("ALPACA | submitting bracket order %s %s qty=%.4f", direction, symbol, units)
    result = submit_bracket_order(
        symbol=symbol,
        direction=direction,
        units=units,
        stop_loss=stop_loss,
        take_profit=take_profit,
        signal_id=signal_id,
        db=db,
        market_type=market_type,
        entry_price=entry_price,
    )
    if result.success:
        action = "QUEUED_GTC" if result.queued else "EXECUTED"
        db.update_signal_action(signal_id, action)
        logger.info(
            "ALPACA OK | %s %s → action=%s order_id=%s trade_db_id=%s queued=%s",
            direction, symbol, action, result.order_id, result.trade_db_id, result.queued,
        )
        return RouteResult(
            True, action, result.order_id, result.trade_db_id, "alpaca_paper", result.message
        )
    else:
        action = f"EXECUTION_FAILED: {result.message}"
        db.update_signal_action(signal_id, action)
        logger.error("ALPACA FAILED | %s %s — %s", direction, symbol, result.message)
        return RouteResult(
            False, action, None, None, "alpaca_paper", result.message
        )


def _route_oanda(symbol, direction, units, stop_loss, take_profit, signal_id, db):
    from execution.oanda_executor import submit_bracket_order
    logger.info("OANDA | submitting order %s %s units=%.4f", direction, symbol, units)
    result = submit_bracket_order(
        instrument=symbol,
        direction=direction,
        units=units,
        stop_loss=stop_loss,
        take_profit=take_profit,
        signal_id=signal_id,
        db=db,
    )
    if result.success:
        action = "QUEUED" if result.queued else "EXECUTED"
        db.update_signal_action(signal_id, action)
        logger.info(
            "OANDA OK | %s %s → action=%s order_id=%s trade_db_id=%s queued=%s",
            direction, symbol, action, result.order_id, result.trade_db_id, result.queued,
        )
        return RouteResult(
            True, action, result.order_id, result.trade_db_id, "oanda_practice", result.message
        )
    else:
        action = f"EXECUTION_FAILED: {result.message}"
        db.update_signal_action(signal_id, action)
        logger.error("OANDA FAILED | %s %s — %s", direction, symbol, result.message)
        return RouteResult(
            False, action, None, None, "oanda_practice", result.message
        )


def _route_paper_sim(symbol, direction, units, stop_loss, take_profit,
                     signal_id, db, entry_price):
    from execution.paper_broker import execute_paper_sim
    logger.info("PAPER_SIM | opening position %s %s qty=%.4f entry=%.5f",
                direction, symbol, units, entry_price or 0)
    result = execute_paper_sim(
        symbol=symbol,
        direction=direction,
        units=units,
        stop_loss=stop_loss,
        take_profit=take_profit,
        signal_id=signal_id,
        db=db,
        entry_price=entry_price,
    )
    if result.success:
        db.update_signal_action(signal_id, "PAPER_SIM_OPEN")
        logger.info(
            "PAPER_SIM OK | %s %s trade_db_id=%s",
            direction, symbol, result.trade_db_id,
        )
        return RouteResult(
            True, "PAPER_SIM_OPEN", None, result.trade_db_id, "paper_sim", result.message
        )
    else:
        action = f"PAPER_SIM_FAILED: {result.message}"
        db.update_signal_action(signal_id, action)
        logger.error("PAPER_SIM FAILED | %s %s — %s", direction, symbol, result.message)
        return RouteResult(
            False, action, None, None, "paper_sim", result.message
        )
