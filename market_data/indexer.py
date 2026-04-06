"""Market Indexer — fetch and index active Polymarket markets.

Periodically fetches all active markets from the Gamma API,
stores them in memory, and provides keyword + semantic search.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import time
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

GAMMA_API_URL = "https://gamma-api.polymarket.com/markets"

_CATEGORIES: dict[str, list[str]] = {
    "crypto": [
        "bitcoin", "btc", "ethereum", "eth", "crypto",
        "defi", "stablecoin", "binance", "coinbase",
    ],
    "ai": [
        "artificial intelligence", "ai", "openai", "chatgpt",
        "deepmind", "anthropic", "llm",
    ],
    "politics_us": [
        "trump", "biden", "congress", "senate", "republican",
        "democrat", "election", "white house",
    ],
    "economy": [
        "fed", "interest rate", "inflation", "gdp",
        "recession", "tariff", "trade war",
    ],
    "tech": [
        "apple", "google", "meta", "microsoft",
        "tesla", "spacex", "nvidia",
    ],
    "conflict": [
        "war", "invasion", "missile", "nato",
        "military", "sanction",
    ],
    "science": [
        "nasa", "climate", "pandemic", "vaccine", "fda",
    ],
    "weather": [
        "temperature", "temp", "rain", "precipitation", "snow",
        "snowfall", "heat", "cold", "weather", "storm", "wind",
        "fahrenheit", "celsius", "degrees", "freeze", "blizzard",
    ],
}


def _safe_float(value: Any) -> float:
    """Convert a value to float, returning 0.0 on failure."""
    try:
        return float(value)
    except (ValueError, TypeError):
        return 0.0


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class MarketIndexer:
    """Indexes active Polymarket markets for keyword + semantic matching."""

    def __init__(
        self,
        refresh_interval: int = 300,
        min_volume: float = 200,
        min_liquidity: float = 100,
        max_volume: float = 2_000_000,
        openai_api_key: str = "",
        similarity_threshold: float = 0.45,
    ) -> None:
        self._refresh_interval = refresh_interval
        self._min_volume = min_volume
        self._min_liquidity = min_liquidity
        self._max_volume = max_volume
        self._openai_api_key = openai_api_key
        self._similarity_threshold = similarity_threshold
        self._markets: list[dict[str, Any]] = []
        self._embeddings: list[list[float]] = []
        self._openai_client: Any = None
        self._market_ids_hash: str = ""
        # BUG 11: Track last successful refresh for staleness detection
        self._last_refresh: float = 0.0
        self._staleness_threshold: float = 600.0  # 10 minutes

    @property
    def market_count(self) -> int:
        """Number of currently indexed markets."""
        return len(self._markets)

    @property
    def is_stale(self) -> bool:
        """True if last successful refresh was more than staleness_threshold ago."""
        if self._last_refresh == 0.0:
            return False  # Never refreshed yet, not stale
        return (time.monotonic() - self._last_refresh) > self._staleness_threshold

    async def refresh(self) -> None:
        """Fetch all active markets and filter by volume/liquidity."""
        raw_markets = await self._fetch_all_markets()
        filtered = []
        for m in raw_markets:
            if not isinstance(m, dict):
                continue
            if not m.get("acceptingOrders") and not m.get("accepting_orders"):
                continue
            volume = _safe_float(m.get("volume", m.get("volumeNum", "0")))
            liquidity = _safe_float(m.get("liquidity", m.get("liquidityNum", "0")))
            if volume < self._min_volume or liquidity < self._min_liquidity:
                continue
            if volume > self._max_volume:
                continue
            filtered.append(m)

        self._markets = filtered
        self._last_refresh = time.monotonic()
        logger.info("Indexed %d active markets", len(self._markets))

        # Compute embeddings only if market list changed
        new_hash = hashlib.md5(
            "|".join(
                sorted(m.get("conditionId", "") for m in filtered)
            ).encode()
        ).hexdigest()

        if new_hash == self._market_ids_hash and self._embeddings:
            logger.debug("Markets unchanged, skipping embedding recompute")
            return

        self._market_ids_hash = new_hash
        if self._openai_api_key and self._markets:
            self._embeddings = []
            try:
                await self._compute_embeddings()
            except Exception:
                logger.exception("Failed to compute market embeddings, using keyword fallback")

    async def _fetch_all_markets(
        self,
        *,
        page_limit: int = 500,
        max_pages: int = 10,
        max_retries: int = 2,
        base_delay: float = 0.5,
    ) -> list[dict[str, Any]]:
        """Fetch all markets from the Gamma API with pagination and retry."""
        all_markets: list[dict[str, Any]] = []
        offset = 0

        for _page in range(max_pages):
            page = await self._fetch_page(
                offset=offset,
                limit=page_limit,
                max_retries=max_retries,
                base_delay=base_delay,
            )
            all_markets.extend(page)
            if len(page) < page_limit:
                break
            offset += page_limit

        return all_markets

    async def _fetch_page(
        self,
        offset: int,
        limit: int,
        *,
        max_retries: int = 2,
        base_delay: float = 0.5,
    ) -> list[dict[str, Any]]:
        """Fetch a single page of markets with retry on transient errors."""
        timeout = aiohttp.ClientTimeout(total=15)
        for attempt in range(max_retries + 1):
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session, session.get(
                    GAMMA_API_URL,
                    params={
                        "closed": "false",
                        "limit": str(limit),
                        "offset": str(offset),
                    },
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
                    return data if isinstance(data, list) else []
            except Exception:
                if attempt == max_retries:
                    raise
                delay = base_delay * (2 ** attempt)
                logger.warning(
                    "Gamma API retry %d/%d after %.1fs",
                    attempt + 1,
                    max_retries,
                    delay,
                )
                await asyncio.sleep(delay)
        return []  # unreachable, satisfies type checker

    def match(self, keyword: str) -> list[dict[str, Any]]:
        """Find markets whose question contains at least half the keyword words."""
        words = keyword.lower().split()
        if not words:
            return []
        min_matches = max(1, len(words) // 2)
        results = []
        for m in self._markets:
            question = m.get("question", "").lower()
            score = sum(1 for w in words if w in question)
            if score >= min_matches:
                results.append(m)
        return results

    def best_match(self, keyword: str) -> dict[str, Any] | None:
        """Return the best matching market by word overlap then volume, or None."""
        words = keyword.lower().split()
        if not words:
            return None
        min_matches = max(1, len(words) // 2)
        scored: list[tuple[dict[str, Any], int]] = []
        for m in self._markets:
            question = m.get("question", "").lower()
            score = sum(1 for w in words if w in question)
            if score >= min_matches:
                scored.append((m, score))
        if not scored:
            return None
        # Sort by score desc, then volume desc
        return max(scored, key=lambda x: (x[1], _safe_float(x[0].get("volume", "0"))))[0]

    def category_match(self, query: str) -> dict[str, Any] | None:
        """Find best market by category keyword overlap, ranked by volume.

        Matches the query against predefined category keyword lists,
        then searches indexed markets for questions containing any
        keyword from the matched categories. Returns the highest-volume
        match, or None.
        """
        query_lower = query.lower()

        # Collect all category keywords that appear in the query
        matched_keywords: set[str] = set()
        for _cat, keywords in _CATEGORIES.items():
            for kw in keywords:
                if kw in query_lower:
                    matched_keywords.update(keywords)
                    break  # category matched, take all its keywords

        if not matched_keywords:
            return None

        # Search markets whose question contains any matched keyword
        candidates: list[dict[str, Any]] = []
        for m in self._markets:
            question_lower = m.get("question", "").lower()
            if any(kw in question_lower for kw in matched_keywords):
                candidates.append(m)

        if not candidates:
            return None

        return max(candidates, key=lambda m: _safe_float(m.get("volume", "0")))

    @staticmethod
    def extract_token_ids(market: dict[str, Any]) -> tuple[str, str] | None:
        """Extract (yes_token_id, no_token_id) from a market dict."""
        clob_ids = market.get("clobTokenIds", [])
        if isinstance(clob_ids, str):
            try:
                clob_ids = json.loads(clob_ids)
            except (json.JSONDecodeError, TypeError):
                clob_ids = []
        if isinstance(clob_ids, list) and len(clob_ids) >= 2:
            yes_id, no_id = str(clob_ids[0]), str(clob_ids[1])
            if yes_id and no_id:
                return (yes_id, no_id)
        return None

    def _get_openai_client(self) -> Any:
        """Lazy-init OpenAI client."""
        if self._openai_client is None:
            from openai import AsyncOpenAI
            self._openai_client = AsyncOpenAI(api_key=self._openai_api_key)
        return self._openai_client

    async def _get_embedding(self, text: str) -> list[float]:
        """Get embedding for a single text string."""
        client = self._get_openai_client()
        response = await client.embeddings.create(
            model="text-embedding-3-small",
            input=text,
        )
        return response.data[0].embedding

    async def _compute_embeddings(self) -> None:
        """Compute embeddings for all indexed market questions (batched)."""
        questions = [m.get("question", "") for m in self._markets]
        client = self._get_openai_client()
        all_embeddings: list[list[float]] = []
        batch_size = 2048  # OpenAI API limit

        for start in range(0, len(questions), batch_size):
            batch = questions[start : start + batch_size]
            response = await client.embeddings.create(
                model="text-embedding-3-small",
                input=batch,
            )
            all_embeddings.extend(item.embedding for item in response.data)

        self._embeddings = all_embeddings
        logger.info("Computed embeddings for %d markets", len(self._embeddings))

    async def semantic_best_match(self, query: str) -> dict[str, Any] | None:
        """Find best matching market using cosine similarity on embeddings.

        Falls back to keyword matching if embeddings are not available.
        """
        if not self._embeddings:
            return self.best_match(query)

        # Guard: embeddings must match markets count
        if len(self._embeddings) != len(self._markets):
            logger.warning(
                "Embeddings/markets desync (%d vs %d), falling back to keyword",
                len(self._embeddings), len(self._markets),
            )
            self._embeddings = []
            return self.best_match(query)

        try:
            query_emb = await self._get_embedding(query)
            if not any(query_emb):
                logger.warning("Query embedding is all-zero, falling back to keyword")
                return self.best_match(query)
        except Exception:
            logger.warning("Embedding query failed, falling back to keyword match")
            return self.best_match(query)

        best_sim = -1.0
        best_idx = -1
        for i, emb in enumerate(self._embeddings):
            sim = cosine_similarity(query_emb, emb)
            if sim > best_sim:
                best_sim = sim
                best_idx = i

        if best_sim < self._similarity_threshold:
            logger.info(
                "No semantic match above %.2f (best=%.2f) for: %s",
                self._similarity_threshold, best_sim, query[:60],
            )
            # Category fallback disabled — it produces irrelevant matches
            # (e.g. "Trump tariffs" → "Will 6 Fed rate cuts happen?").
            # A no_match is better than a bad match that wastes exposure.
            return None

        return self._markets[best_idx]

    async def close(self) -> None:
        """Close the OpenAI client if initialized."""
        if self._openai_client is not None:
            try:
                await self._openai_client.close()
            except Exception:
                logger.exception("Failed to close OpenAI client")
            self._openai_client = None

    async def run_refresh_loop(self) -> None:
        """Background loop that refreshes the market index periodically."""
        while True:
            try:
                await self.refresh()
            except Exception:
                logger.exception("Market refresh failed")
                # BUG 11: Alert if market data is stale
                if self.is_stale:
                    logger.critical(
                        "Market index stale! Last refresh %.0fs ago",
                        time.monotonic() - self._last_refresh,
                    )
            await asyncio.sleep(self._refresh_interval)
