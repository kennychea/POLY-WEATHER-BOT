"""Tests for market_data/price_fetcher.py -- Polymarket CLOB orderbook."""

from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest


def _fake_book_response(bids=None, asks=None):
    """Build a fake CLOB orderbook response."""
    if bids is None:
        bids = [{"price": "0.55", "size": "100"}, {"price": "0.54", "size": "200"}]
    if asks is None:
        asks = [{"price": "0.57", "size": "150"}, {"price": "0.58", "size": "250"}]
    return {"bids": bids, "asks": asks}


# -- Creation -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetcher_creation():
    """Should create a PriceFetcher."""
    from market_data.price_fetcher import PriceFetcher
    fetcher = PriceFetcher()
    assert fetcher is not None


# -- fetch_book ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_book_returns_orderbook():
    """fetch_book() should return best_bid, best_ask, spread, mid."""
    from market_data.price_fetcher import PriceFetcher

    fetcher = PriceFetcher()
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=_fake_book_response())
    mock_resp.raise_for_status = MagicMock()

    with patch("aiohttp.ClientSession") as mock_session_cls:
        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_resp),
            __aexit__=AsyncMock(return_value=False),
        ))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session_cls.return_value = mock_session

        book = await fetcher.fetch_book("tok_yes_123")

    assert book is not None
    assert book["best_bid"] == 0.55
    assert book["best_ask"] == 0.57
    assert abs(book["spread"] - 0.02) < 1e-9
    assert abs(book["mid"] - 0.56) < 1e-9


@pytest.mark.asyncio
async def test_fetch_book_empty_bids():
    """fetch_book() should handle empty bids gracefully."""
    from market_data.price_fetcher import PriceFetcher

    fetcher = PriceFetcher()
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=_fake_book_response(bids=[], asks=[]))
    mock_resp.raise_for_status = MagicMock()

    with patch("aiohttp.ClientSession") as mock_session_cls:
        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_resp),
            __aexit__=AsyncMock(return_value=False),
        ))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session_cls.return_value = mock_session

        book = await fetcher.fetch_book("tok_yes_123")

    assert book is None


@pytest.mark.asyncio
async def test_fetch_book_returns_none_on_error():
    """fetch_book() should return None on HTTP error."""
    from market_data.price_fetcher import PriceFetcher

    fetcher = PriceFetcher()

    with patch("aiohttp.ClientSession") as mock_session_cls:
        mock_session = AsyncMock()
        mock_session.get = MagicMock(side_effect=Exception("connection error"))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session_cls.return_value = mock_session

        book = await fetcher.fetch_book("tok_yes_123")

    assert book is None


# -- get_price ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_price_returns_best_ask():
    """get_price() should return best_ask (taker buy price)."""
    from market_data.price_fetcher import PriceFetcher

    fetcher = PriceFetcher()

    with patch.object(
        fetcher, "fetch_book",
        new_callable=AsyncMock,
        return_value={"best_bid": 0.55, "best_ask": 0.57, "spread": 0.02, "mid": 0.56},
    ):
        price = await fetcher.get_price("tok_yes_123")

    assert price == 0.57


@pytest.mark.asyncio
async def test_get_price_returns_none_when_no_book():
    """get_price() should return None if fetch_book fails."""
    from market_data.price_fetcher import PriceFetcher

    fetcher = PriceFetcher()

    with patch.object(
        fetcher, "fetch_book",
        new_callable=AsyncMock, return_value=None,
    ):
        price = await fetcher.get_price("tok_yes_123")

    assert price is None


# -- get_mid_price ------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_mid_price():
    """get_mid_price() should return midpoint."""
    from market_data.price_fetcher import PriceFetcher

    fetcher = PriceFetcher()

    with patch.object(
        fetcher, "fetch_book",
        new_callable=AsyncMock,
        return_value={"best_bid": 0.50, "best_ask": 0.60, "spread": 0.10, "mid": 0.55},
    ):
        price = await fetcher.get_mid_price("tok_123")

    assert price == 0.55


@pytest.mark.asyncio
async def test_get_mid_price_returns_none_on_failure():
    """get_mid_price() should return None on error."""
    from market_data.price_fetcher import PriceFetcher

    fetcher = PriceFetcher()

    with patch.object(
        fetcher, "fetch_book",
        new_callable=AsyncMock, return_value=None,
    ):
        price = await fetcher.get_mid_price("tok_123")

    assert price is None


@pytest.mark.asyncio
async def test_get_mid_price_rejects_wide_spread_stub_book():
    """Stub book (bid=0.01, ask=0.99) must NOT yield a tradable mid of 0.5.

    Regression guard for the 2026-04-11 clone-edge bug: get_mid_price used to
    return (0.01+0.99)/2 = 0.5 from placeholder MM books, producing identical
    edge=0.4697 trade signals across 10 cities.
    """
    from market_data.price_fetcher import PriceFetcher

    fetcher = PriceFetcher()

    stub_book = {
        "best_bid": 0.01,
        "best_ask": 0.99,
        "spread": 0.98,
        "mid": 0.50,
    }
    with patch.object(
        fetcher, "fetch_book",
        new_callable=AsyncMock, return_value=stub_book,
    ):
        price = await fetcher.get_mid_price("tok_stub_123")

    assert price is None, (
        "Wide-spread stub book must be rejected, not collapsed to mid=0.5"
    )


@pytest.mark.asyncio
async def test_get_mid_price_accepts_tight_spread_book():
    """Tight spread (bid=0.48, ask=0.52, spread=0.04) must pass through."""
    from market_data.price_fetcher import PriceFetcher

    fetcher = PriceFetcher()

    tight_book = {
        "best_bid": 0.48,
        "best_ask": 0.52,
        "spread": 0.04,
        "mid": 0.50,
    }
    with patch.object(
        fetcher, "fetch_book",
        new_callable=AsyncMock, return_value=tight_book,
    ):
        price = await fetcher.get_mid_price("tok_tight_123")

    assert price == 0.50


# -- _parse_book price validation ---------------------------------------------


@pytest.mark.asyncio
async def test_parse_book_rejects_zero_bid():
    """Should return None if best_bid is 0."""
    from market_data.price_fetcher import PriceFetcher

    data = {
        "bids": [{"price": "0.0"}],
        "asks": [{"price": "0.65"}],
    }
    assert PriceFetcher._parse_book(data) is None


@pytest.mark.asyncio
async def test_parse_book_rejects_negative_ask():
    """Should return None if best_ask is negative."""
    from market_data.price_fetcher import PriceFetcher

    data = {
        "bids": [{"price": "0.50"}],
        "asks": [{"price": "-0.10"}],
    }
    assert PriceFetcher._parse_book(data) is None


@pytest.mark.asyncio
async def test_parse_book_rejects_zero_ask():
    """Should return None if best_ask is 0."""
    from market_data.price_fetcher import PriceFetcher

    data = {
        "bids": [{"price": "0.40"}],
        "asks": [{"price": "0.0"}],
    }
    assert PriceFetcher._parse_book(data) is None


@pytest.mark.asyncio
async def test_parse_book_rejects_negative_bid():
    """Should return None if best_bid is negative."""
    from market_data.price_fetcher import PriceFetcher

    data = {
        "bids": [{"price": "-0.05"}],
        "asks": [{"price": "0.60"}],
    }
    assert PriceFetcher._parse_book(data) is None


@pytest.mark.asyncio
async def test_parse_book_accepts_valid_prices():
    """Should return parsed book when both prices are positive."""
    from market_data.price_fetcher import PriceFetcher

    data = {
        "bids": [{"price": "0.50"}],
        "asks": [{"price": "0.60"}],
    }
    result = PriceFetcher._parse_book(data)
    assert result is not None
    assert result["best_bid"] == 0.50
    assert result["best_ask"] == 0.60


# -- Retry logic --------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_book_retries_on_failure():
    """_request_book should retry on transient HTTP errors and succeed."""

    from market_data.price_fetcher import PriceFetcher

    fake_book = _fake_book_response()

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=fake_book)
    mock_resp.raise_for_status = MagicMock()

    call_count = 0

    def make_get(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise aiohttp.ClientError("connection timeout")
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=mock_resp)
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    with patch("aiohttp.ClientSession") as mock_session_cls:
        mock_session = AsyncMock()
        mock_session.get = MagicMock(side_effect=make_get)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session_cls.return_value = mock_session

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            fetcher = PriceFetcher()
            result = await fetcher._request_book("tok_yes_123")

    assert result == fake_book
    assert call_count == 2
    mock_sleep.assert_awaited_once()


@pytest.mark.asyncio
async def test_request_book_raises_after_max_retries():
    """_request_book should raise after exhausting all retries."""

    from market_data.price_fetcher import PriceFetcher

    with patch("aiohttp.ClientSession") as mock_session_cls:
        mock_session = AsyncMock()
        mock_session.get = MagicMock(side_effect=aiohttp.ClientError("permanent failure"))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session_cls.return_value = mock_session

        with patch("asyncio.sleep", new_callable=AsyncMock):
            fetcher = PriceFetcher()
            with pytest.raises(aiohttp.ClientError, match="permanent failure"):
                await fetcher._request_book("tok_yes_123")


# -- Session lifecycle -------------------------------------------------------


@pytest.mark.asyncio
async def test_price_fetcher_session_reuse():
    """PriceFetcher should reuse the same session across calls (P1.2)."""
    from market_data.price_fetcher import PriceFetcher
    fetcher = PriceFetcher()
    assert fetcher._session is None

    mock_session = AsyncMock()
    mock_session.closed = False
    with patch("aiohttp.ClientSession", return_value=mock_session):
        s1 = await fetcher._get_session()
        s2 = await fetcher._get_session()

    assert s1 is s2  # Same session object


@pytest.mark.asyncio
async def test_price_fetcher_close():
    """PriceFetcher.close() should close the session."""
    from market_data.price_fetcher import PriceFetcher
    fetcher = PriceFetcher()

    mock_session = AsyncMock()
    mock_session.closed = False
    fetcher._session = mock_session

    await fetcher.close()
    mock_session.close.assert_called_once()
    assert fetcher._session is None


# -- Session reset on error (P3b.3 fix) ----------------------------------------


@pytest.mark.asyncio
async def test_session_reset_on_retry():
    """Session should be reset between retries on error (P3b.3)."""
    from market_data.price_fetcher import PriceFetcher
    fetcher = PriceFetcher()

    # Create a mock session that fails on first request
    mock_session = MagicMock()
    mock_session.closed = False
    mock_session.close = AsyncMock()

    # Make get() raise on first call, succeed on second
    mock_response = AsyncMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = AsyncMock(return_value={"bids": [], "asks": []})
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    call_count = 0

    def _get_side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise ConnectionError("network error")
        return mock_response

    mock_session.get = MagicMock(side_effect=_get_side_effect)

    fetcher._session = mock_session

    # Patch _get_session to return a fresh mock on retry
    fresh_session = MagicMock()
    fresh_session.closed = False
    fresh_session.close = AsyncMock()
    fresh_session.get = MagicMock(return_value=mock_response)

    async def patched_get_session():
        if fetcher._session is None:
            fetcher._session = fresh_session
            return fresh_session
        return fetcher._session

    fetcher._get_session = patched_get_session

    # This should fail on first try, reset session, and retry
    await fetcher._request_book("tok_123", max_retries=1, base_delay=0.01)

    # Original session should have been closed
    mock_session.close.assert_called_once()
    await fetcher.close()
