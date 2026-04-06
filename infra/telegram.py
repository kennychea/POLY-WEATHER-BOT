"""Telegram bot for alerts — signals, trades, errors, heartbeat."""

from __future__ import annotations

import html
import logging

import aiohttp

from infra.types import TradeResult

logger = logging.getLogger(__name__)


class TelegramBot:
    """Sends alerts to a Telegram chat via Bot API."""

    def __init__(self, token: str, chat_id: str) -> None:
        self.token = token
        self.chat_id = chat_id
        self._base_url = f"https://api.telegram.org/bot{token}"
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create a reusable HTTP session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def send(self, text: str) -> None:
        """Send a text message to the configured chat."""
        # BUG 10: Truncate messages exceeding Telegram's 4096 char limit
        if len(text) > 4000:
            text = text[:4000] + "\n... (truncated)"

        url = f"{self._base_url}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
        }
        try:
            session = await self._get_session()
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    logger.error("Telegram API error: %s", resp.status)
                    return
                # BUG 7: Check the 'ok' field — HTTP 200 with ok=false is a failure
                data = await resp.json()
                if not data.get("ok"):
                    logger.error(
                        "Telegram API returned ok=false: %s",
                        data.get("description", "unknown"),
                    )
        except Exception:
            logger.exception("Telegram send failed")

    async def alert_trade_result(self, result: TradeResult) -> None:
        """Send a formatted trade result alert."""
        emoji = "W" if result.win else "L"
        mid = html.escape(result.market_id)
        msg = (
            f"<b>TRADE {emoji}</b>\n"
            f"Market: <code>{mid}</code>\n"
            f"Entry: {result.entry_price:.4f} -> Exit: {result.exit_price:.4f}\n"
            f"Size: ${result.size_usdc:.2f}\n"
            f"PnL: ${result.pnl:+.2f}"
        )
        await self.send(msg)

    async def alert_error(self, error_msg: str) -> None:
        """Send an error alert."""
        msg = f"<b>ERROR</b>\n{html.escape(error_msg)}"
        await self.send(msg)

    async def heartbeat(
        self,
        uptime_seconds: int,
        signals_count: int,
        win_rate: float,
        extra_metrics: dict[str, object] | None = None,
    ) -> None:
        """Send a heartbeat status message."""
        hours = uptime_seconds // 3600
        minutes = (uptime_seconds % 3600) // 60
        msg = (
            f"<b>HEARTBEAT</b>\n"
            f"Uptime: {hours}h {minutes}m\n"
            f"Signals: {signals_count}\n"
            f"Win rate: {win_rate * 100:.0f}%"
        )
        if extra_metrics:
            for key, val in extra_metrics.items():
                msg += f"\n{key}: {val}"
        await self.send(msg)
