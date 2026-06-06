"""
Simulated paper broker for international equities (yfinance market type).
Executes trades at the current EOD price and monitors open positions
for stop-loss and take-profit hits each analysis cycle.
"""
import logging
from dataclasses import dataclass

from database.db_manager import DBManager

logger = logging.getLogger(__name__)

PAPER_SIM_STARTING_VALUE = 100_000.0
BROKER_TAG = "paper_sim"


@dataclass
class ExecutionResult:
    success: bool
    order_id: str | None
    message: str
    trade_db_id: int | None = None


def execute_paper_sim(
    symbol: str,
    direction: str,
    units: float,
    stop_loss: float,
    take_profit: float,
    signal_id: int,
    db: DBManager,
    entry_price: float | None = None,
) -> ExecutionResult:
    """
    Simulate opening a position for an international equity.
    Records trade in DB with broker='paper_sim' and status='OPEN'.
    """
    if entry_price is None or entry_price <= 0:
        return ExecutionResult(False, None, "entry_price missing or zero")

    qty = round(units, 6)
    logger.info(
        "paper_sim OPEN | %s %s qty=%.4f entry=%.5f SL=%.5f TP=%.5f",
        direction, symbol, qty, entry_price, stop_loss, take_profit,
    )

    try:
        trade_id = db.insert_trade(
            signal_id=signal_id,
            instrument=symbol,
            direction=direction,
            entry_price=entry_price,
            status="OPEN",
            broker=BROKER_TAG,
            order_id=None,
            units=qty,
            stop_loss=stop_loss,
            take_profit=take_profit,
        )
        logger.info(
            "paper_sim trade recorded | trade_id=%d %s %s entry=%.5f",
            trade_id, direction, symbol, entry_price,
        )
        return ExecutionResult(True, None, "Paper sim trade opened", trade_id)
    except Exception as exc:
        logger.error("paper_sim insert failed for %s: %s", symbol, exc)
        return ExecutionResult(False, None, str(exc))


def manage_positions(db: DBManager) -> dict:
    """
    Check all open paper_sim positions against their stop-loss and take-profit.
    Fetches latest EOD price from yfinance and closes trades that hit their levels.
    Returns summary dict.
    """
    from data.collectors.yfinance_collector import get_latest_price

    open_trades = db.get_open_paper_sim_trades()
    if not open_trades:
        logger.info("paper_sim position management: no open positions")
        return {"checked": 0, "closed_sl": 0, "closed_tp": 0, "errors": 0}

    logger.info("paper_sim position management: checking %d open positions", len(open_trades))
    closed_sl = closed_tp = errors = 0

    for trade in open_trades:
        symbol    = trade["instrument"]
        direction = trade["direction"]
        entry     = float(trade.get("entry_price") or 0)
        stop_loss   = float(trade.get("stop_loss") or 0)
        take_profit = float(trade.get("take_profit") or 0)
        units       = float(trade.get("units") or 0)
        # SQLite rows have 'id'; Supabase rows have 'local_id' as the SQLite pk
        trade_id    = trade.get("local_id") or trade.get("id")

        if not stop_loss or not take_profit or not entry:
            logger.warning("paper_sim trade %s missing levels — skipping", trade_id)
            continue

        current_price = get_latest_price(symbol)
        if current_price is None:
            logger.warning("paper_sim: no price data for %s — skipping", symbol)
            errors += 1
            continue

        logger.debug(
            "paper_sim check | %s %s entry=%.5f current=%.5f SL=%.5f TP=%.5f",
            direction, symbol, entry, current_price, stop_loss, take_profit,
        )

        hit_sl = hit_tp = False
        if direction == "LONG":
            hit_sl = current_price <= stop_loss
            hit_tp = current_price >= take_profit
        else:  # SHORT
            hit_sl = current_price >= stop_loss
            hit_tp = current_price <= take_profit

        if hit_tp or hit_sl:
            exit_price = take_profit if hit_tp else stop_loss
            if direction == "LONG":
                pnl = (exit_price - entry) * units
            else:
                pnl = (entry - exit_price) * units
            pnl_pct = ((exit_price - entry) / entry * 100) if direction == "LONG" else \
                      ((entry - exit_price) / entry * 100)
            reason = "TP" if hit_tp else "SL"

            db.close_trade(trade_id, exit_price, round(pnl, 4), round(pnl_pct, 4))
            logger.info(
                "paper_sim CLOSED | %s %s exit=%.5f pnl=%.2f (%.2f%%) reason=%s",
                direction, symbol, exit_price, pnl, pnl_pct, reason,
            )
            if hit_tp:
                closed_tp += 1
            else:
                closed_sl += 1
        else:
            unrealized = (current_price - entry) * units if direction == "LONG" else \
                         (entry - current_price) * units
            logger.debug(
                "paper_sim HOLD | %s %s unrealized=%.2f current=%.5f",
                direction, symbol, unrealized, current_price,
            )

    summary = {"checked": len(open_trades), "closed_sl": closed_sl,
               "closed_tp": closed_tp, "errors": errors}
    logger.info("paper_sim position management done: %s", summary)
    return summary


def get_portfolio_value() -> float:
    """
    Compute paper_sim portfolio value.
    = starting_balance + realized P&L (open positions tracked separately).
    """
    try:
        from database.db_manager import DBManager as _DBManager
        from config import config
        db = _DBManager(config.DB_PATH)
        realized = db.get_paper_sim_realized_pnl()
        value = PAPER_SIM_STARTING_VALUE + realized
        logger.info("paper_sim portfolio value: %.2f (realized_pnl=%.2f)", value, realized)
        return value
    except Exception as exc:
        logger.error("Failed to compute paper_sim portfolio value: %s", exc)
        return PAPER_SIM_STARTING_VALUE
