"""Tests for core/reconciler.py — post-mortem analysis of trades."""

from datetime import UTC, datetime

import pytest

from infra.types import SignalType, TradeResult


@pytest.mark.asyncio
async def test_reconciler_creation():
    """Should create a Reconciler."""
    from core.reconciler import Reconciler
    rec = Reconciler()
    assert rec is not None


@pytest.mark.asyncio
async def test_reconciler_analyze_win():
    """Should analyze a winning trade."""
    from core.reconciler import Reconciler
    rec = Reconciler()
    result = TradeResult(
        market_id="0xabc",
        signal_type=SignalType.BUY_YES,
        entry_price=0.60,
        exit_price=1.0,
        size_usdc=10.0,
        pnl=6.67,
        win=True,
        timestamp=datetime.now(UTC),
    )
    analysis = rec.analyze(result)
    assert analysis["outcome"] == "win"
    assert analysis["return_pct"] > 0


@pytest.mark.asyncio
async def test_reconciler_analyze_loss():
    """Should analyze a losing trade."""
    from core.reconciler import Reconciler
    rec = Reconciler()
    result = TradeResult(
        market_id="0xabc",
        signal_type=SignalType.BUY_YES,
        entry_price=0.60,
        exit_price=0.0,
        size_usdc=10.0,
        pnl=-10.0,
        win=False,
        timestamp=datetime.now(UTC),
    )
    analysis = rec.analyze(result)
    assert analysis["outcome"] == "loss"
    assert analysis["return_pct"] < 0


@pytest.mark.asyncio
async def test_reconciler_cumulative_stats():
    """Should track cumulative statistics."""
    from core.reconciler import Reconciler
    rec = Reconciler()
    results = [
        TradeResult("0x1", SignalType.BUY_YES, 0.6, 1.0, 10, 6.67, True, datetime.now(UTC)),
        TradeResult("0x2", SignalType.BUY_YES, 0.6, 0.0, 10, -10.0, False, datetime.now(UTC)),
        TradeResult("0x3", SignalType.BUY_NO, 0.4, 1.0, 10, 15.0, True, datetime.now(UTC)),
    ]
    for r in results:
        rec.analyze(r)
    stats = rec.stats()
    assert stats["total_trades"] == 3
    assert stats["wins"] == 2
    assert stats["losses"] == 1
    assert stats["win_rate"] == pytest.approx(2 / 3, abs=0.01)
    assert stats["total_pnl"] == pytest.approx(11.67, abs=0.01)


@pytest.mark.asyncio
async def test_reconciler_stats_empty():
    """Stats should handle empty history."""
    from core.reconciler import Reconciler
    rec = Reconciler()
    stats = rec.stats()
    assert stats["total_trades"] == 0
    assert stats["win_rate"] == 0.0
    assert stats["total_pnl"] == 0.0


@pytest.mark.asyncio
async def test_reconciler_profit_factor():
    """Should calculate profit factor (gross wins / gross losses)."""
    from core.reconciler import Reconciler
    rec = Reconciler()
    results = [
        TradeResult("0x1", SignalType.BUY_YES, 0.6, 1.0, 10, 6.67, True, datetime.now(UTC)),
        TradeResult("0x2", SignalType.BUY_YES, 0.6, 0.0, 10, -10.0, False, datetime.now(UTC)),
    ]
    for r in results:
        rec.analyze(r)
    stats = rec.stats()
    # profit_factor = 6.67 / 10.0 = 0.667
    assert stats["profit_factor"] == pytest.approx(0.667, abs=0.01)
