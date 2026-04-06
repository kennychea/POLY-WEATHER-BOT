"""Tests for market_data/ws_price_feed.py -- WebSocket price feed with HTTP fallback."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

from market_data.ws_price_feed import WsPriceFeed


def _make_feed(staleness: float = 30.0) -> tuple[WsPriceFeed, AsyncMock]:
    """Create a WsPriceFeed with a mocked PriceFetcher fallback."""
    fallback = AsyncMock()
    feed = WsPriceFeed(fallback_fetcher=fallback, staleness_seconds=staleness)
    return feed, fallback


def _inject_price(
    feed: WsPriceFeed,
    token_id: str,
    *,
    best_bid: float = 0.55,
    best_ask: float = 0.57,
    mid: float = 0.56,
    spread: float = 0.02,
    age: float = 0.0,
) -> None:
    """Inject a price entry directly into the cache."""
    feed._prices[token_id] = {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid": mid,
        "spread": spread,
        "updated_at": time.monotonic() - age,
    }


# -- subscribe ---------------------------------------------------------------


async def test_subscribe_adds_token():
    """subscribe() should add the token_id to the subscription set."""
    feed, _ = _make_feed()
    assert len(feed._subscriptions) == 0

    await feed.subscribe("tok_yes_abc")
    assert "tok_yes_abc" in feed._subscriptions

    await feed.subscribe("tok_no_xyz")
    assert len(feed._subscriptions) == 2


async def test_subscribe_resends_to_ws():
    """subscribe() should resend subscriptions when WS is connected."""
    feed, _ = _make_feed()

    # Simulate connected WS
    mock_ws = AsyncMock()
    feed._ws = mock_ws

    await feed.subscribe("tok_new_123")

    assert "tok_new_123" in feed._subscriptions
    mock_ws.send.assert_awaited_once()  # Should have sent subscription message


# -- get_price ---------------------------------------------------------------


async def test_get_price_from_cache():
    """get_price() should return best_ask from a fresh cached entry."""
    feed, fallback = _make_feed()
    _inject_price(feed, "tok_yes_123", best_ask=0.62)

    result = await feed.get_price("tok_yes_123")

    assert result == 0.62
    fallback.get_price.assert_not_awaited()


async def test_get_price_stale_falls_back():
    """get_price() should fall back to HTTP when cache entry is stale."""
    feed, fallback = _make_feed(staleness=30.0)
    _inject_price(feed, "tok_yes_123", best_ask=0.62, age=60.0)  # 60s old > 30s staleness

    fallback.get_price = AsyncMock(return_value=0.65)
    result = await feed.get_price("tok_yes_123")

    assert result == 0.65
    fallback.get_price.assert_awaited_once_with("tok_yes_123")


async def test_get_price_missing_falls_back():
    """get_price() should fall back to HTTP when token not in cache."""
    feed, fallback = _make_feed()

    fallback.get_price = AsyncMock(return_value=0.70)
    result = await feed.get_price("tok_unknown")

    assert result == 0.70
    fallback.get_price.assert_awaited_once_with("tok_unknown")


# -- get_mid_price -----------------------------------------------------------


async def test_get_mid_price_from_cache():
    """get_mid_price() should return mid from a fresh cached entry."""
    feed, fallback = _make_feed()
    _inject_price(feed, "tok_yes_123", mid=0.56)

    result = await feed.get_mid_price("tok_yes_123")

    assert result == 0.56
    fallback.get_mid_price.assert_not_awaited()


async def test_get_mid_price_stale_falls_back():
    """get_mid_price() should fall back to HTTP when cache is stale."""
    feed, fallback = _make_feed(staleness=10.0)
    _inject_price(feed, "tok_yes_123", mid=0.56, age=20.0)

    fallback.get_mid_price = AsyncMock(return_value=0.58)
    result = await feed.get_mid_price("tok_yes_123")

    assert result == 0.58
    fallback.get_mid_price.assert_awaited_once_with("tok_yes_123")


# -- get_book ----------------------------------------------------------------


async def test_get_book_from_cache():
    """get_book() should return the full cached dict when fresh."""
    feed, fallback = _make_feed()
    _inject_price(feed, "tok_yes_123", best_bid=0.50, best_ask=0.60, mid=0.55, spread=0.10)

    result = await feed.get_book("tok_yes_123")

    assert result is not None
    assert result["best_bid"] == 0.50
    assert result["best_ask"] == 0.60
    assert result["mid"] == 0.55
    assert result["spread"] == 0.10
    fallback.fetch_book.assert_not_awaited()


async def test_get_book_stale_falls_back():
    """get_book() should fall back to HTTP when cache is stale."""
    feed, fallback = _make_feed(staleness=5.0)
    _inject_price(feed, "tok_yes_123", age=10.0)

    expected = {"best_bid": 0.50, "best_ask": 0.60, "mid": 0.55, "spread": 0.10}
    fallback.fetch_book = AsyncMock(return_value=expected)

    result = await feed.get_book("tok_yes_123")

    assert result == expected
    fallback.fetch_book.assert_awaited_once_with("tok_yes_123")


# -- _handle_message ---------------------------------------------------------


async def test_handle_message_updates_cache():
    """_handle_message() should parse a valid WS message and update cache."""
    feed, _ = _make_feed()
    import json

    msg = json.dumps({
        "asset_id": "tok_ws_001",
        "bids": [{"price": "0.45", "size": "100"}],
        "asks": [{"price": "0.48", "size": "200"}],
    })
    feed._handle_message(msg)

    assert "tok_ws_001" in feed._prices
    entry = feed._prices["tok_ws_001"]
    assert entry["best_bid"] == 0.45
    assert entry["best_ask"] == 0.48
    assert abs(entry["mid"] - 0.465) < 1e-9
    assert abs(entry["spread"] - 0.03) < 1e-9


async def test_handle_message_ignores_empty_book():
    """_handle_message() should ignore messages with no bids and no asks."""
    feed, _ = _make_feed()
    import json

    msg = json.dumps({
        "asset_id": "tok_empty",
        "bids": [],
        "asks": [],
    })
    feed._handle_message(msg)

    assert "tok_empty" not in feed._prices


async def test_handle_message_ignores_no_asset_id():
    """_handle_message() should ignore messages without asset_id."""
    feed, _ = _make_feed()
    import json

    msg = json.dumps({"type": "heartbeat"})
    feed._handle_message(msg)

    assert len(feed._prices) == 0


async def test_handle_message_handles_bytes():
    """_handle_message() should handle bytes input."""
    feed, _ = _make_feed()
    import json

    msg = json.dumps({
        "asset_id": "tok_bytes",
        "bids": [{"price": "0.30", "size": "50"}],
        "asks": [{"price": "0.35", "size": "75"}],
    }).encode("utf-8")
    feed._handle_message(msg)

    assert "tok_bytes" in feed._prices
    assert feed._prices["tok_bytes"]["best_ask"] == 0.35


# -- Cache pruning (S2.2) -----------------------------------------------


async def test_prune_stale_cache_removes_old_entries():
    """_prune_stale_cache should remove entries older than 10x staleness."""
    feed, _ = _make_feed()
    # Inject a stale entry (updated_at far in the past)
    feed._prices["tok_stale"] = {
        "best_bid": 0.50,
        "best_ask": 0.55,
        "mid": 0.525,
        "spread": 0.05,
        "updated_at": time.monotonic() - 500,  # 500s ago, threshold=30*10=300
    }
    feed._prices["tok_fresh"] = {
        "best_bid": 0.60,
        "best_ask": 0.65,
        "mid": 0.625,
        "spread": 0.05,
        "updated_at": time.monotonic(),  # just now
    }

    feed._prune_stale_cache()

    assert "tok_stale" not in feed._prices
    assert "tok_fresh" in feed._prices


async def test_prune_stale_cache_keeps_recent_entries():
    """_prune_stale_cache should keep entries within threshold."""
    feed, _ = _make_feed()
    feed._prices["tok_1"] = {
        "best_bid": 0.50,
        "best_ask": 0.55,
        "mid": 0.525,
        "spread": 0.05,
        "updated_at": time.monotonic() - 10,  # 10s ago, well within 300s
    }
    feed._prices["tok_2"] = {
        "best_bid": 0.60,
        "best_ask": 0.65,
        "mid": 0.625,
        "spread": 0.05,
        "updated_at": time.monotonic() - 100,  # 100s ago, still within 300s
    }

    feed._prune_stale_cache()

    assert len(feed._prices) == 2
