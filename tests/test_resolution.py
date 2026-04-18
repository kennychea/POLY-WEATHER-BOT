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


class _FakeDedup:
    """Minimal stub matching _TradeDedup interface for tests."""

    def __init__(self) -> None:
        self._active: set[tuple[str, str]] = set()

    def add(self, market_id: str, signal_type: str) -> None:
        self._active.add((market_id, signal_type))

    def remove(self, market_id: str, signal_type: str) -> None:
        self._active.discard((market_id, signal_type))

    def __contains__(self, market_id: str) -> bool:
        return any(mid == market_id for mid, _ in self._active)


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
        "dedup": _FakeDedup(),
    }

    # Async mocks
    mocks["db_writer"].enqueue = AsyncMock()
    mocks["db_writer"].mark_position_resolved = AsyncMock()
    mocks["db_writer"].resolve_forecast = AsyncMock()  # Phase 12.A.4
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
    mocks["db_writer"].mark_position_resolved.assert_called_once_with("cond_abc", "buy_yes")


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
async def test_shadow_trade_resolves_without_bankroll_impact():
    """P10.1 revisit: a shadow trade must resolve with correct pnl AND leave
    risk_manager bankroll totally untouched (zero calls to update_bankroll,
    add_exposure, or release_exposure; zero reconciler.analyze calls).
    """
    trader, mocks = _build_trader()
    signal = _make_signal(signal_type=SignalType.BUY_YES)
    now = datetime.now(UTC)

    # Open a shadow trade via the new API
    shadow = await trader.open_shadow_trade(signal, size_usdc=10.0)
    assert shadow is not None
    assert trader.pending_count == 0, "shadow trades must not count in pending_count"
    assert mocks["risk_manager"].add_exposure.call_count == 0

    # Simulate market resolving in the YES direction (win for BUY_YES)
    resolution = MarketResolution(
        source="gamma_closed",
        resolution_price=1.0,
        resolved_at=now,
    )
    mocks["market_indexer"].check_resolution.return_value = resolution
    await trader.check_pending()

    # Shadow trade should be resolved with a positive PnL row in shadow_trades
    shadow_calls = [
        call for call in mocks["db_writer"].enqueue.call_args_list
        if call[0][0] == "shadow_trades"
    ]
    # At least one insert for open + one update-style insert for resolve
    assert len(shadow_calls) >= 2, f"expected >=2 shadow_trades writes, got {len(shadow_calls)}"
    resolved_row = shadow_calls[-1][0][1]
    # Accept dict or dataclass-shaped payloads
    status = resolved_row["status"] if isinstance(resolved_row, dict) else resolved_row.status
    pnl = resolved_row["pnl_usd"] if isinstance(resolved_row, dict) else resolved_row.pnl_usd
    assert status == "resolved"
    assert pnl is not None and pnl > 0, f"BUY_YES @ 0.55 resolving to 1.0 should win, pnl={pnl}"

    # CRITICAL: no bankroll side-effects
    mocks["risk_manager"].add_exposure.assert_not_called()
    mocks["risk_manager"].release_exposure.assert_not_called()
    mocks["risk_manager"].update_bankroll.assert_not_called()
    mocks["reconciler"].analyze.assert_not_called()


@pytest.mark.asyncio
async def test_bankroll_isolated_from_shadow_loss():
    """A losing shadow trade of -$9.82 must NOT reduce bankroll by one cent."""
    trader, mocks = _build_trader()
    signal = _make_signal(signal_type=SignalType.BUY_YES)
    now = datetime.now(UTC)

    await trader.open_shadow_trade(signal, size_usdc=10.0)

    # Market resolves to 0.0 — BUY_YES @ 0.55 goes to zero (full loss minus fees)
    resolution = MarketResolution(
        source="gamma_closed",
        resolution_price=0.0,
        resolved_at=now,
    )
    mocks["market_indexer"].check_resolution.return_value = resolution
    await trader.check_pending()

    # Verify at least one resolved shadow_trades row with negative PnL
    shadow_calls = [
        call for call in mocks["db_writer"].enqueue.call_args_list
        if call[0][0] == "shadow_trades"
    ]
    resolved_row = shadow_calls[-1][0][1]
    pnl = resolved_row["pnl_usd"] if isinstance(resolved_row, dict) else resolved_row.pnl_usd
    assert pnl is not None and pnl < 0

    # CRITICAL: no bankroll impact whatsoever
    mocks["risk_manager"].update_bankroll.assert_not_called()
    mocks["risk_manager"].release_exposure.assert_not_called()
    mocks["risk_manager"].add_exposure.assert_not_called()


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


@pytest.mark.asyncio
async def test_open_trade_persists_calibration_metadata():
    """Phase 12.A.3: open_trade must accept and persist metadata dict.

    forecast_probability, ensemble_std, regime, horizon_hours are written
    to the WeatherPaperTrade row so Phase D can run reliability analysis.
    """
    trader, mocks = _build_trader()
    signal = _make_signal()
    metadata = {
        "forecast_probability": 0.65,
        "ensemble_std": 0.12,
        "regime": "clob_full",
        "horizon_hours": 24,
    }

    trade = await trader.open_trade(signal, size_usdc=10.0, metadata=metadata)

    assert trade is not None
    assert trade.forecast_probability == 0.65
    assert trade.ensemble_std == 0.12
    assert trade.regime == "clob_full"
    assert trade.horizon_hours == 24

    # Verify the row enqueued to DB has the metadata too
    paper_call = None
    for call in mocks["db_writer"].enqueue.call_args_list:
        if call[0][0] == "paper_trades":
            paper_call = call
    assert paper_call is not None
    enqueued_trade = paper_call[0][1]
    assert enqueued_trade.forecast_probability == 0.65
    assert enqueued_trade.regime == "clob_full"


@pytest.mark.asyncio
async def test_open_trade_without_metadata_defaults_to_none():
    """Phase 12.A.3: metadata kwarg is optional. When absent, all 4 fields
    remain None (backward compat with callers that haven't been updated).
    """
    trader, mocks = _build_trader()
    signal = _make_signal()

    trade = await trader.open_trade(signal, size_usdc=10.0)

    assert trade is not None
    assert trade.forecast_probability is None
    assert trade.ensemble_std is None
    assert trade.regime is None
    assert trade.horizon_hours is None


@pytest.mark.asyncio
async def test_resolution_preserves_calibration_metadata():
    """Phase 12.A.4: resolved trades must preserve forecast_probability/std/
    regime/horizon from the original open so the final paper_trades row is
    usable for calibration analysis.
    """
    trader, mocks = _build_trader()
    now = datetime.now(UTC)
    # Trade opened with calibration metadata
    trade = WeatherPaperTrade(
        trade_id="paper_metadata1",
        market_question="Will NYC high exceed 90F?",
        market_id="cond_abc",
        token_id="tok_yes",
        signal_type=SignalType.BUY_YES,
        size_usdc=10.0,
        entry_price=0.55,
        exit_price=None,
        fees=0.10,
        pnl=None,
        status="pending",
        opened_at=now - timedelta(hours=1),
        resolved_at=None,
        resolution_source="",
        forecast_probability=0.72,
        ensemble_std=0.08,
        regime="clob_full",
        horizon_hours=24,
    )
    signal = _make_signal(signal_type=SignalType.BUY_YES)
    resolution = MarketResolution(
        source="gamma_closed",
        resolution_price=1.0,
        resolved_at=now,
    )
    mocks["market_indexer"].check_resolution.return_value = resolution
    trader._pending.append((trade, signal, trade.opened_at))
    await trader.check_pending()

    # Grab the resolved trade enqueued to DB
    resolved_trade = None
    for call in mocks["db_writer"].enqueue.call_args_list:
        if call[0][0] == "paper_trades" and call[0][1].status == "resolved":
            resolved_trade = call[0][1]
    assert resolved_trade is not None
    assert resolved_trade.forecast_probability == 0.72
    assert resolved_trade.ensemble_std == 0.08
    assert resolved_trade.regime == "clob_full"
    assert resolved_trade.horizon_hours == 24


@pytest.mark.asyncio
async def test_resolution_calls_resolve_forecast():
    """Phase 12.A.4: on trade resolution, db_writer.resolve_forecast(market_id,
    actual_outcome) must be called so forecast_log.actual_outcome and brier_score
    get populated.

    actual_outcome = 1 if YES resolved true (resolution_price == 1.0), else 0.
    """
    trader, mocks = _build_trader()
    mocks["db_writer"].resolve_forecast = AsyncMock()
    now = datetime.now(UTC)
    trade = _make_trade(signal_type=SignalType.BUY_YES)
    signal = _make_signal(signal_type=SignalType.BUY_YES)
    resolution = MarketResolution(
        source="gamma_closed",
        resolution_price=1.0,  # YES resolved true
        resolved_at=now,
    )
    mocks["market_indexer"].check_resolution.return_value = resolution
    trader._pending.append((trade, signal, trade.opened_at))
    await trader.check_pending()

    mocks["db_writer"].resolve_forecast.assert_awaited_once_with(
        trade.market_id, 1
    )


@pytest.mark.asyncio
async def test_resolve_forecast_outcome_zero_when_yes_resolves_false():
    """YES resolves false (resolution_price=0.0) → actual_outcome=0 regardless
    of trade side. forecast_log tracks P(YES) so the outcome is YES-centric.
    """
    trader, mocks = _build_trader()
    mocks["db_writer"].resolve_forecast = AsyncMock()
    now = datetime.now(UTC)
    trade = _make_trade(signal_type=SignalType.BUY_NO)  # BUY_NO wins if YES=0
    signal = _make_signal(signal_type=SignalType.BUY_NO)
    resolution = MarketResolution(
        source="gamma_closed",
        resolution_price=0.0,  # YES resolved false
        resolved_at=now,
    )
    mocks["market_indexer"].check_resolution.return_value = resolution
    trader._pending.append((trade, signal, trade.opened_at))
    await trader.check_pending()

    mocks["db_writer"].resolve_forecast.assert_awaited_once_with(
        trade.market_id, 0
    )
