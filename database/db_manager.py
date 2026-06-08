"""
Dual-write database layer.
Writes to both Supabase (primary) and local SQLite (backup) simultaneously.
All reads come from Supabase with automatic SQLite fallback.
If Supabase write fails, error is logged and SQLite backup preserves the data.
"""
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from config import config

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_config (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT    NOT NULL,
    instrument      TEXT    NOT NULL,
    market_type     TEXT,
    direction       TEXT,
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
    units       REAL,
    stop_loss   REAL,
    take_profit REAL
);

CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT    NOT NULL,
    total_value     REAL,
    cash            REAL,
    open_positions  INTEGER,
    daily_pnl       REAL,
    total_pnl       REAL,
    drawdown_pct    REAL,
    alpaca_value    REAL,
    oanda_value     REAL,
    paper_sim_value REAL
);

CREATE TABLE IF NOT EXISTS queued_signals (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id                INTEGER REFERENCES signals(id),
    instrument               TEXT    NOT NULL,
    market_type              TEXT    NOT NULL,
    direction                TEXT    NOT NULL,
    entry_price              REAL,
    stop_loss                REAL,
    take_profit              REAL,
    units                    REAL,
    broker                   TEXT,
    created_at               TEXT    NOT NULL,
    scheduled_execution_time TEXT,
    status                   TEXT    DEFAULT 'PENDING'
);
"""


def _init_supabase():
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_KEY")
    if not url or not key:
        logger.warning("Supabase credentials missing — SQLite-only mode")
        return None
    try:
        from supabase import create_client
        client = create_client(url, key)
        logger.info("Supabase client initialised (dual-write active)")
        return client
    except Exception as exc:
        logger.error("Supabase init failed: %s", exc)
        return None


class DBManager:
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or config.DB_PATH
        self._init_sqlite()
        self._sb = _init_supabase()

    # ── SQLite ─────────────────────────────────────────────────────────────

    @contextmanager
    def _conn(self):
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

    def _init_sqlite(self) -> None:
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)
        logger.debug("SQLite initialised at %s", self.db_path)

    def _migrate(self, conn) -> None:
        """Add new columns to existing tables (idempotent)."""
        migrations = [
            "ALTER TABLE trades ADD COLUMN stop_loss REAL",
            "ALTER TABLE trades ADD COLUMN take_profit REAL",
            "ALTER TABLE trades ADD COLUMN unrealised_pnl REAL DEFAULT 0",
            "ALTER TABLE trades ADD COLUMN exit_reason TEXT",
            "ALTER TABLE trades ADD COLUMN current_price REAL DEFAULT 0",
            "ALTER TABLE trades ADD COLUMN last_checked TEXT",
            "ALTER TABLE portfolio_snapshots ADD COLUMN alpaca_value REAL",
            "ALTER TABLE portfolio_snapshots ADD COLUMN oanda_value REAL",
            "ALTER TABLE portfolio_snapshots ADD COLUMN paper_sim_value REAL",
            "ALTER TABLE portfolio_snapshots ADD COLUMN week_start_value REAL",
            "ALTER TABLE portfolio_snapshots ADD COLUMN weekly_return_pct REAL",
        ]
        for sql in migrations:
            try:
                conn.execute(sql)
            except Exception:
                pass  # column already exists — safe to ignore

    # ── Agent config (persistent key/value store) ─────────────────────────

    def get_config(self, key: str) -> str | None:
        if self._sb:
            try:
                resp = self._sb.table("agent_config").select("value").eq("key", key).execute()
                if resp.data:
                    return resp.data[0]["value"]
            except Exception as exc:
                logger.warning("Supabase get_config fallback for %s: %s", key, exc)
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM agent_config WHERE key=?", (key,)).fetchone()
            return row[0] if row else None

    def set_config(self, key: str, value: str) -> None:
        updated_at = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO agent_config (key, value, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, value, updated_at),
            )
        if self._sb:
            try:
                self._sb.table("agent_config").upsert(
                    {"key": key, "value": value, "updated_at": updated_at},
                    on_conflict="key",
                ).execute()
            except Exception as exc:
                logger.error("Supabase set_config [%s] failed: %s", key, exc)

    # ── Supabase helpers ───────────────────────────────────────────────────

    def _sb_insert(self, table: str, data: dict) -> None:
        if not self._sb:
            return
        try:
            self._sb.table(table).insert(data).execute()
        except Exception as exc:
            logger.error("Supabase insert [%s] failed — data safe in SQLite: %s", table, exc)

    def _sb_update(self, table: str, local_id: int, data: dict) -> None:
        if not self._sb:
            return
        try:
            self._sb.table(table).update(data).eq("local_id", local_id).execute()
        except Exception as exc:
            logger.error("Supabase update [%s] local_id=%s failed: %s", table, local_id, exc)

    # ── Signals ────────────────────────────────────────────────────────────

    def insert_signal(self, **kwargs) -> int:
        kwargs.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        cols = ", ".join(kwargs.keys())
        placeholders = ", ".join(["?"] * len(kwargs))
        with self._conn() as conn:
            cur = conn.execute(
                f"INSERT INTO signals ({cols}) VALUES ({placeholders})",
                list(kwargs.values()),
            )
            local_id = cur.lastrowid
        self._sb_insert("signals", {**kwargs, "local_id": local_id})
        return local_id

    def get_signal(self, signal_id: int) -> dict | None:
        if self._sb:
            try:
                resp = self._sb.table("signals").select("*").eq("local_id", signal_id).execute()
                if resp.data:
                    return resp.data[0]
            except Exception as exc:
                logger.warning("Supabase get_signal fallback to SQLite: %s", exc)
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM signals WHERE id=?", (signal_id,)).fetchone()
            return dict(row) if row else None

    def get_recent_signals(self, limit: int = 20) -> list[dict]:
        if self._sb:
            try:
                resp = (
                    self._sb.table("signals")
                    .select("*")
                    .order("local_id", desc=True)
                    .limit(limit)
                    .execute()
                )
                if resp.data is not None:
                    return resp.data
            except Exception as exc:
                logger.warning("Supabase get_recent_signals fallback to SQLite: %s", exc)
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def get_signals_today(self) -> list[dict]:
        today    = datetime.now(timezone.utc).date().isoformat()
        tomorrow = (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()
        if self._sb:
            try:
                resp = (
                    self._sb.table("signals")
                    .select("*")
                    .gte("timestamp", today)
                    .lt("timestamp", tomorrow)
                    .order("local_id")
                    .execute()
                )
                if resp.data is not None:
                    return resp.data
            except Exception as exc:
                logger.warning("Supabase get_signals_today fallback to SQLite: %s", exc)
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM signals WHERE timestamp LIKE ? ORDER BY id",
                (f"{today}%",),
            ).fetchall()
            return [dict(r) for r in rows]

    def update_signal_action(self, signal_id: int, action: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE signals SET action_taken=? WHERE id=?",
                (action, signal_id),
            )
        self._sb_update("signals", signal_id, {"action_taken": action})

    def get_signals_this_week(self) -> list[dict]:
        week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        if self._sb:
            try:
                resp = (
                    self._sb.table("signals")
                    .select("*")
                    .gte("timestamp", week_ago)
                    .order("local_id")
                    .execute()
                )
                if resp.data is not None:
                    return resp.data
            except Exception as exc:
                logger.warning("Supabase get_signals_this_week fallback to SQLite: %s", exc)
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM signals WHERE timestamp >= ? ORDER BY id", (week_ago,)
            ).fetchall()
            return [dict(r) for r in rows]

    # ── Trades ─────────────────────────────────────────────────────────────

    def insert_trade(self, **kwargs) -> int:
        kwargs.setdefault("entry_time", datetime.now(timezone.utc).isoformat())
        kwargs.setdefault("status", "OPEN")
        cols = ", ".join(kwargs.keys())
        placeholders = ", ".join(["?"] * len(kwargs))
        with self._conn() as conn:
            cur = conn.execute(
                f"INSERT INTO trades ({cols}) VALUES ({placeholders})",
                list(kwargs.values()),
            )
            local_id = cur.lastrowid
        self._sb_insert("trades", {**kwargs, "local_id": local_id})
        return local_id

    def close_trade(
        self,
        trade_id: int,
        exit_price: float,
        pnl: float,
        pnl_pct: float,
        exit_reason: str = "",
    ) -> None:
        exit_time = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute(
                "UPDATE trades SET exit_price=?, exit_time=?, pnl=?, pnl_pct=?, "
                "status='CLOSED', exit_reason=? WHERE id=?",
                (exit_price, exit_time, pnl, pnl_pct, exit_reason, trade_id),
            )
        self._sb_update("trades", trade_id, {
            "exit_price":  exit_price,
            "exit_time":   exit_time,
            "pnl":         pnl,
            "pnl_pct":     pnl_pct,
            "status":      "CLOSED",
            "exit_reason": exit_reason,
        })

    def update_trade_unrealised_pnl(self, trade_id: int, unrealised_pnl: float) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE trades SET unrealised_pnl=? WHERE id=?",
                (unrealised_pnl, trade_id),
            )
        self._sb_update("trades", trade_id, {"unrealised_pnl": unrealised_pnl})

    def update_trade_live(self, trade_id: int, current_price: float, unrealised_pnl: float) -> None:
        """Update current_price, unrealised_pnl, and last_checked timestamp together."""
        last_checked = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute(
                "UPDATE trades SET current_price=?, unrealised_pnl=?, last_checked=? WHERE id=?",
                (current_price, unrealised_pnl, last_checked, trade_id),
            )
        self._sb_update("trades", trade_id, {
            "current_price": current_price,
            "unrealised_pnl": unrealised_pnl,
            "last_checked": last_checked,
        })

    def count_open_trades(self) -> int:
        if self._sb:
            try:
                resp = self._sb.table("trades").select("local_id").eq("status", "OPEN").execute()
                return len(resp.data or [])
            except Exception as exc:
                logger.warning("Supabase count_open_trades fallback to SQLite: %s", exc)
        with self._conn() as conn:
            row = conn.execute("SELECT COUNT(*) FROM trades WHERE status='OPEN'").fetchone()
            return row[0] if row else 0

    def get_open_trades(self) -> list[dict]:
        if self._sb:
            try:
                resp = self._sb.table("trades").select("*").eq("status", "OPEN").execute()
                if resp.data is not None:
                    return resp.data
            except Exception as exc:
                logger.warning("Supabase get_open_trades fallback to SQLite: %s", exc)
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM trades WHERE status='OPEN'").fetchall()
            return [dict(r) for r in rows]

    def get_trades_today(self) -> list[dict]:
        today    = datetime.now(timezone.utc).date().isoformat()
        tomorrow = (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()
        if self._sb:
            try:
                resp = (
                    self._sb.table("trades")
                    .select("*")
                    .gte("entry_time", today)
                    .lt("entry_time", tomorrow)
                    .execute()
                )
                if resp.data is not None:
                    return resp.data
            except Exception as exc:
                logger.warning("Supabase get_trades_today fallback to SQLite: %s", exc)
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM trades WHERE entry_time LIKE ?", (f"{today}%",)
            ).fetchall()
            return [dict(r) for r in rows]

    def get_recent_closed_trades(self, limit: int = 10) -> list[dict]:
        if self._sb:
            try:
                resp = (
                    self._sb.table("trades")
                    .select("*")
                    .eq("status", "CLOSED")
                    .order("local_id", desc=True)
                    .limit(limit)
                    .execute()
                )
                if resp.data is not None:
                    return resp.data
            except Exception as exc:
                logger.warning("Supabase get_recent_closed_trades fallback to SQLite: %s", exc)
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM trades WHERE status='CLOSED' ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def get_closed_trades_today(self) -> list[dict]:
        today    = datetime.now(timezone.utc).date().isoformat()
        tomorrow = (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()
        if self._sb:
            try:
                resp = (
                    self._sb.table("trades")
                    .select("*")
                    .eq("status", "CLOSED")
                    .gte("exit_time", today)
                    .lt("exit_time", tomorrow)
                    .order("local_id")
                    .execute()
                )
                if resp.data is not None:
                    return resp.data
            except Exception as exc:
                logger.warning("Supabase get_closed_trades_today fallback to SQLite: %s", exc)
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM trades WHERE status='CLOSED' AND exit_time LIKE ? ORDER BY id",
                (f"{today}%",),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_closed_trades_this_week(self) -> list[dict]:
        week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        if self._sb:
            try:
                resp = (
                    self._sb.table("trades")
                    .select("*")
                    .eq("status", "CLOSED")
                    .gte("exit_time", week_ago)
                    .order("local_id")
                    .execute()
                )
                if resp.data is not None:
                    return resp.data
            except Exception as exc:
                logger.warning("Supabase get_closed_trades_this_week fallback to SQLite: %s", exc)
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM trades WHERE status='CLOSED' AND exit_time >= ? ORDER BY id",
                (week_ago,),
            ).fetchall()
            return [dict(r) for r in rows]

    # ── Queued signals ─────────────────────────────────────────────────────

    def insert_queued_signal(self, **kwargs) -> int:
        kwargs.setdefault("created_at", datetime.now(timezone.utc).isoformat())
        kwargs.setdefault("status", "PENDING")
        cols = ", ".join(kwargs.keys())
        placeholders = ", ".join(["?"] * len(kwargs))
        with self._conn() as conn:
            cur = conn.execute(
                f"INSERT INTO queued_signals ({cols}) VALUES ({placeholders})",
                list(kwargs.values()),
            )
            local_id = cur.lastrowid
        self._sb_insert("queued_signals", {**kwargs, "local_id": local_id})
        return local_id

    def get_pending_queued_signals(self, broker: str | None = None) -> list[dict]:
        if self._sb:
            try:
                q = self._sb.table("queued_signals").select("*").eq("status", "PENDING")
                if broker:
                    q = q.eq("broker", broker)
                resp = q.order("local_id").execute()
                if resp.data is not None:
                    return resp.data
            except Exception as exc:
                logger.warning("Supabase get_pending_queued_signals fallback: %s", exc)
        with self._conn() as conn:
            if broker:
                rows = conn.execute(
                    "SELECT * FROM queued_signals WHERE status='PENDING' AND broker=? ORDER BY id",
                    (broker,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM queued_signals WHERE status='PENDING' ORDER BY id"
                ).fetchall()
            return [dict(r) for r in rows]

    def close_queued_signal(self, queued_id: int, status: str = "EXECUTED") -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE queued_signals SET status=? WHERE id=?",
                (status, queued_id),
            )
        self._sb_update("queued_signals", queued_id, {"status": status})

    # ── Paper sim helpers ──────────────────────────────────────────────────

    def get_open_paper_sim_trades(self) -> list[dict]:
        if self._sb:
            try:
                resp = (
                    self._sb.table("trades")
                    .select("*")
                    .eq("status", "OPEN")
                    .eq("broker", "paper_sim")
                    .execute()
                )
                if resp.data is not None:
                    return resp.data
            except Exception as exc:
                logger.warning("Supabase get_open_paper_sim_trades fallback: %s", exc)
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM trades WHERE status='OPEN' AND broker='paper_sim'"
            ).fetchall()
            return [dict(r) for r in rows]

    def get_paper_sim_realized_pnl(self) -> float:
        """Sum of P&L for all CLOSED paper_sim trades."""
        if self._sb:
            try:
                resp = (
                    self._sb.table("trades")
                    .select("pnl")
                    .eq("status", "CLOSED")
                    .eq("broker", "paper_sim")
                    .execute()
                )
                if resp.data is not None:
                    return sum(float(r.get("pnl") or 0) for r in resp.data)
            except Exception as exc:
                logger.warning("Supabase get_paper_sim_realized_pnl fallback: %s", exc)
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(pnl),0) FROM trades WHERE status='CLOSED' AND broker='paper_sim'"
            ).fetchone()
            return float(row[0]) if row else 0.0

    # ── Portfolio snapshots ────────────────────────────────────────────────

    def get_week_start_snapshot(self) -> dict | None:
        """Return the earliest portfolio snapshot from this calendar week (Mon–Sun)."""
        now = datetime.now(timezone.utc)
        monday = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        monday_iso = monday.isoformat()
        if self._sb:
            try:
                resp = (
                    self._sb.table("portfolio_snapshots")
                    .select("*")
                    .gte("timestamp", monday_iso)
                    .order("local_id")
                    .limit(1)
                    .execute()
                )
                if resp.data:
                    return resp.data[0]
            except Exception as exc:
                logger.warning("Supabase get_week_start_snapshot fallback: %s", exc)
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM portfolio_snapshots WHERE timestamp >= ? ORDER BY id ASC LIMIT 1",
                (monday_iso,),
            ).fetchone()
            return dict(row) if row else None

    def insert_snapshot(self, **kwargs) -> int:
        kwargs.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        cols = ", ".join(kwargs.keys())
        placeholders = ", ".join(["?"] * len(kwargs))
        with self._conn() as conn:
            cur = conn.execute(
                f"INSERT INTO portfolio_snapshots ({cols}) VALUES ({placeholders})",
                list(kwargs.values()),
            )
            local_id = cur.lastrowid
        self._sb_insert("portfolio_snapshots", {**kwargs, "local_id": local_id})
        return local_id

    def latest_portfolio_snapshot(self) -> dict | None:
        if self._sb:
            try:
                resp = (
                    self._sb.table("portfolio_snapshots")
                    .select("*")
                    .order("local_id", desc=True)
                    .limit(1)
                    .execute()
                )
                if resp.data:
                    return resp.data[0]
            except Exception as exc:
                logger.warning("Supabase latest_portfolio_snapshot fallback to SQLite: %s", exc)
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM portfolio_snapshots ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return dict(row) if row else None

    def get_snapshot_days_ago(self, days: int) -> dict | None:
        target = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        if self._sb:
            try:
                resp = (
                    self._sb.table("portfolio_snapshots")
                    .select("*")
                    .lte("timestamp", target)
                    .order("local_id", desc=True)
                    .limit(1)
                    .execute()
                )
                if resp.data:
                    return resp.data[0]
            except Exception as exc:
                logger.warning("Supabase get_snapshot_days_ago fallback to SQLite: %s", exc)
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM portfolio_snapshots WHERE timestamp <= ? ORDER BY id DESC LIMIT 1",
                (target,),
            ).fetchone()
            return dict(row) if row else None
