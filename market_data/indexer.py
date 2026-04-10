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
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp

from infra.types import MarketResolution

logger = logging.getLogger(__name__)

GAMMA_API_URL = "https://gamma-api.polymarket.com/markets"
GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"

# Cities with known Polymarket weather events (slug format)
_WEATHER_CITY_SLUGS: list[str] = [
    "nyc", "chicago", "miami", "los-angeles", "houston", "dallas",
    "london", "paris", "seoul", "shanghai", "hong-kong", "tokyo",
    "beijing", "madrid", "san-francisco", "denver", "seattle",
    "atlanta", "austin",
    # Discovered on Polymarket 2026-04-09
    "toronto", "buenos-aires", "wellington", "moscow", "mexico-city",
    "singapore", "jakarta", "kuala-lumpur", "sao-paulo", "amsterdam",
    "taipei", "istanbul",
    # Phase 10.C additions (2026-04-10)
    "tel-aviv", "panama-city", "warsaw", "munich", "helsinki",
    "lucknow", "ankara", "milan", "shenzhen", "chongqing",
    "wuhan", "busan", "chengdu",
]

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


# Lowercase question-text city alias → Polymarket event slug fragment
# Longer aliases first to prevent partial matches (e.g., "new york" before "york")
_QUESTION_CITY_TO_SLUG: dict[str, str] = {
    "new york city": "nyc", "san francisco": "san-francisco",
    "los angeles": "los-angeles", "washington dc": "washington-dc",
    "washington d.c.": "washington-dc", "hong kong": "hong-kong",
    "buenos aires": "buenos-aires", "mexico city": "mexico-city",
    "kuala lumpur": "kuala-lumpur", "sao paulo": "sao-paulo",
    "são paulo": "sao-paulo",
    # Phase 10.C multi-word aliases (long forms first so longer aliases win)
    "tel aviv": "tel-aviv", "panama city": "panama-city",
    "new york": "nyc", "nyc": "nyc",
    "chicago": "chicago", "miami": "miami", "houston": "houston",
    "phoenix": "phoenix", "denver": "denver", "seattle": "seattle",
    "boston": "boston", "atlanta": "atlanta", "dallas": "dallas",
    "minneapolis": "minneapolis", "detroit": "detroit", "austin": "austin",
    "london": "london", "paris": "paris", "madrid": "madrid",
    "berlin": "berlin", "rome": "rome", "tokyo": "tokyo",
    "seoul": "seoul", "shanghai": "shanghai", "beijing": "beijing",
    "sydney": "sydney", "mumbai": "mumbai", "toronto": "toronto",
    "wellington": "wellington", "moscow": "moscow", "singapore": "singapore",
    "jakarta": "jakarta", "amsterdam": "amsterdam", "taipei": "taipei",
    "istanbul": "istanbul",
    # Phase 10.C single-word additions
    "warsaw": "warsaw", "munich": "munich", "helsinki": "helsinki",
    "lucknow": "lucknow", "ankara": "ankara", "milan": "milan",
    "shenzhen": "shenzhen", "chongqing": "chongqing", "wuhan": "wuhan",
    "busan": "busan", "chengdu": "chengdu",
}

_MONTHS_MAP: dict[str, str] = {
    "january": "january", "february": "february", "march": "march",
    "april": "april", "may": "may", "june": "june", "july": "july",
    "august": "august", "september": "september", "october": "october",
    "november": "november", "december": "december",
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
        # Cache condition_ids where Gamma returns mismatched data,
        # so we don't spam the API with calls we know will fail.
        # Persistent across refresh cycles (migrated markets stay migrated).
        self._gamma_mismatch_ids: set[str] = set()

    @property
    def markets(self) -> list[dict[str, Any]]:
        """All currently indexed markets."""
        return self._markets

    @property
    def gamma_mismatch_ids(self) -> set[str]:
        """Condition IDs that Gamma returns mismatched data for."""
        return self._gamma_mismatch_ids

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

    async def _fetch_weather_events(self, forecast_days: int = 5) -> list[dict[str, Any]]:
        """Fetch weather event sub-markets by constructing known slug patterns."""
        today = datetime.now(timezone.utc)
        slugs: list[str] = []
        for day_offset in range(forecast_days):
            d = today + timedelta(days=day_offset)
            month_name = d.strftime("%B").lower()
            day, year = d.day, d.year
            for city in _WEATHER_CITY_SLUGS:
                slugs.append(f"highest-temperature-in-{city}-on-{month_name}-{day}-{year}")

        sem = asyncio.Semaphore(5)
        timeout = aiohttp.ClientTimeout(total=10)

        async def _fetch_one(slug: str) -> list[dict[str, Any]]:
            async with sem:
                try:
                    async with aiohttp.ClientSession(timeout=timeout) as session, session.get(
                        GAMMA_EVENTS_URL, params={"slug": slug},
                    ) as resp:
                        if resp.status != 200:
                            return []
                        data = await resp.json()
                        if data and isinstance(data, list):
                            return data[0].get("markets", [])
                except Exception:
                    return []
            return []

        results = await asyncio.gather(*[_fetch_one(s) for s in slugs], return_exceptions=True)
        all_markets: list[dict[str, Any]] = []
        for r in results:
            if isinstance(r, list):
                all_markets.extend(r)
        logger.info("Weather events: fetched %d sub-markets from %d slugs", len(all_markets), len(slugs))
        return all_markets

    async def refresh(self) -> None:
        """Fetch all active markets and filter by volume/liquidity."""
        # Don't clear mismatch IDs — they represent permanently migrated markets.
        # The check_resolution guard will still suppress repeat API calls.
        raw_markets = await self._fetch_all_markets()
        weather_markets = await self._fetch_weather_events()
        raw_markets.extend(weather_markets)
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

    async def check_resolution(
        self, condition_id: str, market_question: str = "",
    ) -> MarketResolution | None:
        """Check if a market has resolved, using cache then API then slug fallback.

        L1: Check cached self._markets for matching conditionId with closed=true.
        L2: Targeted Gamma API call with conditionId guard (Gamma is unreliable!).
        L3: Slug-based event lookup when condition_id is migrated (P10.2).
        """
        # L1: check cached markets
        for m in self._markets:
            if m.get("conditionId") == condition_id:
                if m.get("closed") is True or str(m.get("closed")).lower() == "true":
                    return self._extract_resolution(m, "gamma_cache_fallback")
                return None  # Found but not closed/settled

        # Skip API call if we already know Gamma returns wrong data for this id
        if condition_id in self._gamma_mismatch_ids:
            # P10.2: Try slug-based resolution for migrated markets
            if market_question:
                return await self._resolve_via_slug(market_question)
            return None

        # L2: targeted API call
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    GAMMA_API_URL, params={"condition_id": condition_id}
                ) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
                    if not isinstance(data, list) or not data:
                        return None
                    market = data[0]
                    # GUARD: verify conditionId matches (Gamma is unreliable!)
                    if market.get("conditionId") != condition_id:
                        self._gamma_mismatch_ids.add(condition_id)
                        logger.warning(
                            "Gamma condition_id mismatch (suppressing): asked %s, got %s",
                            condition_id,
                            market.get("conditionId"),
                        )
                        # Immediately try slug fallback instead of waiting
                        if market_question:
                            return await self._resolve_via_slug(market_question)
                        return None
                    if market.get("closed") is True or str(market.get("closed")).lower() == "true":
                        return self._extract_resolution(market, "gamma_resolved")
        except Exception:
            logger.warning("Resolution check failed for %s", condition_id)
        return None

    @staticmethod
    def _extract_resolution(market: dict, source: str) -> MarketResolution | None:
        """Extract resolution data from a Gamma market dict."""
        import json as _json

        prices_raw = market.get("outcomePrices")
        if not prices_raw:
            return None
        try:
            prices = _json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
            yes_price = float(prices[0])
            return MarketResolution(
                condition_id=market.get("conditionId", ""),
                resolution_price=yes_price,
                closed=True,
                source=source,
            )
        except (ValueError, IndexError, TypeError):
            return None

    @staticmethod
    def _build_slug_from_question(question: str) -> str | None:
        """Parse a weather market question to build the Polymarket event slug.

        Example: "Will the highest temperature in NYC be between 40-41°F on April 8?"
        → "highest-temperature-in-nyc-on-april-8-2026"
        """
        q_lower = question.lower()

        # Metric: highest or lowest
        if "lowest" in q_lower:
            metric = "lowest"
        elif "highest" in q_lower:
            metric = "highest"
        else:
            return None

        # City: match longest alias first (dict is ordered long→short)
        city_slug = None
        for alias, slug in _QUESTION_CITY_TO_SLUG.items():
            if alias in q_lower:
                city_slug = slug
                break
        if city_slug is None:
            return None

        # Date: extract month name + day
        month_name = None
        day = None
        for m_name in _MONTHS_MAP:
            if m_name in q_lower:
                day_match = re.search(rf"{m_name}\s+(\d{{1,2}})", q_lower)
                if day_match:
                    month_name = m_name
                    day = int(day_match.group(1))
                break
        if month_name is None or day is None:
            return None

        year = datetime.now(timezone.utc).year
        return f"{metric}-temperature-in-{city_slug}-on-{month_name}-{day}-{year}"

    async def _resolve_via_slug(self, market_question: str) -> MarketResolution | None:
        """Try to resolve a market by fetching its event via slug and matching by question.

        This handles the Gamma condition_id migration issue: after weather markets close,
        Polymarket assigns new condition_ids. The event slug remains stable, so we fetch
        the event, find the matching sub-market by question text, and read outcomePrices.
        """
        slug = self._build_slug_from_question(market_question)
        if slug is None:
            return None

        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    GAMMA_EVENTS_URL, params={"slug": slug},
                ) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
                    if not data or not isinstance(data, list):
                        return None

                    event = data[0]
                    markets = event.get("markets", [])

                    # Exact match by question text (normalized)
                    q_norm = market_question.strip().lower()
                    for m in markets:
                        mq = (m.get("question") or "").strip().lower()
                        if mq == q_norm:
                            if m.get("closed") is True or str(m.get("closed")).lower() == "true":
                                logger.info(
                                    "Slug resolution matched: %s → %s",
                                    slug, m.get("question", "")[:60],
                                )
                                return self._extract_resolution(m, "slug_resolved")
                            return None  # Found but not closed yet

                    # Fuzzy fallback: substring containment
                    for m in markets:
                        mq = (m.get("question") or "").strip().lower()
                        if q_norm in mq or mq in q_norm:
                            if m.get("closed") is True or str(m.get("closed")).lower() == "true":
                                logger.info(
                                    "Slug resolution fuzzy match: %s → %s",
                                    slug, m.get("question", "")[:60],
                                )
                                return self._extract_resolution(m, "slug_resolved")
                            return None
        except Exception:
            logger.warning("Slug-based resolution failed for: %s", market_question[:60])

        return None

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
