"""
Paper trade execution via Alpaca (US equities + crypto).
Bracket orders: entry + stop-loss leg + take-profit leg.
Falls back to GTC limit bracket when market is closed.
"""
import logging
from dataclasses import dataclass, field

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest,
    LimitOrderRequest,
    TakeProfitRequest,
    StopLossRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce

from config import config
from database.db_manager import DBManager

logger = logging.getLogger(__name__)

PAPER_SIM_STARTING_VALUE = 100_000.0


@dataclass
class ExecutionResult:
    success: bool
    order_id: str | None
    message: str
    trade_db_id: int | None = None
    queued: bool = False


def _client() -> TradingClient:
    return TradingClient(
        api_key=config.ALPACA_API_KEY,
        secret_key=config.ALPACA_SECRET_KEY,
        paper=True,
    )


def _normalize_symbol(symbol: str, market_type: str) -> str:
    """Normalise to Alpaca format.
    US equities: BRK-B → BRK/B  (Alpaca uses slash for share classes)
    Crypto: BTCUSD → BTC/USD, BTC-USD → BTC/USD
    """
    sym = symbol.upper().strip()
    if market_type == "crypto":
        if "/" not in sym:
            for quote in ("USDT", "USD", "BTC", "ETH"):
                if sym.endswith(quote) and len(sym) > len(quote):
                    sym = sym[: -len(quote)] + "/" + quote
                    break
        return sym
    else:
        # Equity: dashes become slashes for class shares (BRK-B → BRK/B)
        if "-" in sym and "." not in sym:
            sym = sym.replace("-", "/")
        return sym


def _is_market_open() -> bool:
    """Return True if the US equity market is currently open (Alpaca clock)."""
    try:
        clock = _client().get_clock()
        logger.info(
            "Alpaca clock: is_open=%s next_open=%s next_close=%s",
            clock.is_open, clock.next_open, clock.next_close,
        )
        return bool(clock.is_open)
    except Exception as exc:
        logger.warning("Could not check Alpaca market clock: %s — assuming CLOSED", exc)
        return False


def submit_bracket_order(
    symbol: str,
    direction: str,
    units: float,
    stop_loss: float,
    take_profit: float,
    signal_id: int,
    db: DBManager,
    market_type: str = "us_equity",
    entry_price: float | None = None,
) -> ExecutionResult:
    """
    Submit a bracket order for a US equity or crypto position.
    If the market is closed, submits a GTC limit bracket at entry_price.
    direction: 'LONG' or 'SHORT'
    """
    if not config.PAPER_TRADING:
        return ExecutionResult(False, None, "LIVE trading not enabled — paper only")

    norm_symbol = _normalize_symbol(symbol, market_type)
    side = OrderSide.BUY if direction == "LONG" else OrderSide.SELL
    qty = round(units, 6)

    logger.info(
        "Alpaca submit attempt | %s %s %s qty=%.6f SL=%.5f TP=%.5f entry=%.5f",
        direction, norm_symbol, market_type, qty, stop_loss, take_profit,
        entry_price or 0,
    )

    # Crypto is 24/7; equities require market-hours check
    market_open = True if market_type == "crypto" else _is_market_open()
    queued = not market_open
    logger.info("Market open: %s  (queued=%s) for %s", market_open, queued, norm_symbol)

    try:
        client = _client()
        sl_req = StopLossRequest(stop_price=round(stop_loss, 4))
        tp_req = TakeProfitRequest(limit_price=round(take_profit, 4))

        if market_open or market_type == "crypto":
            order_req = MarketOrderRequest(
                symbol=norm_symbol,
                qty=qty,
                side=side,
                time_in_force=TimeInForce.DAY,
                order_class="bracket",
                take_profit=tp_req,
                stop_loss=sl_req,
            )
            logger.info("Submitting MARKET bracket order for %s", norm_symbol)
        else:
            # Market closed → GTC limit bracket (holds until next open, fills at limit price)
            limit_price = round(entry_price, 4) if entry_price else round(
                (stop_loss + take_profit) / 2, 4
            )
            order_req = LimitOrderRequest(
                symbol=norm_symbol,
                qty=qty,
                side=side,
                time_in_force=TimeInForce.GTC,
                limit_price=limit_price,
                order_class="bracket",
                take_profit=tp_req,
                stop_loss=sl_req,
            )
            logger.info(
                "Market CLOSED — submitting GTC LIMIT bracket for %s at %.4f",
                norm_symbol, limit_price,
            )

        order = client.submit_order(order_req)
        order_id = str(order.id)
        logger.info(
            "Alpaca order ACCEPTED | %s %s order_id=%s status=%s queued=%s",
            direction, norm_symbol, order_id, order.status, queued,
        )

        trade_id = db.insert_trade(
            signal_id=signal_id,
            instrument=norm_symbol,
            direction=direction,
            entry_price=entry_price,
            status="OPEN",
            broker="alpaca_paper",
            order_id=order_id,
            units=qty,
        )
        logger.info("Trade written to DB: trade_id=%d instrument=%s", trade_id, norm_symbol)

        if queued:
            db.insert_queued_signal(
                signal_id=signal_id,
                instrument=norm_symbol,
                market_type=market_type,
                direction=direction,
                entry_price=entry_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                units=qty,
                broker="alpaca_paper",
                status="SUBMITTED_GTC",
            )

        return ExecutionResult(True, order_id, "Order submitted", trade_id, queued=queued)

    except Exception as exc:
        logger.error(
            "Alpaca order FAILED | %s %s qty=%.4f SL=%.4f TP=%.4f | error: %s",
            direction, norm_symbol, qty, stop_loss, take_profit, exc,
        )
        return ExecutionResult(False, None, str(exc))


def close_position(symbol: str, db: DBManager, market_type: str = "us_equity") -> ExecutionResult:
    norm_symbol = _normalize_symbol(symbol, market_type)
    try:
        _client().close_position(norm_symbol)
        logger.info("Alpaca position closed: %s", norm_symbol)
        return ExecutionResult(True, None, f"Position closed: {norm_symbol}")
    except Exception as exc:
        logger.error("Failed to close Alpaca position %s: %s", norm_symbol, exc)
        return ExecutionResult(False, None, str(exc))


def get_account() -> dict:
    try:
        acct = _client().get_account()
        result = {
            "portfolio_value": float(acct.portfolio_value),
            "cash":            float(acct.cash),
            "equity":          float(acct.equity),
            "buying_power":    float(acct.buying_power),
        }
        logger.info(
            "Alpaca account: portfolio_value=%.2f cash=%.2f equity=%.2f",
            result["portfolio_value"], result["cash"], result["equity"],
        )
        return result
    except Exception as exc:
        logger.error("Failed to get Alpaca account: %s", exc)
        return {}
