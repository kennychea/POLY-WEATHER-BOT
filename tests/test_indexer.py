"""Tests for market_data/indexer.py -- market resolution checking."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from infra.types import MarketResolution
from market_data.indexer import MarketIndexer


# -- Helpers ------------------------------------------------------------------


def _make_market(
    condition_id: str = "cond_abc",
    closed: bool = False,
    outcome_prices: str = '["1.0","0.0"]',
    question: str = "Will it rain?",
    volume: str = "5000",
    liquidity: str = "1000",
) -> dict:
    """Build a fake Gamma market dict."""
    return {
        "conditionId": condition_id,
        "closed": closed,
        "outcomePrices": outcome_prices,
        "question": question,
        "volume": volume,
        "liquidity": liquidity,
        "acceptingOrders": True,
    }


def _mock_aiohttp_session(response_data, status: int = 200):
    """Create a mock aiohttp.ClientSession context manager."""
    mock_resp = AsyncMock()
    mock_resp.status = status
    mock_resp.json = AsyncMock(return_value=response_data)

    mock_get = MagicMock(return_value=AsyncMock(
        __aenter__=AsyncMock(return_value=mock_resp),
        __aexit__=AsyncMock(return_value=False),
    ))

    mock_session = AsyncMock()
    mock_session.get = mock_get
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    return mock_session


# -- check_resolution: cache tests -------------------------------------------


@pytest.mark.asyncio
async def test_check_resolution_from_cache_closed():
    """Market in cache with closed=true should return MarketResolution."""
    indexer = MarketIndexer()
    indexer._markets = [
        _make_market(condition_id="cond_123", closed=True, outcome_prices='["1.0","0.0"]'),
    ]

    result = await indexer.check_resolution("cond_123")

    assert result is not None
    assert isinstance(result, MarketResolution)
    assert result.condition_id == "cond_123"
    assert result.resolution_price == 1.0
    assert result.closed is True
    assert result.source == "gamma_cache_fallback"


@pytest.mark.asyncio
async def test_check_resolution_from_cache_open():
    """Market in cache with closed=false and unsettled prices should return None."""
    indexer = MarketIndexer()
    indexer._markets = [
        _make_market(condition_id="cond_456", closed=False, outcome_prices='["0.5","0.5"]'),
    ]

    result = await indexer.check_resolution("cond_456")

    assert result is None


@pytest.mark.asyncio
async def test_near_settled_no_longer_resolves():
    """Market at extreme price (0.9995) but NOT closed should stay pending (P8.1 fix)."""
    indexer = MarketIndexer()
    indexer._markets = [
        _make_market(condition_id="cond_456", closed=False, outcome_prices='["0.9995","0.0005"]'),
    ]

    result = await indexer.check_resolution("cond_456")

    # P8.1: near-settled removed — only closed=true resolves
    assert result is None


@pytest.mark.asyncio
async def test_only_closed_true_resolves():
    """Only markets with closed=true should trigger resolution."""
    indexer = MarketIndexer()
    # Market at 0.01 (extreme low) but NOT closed
    indexer._markets = [
        _make_market(condition_id="cond_low", closed=False, outcome_prices='["0.005","0.995"]'),
    ]

    result = await indexer.check_resolution("cond_low")
    assert result is None

    # Now set closed=true — should resolve
    indexer._markets = [
        _make_market(condition_id="cond_low", closed=True, outcome_prices='["0.005","0.995"]'),
    ]

    result = await indexer.check_resolution("cond_low")
    assert result is not None
    assert result.resolution_price == 0.005
    assert result.source == "gamma_cache_fallback"


# -- check_resolution: API fallback -------------------------------------------


@pytest.mark.asyncio
async def test_check_resolution_api_fallback():
    """Market not in cache should trigger API call and return resolution."""
    indexer = MarketIndexer()
    indexer._markets = []  # Empty cache

    api_market = _make_market(
        condition_id="cond_789",
        closed=True,
        outcome_prices='["0.0","1.0"]',
    )

    mock_session = _mock_aiohttp_session([api_market], status=200)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = await indexer.check_resolution("cond_789")

    assert result is not None
    assert isinstance(result, MarketResolution)
    assert result.condition_id == "cond_789"
    assert result.resolution_price == 0.0
    assert result.source == "gamma_resolved"


@pytest.mark.asyncio
async def test_check_resolution_api_mismatch():
    """conditionId mismatch from Gamma API should return None."""
    indexer = MarketIndexer()
    indexer._markets = []  # Empty cache

    # API returns a market with a DIFFERENT conditionId
    api_market = _make_market(
        condition_id="cond_WRONG",
        closed=True,
        outcome_prices='["1.0","0.0"]',
    )

    mock_session = _mock_aiohttp_session([api_market], status=200)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = await indexer.check_resolution("cond_expected")

    assert result is None


# -- _extract_resolution ------------------------------------------------------


def test_extract_resolution_valid():
    """Should parse outcomePrices correctly from a market dict."""
    market = _make_market(
        condition_id="cond_extract",
        closed=True,
        outcome_prices='["0.75","0.25"]',
    )

    result = MarketIndexer._extract_resolution(market, "gamma_resolved")

    assert result is not None
    assert isinstance(result, MarketResolution)
    assert result.condition_id == "cond_extract"
    assert result.resolution_price == 0.75
    assert result.closed is True
    assert result.source == "gamma_resolved"


def test_extract_resolution_no_prices():
    """Should return None when outcomePrices is missing."""
    market = _make_market(condition_id="cond_no_prices")
    del market["outcomePrices"]

    result = MarketIndexer._extract_resolution(market, "gamma_resolved")

    assert result is None


# -- P10.2: _build_slug_from_question -----------------------------------------


class TestBuildSlugFromQuestion:
    """Test slug construction from weather market question text."""

    def test_bucket_fahrenheit_nyc(self):
        q = "Will the highest temperature in NYC be between 40-41°F on April 8?"
        slug = MarketIndexer._build_slug_from_question(q)
        assert slug is not None
        assert slug.startswith("highest-temperature-in-nyc-on-april-8-")

    def test_exact_celsius_madrid(self):
        q = "Will the highest temperature in Madrid be 27°C on April 5?"
        slug = MarketIndexer._build_slug_from_question(q)
        assert slug is not None
        assert "madrid" in slug
        assert "april-5-" in slug

    def test_lowest_temperature(self):
        q = "Will the lowest temperature in Chicago be 30°F or below on March 15?"
        slug = MarketIndexer._build_slug_from_question(q)
        assert slug is not None
        assert slug.startswith("lowest-temperature-in-chicago-on-march-15-")

    def test_multi_word_city(self):
        q = "Will the highest temperature in San Francisco be between 60-65°F on April 9?"
        slug = MarketIndexer._build_slug_from_question(q)
        assert slug is not None
        assert "san-francisco" in slug

    def test_nyc_alias(self):
        q = "Will the highest temperature in New York City be 74°F or higher on April 5?"
        slug = MarketIndexer._build_slug_from_question(q)
        assert slug is not None
        assert "nyc" in slug

    def test_unknown_city_returns_none(self):
        q = "Will the highest temperature in Narnia be 40°F on April 1?"
        slug = MarketIndexer._build_slug_from_question(q)
        assert slug is None

    def test_no_temperature_returns_none(self):
        q = "Will it rain in NYC on April 8?"
        slug = MarketIndexer._build_slug_from_question(q)
        assert slug is None

    def test_no_date_returns_none(self):
        q = "Will the highest temperature in NYC be 40°F?"
        slug = MarketIndexer._build_slug_from_question(q)
        assert slug is None

    def test_international_city_buenos_aires(self):
        q = "Will the highest temperature in Buenos Aires be between 28-29°C on April 10?"
        slug = MarketIndexer._build_slug_from_question(q)
        assert slug is not None
        assert "buenos-aires" in slug


# -- P10.2: check_resolution with slug fallback --------------------------------


@pytest.mark.asyncio
async def test_check_resolution_slug_fallback_on_mismatch():
    """When condition_id is in gamma_mismatch_ids, should try slug resolution."""
    indexer = MarketIndexer()
    indexer._markets = []
    indexer._gamma_mismatch_ids.add("cond_migrated")

    # Mock the Gamma events API to return a closed market
    question = "Will the highest temperature in NYC be between 40-41°F on April 8?"
    event_data = [{
        "markets": [
            _make_market(
                condition_id="cond_NEW",
                closed=True,
                outcome_prices='["1.0","0.0"]',
                question=question,
            ),
        ],
    }]

    mock_session = _mock_aiohttp_session(event_data, status=200)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = await indexer.check_resolution(
            "cond_migrated", market_question=question,
        )

    assert result is not None
    assert result.resolution_price == 1.0
    assert result.source == "slug_resolved"


@pytest.mark.asyncio
async def test_check_resolution_slug_fallback_not_closed():
    """Slug resolution should return None if the matched market isn't closed yet."""
    indexer = MarketIndexer()
    indexer._markets = []
    indexer._gamma_mismatch_ids.add("cond_migrated")

    question = "Will the highest temperature in NYC be between 40-41°F on April 8?"
    event_data = [{
        "markets": [
            _make_market(
                condition_id="cond_NEW",
                closed=False,
                outcome_prices='["0.5","0.5"]',
                question=question,
            ),
        ],
    }]

    mock_session = _mock_aiohttp_session(event_data, status=200)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = await indexer.check_resolution(
            "cond_migrated", market_question=question,
        )

    assert result is None


@pytest.mark.asyncio
async def test_check_resolution_slug_fallback_no_question():
    """Without market_question, mismatch should still return None (backward compat)."""
    indexer = MarketIndexer()
    indexer._markets = []
    indexer._gamma_mismatch_ids.add("cond_migrated")

    result = await indexer.check_resolution("cond_migrated")

    assert result is None


@pytest.mark.asyncio
async def test_check_resolution_api_mismatch_triggers_slug():
    """First-time Gamma mismatch should immediately try slug resolution."""
    indexer = MarketIndexer()
    indexer._markets = []

    question = "Will the highest temperature in Denver be between 50-55°F on April 7?"

    # L2 API returns wrong condition_id (mismatch)
    wrong_market = _make_market(condition_id="cond_WRONG", closed=True)

    # We need two separate session mocks: first for L2 API, second for slug
    call_count = {"n": 0}
    resolved_market = _make_market(
        condition_id="cond_NEW",
        closed=True,
        outcome_prices='["0.0","1.0"]',
        question=question,
    )

    mock_resp_l2 = AsyncMock()
    mock_resp_l2.status = 200
    mock_resp_l2.json = AsyncMock(return_value=[wrong_market])

    mock_resp_slug = AsyncMock()
    mock_resp_slug.status = 200
    mock_resp_slug.json = AsyncMock(return_value=[{"markets": [resolved_market]}])

    def _make_get_cm(*args, **kwargs):
        call_count["n"] += 1
        resp = mock_resp_l2 if call_count["n"] == 1 else mock_resp_slug
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=resp)
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    mock_session = AsyncMock()
    mock_session.get = MagicMock(side_effect=_make_get_cm)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = await indexer.check_resolution(
            "cond_expected", market_question=question,
        )

    assert result is not None
    assert result.resolution_price == 0.0
    assert result.source == "slug_resolved"
    assert "cond_expected" in indexer._gamma_mismatch_ids
