"""
Lightweight position monitor — runs every 30 minutes.
Checks open paper_sim, Alpaca, and OANDA trades for stop-loss / take-profit hits.
Does NOT run a full analysis cycle or touch the watchlist.
"""
import logging
from datetime import datetime, timezone

import yfinance as yf

from database.db_manager import DBManager

logger = logging.getLogger(__name__)


# ── Price fetching ────────────────────────────────────────────────────────────

def _fetch_price(symbol: str) -> tuple[float | None, str]:
    """
    Fetch current price via yfinance fast_info.
    Returns (price, source_note) where source_note explains data age.
    fast_info.last_price = real-time during hours, last close when market closed.
    """
    try:
        ticker = yf.Ticker(symbol)
        fi = ticker.fast_info

        price = None
        # fast_info attributes vary by exchange; try both forms
        for attr in ("last_price", "lastPrice", "regularMarketPrice"):
            try:
                val = getattr(fi, attr, None)
                if val and float(val) > 0:
                    price = float(val)
                    break
            except Exception:
                continue

        if price is None or price <= 0:
            # Fallback: last row of daily history
            hist = ticker.history(period="2d", interval="1d", auto_adjust=True)
            if not hist.empty:
                price = float(hist["Close"].iloc[-1])
                return price, "daily_history_fallback"
            return None, "no_data"

        # Try to get timestamp for staleness warning
        try:
            ts = getattr(fi, "regular_market_time", None)
            if ts:
                age_hours = (datetime.now(timezone.utc).timestamp() - float(ts)) / 3600
                note = f"age={age_hours:.1f}h"
                if age_hours > 24:
                    logger.warning(
                        "Stale price for %s: %.5f (%.1f hours old)", symbol, price, age_hours
                    )
                return price, note
        except Exception:
            pass

        return price, "fast_info"

    except Exception as exc:
        logger.error("Price fetch failed for %s: %s", symbol, exc)
        return None, f"error: {exc}"


# ── paper_sim position check ──────────────────────────────────────────────────

def _check_paper_sim(db: DBManager) -> dict:
    """Check all OPEN paper_sim trades for SL/TP hits. Returns summary."""
    from notifications.email_notifier import send_position_closed

    trades = db.get_open_paper_sim_trades()
    if not trades:
        return {"checked": 0, "closed_sl": 0, "closed_tp": 0, "updated": 0, "errors": 0}

    logger.info("paper_sim monitor: checking %d open positions", len(trades))
    closed_sl = closed_tp = updated = errors = 0

    for trade in trades:
        symbol      = trade["instrument"]
        direction   = trade["direction"]
        entry       = float(trade.get("entry_price") or 0)
        stop_loss   = float(trade.get("stop_loss") or 0)
        take_profit = float(trade.get("take_profit") or 0)
        units       = float(trade.get("units") or 0)
        trade_id    = trade.get("local_id") or trade.get("id")

        if not entry or not stop_loss or not take_profit:
            logger.warning("paper_sim trade %s missing levels — skipping", trade_id)
            continue

        price, source = _fetch_price(symbol)
        if price is None:
            logger.warning("No price for %s (trade %s) — skipping", symbol, trade_id)
            errors += 1
            continue

        logger.info(
            "paper_sim | %s %s entry=%.5f current=%.5f SL=%.5f TP=%.5f [%s]",
            direction, symbol, entry, price, stop_loss, take_profit, source,
        )

        hit_sl = (direction == "LONG" and price <= stop_loss) or \
                 (direction == "SHORT" and price >= stop_loss)
        hit_tp = (direction == "LONG" and price >= take_profit) or \
                 (direction == "SHORT" and price <= take_profit)

        if hit_tp or hit_sl:
            exit_price  = take_profit if hit_tp else stop_loss
            exit_reason = "TAKE_PROFIT" if hit_tp else "STOP_LOSS"

            if direction == "LONG":
                pnl     = (exit_price - entry) * units
                pnl_pct = (exit_price - entry) / entry * 100
            else:
                pnl     = (entry - exit_price) * units
                pnl_pct = (entry - exit_price) / entry * 100

            db.close_trade(
                trade_id, round(exit_price, 6),
                round(pnl, 4), round(pnl_pct, 4),
                exit_reason=exit_reason,
            )

            closed_trade = {**trade, "exit_price": exit_price, "pnl": pnl,
                            "pnl_pct": pnl_pct, "exit_reason": exit_reason}
            send_position_closed(closed_trade, exit_reason)

            logger.info(
                "paper_sim CLOSED | %s %s exit=%.5f pnl=%.2f (%.2f%%) reason=%s",
                direction, symbol, exit_price, pnl, pnl_pct, exit_reason,
            )
            if hit_tp:
                closed_tp += 1
            else:
                closed_sl += 1
        else:
            if direction == "LONG":
                unrealised_pct = (price - entry) / entry * 100
            else:
                unrealised_pct = (entry - price) / entry * 100

            db.update_trade_live(trade_id, round(price, 6), round(unrealised_pct, 4))
            updated += 1
            logger.debug(
                "paper_sim HOLD | %s %s unrealised=%.2f%% current=%.5f",
                direction, symbol, unrealised_pct, price,
            )

    return {"checked": len(trades), "closed_sl": closed_sl,
            "closed_tp": closed_tp, "updated": updated, "errors": errors}


# ── Alpaca position check ─────────────────────────────────────────────────────

def _check_alpaca(db: DBManager) -> dict:
    """
    Reconcile our OPEN Alpaca trades against live Alpaca positions.
    If a trade no longer appears as an open position in Alpaca, it was
    closed by a native SL/TP — update our DB to reflect that.
    """
    from notifications.email_notifier import send_position_closed

    db_trades = [t for t in db.get_open_trades() if t.get("broker") == "alpaca_paper"]
    if not db_trades:
        return {"checked": 0, "reconciled": 0, "errors": 0}

    logger.info("Alpaca monitor: checking %d open trades against Alpaca positions", len(db_trades))

    try:
        from execution.alpaca_executor import _client, _normalize_symbol
        client = _client()
        alpaca_positions = {p.symbol: p for p in client.get_all_positions()}
    except Exception as exc:
        logger.error("Failed to fetch Alpaca positions: %s", exc)
        return {"checked": len(db_trades), "reconciled": 0, "errors": 1}

    logger.info("Alpaca open positions: %s", list(alpaca_positions.keys()))
    reconciled = errors = 0

    for trade in db_trades:
        raw_symbol = trade["instrument"]
        trade_id   = trade.get("local_id") or trade.get("id")
        direction  = trade["direction"]
        entry      = float(trade.get("entry_price") or 0)
        units      = float(trade.get("units") or 0)

        if raw_symbol not in alpaca_positions:
            # Position gone from Alpaca — was closed by SL/TP or manually
            price, source = _fetch_price(raw_symbol)
            if price is None or not entry:
                logger.warning(
                    "Alpaca trade %s closed but can't determine price — skipping", raw_symbol
                )
                errors += 1
                continue

            if direction == "LONG":
                pnl     = (price - entry) * units
                pnl_pct = (price - entry) / entry * 100
            else:
                pnl     = (entry - price) * units
                pnl_pct = (entry - price) / entry * 100

            # Determine exit_reason from SL/TP levels if available
            stop_loss   = float(trade.get("stop_loss") or 0)
            take_profit = float(trade.get("take_profit") or 0)
            if take_profit and price >= take_profit:
                exit_reason = "TAKE_PROFIT"
            elif stop_loss and (
                (direction == "LONG" and price <= stop_loss) or
                (direction == "SHORT" and price >= stop_loss)
            ):
                exit_reason = "STOP_LOSS"
            else:
                exit_reason = "ALPACA_CLOSED"

            db.close_trade(
                trade_id, round(price, 6),
                round(pnl, 4), round(pnl_pct, 4),
                exit_reason=exit_reason,
            )

            closed_trade = {**trade, "exit_price": price, "pnl": pnl,
                            "pnl_pct": pnl_pct, "exit_reason": exit_reason}
            send_position_closed(closed_trade, exit_reason)

            logger.info(
                "Alpaca RECONCILED | %s %s closed=%s exit=%.5f pnl=%.2f%%",
                direction, raw_symbol, exit_reason, price, pnl_pct,
            )
            reconciled += 1
        else:
            # Still open — update current price and unrealised P&L
            pos = alpaca_positions[raw_symbol]
            try:
                unrealised_pct = float(pos.unrealized_plpc) * 100
                current_price = float(pos.current_price)
                db.update_trade_live(trade_id, round(current_price, 6), round(unrealised_pct, 4))
                logger.debug(
                    "Alpaca HOLD | %s current=%.5f unrealised=%.2f%%",
                    raw_symbol, current_price, unrealised_pct,
                )
            except Exception as exc:
                logger.warning("Failed to update Alpaca live price for %s: %s", raw_symbol, exc)

    return {"checked": len(db_trades), "reconciled": reconciled, "errors": errors}


# ── OANDA position check ──────────────────────────────────────────────────────

def _fetch_oanda_prices(instruments: list[str]) -> dict[str, float]:
    """Fetch mid prices for a list of OANDA instruments. Returns {instrument: mid_price}."""
    if not instruments:
        return {}
    try:
        import oandapyV20
        import oandapyV20.endpoints.pricing as pricing_ep
        from config import config
        client = oandapyV20.API(access_token=config.OANDA_API_TOKEN, environment="practice")
        req = pricing_ep.PricingInfo(
            accountID=config.OANDA_ACCOUNT_ID,
            params={"instruments": ",".join(instruments)},
        )
        client.request(req)
        result = {}
        for p in req.response.get("prices", []):
            instr = p["instrument"]
            best_bid = float(p["bids"][0]["price"]) if p.get("bids") else None
            best_ask = float(p["asks"][0]["price"]) if p.get("asks") else None
            if best_bid and best_ask:
                result[instr] = round((best_bid + best_ask) / 2, 6)
        return result
    except Exception as exc:
        logger.error("OANDA pricing fetch failed: %s", exc)
        return {}


def _check_oanda(db: DBManager) -> dict:
    """
    Reconcile open oanda_practice trades against live OANDA positions.
    Updates current_price and unrealised_pnl every cycle.
    If a trade is no longer open in OANDA (closed by SL/TP), close it in the DB.
    """
    from notifications.email_notifier import send_position_closed

    db_trades = [t for t in db.get_open_trades() if t.get("broker") == "oanda_practice"]
    if not db_trades:
        return {"checked": 0, "updated": 0, "reconciled": 0, "errors": 0}

    logger.info("OANDA monitor: checking %d open trades", len(db_trades))

    try:
        import oandapyV20
        import oandapyV20.endpoints.trades as trades_ep
        import oandapyV20.endpoints.transactions as txn_ep
        from config import config
        client = oandapyV20.API(access_token=config.OANDA_API_TOKEN, environment="practice")
        req = trades_ep.TradesList(accountID=config.OANDA_ACCOUNT_ID)
        client.request(req)
        oanda_open = {t["id"]: t for t in req.response.get("trades", [])}
    except Exception as exc:
        logger.error("Failed to fetch OANDA open trades: %s", exc)
        return {"checked": len(db_trades), "updated": 0, "reconciled": 0, "errors": 1}

    # Fetch current mid prices for all instruments
    instruments = list({t["instrument"] for t in db_trades})
    current_prices = _fetch_oanda_prices(instruments)

    updated = reconciled = errors = 0

    for trade in db_trades:
        trade_id   = trade.get("local_id") or trade.get("id")
        oanda_id   = str(trade.get("order_id", ""))
        instrument = trade["instrument"]
        direction  = trade["direction"]
        entry      = float(trade.get("entry_price") or 0)
        units      = float(trade.get("units") or 0)

        if oanda_id in oanda_open:
            # Still open — update current price from pricing API
            price = current_prices.get(instrument)
            if price is None:
                logger.warning("No OANDA price for %s — skipping update", instrument)
                errors += 1
                continue

            if direction == "LONG":
                unrealised_pct = (price - entry) / entry * 100 if entry else 0
            else:
                unrealised_pct = (entry - price) / entry * 100 if entry else 0

            oanda_trade = oanda_open[oanda_id]
            logger.info(
                "OANDA HOLD | %s %s entry=%.5f current=%.5f unrealised=%.4f%% "
                "OANDA_unr=%.4f SL=%s TP=%s",
                direction, instrument, entry, price, unrealised_pct,
                float(oanda_trade.get("unrealizedPL", 0)),
                trade.get("stop_loss"), trade.get("take_profit"),
            )
            db.update_trade_live(trade_id, round(price, 6), round(unrealised_pct, 4))
            updated += 1

        else:
            # Trade no longer in OANDA open list — was closed by SL/TP or manually
            # Fetch trade details to get closing price
            close_price = None
            exit_reason = "OANDA_CLOSED"
            try:
                req_detail = trades_ep.TradeDetails(
                    accountID=config.OANDA_ACCOUNT_ID,
                    tradeID=oanda_id,
                )
                client.request(req_detail)
                detail = req_detail.response.get("trade", {})
                close_txn = detail.get("closingTransactionIDs", [])
                # averageClosePrice is set on the trade when fully closed
                avg_close = detail.get("averageClosePrice")
                if avg_close:
                    close_price = float(avg_close)
                # Infer exit reason from which order closed it
                sl_order = trade.get("stop_loss")
                tp_order = trade.get("take_profit")
                if close_price and sl_order and tp_order:
                    sl = float(sl_order)
                    tp = float(tp_order)
                    if direction == "LONG":
                        exit_reason = "TAKE_PROFIT" if close_price >= tp else "STOP_LOSS"
                    else:
                        exit_reason = "TAKE_PROFIT" if close_price <= tp else "STOP_LOSS"
            except Exception as exc:
                logger.warning("Could not fetch closed OANDA trade %s details: %s", oanda_id, exc)

            if close_price is None:
                close_price = current_prices.get(instrument)
            if close_price is None or not entry:
                logger.warning(
                    "OANDA trade %s closed but can't determine price — skipping", trade_id
                )
                errors += 1
                continue

            if direction == "LONG":
                pnl     = (close_price - entry) * units
                pnl_pct = (close_price - entry) / entry * 100
            else:
                pnl     = (entry - close_price) * units
                pnl_pct = (entry - close_price) / entry * 100

            db.close_trade(
                trade_id, round(close_price, 6),
                round(pnl, 4), round(pnl_pct, 4),
                exit_reason=exit_reason,
            )
            closed_trade = {**trade, "exit_price": close_price,
                            "pnl": pnl, "pnl_pct": pnl_pct, "exit_reason": exit_reason}
            send_position_closed(closed_trade, exit_reason)

            logger.info(
                "OANDA RECONCILED | %s %s exit=%.5f pnl=%.2f (%.2f%%) reason=%s",
                direction, instrument, close_price, pnl, pnl_pct, exit_reason,
            )
            reconciled += 1

    return {"checked": len(db_trades), "updated": updated,
            "reconciled": reconciled, "errors": errors}


# ── Public entry point ────────────────────────────────────────────────────────

def run_position_check(db: DBManager) -> dict:
    """
    Main entry point — called every 30 minutes by the scheduler.
    Checks paper_sim, Alpaca, and OANDA open positions. Returns combined summary.
    """
    logger.info("── Position monitor started ──")
    start = datetime.now(timezone.utc)

    paper  = _check_paper_sim(db)
    alpaca = _check_alpaca(db)
    oanda  = _check_oanda(db)

    elapsed = (datetime.now(timezone.utc) - start).total_seconds()
    summary = {
        "paper_sim": paper,
        "alpaca":    alpaca,
        "oanda":     oanda,
        "elapsed_s": round(elapsed, 2),
    }
    logger.info(
        "── Position monitor done in %.2fs — paper_sim=%s alpaca=%s oanda=%s ──",
        elapsed, paper, alpaca, oanda,
    )
    return summary
