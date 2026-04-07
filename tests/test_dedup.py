"""Tests for _TradeDedup in-memory deduplication."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock


@pytest.mark.asyncio
async def test_dedup_blocks_duplicate():
    """Adding a position should block the same (market_id, signal_type) pair."""
    from main import _TradeDedup

    dedup = _TradeDedup()
    dedup.add("market_1", "BUY_YES")

    assert dedup.is_active("market_1", "BUY_YES") is True


@pytest.mark.asyncio
async def test_dedup_allows_different_direction():
    """A BUY_YES position should NOT block BUY_NO on the same market."""
    from main import _TradeDedup

    dedup = _TradeDedup()
    dedup.add("market_1", "BUY_YES")

    assert dedup.is_active("market_1", "BUY_YES") is True
    assert dedup.is_active("market_1", "BUY_NO") is False


@pytest.mark.asyncio
async def test_dedup_remove_allows_reopen():
    """After removing a position, the same pair should be allowed again."""
    from main import _TradeDedup

    dedup = _TradeDedup()
    dedup.add("market_1", "BUY_YES")
    assert dedup.is_active("market_1", "BUY_YES") is True

    dedup.remove("market_1", "BUY_YES")
    assert dedup.is_active("market_1", "BUY_YES") is False


@pytest.mark.asyncio
async def test_dedup_hydrate_from_db():
    """Hydrate should load pending positions from the DB writer."""
    from main import _TradeDedup

    mock_db = AsyncMock()
    mock_db.read_traded_positions.return_value = [
        {"market_id": "m1", "signal_type": "BUY_YES"},
        {"market_id": "m2", "signal_type": "BUY_NO"},
    ]

    dedup = _TradeDedup()
    await dedup.hydrate(mock_db)

    mock_db.read_traded_positions.assert_awaited_once_with("pending")
    assert dedup.is_active("m1", "BUY_YES") is True
    assert dedup.is_active("m2", "BUY_NO") is True
    assert dedup.is_active("m3", "BUY_YES") is False
