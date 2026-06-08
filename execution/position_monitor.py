"""
Lightweight position monitor — runs every 30 minutes.
Checks open paper_sim and Alpaca trades for stop-loss / take-profit hits.
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


# ── Public entry point ────────────────────────────────────────────────────────

def run_position_check(db: DBManager) -> dict:
    """
    Main entry point — called every 30 minutes by the scheduler.
    Checks paper_sim and Alpaca open positions. Returns combined summary.
    """
    logger.info("── Position monitor started ──")
    start = datetime.now(timezone.utc)

    paper = _check_paper_sim(db)
    alpaca = _check_alpaca(db)

    elapsed = (datetime.now(timezone.utc) - start).total_seconds()
    summary = {
        "paper_sim": paper,
        "alpaca":    alpaca,
        "elapsed_s": round(elapsed, 2),
    }
    logger.info(
        "── Position monitor done in %.2fs — paper_sim=%s alpaca=%s ──",
        elapsed, paper, alpaca,
    )
    return summary
