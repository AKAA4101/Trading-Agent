"""
Paper trade execution via OANDA practice environment (forex).
Market orders with attached stop-loss and take-profit.
Queues signals on weekends when OANDA is closed.
"""
import logging
import json
from dataclasses import dataclass
from datetime import datetime, timezone

import oandapyV20
import oandapyV20.endpoints.orders as orders
import oandapyV20.endpoints.positions as positions
import oandapyV20.endpoints.accounts as accounts_ep

from config import config
from database.db_manager import DBManager

logger = logging.getLogger(__name__)

MIN_UNITS = 1_000   # floor for paper trades


@dataclass
class ExecutionResult:
    success: bool
    order_id: str | None
    message: str
    trade_db_id: int | None = None
    queued: bool = False


def _client() -> oandapyV20.API:
    return oandapyV20.API(access_token=config.OANDA_API_TOKEN, environment="practice")


def _normalize_instrument(symbol: str) -> str:
    """Convert EUR/USD → EUR_USD (OANDA format)."""
    return symbol.replace("/", "_").upper()


def _is_forex_open() -> bool:
    """Forex is open Mon 00:00 – Fri 22:00 UTC; closed weekends."""
    now = datetime.now(timezone.utc)
    # weekday(): Monday=0 … Sunday=6
    if now.weekday() == 5:  # Saturday all day
        return False
    if now.weekday() == 6:  # Sunday all day
        return False
    # Friday after 22:00 UTC
    if now.weekday() == 4 and now.hour >= 22:
        return False
    return True


def _units_str(units_float: float, direction: str) -> str:
    """OANDA requires unit string; negative = short. Enforce minimum."""
    u = max(MIN_UNITS, int(round(abs(units_float))))
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
    Submit a market order with SL and TP to OANDA practice account.
    Queues to DB if forex market is currently closed (weekends).
    """
    if not config.PAPER_TRADING:
        return ExecutionResult(False, None, "LIVE trading not enabled — paper only")

    norm_instrument = _normalize_instrument(instrument)
    unit_str = _units_str(units, direction)

    logger.info(
        "OANDA submit attempt | %s %s units=%s SL=%.5f TP=%.5f",
        direction, norm_instrument, unit_str, stop_loss, take_profit,
    )

    forex_open = _is_forex_open()
    logger.info("Forex market open: %s for %s", forex_open, norm_instrument)

    if not forex_open:
        # Queue for Monday open
        queued_id = db.insert_queued_signal(
            signal_id=signal_id,
            instrument=norm_instrument,
            market_type="forex",
            direction=direction,
            stop_loss=stop_loss,
            take_profit=take_profit,
            units=abs(units),
            broker="oanda_practice",
        )
        msg = "Forex market closed (weekend) — signal queued"
        logger.info("OANDA queued signal id=%d: %s %s", queued_id, direction, norm_instrument)
        return ExecutionResult(True, None, msg, None, queued=True)

    try:
        client = _client()

        order_body = {
            "order": {
                "type": "MARKET",
                "instrument": norm_instrument,
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

        logger.info(
            "OANDA order body: %s",
            json.dumps(order_body, separators=(",", ":")),
        )

        req = orders.OrderCreate(accountID=config.OANDA_ACCOUNT_ID, data=order_body)
        client.request(req)
        response = req.response

        logger.info("OANDA raw response: %s", json.dumps(response, default=str))

        order_id = (
            response.get("orderFillTransaction", {}).get("id")
            or response.get("relatedTransactionIDs", ["unknown"])[0]
            or "unknown"
        )
        fill_price_str = (
            response.get("orderFillTransaction", {}).get("price")
            or response.get("orderFillTransaction", {}).get("tradeOpened", {}).get("price")
        )
        fill_price = float(fill_price_str) if fill_price_str else None

        trade_id = db.insert_trade(
            signal_id=signal_id,
            instrument=norm_instrument,
            direction=direction,
            entry_price=fill_price,
            status="OPEN",
            broker="oanda_practice",
            order_id=str(order_id),
            units=abs(units),
        )
        logger.info(
            "OANDA order ACCEPTED | %s %s units=%s order_id=%s fill_price=%s trade_db_id=%d",
            direction, norm_instrument, unit_str, order_id, fill_price, trade_id,
        )
        return ExecutionResult(True, str(order_id), "Order submitted", trade_id)

    except Exception as exc:
        logger.error(
            "OANDA order FAILED | %s %s units=%s SL=%.5f TP=%.5f | error: %s",
            direction, norm_instrument, unit_str, stop_loss, take_profit, exc,
        )
        return ExecutionResult(False, None, str(exc))


def execute_queued_signals(db: DBManager) -> int:
    """Execute any pending queued forex signals when market is open. Returns count executed."""
    if not _is_forex_open():
        return 0
    pending = db.get_pending_queued_signals(broker="oanda_practice")
    executed = 0
    for q in pending:
        logger.info("Executing queued OANDA signal: %s %s", q["direction"], q["instrument"])
        result = submit_bracket_order(
            instrument=q["instrument"],
            direction=q["direction"],
            units=q.get("units", MIN_UNITS),
            stop_loss=q["stop_loss"],
            take_profit=q["take_profit"],
            signal_id=q["signal_id"],
            db=db,
        )
        if result.success and not result.queued:
            db.close_queued_signal(q["id"], "EXECUTED")
            db.update_signal_action(q["signal_id"], "EXECUTED")
            executed += 1
        elif not result.success:
            db.close_queued_signal(q["id"], f"FAILED: {result.message}")
    return executed


def close_position(instrument: str) -> ExecutionResult:
    """Close all units of an OANDA position."""
    norm_instrument = _normalize_instrument(instrument)
    try:
        client = _client()
        data = {"longUnits": "ALL", "shortUnits": "ALL"}
        req = positions.PositionClose(
            accountID=config.OANDA_ACCOUNT_ID,
            instrument=norm_instrument,
            data=data,
        )
        client.request(req)
        logger.info("OANDA position closed: %s", norm_instrument)
        return ExecutionResult(True, None, f"Position closed: {norm_instrument}")
    except Exception as exc:
        logger.error("Failed to close OANDA position %s: %s", norm_instrument, exc)
        return ExecutionResult(False, None, str(exc))


def get_account() -> dict:
    try:
        client = _client()
        req = accounts_ep.AccountSummary(accountID=config.OANDA_ACCOUNT_ID)
        client.request(req)
        acct = req.response.get("account", {})
        result = {
            "balance":          float(acct.get("balance", 0)),
            "unrealised_pnl":   float(acct.get("unrealizedPL", 0)),
            "nav":              float(acct.get("NAV", 0)),
            "open_trade_count": int(acct.get("openTradeCount", 0)),
        }
        logger.info(
            "OANDA account: balance=%.2f NAV=%.2f open_trades=%d",
            result["balance"], result["nav"], result["open_trade_count"],
        )
        return result
    except Exception as exc:
        logger.error("Failed to get OANDA account: %s", exc)
        return {}
