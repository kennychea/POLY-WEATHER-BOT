"""Tests for infra/heartbeat.py — health check loop."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_heartbeat_sends_status():
    """Should send a heartbeat via Telegram."""
    from infra.heartbeat import Heartbeat
    mock_telegram = MagicMock()
    mock_telegram.heartbeat = AsyncMock()
    hb = Heartbeat(telegram=mock_telegram, interval_seconds=60)
    await hb.send_heartbeat(signals_count=5, win_rate=0.80)
    mock_telegram.heartbeat.assert_called_once()
    call_kwargs = mock_telegram.heartbeat.call_args[1]
    assert call_kwargs["signals_count"] == 5
    assert call_kwargs["win_rate"] == 0.80


@pytest.mark.asyncio
async def test_heartbeat_tracks_uptime():
    """Should track uptime in seconds."""
    from infra.heartbeat import Heartbeat
    mock_telegram = MagicMock()
    mock_telegram.heartbeat = AsyncMock()
    hb = Heartbeat(telegram=mock_telegram, interval_seconds=60)
    await asyncio.sleep(0.05)
    uptime = hb.get_uptime_seconds()
    assert uptime >= 0.04
    assert uptime < 5


@pytest.mark.asyncio
async def test_heartbeat_run_loop_sends_periodically():
    """Should send heartbeats in a loop."""
    from infra.heartbeat import Heartbeat
    mock_telegram = MagicMock()
    mock_telegram.heartbeat = AsyncMock()
    hb = Heartbeat(telegram=mock_telegram, interval_seconds=0.05)
    task = asyncio.create_task(hb.run(signals_count_fn=lambda: 3, win_rate_fn=lambda: 0.75))
    await asyncio.sleep(0.15)
    hb.stop()
    await task
    assert mock_telegram.heartbeat.call_count >= 2


@pytest.mark.asyncio
async def test_heartbeat_stop():
    """Should stop cleanly."""
    from infra.heartbeat import Heartbeat
    mock_telegram = MagicMock()
    mock_telegram.heartbeat = AsyncMock()
    hb = Heartbeat(telegram=mock_telegram, interval_seconds=0.05)
    task = asyncio.create_task(hb.run(signals_count_fn=lambda: 0, win_rate_fn=lambda: 0.0))
    await asyncio.sleep(0.05)
    hb.stop()
    await task
    assert not hb._running


def test_heartbeat_creation():
    """Should create a Heartbeat with telegram and interval."""
    from infra.heartbeat import Heartbeat
    mock_telegram = MagicMock()
    hb = Heartbeat(telegram=mock_telegram, interval_seconds=300)
    assert hb._interval == 300
    assert hb._running is True


@pytest.mark.asyncio
async def test_heartbeat_survives_callback_error():
    """Heartbeat loop should continue if callback raises."""
    from infra.heartbeat import Heartbeat
    mock_telegram = MagicMock()
    call_count = 0

    async def failing_heartbeat(**kwargs):  # noqa: ARG001
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            msg = "Telegram down"
            raise ConnectionError(msg)

    mock_telegram.heartbeat = failing_heartbeat
    hb = Heartbeat(telegram=mock_telegram, interval_seconds=0.05)
    task = asyncio.create_task(
        hb.run(signals_count_fn=lambda: 0, win_rate_fn=lambda: 0.0),
    )
    await asyncio.sleep(0.2)
    hb.stop()
    await task
    # Should have survived the error and continued
    assert call_count >= 2
