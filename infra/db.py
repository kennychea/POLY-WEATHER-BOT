"""Async SQLite queue writer — never blocks the main loop.

All writes go through an asyncio.Queue. A background worker task
drains the queue and batches writes into transactions.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

# ── SQL schemas ─────────────────────────────────────────────────────

_SCHEMAS: dict[str, str] = {
    "trade_results": """
        CREATE TABLE IF NOT EXISTS trade_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id TEXT NOT NULL,
            signal_type TEXT NOT NULL,
            entry_price REAL NOT NULL,
            exit_price REAL NOT NULL,
            size_usdc REAL NOT NULL,
            pnl REAL NOT NULL,
            win INTEGER NOT NULL,
            timestamp TEXT NOT NULL
        )
    """,
    "weather_signals": """
        CREATE TABLE IF NOT EXISTS weather_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_question TEXT NOT NULL,
            market_id TEXT NOT NULL,
            token_id TEXT NOT NULL,
            signal_type TEXT NOT NULL,
            forecast_probability REAL NOT NULL,
            market_price REAL NOT NULL,
            edge REAL NOT NULL,
            location TEXT NOT NULL,
            forecast_date TEXT NOT NULL,
            weather_metric TEXT NOT NULL,
            threshold_value REAL NOT NULL,
            timestamp TEXT NOT NULL,
            ensemble_member_count INTEGER NOT NULL DEFAULT 0,
            confidence TEXT NOT NULL DEFAULT 'medium',
            net_edge REAL NOT NULL DEFAULT 0.0
        )
    """,
    "paper_trades": """
        CREATE TABLE IF NOT EXISTS paper_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id TEXT NOT NULL,
            market_question TEXT NOT NULL,
            market_id TEXT NOT NULL,
            token_id TEXT NOT NULL,
            signal_type TEXT NOT NULL,
            size_usdc REAL NOT NULL,
            entry_price REAL NOT NULL,
            exit_price REAL,
            fees REAL NOT NULL,
            pnl REAL,
            status TEXT NOT NULL,
            opened_at TEXT NOT NULL,
            resolved_at TEXT
        )
    """,
    "calibration_log": """
        CREATE TABLE IF NOT EXISTS calibration_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id TEXT NOT NULL,
            confidence REAL NOT NULL,
            estimated_probability REAL NOT NULL,
            market_price_at_signal REAL NOT NULL,
            market_price_at_resolution REAL NOT NULL,
            delay_seconds INTEGER NOT NULL,
            pnl REAL NOT NULL,
            win INTEGER NOT NULL,
            timestamp TEXT NOT NULL,
            weather_metric TEXT NOT NULL DEFAULT '',
            location TEXT NOT NULL DEFAULT ''
        )
    """,
}

# Column order per table (excludes auto-increment id)
_COLUMNS: dict[str, list[str]] = {
    "trade_results": [
        "market_id", "signal_type", "entry_price", "exit_price",
        "size_usdc", "pnl", "win", "timestamp",
    ],
    "weather_signals": [
        "market_question", "market_id", "token_id", "signal_type",
        "forecast_probability", "market_price", "edge",
        "location", "forecast_date", "weather_metric",
        "threshold_value", "timestamp",
        "ensemble_member_count", "confidence", "net_edge",
    ],
    "paper_trades": [
        "trade_id", "market_question", "market_id", "token_id",
        "signal_type", "size_usdc", "entry_price", "exit_price",
        "fees", "pnl", "status", "opened_at", "resolved_at",
    ],
    "calibration_log": [
        "trade_id", "confidence", "estimated_probability",
        "market_price_at_signal", "market_price_at_resolution",
        "delay_seconds", "pnl", "win", "timestamp",
        "weather_metric", "location",
    ],
}


# ── DbWriter ────────────────────────────────────────────────────────


class DbWriter:
    """Async SQLite writer that queues writes for background flushing."""

    def __init__(self, db_path: str, telegram: Any = None) -> None:
        self._db_path = db_path
        self._queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue(maxsize=10000)
        self._running = True
        self._conn: aiosqlite.Connection | None = None
        self._telegram = telegram
        self._drop_count = 0

    async def init_db(self) -> None:
        """Create tables if they don't exist, and migrate columns."""
        async with self._get_conn() as conn:
            await conn.execute("PRAGMA journal_mode=WAL")
            for schema in _SCHEMAS.values():
                await conn.execute(schema)
            # Migrate existing tables — add new columns if missing
            for col in ("weather_metric", "location"):
                for table in ("calibration_log",):
                    with contextlib.suppress(Exception):
                        await conn.execute(
                            f"ALTER TABLE {table} ADD COLUMN {col}"
                            " TEXT NOT NULL DEFAULT ''"
                        )
            # Index for read_pending_trades subquery performance
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_paper_trades_status"
                " ON paper_trades (trade_id, status)"
            )
            await conn.commit()

    @asynccontextmanager
    async def _get_conn(self):
        """Get or create the database connection."""
        if self._conn is None:
            self._conn = await aiosqlite.connect(self._db_path)
        yield self._conn

    async def enqueue(self, table: str, record: Any) -> None:
        """Add a record to the write queue (non-blocking).

        Uses put_nowait to avoid stalling the pipeline if the queue is full.
        """
        data = asdict(record)
        # Convert datetime objects to ISO strings
        for key, val in data.items():
            if hasattr(val, "isoformat"):
                data[key] = val.isoformat()
        try:
            self._queue.put_nowait((table, data))
        except asyncio.QueueFull:
            self._drop_count += 1
            logger.warning(
                "DB queue full, dropping %s record (total drops: %d)",
                table, self._drop_count,
            )
            if self._telegram and self._drop_count % 10 == 1:
                asyncio.create_task(self._alert_overflow(table))

    async def flush(self) -> None:
        """Write all queued items to the database in one transaction."""
        items: list[tuple[str, dict[str, Any]]] = []
        while not self._queue.empty():
            items.append(self._queue.get_nowait())

        if not items:
            return

        try:
            async with self._get_conn() as conn:
                for table, data in items:
                    cols = _COLUMNS[table]
                    placeholders = ", ".join("?" for _ in cols)
                    col_names = ", ".join(cols)
                    values = [data[c] for c in cols]
                    await conn.execute(
                        f"INSERT INTO {table} ({col_names}) VALUES ({placeholders})",  # noqa: S608
                        values,
                    )
                await conn.commit()
        except Exception:
            logger.exception("DB flush failed, requeuing %d items", len(items))
            # Close and reset connection so next flush creates a fresh one
            if self._conn is not None:
                with contextlib.suppress(Exception):
                    await self._conn.close()
                self._conn = None
            requeued = 0
            for item in items:
                try:
                    self._queue.put_nowait(item)
                    requeued += 1
                except asyncio.QueueFull:
                    dropped = len(items) - requeued
                    self._drop_count += dropped
                    logger.error(
                        "Queue full during requeue, dropping %d items "
                        "(total drops: %d)",
                        dropped, self._drop_count,
                    )
                    break

    async def run_worker(self, flush_interval: float = 0.1) -> None:
        """Background worker that flushes the queue periodically."""
        try:
            while self._running:
                try:
                    await self.flush()
                except Exception:
                    logger.exception("DB worker flush error")
                await asyncio.sleep(flush_interval)
        finally:
            # Final flush on stop — runs even if sleep is cancelled
            try:
                await self.flush()
            except Exception:
                logger.exception("DB final flush failed")

    def stop(self) -> None:
        """Signal the worker to stop."""
        self._running = False

    async def read_pending_trades(self) -> list[dict[str, Any]]:
        """Read pending trades that have NOT been resolved yet."""
        async with self._get_conn() as conn:
            cursor = await conn.execute(
                "SELECT * FROM paper_trades WHERE status = 'pending'"
                " AND trade_id NOT IN"
                " (SELECT trade_id FROM paper_trades WHERE status = 'resolved')",
            )
            rows = await cursor.fetchall()
            cols = [desc[0] for desc in cursor.description]
            return [dict(zip(cols, row, strict=False)) for row in rows]

    async def read_resolved_trades(self) -> list[dict[str, Any]]:
        """Read all resolved paper trades from the database."""
        async with self._get_conn() as conn:
            cursor = await conn.execute(
                "SELECT * FROM paper_trades WHERE status = 'resolved'",
            )
            rows = await cursor.fetchall()
            cols = [desc[0] for desc in cursor.description]
            return [dict(zip(cols, row, strict=False)) for row in rows]

    async def read_recent_trades(self, limit: int = 5) -> list[dict[str, Any]]:
        """Read the most recent paper trades (any status), ordered by opened_at DESC."""
        async with self._get_conn() as conn:
            cursor = await conn.execute(
                "SELECT * FROM paper_trades ORDER BY opened_at DESC LIMIT ?",
                (limit,),
            )
            rows = await cursor.fetchall()
            cols = [desc[0] for desc in cursor.description]
            return [dict(zip(cols, row, strict=False)) for row in rows]

    async def read_calibration_log(self) -> list[dict[str, Any]]:
        """Read all calibration log entries."""
        async with self._get_conn() as conn:
            cursor = await conn.execute(
                "SELECT * FROM calibration_log ORDER BY timestamp DESC",
            )
            rows = await cursor.fetchall()
            cols = [desc[0] for desc in cursor.description]
            return [dict(zip(cols, row, strict=False)) for row in rows]

    async def read_recent_signals(self, limit: int = 5) -> list[dict[str, Any]]:
        """Read the most recent weather signals, ordered by timestamp DESC."""
        async with self._get_conn() as conn:
            cursor = await conn.execute(
                "SELECT * FROM weather_signals ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            )
            rows = await cursor.fetchall()
            cols = [desc[0] for desc in cursor.description]
            return [dict(zip(cols, row, strict=False)) for row in rows]

    async def _alert_overflow(self, table: str) -> None:
        """Send Telegram alert on queue overflow."""
        try:
            await self._telegram.send(
                f"DB queue overflow: dropping {table} records ({self._drop_count} total drops)"
            )
        except Exception:
            logger.exception("Failed to send overflow alert")

    @property
    def drop_count(self) -> int:
        """Number of records dropped due to queue overflow."""
        return self._drop_count

    @property
    def queue_depth(self) -> int:
        """Current number of items in the write queue."""
        return self._queue.qsize()

    async def close(self) -> None:
        """Close the database connection."""
        self.stop()
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
