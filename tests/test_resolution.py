"""Tests for event-based trade resolution (P5.2.2).

Covers: market closed YES/NO wins, BUY_NO price inversion,
pending stays when market open, 7-day force-resolve,
resolution_source tracking, and calibration logging.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from infra.types import (
    MarketResolution,
    SignalType,
    WeatherPaperTrade,
    WeatherSignal,
)
from simulator.paper_trader import WeatherPaperTrader


# ── Helpers ──────────────────────────────────────────────────────────


def _make_signal(
    signal_type: SignalType = SignalType.BUY_YES,
    confidence: str = "medium",
) -> WeatherSignal:
    return WeatherSignal(
        market_question="Will NYC high exceed 90F?",
        market_id="cond_abc",
        token_id="tok_yes",
        signal_type=signal_type,
        forecast_probability=0.72,
        market_price=0.55,
        edge=0.17,
        location="New York",
        forecast_date="2026-04-10",
        weather_metric="temp_high",
        threshold_value=90.0,
        timestamp=datetime.now(UTC),
        confidence=confidence,
    )


def _make_trade(
    signal_type: SignalType = SignalType.BUY_YES,
    entry_price: float = 0.55,
    size_usdc: float = 10.0,
    opened_at: datetime | None = None,
) -> WeatherPaperTrade:
    return WeatherPaperTrade(
        trade_id="paper_test123",
        market_question="Will NYC high exceed 90F?",
        market_id="cond_abc",
        token_id="tok_yes",
        signal_type=signal_type,
        size_usdc=size_usdc,
        entry_price=entry_price,
        exit_price=None,
        fees=size_usdc * 0.01,  # 1% entry fee
        pnl=None,
        status="pending",
        opened_at=opened_at or datetime.now(UTC),
        resolved_at=None,
        resolution_source="",
    )


def _build_trader() -> tuple[WeatherPaperTrader, dict]:
    """Build a paper trader with mock dependencies, return (trader, mocks)."""
    mocks = {
        "price_fetcher": MagicMock(),
        "db_writer": MagicMock(),
        "telegram": MagicMock(),
        "risk_manager": MagicMock(),
        "reconciler": MagicMock(),
        "market_indexer": MagicMock(),
        "dedup": set(),
    }

    # Async mocks
    mocks["db_writer"].enqueue = AsyncMock()
    mocks["db_writer"].mark_position_resolved = AsyncMock()
    mocks["telegram"].send = AsyncMock()
    mocks["telegram"].alert_resolution = AsyncMock()
    mocks["market_indexer"].check_resolution = AsyncMock(return_value=None)

    # Risk manager
    mocks["risk_manager"].release_exposure = MagicMock()
    mocks["risk_manager"].update_bankroll = MagicMock()
    mocks["risk_manager"].add_exposure = MagicMock()

    # Reconciler
    mocks["reconciler"].analyze = MagicMock()

    trader = WeatherPaperTrader(
        price_fetcher=mocks["price_fetcher"],
        db_writer=mocks["db_writer"],
        telegram=mocks["telegram"],
        risk_manager=mocks["risk_manager"],
        reconciler=mocks["reconciler"],
        taker_fee_pct=0.01,
    )
    trader.set_market_indexer(mocks["market_indexer"])
    trader.set_dedup(mocks["dedup"])

    return trader, mocks


# ── Tests ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_with_market_closed_yes_wins():
    """BUY_YES + resolution_price=1.0 should produce a winning trade."""
    trader, mocks = _build_trader()
    trade = _make_trade(signal_type=SignalType.BUY_YES, entry_price=0.55)
    signal = _make_signal(signal_type=SignalType.BUY_YES)
    now = datetime.now(UTC)

    resolution = MarketResolution(
        source="gamma_closed",
        resolution_price=1.0,
        resolved_at=now,
    )
    mocks["market_indexer"].check_resolution.return_value = resolution

    # Add trade to pending
    trader._pending.append((trade, signal, trade.opened_at))
    await trader.check_pending()

    # Trade should be resolved
    assert trader.pending_count == 0
    assert mocks["db_writer"].enqueue.call_count >= 1

    # Check the resolved trade written to DB
    paper_call = None
    for call in mocks["db_writer"].enqueue.call_args_list:
        if call[0][0] == "paper_trades":
            paper_call = call
    assert paper_call is not None
    resolved_trade = paper_call[0][1]
    assert resolved_trade.exit_price == 1.0
    assert resolved_trade.pnl is not None
    assert resolved_trade.pnl > 0  # Winning trade
    assert resolved_trade.status == "resolved"


@pytest.mark.asyncio
async def test_resolve_with_market_closed_yes_loses():
    """BUY_YES + resolution_price=0.0 should produce a losing trade."""
    trader, mocks = _build_trader()
    trade = _make_trade(signal_type=SignalType.BUY_YES, entry_price=0.55)
    signal = _make_signal(signal_type=SignalType.BUY_YES)
    now = datetime.now(UTC)

    resolution = MarketResolution(
        source="gamma_closed",
        resolution_price=0.0,
        resolved_at=now,
    )
    mocks["market_indexer"].check_resolution.return_value = resolution

    trader._pending.append((trade, signal, trade.opened_at))
    await trader.check_pending()

    assert trader.pending_count == 0

    paper_call = None
    for call in mocks["db_writer"].enqueue.call_args_list:
        if call[0][0] == "paper_trades":
            paper_call = call
    assert paper_call is not None
    resolved_trade = paper_call[0][1]
    assert resolved_trade.exit_price == 0.0
    assert resolved_trade.pnl is not None
    assert resolved_trade.pnl < 0  # Losing trade


@pytest.mark.asyncio
async def test_resolve_buy_no_inverts_price():
    """BUY_NO + resolution_price=0.0 should invert to exit_price=1.0 (win)."""
    trader, mocks = _build_trader()
    trade = _make_trade(signal_type=SignalType.BUY_NO, entry_price=0.45)
    signal = _make_signal(signal_type=SignalType.BUY_NO)
    now = datetime.now(UTC)

    # NO wins: YES resolution_price=0.0, so NO token exit = 1.0 - 0.0 = 1.0
    resolution = MarketResolution(
        source="gamma_closed",
        resolution_price=0.0,
        resolved_at=now,
    )
    mocks["market_indexer"].check_resolution.return_value = resolution

    trader._pending.append((trade, signal, trade.opened_at))
    await trader.check_pending()

    assert trader.pending_count == 0

    paper_call = None
    for call in mocks["db_writer"].enqueue.call_args_list:
        if call[0][0] == "paper_trades":
            paper_call = call
    assert paper_call is not None
    resolved_trade = paper_call[0][1]
    assert resolved_trade.exit_price == 1.0  # Inverted from 0.0
    assert resolved_trade.pnl is not None
    assert resolved_trade.pnl > 0  # BUY_NO wins when YES goes to 0


@pytest.mark.asyncio
async def test_pending_trade_stays_when_market_open():
    """When check_resolution returns None, trade should stay pending."""
    trader, mocks = _build_trader()
    trade = _make_trade()
    signal = _make_signal()

    # Market still open — check_resolution returns None
    mocks["market_indexer"].check_resolution.return_value = None

    trader._pending.append((trade, signal, trade.opened_at))
    await trader.check_pending()

    assert trader.pending_count == 1
    assert mocks["db_writer"].enqueue.call_count == 0


@pytest.mark.asyncio
async def test_force_resolve_after_7_days():
    """>7 days pending with no resolution should trigger force-resolve."""
    trader, mocks = _build_trader()
    opened_at = datetime.now(UTC) - timedelta(days=8)
    trade = _make_trade(opened_at=opened_at)
    signal = _make_signal()

    # Market still open after 8 days
    mocks["market_indexer"].check_resolution.return_value = None

    trader._pending.append((trade, signal, opened_at))
    await trader.check_pending()

    assert trader.pending_count == 0

    # Check the force-resolved trade
    paper_call = None
    for call in mocks["db_writer"].enqueue.call_args_list:
        if call[0][0] == "paper_trades":
            paper_call = call
    assert paper_call is not None
    resolved_trade = paper_call[0][1]
    assert resolved_trade.status == "force_resolved"
    assert resolved_trade.resolution_source == "force_resolved"
    assert resolved_trade.pnl is not None
    assert resolved_trade.pnl < 0  # Loses fees
    # Dedup should be cleaned up
    assert "cond_abc" not in mocks["dedup"]
    mocks["db_writer"].mark_position_resolved.assert_called_once_with("cond_abc")


@pytest.mark.asyncio
async def test_resolution_source_recorded():
    """Resolved trade should have correct resolution_source from MarketResolution."""
    trader, mocks = _build_trader()
    trade = _make_trade()
    signal = _make_signal()
    now = datetime.now(UTC)

    resolution = MarketResolution(
        source="gamma_closed",
        resolution_price=1.0,
        resolved_at=now,
    )
    mocks["market_indexer"].check_resolution.return_value = resolution

    trader._pending.append((trade, signal, trade.opened_at))
    await trader.check_pending()

    paper_call = None
    for call in mocks["db_writer"].enqueue.call_args_list:
        if call[0][0] == "paper_trades":
            paper_call = call
    assert paper_call is not None
    resolved_trade = paper_call[0][1]
    assert resolved_trade.resolution_source == "gamma_closed"


@pytest.mark.asyncio
async def test_resolve_trade_records_calibration():
    """Resolution should write a CalibrationRecord with correct confidence mapping."""
    trader, mocks = _build_trader()
    trade = _make_trade()
    signal = _make_signal(confidence="high")
    now = datetime.now(UTC)

    resolution = MarketResolution(
        source="gamma_closed",
        resolution_price=1.0,
        resolved_at=now,
    )
    mocks["market_indexer"].check_resolution.return_value = resolution

    trader._pending.append((trade, signal, trade.opened_at))
    await trader.check_pending()

    # Find calibration_log enqueue call
    cal_call = None
    for call in mocks["db_writer"].enqueue.call_args_list:
        if call[0][0] == "calibration_log":
            cal_call = call
    assert cal_call is not None
    cal_record = cal_call[0][1]
    assert cal_record.confidence == 0.9  # "high" -> 0.9
    assert cal_record.trade_id == trade.trade_id
    assert cal_record.weather_metric == "temp_high"
    assert cal_record.location == "New York"
