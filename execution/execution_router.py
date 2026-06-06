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

    # ── Global position guard ─────────────────────────────────────────────
    open_count = db.count_open_trades()
    if open_count >= MAX_OPEN_POSITIONS:
        msg = f"Global position limit: {open_count}/{MAX_OPEN_POSITIONS} open"
        logger.warning("ROUTE BLOCKED %s — %s", symbol, msg)
        db.update_signal_action(signal_id, f"SKIPPED: {msg}")
        return RouteResult(False, f"SKIPPED: {msg}", None, None, None, msg)

    logger.info(
        "Routing | %s %s market_type=%s units=%.4f SL=%.5f TP=%.5f",
        direction, symbol, market_type, units, stop_loss, take_profit,
    )

    if market_type in ("us_equity", "crypto"):
        return _route_alpaca(
            symbol, market_type, direction, units, stop_loss, take_profit,
            signal_id, db, entry_price,
        )
    elif market_type == "forex":
        return _route_oanda(
            symbol, direction, units, stop_loss, take_profit,
            signal_id, db,
        )
    elif market_type == "yfinance":
        return _route_paper_sim(
            symbol, direction, units, stop_loss, take_profit,
            signal_id, db, entry_price,
        )
    else:
        msg = f"No execution path for market_type={market_type}"
        logger.warning("ROUTE SKIPPED %s — %s", symbol, msg)
        db.update_signal_action(signal_id, f"SKIPPED: {msg}")
        return RouteResult(False, f"SKIPPED: {msg}", None, None, None, msg)


# ── Per-broker helpers ────────────────────────────────────────────────────

def _route_alpaca(symbol, market_type, direction, units, stop_loss, take_profit,
                  signal_id, db, entry_price):
    from execution.alpaca_executor import submit_bracket_order
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
        logger.info("Alpaca route OK: %s %s → %s", direction, symbol, action)
        return RouteResult(
            True, action, result.order_id, result.trade_db_id, "alpaca_paper", result.message
        )
    else:
        action = f"EXECUTION_FAILED: {result.message}"
        db.update_signal_action(signal_id, action)
        logger.error("Alpaca route FAILED: %s %s — %s", direction, symbol, result.message)
        return RouteResult(
            False, action, None, None, "alpaca_paper", result.message
        )


def _route_oanda(symbol, direction, units, stop_loss, take_profit, signal_id, db):
    from execution.oanda_executor import submit_bracket_order
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
        logger.info("OANDA route OK: %s %s → %s", direction, symbol, action)
        return RouteResult(
            True, action, result.order_id, result.trade_db_id, "oanda_practice", result.message
        )
    else:
        action = f"EXECUTION_FAILED: {result.message}"
        db.update_signal_action(signal_id, action)
        logger.error("OANDA route FAILED: %s %s — %s", direction, symbol, result.message)
        return RouteResult(
            False, action, None, None, "oanda_practice", result.message
        )


def _route_paper_sim(symbol, direction, units, stop_loss, take_profit,
                     signal_id, db, entry_price):
    from execution.paper_broker import execute_paper_sim
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
        logger.info("paper_sim route OK: %s %s trade_id=%s", direction, symbol, result.trade_db_id)
        return RouteResult(
            True, "PAPER_SIM_OPEN", None, result.trade_db_id, "paper_sim", result.message
        )
    else:
        action = f"PAPER_SIM_FAILED: {result.message}"
        db.update_signal_action(signal_id, action)
        logger.error("paper_sim route FAILED: %s %s — %s", direction, symbol, result.message)
        return RouteResult(
            False, action, None, None, "paper_sim", result.message
        )
