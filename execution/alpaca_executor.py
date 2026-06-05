"""
Paper trade execution via Alpaca (US equities + crypto).
Uses bracket orders: entry + stop-loss leg + take-profit leg.
"""
import logging
from dataclasses import dataclass

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest,
    TakeProfitRequest,
    StopLossRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, OrderType

from config import config
from database.db_manager import DBManager

logger = logging.getLogger(__name__)


@dataclass
class ExecutionResult:
    success: bool
    order_id: str | None
    message: str
    trade_db_id: int | None = None


def _client() -> TradingClient:
    return TradingClient(
        api_key=config.ALPACA_API_KEY,
        secret_key=config.ALPACA_SECRET_KEY,
        paper=True,
    )


def submit_bracket_order(
    symbol: str,
    direction: str,
    units: float,
    stop_loss: float,
    take_profit: float,
    signal_id: int,
    db: DBManager,
) -> ExecutionResult:
    """
    Submit a bracket order (entry + SL + TP) for a US equity or crypto.
    direction: 'LONG' or 'SHORT'
    units: number of shares / crypto units (fractional supported)
    """
    if not config.PAPER_TRADING:
        return ExecutionResult(False, None, "LIVE trading not enabled — paper only")

    side = OrderSide.BUY if direction == "LONG" else OrderSide.SELL
    qty = round(units, 6)

    try:
        client = _client()

        # Alpaca bracket order via separate SL/TP legs
        order_req = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=side,
            time_in_force=TimeInForce.DAY,
            order_class="bracket",
            take_profit=TakeProfitRequest(limit_price=round(take_profit, 4)),
            stop_loss=StopLossRequest(stop_price=round(stop_loss, 4)),
        )
        order = client.submit_order(order_req)
        order_id = str(order.id)

        # Record in database
        trade_id = db.insert_trade(
            signal_id=signal_id,
            instrument=symbol,
            direction=direction,
            entry_price=None,  # filled async
            status="OPEN",
            broker="alpaca",
            order_id=order_id,
            units=qty,
        )

        logger.info("Alpaca bracket order submitted: %s %s %s x%.4f | order_id=%s",
                    direction, symbol, side.value, qty, order_id)
        return ExecutionResult(True, order_id, "Order submitted", trade_id)

    except Exception as exc:
        logger.error("Alpaca order failed for %s: %s", symbol, exc)
        return ExecutionResult(False, None, str(exc))


def close_position(symbol: str, db: DBManager) -> ExecutionResult:
    """Close an existing Alpaca position."""
    try:
        client = _client()
        client.close_position(symbol)
        logger.info("Alpaca position closed: %s", symbol)
        return ExecutionResult(True, None, f"Position closed: {symbol}")
    except Exception as exc:
        logger.error("Failed to close Alpaca position for %s: %s", symbol, exc)
        return ExecutionResult(False, None, str(exc))


def get_account() -> dict:
    try:
        client = _client()
        acct = client.get_account()
        return {
            "portfolio_value": float(acct.portfolio_value),
            "cash": float(acct.cash),
            "equity": float(acct.equity),
            "buying_power": float(acct.buying_power),
        }
    except Exception as exc:
        logger.error("Failed to get Alpaca account: %s", exc)
        return {}
