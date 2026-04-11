"""Price Fetcher — get orderbook snapshots from Polymarket CLOB API.

Fetches best bid/ask for a given token_id, used for T0 and T+30s price lookups.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

CLOB_BOOK_URL = "https://clob.polymarket.com/book"

# Reject orderbooks whose bid/ask spread exceeds this threshold. Polymarket
# weather books frequently contain placeholder MM orders (bid=0.01, ask=0.99)
# which would collapse to mid=0.5 and seed meaningless trade signals. Any real
# tradable book has a spread well under 20 cents — see 2026-04-11 clone-edge
# incident for the regression this guards against.
MAX_TRADEABLE_SPREAD = 0.20


class PriceFetcher:
    """Fetches orderbook snapshots from the Polymarket CLOB API."""

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create a reusable HTTP session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
            )
        return self._session

    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def fetch_book(self, token_id: str) -> dict[str, float] | None:
        """Fetch orderbook and return best_bid, best_ask, spread, mid.

        Returns None if the book is empty or the request fails.
        """
        try:
            data = await self._request_book(token_id)
        except Exception:
            logger.exception("Failed to fetch book for %s", token_id)
            return None

        return self._parse_book(data)

    async def _request_book(
        self,
        token_id: str,
        *,
        max_retries: int = 2,
        base_delay: float = 0.5,
    ) -> dict[str, Any]:
        """Make HTTP request to CLOB book endpoint with retry on transient errors."""
        session = await self._get_session()
        for attempt in range(max_retries + 1):
            try:
                async with session.get(
                    CLOB_BOOK_URL,
                    params={"token_id": token_id},
                ) as resp:
                    resp.raise_for_status()
                    return await resp.json()
            except Exception:
                if attempt == max_retries:
                    raise
                # Reset session on error (may be in broken state)
                if self._session and not self._session.closed:
                    await self._session.close()
                self._session = None
                session = await self._get_session()
                delay = base_delay * (2 ** attempt)
                logger.warning(
                    "CLOB API retry %d/%d after %.1fs",
                    attempt + 1,
                    max_retries,
                    delay,
                )
                await asyncio.sleep(delay)
        return {}  # unreachable, satisfies type checker

    @staticmethod
    def _parse_book(data: dict[str, Any]) -> dict[str, float] | None:
        """Parse orderbook response into best_bid, best_ask, spread, mid."""
        bids = data.get("bids", [])
        asks = data.get("asks", [])

        if not bids or not asks:
            return None

        try:
            best_bid = float(bids[0]["price"])
            best_ask = float(asks[0]["price"])
        except (KeyError, TypeError, ValueError, IndexError):
            return None

        if best_bid <= 0 or best_ask <= 0:
            return None

        spread = best_ask - best_bid
        mid = (best_bid + best_ask) / 2.0

        return {
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread": spread,
            "mid": mid,
        }

    async def get_price(self, token_id: str) -> float | None:
        """Get the best ask (taker buy price) for a token.

        Returns None if the book is empty or unavailable.
        """
        book = await self.fetch_book(token_id)
        if book is None:
            return None
        return book["best_ask"]

    async def get_mid_price(self, token_id: str) -> float | None:
        """Get the midpoint price for a token.

        Returns None if the book is empty, unavailable, or the spread exceeds
        MAX_TRADEABLE_SPREAD (stub/illiquid book — mid is meaningless).
        """
        book = await self.fetch_book(token_id)
        if book is None:
            return None
        spread = book["best_ask"] - book["best_bid"]
        if spread > MAX_TRADEABLE_SPREAD:
            logger.warning(
                "REJECT wide_spread token=%s spread=%.4f bid=%.4f ask=%.4f",
                token_id, spread, book["best_bid"], book["best_ask"],
            )
            return None
        return book["mid"]
