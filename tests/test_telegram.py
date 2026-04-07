"""Tests for infra/telegram.py — Telegram alert sender."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _mock_session(ok: bool = True, description: str = ""):
    """Create a fully mocked aiohttp.ClientSession."""
    resp_json = {"ok": ok}
    if description:
        resp_json["description"] = description

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read = AsyncMock(return_value=b"ok")
    mock_resp.json = AsyncMock(return_value=resp_json)
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_resp)
    mock_session.closed = False
    mock_session.close = AsyncMock()
    return mock_session


@pytest.mark.asyncio
async def test_telegram_send_message():
    """Should send a message via Telegram API."""
    from infra.telegram import TelegramBot
    bot = TelegramBot(token="123:ABC", chat_id="456")
    mock_sess = _mock_session()
    with patch.object(bot, "_get_session", return_value=mock_sess):
        await bot.send("Test message")
    mock_sess.post.assert_called_once()


@pytest.mark.asyncio
async def test_telegram_send_error_alert():
    """Should format and send an error alert."""
    from infra.telegram import TelegramBot
    bot = TelegramBot(token="123:ABC", chat_id="456")
    with patch.object(bot, "send", new_callable=AsyncMock) as mock_send:
        await bot.alert_error("Connection lost to Binance WS")
        mock_send.assert_called_once()
        msg = mock_send.call_args[0][0]
        assert "Connection lost" in msg


@pytest.mark.asyncio
async def test_telegram_send_trade_result():
    """Should format and send a trade result."""
    from infra.telegram import TelegramBot
    from infra.types import SignalType, TradeResult
    bot = TelegramBot(token="123:ABC", chat_id="456")
    now = datetime.now(UTC)
    result = TradeResult(
        market_id="0xabc",
        signal_type=SignalType.BUY_YES,
        entry_price=0.45,
        exit_price=1.0,
        size_usdc=10.0,
        pnl=5.50,
        win=True,
        timestamp=now,
    )
    with patch.object(bot, "send", new_callable=AsyncMock) as mock_send:
        await bot.alert_trade_result(result)
        mock_send.assert_called_once()
        msg = mock_send.call_args[0][0]
        assert "5.50" in msg


@pytest.mark.asyncio
async def test_telegram_heartbeat():
    """Should send a heartbeat status message."""
    from infra.telegram import TelegramBot
    bot = TelegramBot(token="123:ABC", chat_id="456")
    with patch.object(bot, "send", new_callable=AsyncMock) as mock_send:
        await bot.heartbeat(uptime_seconds=3600, signals_count=12, win_rate=0.85)
        mock_send.assert_called_once()
        msg = mock_send.call_args[0][0]
        assert "12" in msg
        assert "85" in msg


def test_telegram_bot_creation():
    """Should create a TelegramBot with token and chat_id."""
    from infra.telegram import TelegramBot
    bot = TelegramBot(token="123:ABC", chat_id="456")
    assert bot.token == "123:ABC"
    assert bot.chat_id == "456"


# -- BUG 7: Check 'ok' field in Telegram API response ------------------------


@pytest.mark.asyncio
async def test_telegram_send_logs_error_when_ok_false():
    """BUG 7: send() should log error when API returns ok=false."""
    from infra.telegram import TelegramBot
    bot = TelegramBot(token="123:ABC", chat_id="456")
    mock_sess = _mock_session(ok=False, description="Bad Request: chat not found")
    with patch.object(bot, "_get_session", return_value=mock_sess):
        # Should not raise
        await bot.send("Test message")
    # Response was read (json parsed)
    mock_resp = mock_sess.post.return_value
    mock_resp.__aenter__.return_value.json.assert_called_once()


@pytest.mark.asyncio
async def test_telegram_send_parses_json_on_success():
    """BUG 7: send() should parse JSON and succeed when ok=true."""
    from infra.telegram import TelegramBot
    bot = TelegramBot(token="123:ABC", chat_id="456")
    mock_sess = _mock_session(ok=True)
    with patch.object(bot, "_get_session", return_value=mock_sess):
        await bot.send("Test message")
    mock_sess.post.assert_called_once()


# -- BUG 10: Message truncation at 4000 chars ---------------------------------


@pytest.mark.asyncio
async def test_telegram_send_truncates_long_messages():
    """BUG 10: send() should truncate messages > 4000 chars."""
    from infra.telegram import TelegramBot
    bot = TelegramBot(token="123:ABC", chat_id="456")
    long_msg = "A" * 5000
    mock_sess = _mock_session()
    with patch.object(bot, "_get_session", return_value=mock_sess):
        await bot.send(long_msg)
    # Check the posted payload
    call_args = mock_sess.post.call_args
    sent_text = call_args[1]["json"]["text"]
    assert len(sent_text) <= 4020  # 4000 + "... (truncated)" suffix
    assert sent_text.endswith("(truncated)")


@pytest.mark.asyncio
async def test_telegram_send_no_truncation_for_short():
    """BUG 10: send() should not truncate messages <= 4000 chars."""
    from infra.telegram import TelegramBot
    bot = TelegramBot(token="123:ABC", chat_id="456")
    short_msg = "Hello world"
    mock_sess = _mock_session()
    with patch.object(bot, "_get_session", return_value=mock_sess):
        await bot.send(short_msg)
    call_args = mock_sess.post.call_args
    sent_text = call_args[1]["json"]["text"]
    assert sent_text == "Hello world"


# -- P5.5.1: Structured alert methods -----------------------------------------


@pytest.mark.asyncio
async def test_telegram_alert_edge():
    """alert_edge() should format and send an edge discovery alert."""
    from infra.telegram import TelegramBot
    bot = TelegramBot(token="123:ABC", chat_id="456")
    with patch.object(bot, "send", new_callable=AsyncMock) as mock_send:
        await bot.alert_edge(
            signal_type="BUY_YES",
            edge=0.08,
            net_edge=0.06,
            market_question="Will it rain more than 1 inch in NYC on April 10?",
            location="New York",
            price=0.42,
        )
        mock_send.assert_called_once()
        msg = mock_send.call_args[0][0]
        assert "EDGE FOUND" in msg
        assert "BUY_YES" in msg
        assert "8.00%" in msg
        assert "6.00%" in msg
        assert "0.4200" in msg
        assert "New York" in msg
        assert "rain more than 1 inch" in msg


@pytest.mark.asyncio
async def test_telegram_alert_resolution_win():
    """alert_resolution() should format a WIN alert correctly."""
    from infra.telegram import TelegramBot
    bot = TelegramBot(token="123:ABC", chat_id="456")
    with patch.object(bot, "send", new_callable=AsyncMock) as mock_send:
        await bot.alert_resolution(
            market_question="Will temperature exceed 90F in Miami on April 12?",
            signal_type="BUY_YES",
            entry_price=0.35,
            exit_price=1.0,
            pnl=6.50,
            win=True,
            resolution_source="market_resolved",
        )
        mock_send.assert_called_once()
        msg = mock_send.call_args[0][0]
        assert "TRADE WIN" in msg
        assert "0.3500" in msg
        assert "1.0000" in msg
        assert "+6.50" in msg
        assert "market_resolved" in msg


@pytest.mark.asyncio
async def test_telegram_alert_resolution_loss():
    """alert_resolution() should format a LOSS alert correctly."""
    from infra.telegram import TelegramBot
    bot = TelegramBot(token="123:ABC", chat_id="456")
    with patch.object(bot, "send", new_callable=AsyncMock) as mock_send:
        await bot.alert_resolution(
            market_question="Will it snow in Chicago on April 15?",
            signal_type="BUY_NO",
            entry_price=0.60,
            exit_price=0.0,
            pnl=-6.00,
            win=False,
            resolution_source="price_timeout",
        )
        mock_send.assert_called_once()
        msg = mock_send.call_args[0][0]
        assert "TRADE LOSS" in msg
        assert "0.6000" in msg
        assert "-6.00" in msg
        assert "price_timeout" in msg


@pytest.mark.asyncio
async def test_telegram_alert_daily_summary():
    """alert_daily_summary() should format all stats correctly."""
    from infra.telegram import TelegramBot
    bot = TelegramBot(token="123:ABC", chat_id="456")
    with patch.object(bot, "send", new_callable=AsyncMock) as mock_send:
        await bot.alert_daily_summary(
            trades_opened=5,
            trades_resolved=3,
            total_pnl=12.50,
            win_rate=0.667,
            bankroll=112.50,
        )
        mock_send.assert_called_once()
        msg = mock_send.call_args[0][0]
        assert "DAILY SUMMARY" in msg
        assert "5" in msg
        assert "3" in msg
        assert "+12.50" in msg
        assert "67%" in msg
        assert "112.50" in msg


@pytest.mark.asyncio
async def test_telegram_alert_edge_disabled():
    """alert_edge() should no-op when bot is disabled (empty token)."""
    from infra.telegram import TelegramBot
    bot = TelegramBot(token="", chat_id="456")
    with patch.object(bot, "send", new_callable=AsyncMock) as mock_send:
        await bot.alert_edge(
            signal_type="BUY_YES", edge=0.08, net_edge=0.06,
            market_question="Test?", location="NYC", price=0.5,
        )
        mock_send.assert_not_called()


@pytest.mark.asyncio
async def test_telegram_alert_resolution_disabled():
    """alert_resolution() should no-op when bot is disabled (empty chat_id)."""
    from infra.telegram import TelegramBot
    bot = TelegramBot(token="123:ABC", chat_id="")
    with patch.object(bot, "send", new_callable=AsyncMock) as mock_send:
        await bot.alert_resolution(
            market_question="Test?", signal_type="BUY_YES",
            entry_price=0.5, exit_price=1.0, pnl=5.0,
            win=True, resolution_source="resolved",
        )
        mock_send.assert_not_called()


@pytest.mark.asyncio
async def test_telegram_alert_daily_summary_disabled():
    """alert_daily_summary() should no-op when bot is disabled."""
    from infra.telegram import TelegramBot
    bot = TelegramBot(token="", chat_id="")
    with patch.object(bot, "send", new_callable=AsyncMock) as mock_send:
        await bot.alert_daily_summary(
            trades_opened=0, trades_resolved=0, total_pnl=0.0,
            win_rate=0.0, bankroll=100.0,
        )
        mock_send.assert_not_called()
