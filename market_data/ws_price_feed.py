"""WebSocket price feed with HTTP fallback for Polymarket CLOB.

Maintains a live in-memory price cache via WS subscription.
Falls back to PriceFetcher HTTP when data is stale or missing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

import websockets

from market_data.price_fetcher import PriceFetcher

logger = logging.getLogger(__name__)

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# Reconnection backoff parameters
_BACKOFF_BASE: float = 1.0
_BACKOFF_CAP: float = 60.0


class WsPriceFeed:
    """WebSocket price feed with HTTP fallback for Polymarket CLOB."""

    def __init__(
        self,
        fallback_fetcher: PriceFetcher,
        staleness_seconds: float = 30.0,
    ) -> None:
        self._fallback = fallback_fetcher
        self._staleness = staleness_seconds
        self._prices: dict[str, dict[str, float]] = {}
        self._subscriptions: set[str] = set()
        self._ws: websockets.WebSocketClientProtocol | None = None  # type: ignore[name-defined]
        self._msg_count: int = 0

    # -- Public API ----------------------------------------------------------

    async def subscribe(self, token_id: str) -> None:
        """Add a token_id to the subscription set and resend if WS connected."""
        self._subscriptions.add(token_id)
        if self._ws is not None:
            try:
                await self._send_subscribe(self._ws)
            except Exception:
                logger.warning("Failed to resend WS subscription for %s", token_id)

    async def get_price(self, token_id: str) -> float | None:
        """Get best_ask from cache, fallback to HTTP if stale or missing."""
        entry = self._get_fresh(token_id)
        if entry is not None:
            return entry["best_ask"]
        return await self._fallback.get_price(token_id)

    async def get_mid_price(self, token_id: str) -> float | None:
        """Get mid from cache, fallback to HTTP if stale or missing."""
        entry = self._get_fresh(token_id)
        if entry is not None:
            return entry["mid"]
        return await self._fallback.get_mid_price(token_id)

    async def get_book(self, token_id: str) -> dict[str, float] | None:
        """Get full book from cache, fallback to HTTP if stale or missing."""
        entry = self._get_fresh(token_id)
        if entry is not None:
            return entry
        return await self._fallback.fetch_book(token_id)

    async def run(self) -> None:
        """Connect to WS, subscribe, process messages, auto-reconnect with backoff."""
        backoff = _BACKOFF_BASE
        while True:
            try:
                logger.info("WsPriceFeed connecting to %s", WS_URL)
                async with websockets.connect(WS_URL) as ws:
                    self._ws = ws
                    backoff = _BACKOFF_BASE  # Reset on successful connect
                    await self._send_subscribe(ws)
                    logger.info(
                        "WsPriceFeed subscribed to %d tokens", len(self._subscriptions)
                    )
                    async for raw_msg in ws:
                        try:
                            self._handle_message(raw_msg)
                            self._msg_count += 1
                            if self._msg_count % 100 == 0:
                                self._prune_stale_cache()
                        except Exception:
                            logger.exception("WsPriceFeed failed to parse message")
            except asyncio.CancelledError:
                logger.info("WsPriceFeed cancelled, shutting down")
                raise
            except Exception:
                logger.exception(
                    "WsPriceFeed connection error, reconnecting in %.1fs", backoff
                )
            self._ws = None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_CAP)

    async def close(self) -> None:
        """Close WS connection."""
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                logger.exception("WsPriceFeed error closing WS")
            finally:
                self._ws = None

    # -- Internal helpers ----------------------------------------------------

    def _get_fresh(self, token_id: str) -> dict[str, float] | None:
        """Return cached entry if fresh, else None."""
        entry = self._prices.get(token_id)
        if entry is None:
            return None
        age = time.monotonic() - entry["updated_at"]
        if age > self._staleness:
            logger.debug(
                "WsPriceFeed stale cache for %s (%.1fs old)", token_id, age
            )
            return None
        return entry

    def _prune_stale_cache(self) -> None:
        """Remove cache entries older than 10x staleness threshold."""
        max_age = self._staleness * 10
        now = time.monotonic()
        stale_keys = [
            k for k, v in self._prices.items()
            if now - v["updated_at"] > max_age
        ]
        for k in stale_keys:
            del self._prices[k]
        if stale_keys:
            logger.debug("Pruned %d stale cache entries", len(stale_keys))

    async def _send_subscribe(
        self, ws: websockets.WebSocketClientProtocol  # type: ignore[name-defined]
    ) -> None:
        """Send subscription message for all tracked token_ids."""
        if not self._subscriptions:
            return
        msg = json.dumps({
            "auth": {},
            "markets": [],
            "assets_ids": list(self._subscriptions),
            "type": "market",
        })
        await ws.send(msg)

    def _handle_message(self, raw_msg: str | bytes) -> None:
        """Parse a WS message and update the price cache."""
        if isinstance(raw_msg, bytes):
            raw_msg = raw_msg.decode("utf-8", errors="replace")

        try:
            data = json.loads(raw_msg)
        except (json.JSONDecodeError, ValueError):
            return  # Skip unparseable messages (heartbeats, empty frames)

        # WS may send arrays (batch updates) — process each item
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    self._handle_single(item)
            return

        if not isinstance(data, dict):
            return

        self._handle_single(data)

    def _handle_single(self, data: dict) -> None:
        """Process a single WS message dict."""
        # The Polymarket CLOB WS sends book updates per asset_id
        asset_id = data.get("asset_id")
        if not asset_id:
            return

        bids = data.get("bids", [])
        asks = data.get("asks", [])

        if not bids and not asks:
            return

        try:
            best_bid = float(bids[0]["price"]) if bids else 0.0
            best_ask = float(asks[0]["price"]) if asks else 0.0
        except (KeyError, TypeError, ValueError, IndexError):
            logger.warning("WsPriceFeed malformed book data for %s", asset_id)
            return

        if best_bid <= 0 and best_ask <= 0:
            return

        mid = (best_bid + best_ask) / 2.0 if best_bid > 0 and best_ask > 0 else 0.0
        spread = (best_ask - best_bid) if best_bid > 0 and best_ask > 0 else 0.0

        self._prices[asset_id] = {
            "best_bid": best_bid,
            "best_ask": best_ask,
            "mid": mid,
            "spread": spread,
            "updated_at": time.monotonic(),
        }
