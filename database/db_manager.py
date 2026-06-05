"""
SQLite database layer.  Creates schema on first use.
"""
import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from config import config

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT    NOT NULL,
    instrument      TEXT    NOT NULL,
    market_type     TEXT    NOT NULL,
    direction       TEXT    NOT NULL,
    technical_score REAL,
    news_verdict    TEXT,
    confidence_score REAL,
    action_taken    TEXT,
    reasoning       TEXT,
    entry_price     REAL,
    stop_loss       REAL,
    take_profit     REAL
);

CREATE TABLE IF NOT EXISTS trades (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id   INTEGER REFERENCES signals(id),
    instrument  TEXT    NOT NULL,
    direction   TEXT    NOT NULL,
    entry_price REAL,
    entry_time  TEXT,
    exit_price  REAL,
    exit_time   TEXT,
    pnl         REAL,
    pnl_pct     REAL,
    status      TEXT    DEFAULT 'OPEN',
    broker      TEXT,
    order_id    TEXT,
    units       REAL
);

CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT    NOT NULL,
    total_value     REAL,
    cash            REAL,
    open_positions  INTEGER,
    daily_pnl       REAL,
    total_pnl       REAL,
    drawdown_pct    REAL
);
"""


class DBManager:
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or config.DB_PATH
        self._initialise()

    @contextmanager
    def _conn(self):
        # timeout=30 and WAL mode allow concurrent writes from multiple threads
        conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _initialise(self) -> None:
        with self._conn() as conn:
            conn.executescript(SCHEMA)
        logger.debug("Database initialised at %s", self.db_path)

    # ── Signals ───────────────────────────────────────────────────

    def insert_signal(self, **kwargs) -> int:
        kwargs.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        cols = ", ".join(kwargs.keys())
        placeholders = ", ".join(["?"] * len(kwargs))
        sql = f"INSERT INTO signals ({cols}) VALUES ({placeholders})"
        with self._conn() as conn:
            cur = conn.execute(sql, list(kwargs.values()))
            return cur.lastrowid

    def get_signal(self, signal_id: int) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM signals WHERE id=?", (signal_id,)).fetchone()
            return dict(row) if row else None

    # ── Trades ────────────────────────────────────────────────────

    def insert_trade(self, **kwargs) -> int:
        kwargs.setdefault("entry_time", datetime.now(timezone.utc).isoformat())
        kwargs.setdefault("status", "OPEN")
        cols = ", ".join(kwargs.keys())
        placeholders = ", ".join(["?"] * len(kwargs))
        sql = f"INSERT INTO trades ({cols}) VALUES ({placeholders})"
        with self._conn() as conn:
            cur = conn.execute(sql, list(kwargs.values()))
            return cur.lastrowid

    def close_trade(self, trade_id: int, exit_price: float, pnl: float, pnl_pct: float) -> None:
        sql = """
            UPDATE trades
               SET exit_price=?, exit_time=?, pnl=?, pnl_pct=?, status='CLOSED'
             WHERE id=?
        """
        with self._conn() as conn:
            conn.execute(sql, (exit_price, datetime.now(timezone.utc).isoformat(), pnl, pnl_pct, trade_id))

    def count_open_trades(self) -> int:
        with self._conn() as conn:
            row = conn.execute("SELECT COUNT(*) FROM trades WHERE status='OPEN'").fetchone()
            return row[0] if row else 0

    def get_open_trades(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM trades WHERE status='OPEN'").fetchall()
            return [dict(r) for r in rows]

    def get_trades_today(self) -> list[dict]:
        today = datetime.now(timezone.utc).date().isoformat()
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM trades WHERE entry_time LIKE ?", (f"{today}%",)
            ).fetchall()
            return [dict(r) for r in rows]

    # ── Portfolio snapshots ───────────────────────────────────────

    def insert_snapshot(self, **kwargs) -> int:
        kwargs.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        cols = ", ".join(kwargs.keys())
        placeholders = ", ".join(["?"] * len(kwargs))
        sql = f"INSERT INTO portfolio_snapshots ({cols}) VALUES ({placeholders})"
        with self._conn() as conn:
            cur = conn.execute(sql, list(kwargs.values()))
            return cur.lastrowid

    def latest_portfolio_snapshot(self) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM portfolio_snapshots ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return dict(row) if row else None

    def get_recent_signals(self, limit: int = 20) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def get_recent_closed_trades(self, limit: int = 10) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM trades WHERE status='CLOSED' ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
