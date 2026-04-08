"""Tests for infra/db.py -- async SQLite queue writer."""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_db_writer_creates_tables():
    """DbWriter should create all tables on init."""
    from infra.db import DbWriter
    writer = DbWriter(":memory:")
    await writer.init_db()
    async with writer._get_conn() as conn:
        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = [row[0] for row in await cursor.fetchall()]
    await writer.close()
    assert "weather_signals" in tables
    assert "trade_results" in tables
    assert "paper_trades" in tables


@pytest.mark.asyncio
async def test_db_writer_enqueue_weather_signal():
    """Enqueuing a weather signal should not block."""
    from infra.db import DbWriter
    from infra.types import SignalType, WeatherSignal
    writer = DbWriter(":memory:")
    await writer.init_db()
    now = datetime.now(UTC)
    signal = WeatherSignal(
        market_question="Will NYC high exceed 90F?",
        market_id="cond_abc",
        token_id="tok_yes",
        signal_type=SignalType.BUY_YES,
        forecast_probability=0.72,
        market_price=0.55,
        edge=0.17,
        location="New York",
        forecast_date="2026-04-10",
        weather_metric="temp_high",
        threshold_value=90.0,
        timestamp=now,
    )
    await writer.enqueue("weather_signals", signal)
    assert writer._queue.qsize() == 1
    await writer.close()


@pytest.mark.asyncio
async def test_db_writer_flush_writes_to_db():
    """Flush should write queued items to the database."""
    from infra.db import DbWriter
    from infra.types import SignalType, WeatherSignal
    writer = DbWriter(":memory:")
    await writer.init_db()
    now = datetime.now(UTC)
    signal = WeatherSignal(
        market_question="Will NYC high exceed 90F?",
        market_id="cond_abc",
        token_id="tok_yes",
        signal_type=SignalType.BUY_YES,
        forecast_probability=0.72,
        market_price=0.55,
        edge=0.17,
        location="New York",
        forecast_date="2026-04-10",
        weather_metric="temp_high",
        threshold_value=90.0,
        timestamp=now,
    )
    await writer.enqueue("weather_signals", signal)
    await writer.flush()
    async with writer._get_conn() as conn:
        cursor = await conn.execute("SELECT COUNT(*) FROM weather_signals")
        count = (await cursor.fetchone())[0]
    await writer.close()
    assert count == 1


@pytest.mark.asyncio
async def test_db_writer_enqueue_trade_result():
    """Should store trade results."""
    from infra.db import DbWriter
    from infra.types import SignalType, TradeResult
    writer = DbWriter(":memory:")
    await writer.init_db()
    now = datetime.now(UTC)
    result = TradeResult(
        market_id="0xabc",
        signal_type=SignalType.BUY_YES,
        entry_price=0.45,
        exit_price=1.0,
        size_usdc=10.0,
        pnl=5.50,
        win=True,
        timestamp=now,
    )
    await writer.enqueue("trade_results", result)
    await writer.flush()
    async with writer._get_conn() as conn:
        cursor = await conn.execute("SELECT pnl, win FROM trade_results")
        row = await cursor.fetchone()
    await writer.close()
    assert row[0] == 5.50
    assert row[1] == 1


@pytest.mark.asyncio
async def test_db_writer_flush_multiple():
    """Should handle multiple items in one flush."""
    from infra.db import DbWriter
    from infra.types import SignalType, WeatherSignal
    writer = DbWriter(":memory:")
    await writer.init_db()
    now = datetime.now(UTC)
    for i in range(3):
        signal = WeatherSignal(
            market_question=f"Weather question {i}",
            market_id=f"cond_{i}",
            token_id=f"tok_{i}",
            signal_type=SignalType.BUY_YES,
            forecast_probability=0.70,
            market_price=0.55,
            edge=0.15,
            location="New York",
            forecast_date="2026-04-10",
            weather_metric="temp_high",
            threshold_value=90.0,
            timestamp=now,
        )
        await writer.enqueue("weather_signals", signal)
    await writer.flush()
    async with writer._get_conn() as conn:
        cursor = await conn.execute("SELECT COUNT(*) FROM weather_signals")
        count = (await cursor.fetchone())[0]
    await writer.close()
    assert count == 3


@pytest.mark.asyncio
async def test_db_writer_run_worker():
    """Worker loop should process queue items."""
    from infra.db import DbWriter
    from infra.types import SignalType, WeatherSignal
    writer = DbWriter(":memory:")
    await writer.init_db()
    now = datetime.now(UTC)
    signal = WeatherSignal(
        market_question="Worker test",
        market_id="cond_w",
        token_id="tok_w",
        signal_type=SignalType.BUY_YES,
        forecast_probability=0.70,
        market_price=0.55,
        edge=0.15,
        location="Chicago",
        forecast_date="2026-04-10",
        weather_metric="temp_high",
        threshold_value=85.0,
        timestamp=now,
    )
    await writer.enqueue("weather_signals", signal)
    task = asyncio.create_task(writer.run_worker())
    await asyncio.sleep(0.15)
    writer.stop()
    await task
    async with writer._get_conn() as conn:
        cursor = await conn.execute("SELECT COUNT(*) FROM weather_signals")
        count = (await cursor.fetchone())[0]
    await writer.close()
    assert count == 1


@pytest.mark.asyncio
async def test_db_writer_flush_requeues_on_error():
    """Flush should requeue items if INSERT fails."""
    import tempfile
    from pathlib import Path
    from unittest.mock import patch

    from infra.db import DbWriter
    from infra.types import SignalType, WeatherSignal

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test_requeue.db")
        writer = DbWriter(db_path)
        await writer.init_db()
        now = datetime.now(UTC)
        signal = WeatherSignal(
            market_question="Requeue test",
            market_id="cond_rq",
            token_id="tok_rq",
            signal_type=SignalType.BUY_YES,
            forecast_probability=0.70,
            market_price=0.55,
            edge=0.15,
            location="Denver",
            forecast_date="2026-04-10",
            weather_metric="temp_high",
            threshold_value=80.0,
            timestamp=now,
        )
        await writer.enqueue("weather_signals", signal)
        assert writer._queue.qsize() == 1

        with patch.object(writer, "_get_conn", side_effect=RuntimeError("DB error")):
            await writer.flush()

        assert writer._queue.qsize() == 1

        await writer.flush()
        async with writer._get_conn() as conn:
            cursor = await conn.execute("SELECT COUNT(*) FROM weather_signals")
            count = (await cursor.fetchone())[0]
        await writer.close()
        assert count == 1


@pytest.mark.asyncio
async def test_db_writer_connection_reset_on_error():
    """Connection should be reset after flush error so next flush reconnects."""
    import tempfile
    from pathlib import Path

    from infra.db import DbWriter
    from infra.types import SignalType, WeatherSignal

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test.db")
        writer = DbWriter(db_path)
        await writer.init_db()
        now = datetime.now(UTC)

        async with writer._get_conn() as conn:
            pass
        assert writer._conn is not None

        signal = WeatherSignal(
            market_question="Reset test",
            market_id="cond_rst",
            token_id="tok_rst",
            signal_type=SignalType.BUY_YES,
            forecast_probability=0.70,
            market_price=0.55,
            edge=0.15,
            location="Seattle",
            forecast_date="2026-04-10",
            weather_metric="precip",
            threshold_value=1.0,
            timestamp=now,
        )
        await writer.enqueue("weather_signals", signal)

        writer._conn.execute = AsyncMock(side_effect=RuntimeError("connection dead"))
        await writer.flush()

        assert writer._conn is None
        assert writer._queue.qsize() == 1

        await writer.flush()
        async with writer._get_conn() as conn:
            cursor = await conn.execute("SELECT COUNT(*) FROM weather_signals")
            count = (await cursor.fetchone())[0]
        await writer.close()
        assert count == 1


@pytest.mark.asyncio
async def test_db_writer_creates_weather_signals_table():
    """init_db should create weather_signals table."""
    from infra.db import DbWriter
    writer = DbWriter(":memory:")
    await writer.init_db()
    async with writer._get_conn() as conn:
        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='weather_signals'"
        )
        row = await cursor.fetchone()
    await writer.close()
    assert row is not None
    assert row[0] == "weather_signals"


@pytest.mark.asyncio
async def test_db_writer_creates_paper_trades_table():
    """init_db should create paper_trades table."""
    from infra.db import DbWriter
    writer = DbWriter(":memory:")
    await writer.init_db()
    async with writer._get_conn() as conn:
        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='paper_trades'"
        )
        row = await cursor.fetchone()
    await writer.close()
    assert row is not None
    assert row[0] == "paper_trades"


@pytest.mark.asyncio
async def test_db_writer_enqueue_weather_signal_flush():
    """Should store weather signals with all fields."""
    from infra.db import DbWriter
    from infra.types import SignalType, WeatherSignal
    writer = DbWriter(":memory:")
    await writer.init_db()
    now = datetime.now(UTC)
    signal = WeatherSignal(
        market_question="Will NYC temp exceed 95F by April 15?",
        market_id="cond_abc",
        token_id="tok_yes",
        signal_type=SignalType.BUY_YES,
        forecast_probability=0.72,
        market_price=0.55,
        edge=0.17,
        location="New York",
        forecast_date="2026-04-15",
        weather_metric="temp_high",
        threshold_value=95.0,
        timestamp=now,
    )
    await writer.enqueue("weather_signals", signal)
    await writer.flush()
    async with writer._get_conn() as conn:
        cursor = await conn.execute("SELECT COUNT(*) FROM weather_signals")
        count = (await cursor.fetchone())[0]
    await writer.close()
    assert count == 1


@pytest.mark.asyncio
async def test_db_writer_enqueue_paper_trade():
    """Should store paper trades."""
    from infra.db import DbWriter
    from infra.types import SignalType, WeatherPaperTrade
    writer = DbWriter(":memory:")
    await writer.init_db()
    now = datetime.now(UTC)
    trade = WeatherPaperTrade(
        trade_id="trade_001",
        market_question="Will Chicago high exceed 85F?",
        market_id="cond_abc",
        token_id="tok_yes",
        signal_type=SignalType.BUY_YES,
        size_usdc=5.0,
        entry_price=0.55,
        exit_price=None,
        fees=0.05,
        pnl=None,
        status="pending",
        opened_at=now,
        resolved_at=None,
    )
    await writer.enqueue("paper_trades", trade)
    await writer.flush()
    async with writer._get_conn() as conn:
        cursor = await conn.execute(
            "SELECT trade_id, status, entry_price FROM paper_trades"
        )
        row = await cursor.fetchone()
    await writer.close()
    assert row[0] == "trade_001"
    assert row[1] == "pending"
    assert row[2] == 0.55


@pytest.mark.asyncio
async def test_db_writer_paper_trade_resolved():
    """Should store resolved paper trades with exit_price and pnl."""
    from infra.db import DbWriter
    from infra.types import SignalType, WeatherPaperTrade
    writer = DbWriter(":memory:")
    await writer.init_db()
    now = datetime.now(UTC)
    trade = WeatherPaperTrade(
        trade_id="trade_002",
        market_question="Will Denver snow exceed 3 inches?",
        market_id="cond_xyz",
        token_id="tok_no",
        signal_type=SignalType.BUY_NO,
        size_usdc=8.0,
        entry_price=0.40,
        exit_price=0.55,
        fees=0.08,
        pnl=1.12,
        status="resolved",
        opened_at=now,
        resolved_at=now,
    )
    await writer.enqueue("paper_trades", trade)
    await writer.flush()
    async with writer._get_conn() as conn:
        cursor = await conn.execute(
            "SELECT pnl, status, exit_price FROM paper_trades"
        )
        row = await cursor.fetchone()
    await writer.close()
    assert row[0] == 1.12
    assert row[1] == "resolved"
    assert row[2] == 0.55


@pytest.mark.asyncio
async def test_db_writer_enqueue_drops_on_full_queue():
    """enqueue should not block when queue is full."""
    from infra.db import DbWriter
    from infra.types import SignalType, WeatherSignal
    writer = DbWriter(":memory:")
    writer._queue = asyncio.Queue(maxsize=2)
    await writer.init_db()
    now = datetime.now(UTC)

    for i in range(2):
        signal = WeatherSignal(
            market_question=f"Question {i}",
            market_id=f"cond_{i}",
            token_id=f"tok_{i}",
            signal_type=SignalType.BUY_YES,
            forecast_probability=0.70,
            market_price=0.55,
            edge=0.15,
            location="Miami",
            forecast_date="2026-04-10",
            weather_metric="temp_high",
            threshold_value=90.0,
            timestamp=now,
        )
        await writer.enqueue("weather_signals", signal)

    assert writer._queue.qsize() == 2

    signal3 = WeatherSignal(
        market_question="Question 3",
        market_id="cond_3",
        token_id="tok_3",
        signal_type=SignalType.BUY_YES,
        forecast_probability=0.70,
        market_price=0.55,
        edge=0.15,
        location="Miami",
        forecast_date="2026-04-10",
        weather_metric="temp_high",
        threshold_value=90.0,
        timestamp=now,
    )
    await asyncio.wait_for(writer.enqueue("weather_signals", signal3), timeout=1.0)
    assert writer._queue.qsize() == 2
    await writer.close()


@pytest.mark.asyncio
async def test_db_writer_final_flush_on_cancel():
    """Worker should do final flush even if cancelled during sleep."""
    import contextlib

    from infra.db import DbWriter
    from infra.types import SignalType, WeatherSignal
    writer = DbWriter(":memory:")
    await writer.init_db()
    now = datetime.now(UTC)

    task = asyncio.create_task(writer.run_worker(flush_interval=10.0))
    await asyncio.sleep(0.05)

    signal = WeatherSignal(
        market_question="Cancel test",
        market_id="cond_c",
        token_id="tok_c",
        signal_type=SignalType.BUY_YES,
        forecast_probability=0.70,
        market_price=0.55,
        edge=0.15,
        location="Boston",
        forecast_date="2026-04-10",
        weather_metric="temp_high",
        threshold_value=75.0,
        timestamp=now,
    )
    await writer.enqueue("weather_signals", signal)

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    async with writer._get_conn() as conn:
        cursor = await conn.execute("SELECT COUNT(*) FROM weather_signals")
        count = (await cursor.fetchone())[0]
    await writer.close()
    assert count == 1


# -- Read methods (P2 persistence) -------------------------------------------


@pytest.mark.asyncio
async def test_read_pending_trades():
    """read_pending_trades should return pending trades from DB."""
    from infra.db import DbWriter
    from infra.types import SignalType, WeatherPaperTrade
    writer = DbWriter(":memory:")
    await writer.init_db()
    now = datetime.now(UTC)

    trade = WeatherPaperTrade(
        trade_id="paper_test1",
        market_question="Test weather question",
        market_id="m1",
        token_id="t1",
        signal_type=SignalType.BUY_YES,
        size_usdc=10.0,
        entry_price=0.50,
        exit_price=None,
        fees=0.10,
        pnl=None,
        status="pending",
        opened_at=now,
        resolved_at=None,
    )
    await writer.enqueue("paper_trades", trade)
    await writer.flush()

    rows = await writer.read_pending_trades()
    assert len(rows) == 1
    assert rows[0]["trade_id"] == "paper_test1"
    assert rows[0]["status"] == "pending"
    await writer.close()


@pytest.mark.asyncio
async def test_read_resolved_trades():
    """read_resolved_trades should return resolved trades from DB."""
    from infra.db import DbWriter
    from infra.types import SignalType, WeatherPaperTrade
    writer = DbWriter(":memory:")
    await writer.init_db()
    now = datetime.now(UTC)

    trade = WeatherPaperTrade(
        trade_id="paper_resolved1",
        market_question="Resolved weather question",
        market_id="m2",
        token_id="t2",
        signal_type=SignalType.BUY_NO,
        size_usdc=20.0,
        entry_price=0.60,
        exit_price=0.40,
        fees=0.40,
        pnl=3.0,
        status="resolved",
        opened_at=now,
        resolved_at=now,
    )
    await writer.enqueue("paper_trades", trade)
    await writer.flush()

    rows = await writer.read_resolved_trades()
    assert len(rows) == 1
    assert rows[0]["trade_id"] == "paper_resolved1"
    assert rows[0]["pnl"] == 3.0
    await writer.close()


@pytest.mark.asyncio
async def test_db_drop_count_and_queue_depth():
    """DB should track drop_count and queue_depth."""
    from infra.db import DbWriter
    writer = DbWriter(":memory:")
    assert writer.drop_count == 0
    assert writer.queue_depth == 0
    await writer.close()


@pytest.mark.asyncio
async def test_flush_requeue_full_increments_drop_count():
    """Requeue failure should increment drop_count."""
    from infra.db import DbWriter
    writer = DbWriter(":memory:")
    await writer.init_db()

    writer._queue = asyncio.Queue(maxsize=1)
    writer._queue.put_nowait(("nonexistent_table", {"bad": "data"}))

    await writer.flush()

    await writer.close()


# -- Read methods (D2 — dashboard + telegram enrichi) -------------------------


@pytest.mark.asyncio
async def test_read_recent_trades():
    """read_recent_trades should return trades ordered by opened_at DESC."""
    from infra.db import DbWriter
    from infra.types import SignalType, WeatherPaperTrade
    writer = DbWriter(":memory:")
    await writer.init_db()
    now = datetime.now(UTC)

    for i in range(3):
        trade = WeatherPaperTrade(
            trade_id=f"paper_{i}",
            market_question=f"Weather question {i}",
            market_id="m1",
            token_id="t1",
            signal_type=SignalType.BUY_YES,
            size_usdc=10.0,
            entry_price=0.50,
            exit_price=0.60 if i < 2 else None,
            fees=0.10,
            pnl=1.0 if i < 2 else None,
            status="resolved" if i < 2 else "pending",
            opened_at=now,
            resolved_at=now if i < 2 else None,
        )
        await writer.enqueue("paper_trades", trade)
    await writer.flush()

    rows = await writer.read_recent_trades(limit=2)
    assert len(rows) == 2
    await writer.close()


@pytest.mark.asyncio
async def test_read_calibration_log():
    """read_calibration_log should return all calibration entries."""
    from infra.db import DbWriter
    writer = DbWriter(":memory:")
    await writer.init_db()

    async with writer._get_conn() as conn:
        await conn.execute(
            "INSERT INTO calibration_log "
            "(trade_id, confidence, estimated_probability, "
            "market_price_at_signal, market_price_at_resolution, "
            "delay_seconds, pnl, win, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("t1", 0.85, 0.70, 0.55, 0.65, 300, 2.5, 1, "2026-04-04T10:00:00"),
        )
        await conn.commit()

    rows = await writer.read_calibration_log()
    assert len(rows) == 1
    assert rows[0]["confidence"] == 0.85
    assert rows[0]["win"] == 1
    await writer.close()


@pytest.mark.asyncio
async def test_read_recent_signals():
    """read_recent_signals should return recent weather signals."""
    from infra.db import DbWriter
    from infra.types import SignalType, WeatherSignal
    writer = DbWriter(":memory:")
    await writer.init_db()
    now = datetime.now(UTC)

    for i in range(3):
        signal = WeatherSignal(
            market_question=f"Weather question {i}",
            market_id=f"cond_{i}",
            token_id=f"tok_{i}",
            signal_type=SignalType.BUY_YES,
            forecast_probability=0.70,
            market_price=0.55,
            edge=0.15,
            location="Atlanta",
            forecast_date="2026-04-10",
            weather_metric="temp_high",
            threshold_value=90.0,
            timestamp=now,
        )
        await writer.enqueue("weather_signals", signal)
    await writer.flush()

    rows = await writer.read_recent_signals(limit=2)
    assert len(rows) == 2
    await writer.close()


# -- BUG B: Ghost pending trades after restart --------------------------------


@pytest.mark.asyncio
async def test_read_pending_trades_excludes_resolved():
    """BUG B: read_pending_trades should NOT return trades that have a resolved row."""
    from infra.db import DbWriter
    from infra.types import SignalType, WeatherPaperTrade
    writer = DbWriter(":memory:")
    await writer.init_db()
    now = datetime.now(UTC)

    pending = WeatherPaperTrade(
        trade_id="paper_ghost",
        market_question="Ghost weather trade",
        market_id="m1",
        token_id="t1",
        signal_type=SignalType.BUY_YES,
        size_usdc=10.0,
        entry_price=0.50,
        exit_price=None,
        fees=0.10,
        pnl=None,
        status="pending",
        opened_at=now,
        resolved_at=None,
    )
    await writer.enqueue("paper_trades", pending)
    await writer.flush()

    resolved = WeatherPaperTrade(
        trade_id="paper_ghost",
        market_question="Ghost weather trade",
        market_id="m1",
        token_id="t1",
        signal_type=SignalType.BUY_YES,
        size_usdc=10.0,
        entry_price=0.50,
        exit_price=0.70,
        fees=0.20,
        pnl=3.0,
        status="resolved",
        opened_at=now,
        resolved_at=now,
    )
    await writer.enqueue("paper_trades", resolved)
    await writer.flush()

    rows = await writer.read_pending_trades()
    assert len(rows) == 0, "Ghost pending trade should be excluded"
    await writer.close()


# -- P1.1: WAL mode -----------------------------------------------------------


@pytest.mark.asyncio
async def test_init_db_creates_paper_trades_index():
    """init_db should create index on paper_trades(trade_id, status)."""
    from infra.db import DbWriter
    writer = DbWriter(":memory:")
    await writer.init_db()
    async with writer._get_conn() as conn:
        cursor = await conn.execute(
            "SELECT name FROM sqlite_master"
            " WHERE type='index' AND name='idx_paper_trades_status'"
        )
        row = await cursor.fetchone()
    await writer.close()
    assert row is not None
    assert row[0] == "idx_paper_trades_status"


@pytest.mark.asyncio
async def test_wal_mode_enabled(tmp_path):
    """init_db should enable WAL journal mode."""
    from infra.db import DbWriter
    db_file = tmp_path / "test_wal.db"
    writer = DbWriter(str(db_file))
    await writer.init_db()
    async with writer._get_conn() as conn:
        cursor = await conn.execute("PRAGMA journal_mode")
        mode = (await cursor.fetchone())[0]
    await writer.close()
    assert mode == "wal"


# -- P5.1.1: traded_positions table -------------------------------------------


@pytest.mark.asyncio
async def test_traded_positions_table_created():
    """init_db should create the traded_positions table."""
    from infra.db import DbWriter
    writer = DbWriter(":memory:")
    await writer.init_db()
    async with writer._get_conn() as conn:
        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='traded_positions'"
        )
        row = await cursor.fetchone()
    await writer.close()
    assert row is not None
    assert row[0] == "traded_positions"


@pytest.mark.asyncio
async def test_enqueue_traded_position():
    """Should store a traded position via enqueue + flush."""
    from dataclasses import dataclass

    from infra.db import DbWriter

    @dataclass
    class _TradedPosition:
        market_id: str
        signal_type: str
        trade_id: str
        opened_at: str
        status: str

    writer = DbWriter(":memory:")
    await writer.init_db()
    pos = _TradedPosition(
        market_id="cond_abc",
        signal_type="BUY_YES",
        trade_id="trade_001",
        opened_at="2026-04-07T12:00:00",
        status="pending",
    )
    await writer.enqueue("traded_positions", pos)
    await writer.flush()
    async with writer._get_conn() as conn:
        cursor = await conn.execute("SELECT COUNT(*) FROM traded_positions")
        count = (await cursor.fetchone())[0]
    await writer.close()
    assert count == 1


@pytest.mark.asyncio
async def test_read_traded_positions_pending():
    """read_traded_positions should return only pending positions by default."""
    from infra.db import DbWriter
    writer = DbWriter(":memory:")
    await writer.init_db()

    async with writer._get_conn() as conn:
        await conn.execute(
            "INSERT INTO traded_positions (market_id, signal_type, trade_id, opened_at, status)"
            " VALUES (?, ?, ?, ?, ?)",
            ("m1", "BUY_YES", "t1", "2026-04-07T12:00:00", "pending"),
        )
        await conn.execute(
            "INSERT INTO traded_positions (market_id, signal_type, trade_id, opened_at, status)"
            " VALUES (?, ?, ?, ?, ?)",
            ("m2", "BUY_NO", "t2", "2026-04-07T12:00:00", "resolved"),
        )
        await conn.commit()

    rows = await writer.read_traded_positions(status="pending")
    assert len(rows) == 1
    assert rows[0]["market_id"] == "m1"
    assert rows[0]["status"] == "pending"

    all_rows = await writer.read_traded_positions(status="resolved")
    assert len(all_rows) == 1
    assert all_rows[0]["market_id"] == "m2"
    await writer.close()


@pytest.mark.asyncio
async def test_mark_position_resolved():
    """mark_position_resolved should update status from pending to resolved."""
    from infra.db import DbWriter
    writer = DbWriter(":memory:")
    await writer.init_db()

    async with writer._get_conn() as conn:
        await conn.execute(
            "INSERT INTO traded_positions (market_id, signal_type, trade_id, opened_at, status)"
            " VALUES (?, ?, ?, ?, ?)",
            ("m1", "BUY_YES", "t1", "2026-04-07T12:00:00", "pending"),
        )
        await conn.commit()

    await writer.mark_position_resolved("m1", "BUY_YES")

    rows = await writer.read_traded_positions(status="pending")
    assert len(rows) == 0

    resolved = await writer.read_traded_positions(status="resolved")
    assert len(resolved) == 1
    assert resolved[0]["market_id"] == "m1"
    assert resolved[0]["status"] == "resolved"
    await writer.close()


# ── Forecast log & Brier score tests (P7.5) ─────────────────────


@pytest.mark.asyncio
async def test_forecast_log_insert_and_read():
    """Should insert and read forecast log entries."""
    from infra.db import DbWriter
    writer = DbWriter(":memory:")
    await writer.init_db()
    await writer.enqueue("forecast_log", {
        "city": "New York",
        "target_date": "2026-04-10",
        "metric": "temp_high",
        "model": "gfs_seamless",
        "member_count": 31,
        "probability": 0.8,
        "actual_outcome": None,
        "brier_score": None,
        "market_id": "cond123",
        "logged_at": "2026-04-09T12:00:00",
    })
    await writer.flush()
    rows = await writer.read_forecast_log()
    assert len(rows) == 1
    assert rows[0]["city"] == "New York"
    assert rows[0]["model"] == "gfs_seamless"
    assert rows[0]["probability"] == 0.8
    assert rows[0]["actual_outcome"] is None
    await writer.close()


@pytest.mark.asyncio
async def test_resolve_forecast_computes_brier():
    """resolve_forecast should set actual_outcome and brier_score."""
    from infra.db import DbWriter
    writer = DbWriter(":memory:")
    await writer.init_db()
    await writer.enqueue("forecast_log", {
        "city": "Chicago",
        "target_date": "2026-04-10",
        "metric": "temp_high",
        "model": "ecmwf_ifs025",
        "member_count": 51,
        "probability": 0.9,
        "actual_outcome": None,
        "brier_score": None,
        "market_id": "cond456",
        "logged_at": "2026-04-09T12:00:00",
    })
    await writer.flush()
    await writer.resolve_forecast("cond456", 1)
    rows = await writer.read_forecast_log()
    assert rows[0]["actual_outcome"] == 1
    assert rows[0]["brier_score"] == pytest.approx(0.01)  # (0.9 - 1)^2
    await writer.close()


@pytest.mark.asyncio
async def test_brier_score_perfect():
    """Brier score of perfect prediction = 0.0."""
    from infra.db import DbWriter
    writer = DbWriter(":memory:")
    await writer.init_db()
    await writer.enqueue("forecast_log", {
        "city": "Miami",
        "target_date": "2026-04-10",
        "metric": "temp_high",
        "model": "gfs_seamless",
        "member_count": 31,
        "probability": 1.0,
        "actual_outcome": None,
        "brier_score": None,
        "market_id": "cond789",
        "logged_at": "2026-04-09T12:00:00",
    })
    await writer.flush()
    await writer.resolve_forecast("cond789", 1)
    scores = await writer.get_brier_scores()
    assert scores["gfs_seamless"] == pytest.approx(0.0)
    await writer.close()


@pytest.mark.asyncio
async def test_brier_scores_per_model():
    """get_brier_scores returns per-model averages."""
    from infra.db import DbWriter
    writer = DbWriter(":memory:")
    await writer.init_db()
    for model, prob in [("gfs_seamless", 0.8), ("ecmwf_ifs025", 0.95)]:
        await writer.enqueue("forecast_log", {
            "city": "Boston",
            "target_date": "2026-04-10",
            "metric": "temp_high",
            "model": model,
            "member_count": 31,
            "probability": prob,
            "actual_outcome": None,
            "brier_score": None,
            "market_id": "condABC",
            "logged_at": "2026-04-09T12:00:00",
        })
    await writer.flush()
    await writer.resolve_forecast("condABC", 1)
    scores = await writer.get_brier_scores()
    assert scores["gfs_seamless"] == pytest.approx(0.04)     # (0.8-1)^2
    assert scores["ecmwf_ifs025"] == pytest.approx(0.0025)   # (0.95-1)^2
    assert scores["ecmwf_ifs025"] < scores["gfs_seamless"]   # ECMWF better
    await writer.close()
