"""Heartbeat — periodic health check via Telegram."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

from infra.telegram import TelegramBot

logger = logging.getLogger(__name__)


class Heartbeat:
    """Sends periodic heartbeat status messages via Telegram."""

    def __init__(self, telegram: TelegramBot, interval_seconds: float) -> None:
        self._telegram = telegram
        self._interval = interval_seconds
        self._running = True
        self._start_time = time.monotonic()

    def get_uptime_seconds(self) -> float:
        """Return seconds since the heartbeat was created."""
        return time.monotonic() - self._start_time

    async def send_heartbeat(
        self,
        signals_count: int,
        win_rate: float,
        extra_metrics: dict[str, Any] | None = None,
    ) -> None:
        """Send a single heartbeat message."""
        uptime = int(self.get_uptime_seconds())
        await self._telegram.heartbeat(
            uptime_seconds=uptime,
            signals_count=signals_count,
            win_rate=win_rate,
            extra_metrics=extra_metrics,
        )

    async def run(
        self,
        signals_count_fn: Callable[[], int],
        win_rate_fn: Callable[[], float],
        extra_metrics_fn: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        """Run the heartbeat loop until stopped."""
        while self._running:
            try:
                extra = extra_metrics_fn() if extra_metrics_fn else None
                await self.send_heartbeat(
                    signals_count=signals_count_fn(),
                    win_rate=win_rate_fn(),
                    extra_metrics=extra,
                )
            except Exception:
                logger.exception("Heartbeat failed")
            await asyncio.sleep(self._interval)

    def stop(self) -> None:
        """Stop the heartbeat loop."""
        self._running = False
