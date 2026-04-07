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
    """Market in cache with closed=false should return None."""
    indexer = MarketIndexer()
    indexer._markets = [
        _make_market(condition_id="cond_456", closed=False),
    ]

    result = await indexer.check_resolution("cond_456")

    assert result is None


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
