"""
Paper trade execution via OANDA practice environment (forex).
Uses market orders with attached stop-loss and take-profit.
Units are OANDA-native (e.g. 10000 for 1 standard lot of EUR_USD).
"""
import logging
import json
from dataclasses import dataclass

import oandapyV20
import oandapyV20.endpoints.orders as orders
import oandapyV20.endpoints.positions as positions
import oandapyV20.endpoints.accounts as accounts_ep

from config import config
from database.db_manager import DBManager

logger = logging.getLogger(__name__)


@dataclass
class ExecutionResult:
    success: bool
    order_id: str | None
    message: str
    trade_db_id: int | None = None


def _client() -> oandapyV20.API:
    return oandapyV20.API(access_token=config.OANDA_API_TOKEN, environment="practice")


def _units_for_pair(units_float: float, direction: str) -> str:
    """OANDA requires unit string; negative = short."""
    u = int(round(units_float))
    return str(u) if direction == "LONG" else str(-u)


def submit_bracket_order(
    instrument: str,
    direction: str,
    units: float,
    stop_loss: float,
    take_profit: float,
    signal_id: int,
    db: DBManager,
) -> ExecutionResult:
    """
    Submit a MARKET order with attached SL and TP to OANDA practice account.
    instrument: OANDA format, e.g. EUR_USD
    units: position size in base currency units
    """
    if not config.PAPER_TRADING:
        return ExecutionResult(False, None, "LIVE trading not enabled — paper only")

    try:
        client = _client()
        unit_str = _units_for_pair(units, direction)

        order_body = {
            "order": {
                "type": "MARKET",
                "instrument": instrument,
                "units": unit_str,
                "timeInForce": "FOK",
                "takeProfitOnFill": {
                    "price": f"{take_profit:.5f}",
                },
                "stopLossOnFill": {
                    "price": f"{stop_loss:.5f}",
                },
            }
        }

        req = orders.OrderCreate(accountID=config.OANDA_ACCOUNT_ID, data=order_body)
        client.request(req)
        response = req.response

        order_id = (
            response.get("orderFillTransaction", {}).get("id")
            or response.get("orderCreateTransaction", {}).get("id")
            or "unknown"
        )

        # Record in database
        trade_id = db.insert_trade(
            signal_id=signal_id,
            instrument=instrument,
            direction=direction,
            entry_price=None,
            status="OPEN",
            broker="oanda",
            order_id=str(order_id),
            units=units,
        )

        logger.info("OANDA order submitted: %s %s units=%s | order_id=%s",
                    direction, instrument, unit_str, order_id)
        return ExecutionResult(True, str(order_id), "Order submitted", trade_id)

    except Exception as exc:
        logger.error("OANDA order failed for %s: %s", instrument, exc)
        return ExecutionResult(False, None, str(exc))


def close_position(instrument: str) -> ExecutionResult:
    """Close all units of an OANDA position."""
    try:
        client = _client()
        data = {"longUnits": "ALL", "shortUnits": "ALL"}
        req = positions.PositionClose(
            accountID=config.OANDA_ACCOUNT_ID,
            instrument=instrument,
            data=data,
        )
        client.request(req)
        logger.info("OANDA position closed: %s", instrument)
        return ExecutionResult(True, None, f"Position closed: {instrument}")
    except Exception as exc:
        logger.error("Failed to close OANDA position %s: %s", instrument, exc)
        return ExecutionResult(False, None, str(exc))


def get_account() -> dict:
    try:
        client = _client()
        req = accounts_ep.AccountSummary(accountID=config.OANDA_ACCOUNT_ID)
        client.request(req)
        acct = req.response.get("account", {})
        return {
            "balance": float(acct.get("balance", 0)),
            "unrealised_pnl": float(acct.get("unrealizedPL", 0)),
            "nav": float(acct.get("NAV", 0)),
            "open_trade_count": int(acct.get("openTradeCount", 0)),
        }
    except Exception as exc:
        logger.error("Failed to get OANDA account: %s", exc)
        return {}
