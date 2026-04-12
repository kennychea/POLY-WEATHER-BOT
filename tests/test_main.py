"""Tests for main._evaluate_market filters added in Phase 10.B (commit 192e64f).

Covers:
- past_date filter: markets whose target_date is before today are rejected
- buy_no_longshot_blocked filter: BUY_NO trades where yes_price <= 0.15
  are rejected (asymmetric loss trap). Boundary value 0.15 IS blocked
  (Phase 10.D made the comparison `<=` to match the 85% breakeven WR math).
- Negative control: BUY_NO with yes_price > 0.15 is NOT blocked by this filter
- Negative control: BUY_YES is blocked by the pre-existing buy_yes_blocked
  filter, proving the buy_no_longshot filter targets only BUY_NO
- Phase 10.B.2 stale-price refetch: live CLOB price re-check before open_trade
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import main as main_module
from infra.types import EdgeResult, SignalType
from weather.market_scanner import ParsedWeatherMarket


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _make_market_dict(
    *,
    condition_id: str = "cond_test_123",
    yes_price: float = 0.50,
    no_price: float | None = None,
    question: str = "Will the highest temperature in New York be between 50-55F?",
) -> dict:
    """Build a minimal Polymarket market dict with token IDs and prices."""
    if no_price is None:
        no_price = round(1.0 - yes_price, 6)
    return {
        "question": question,
        "conditionId": condition_id,
        "clobTokenIds": ["yes_token_id_abc", "no_token_id_xyz"],
        "outcomePrices": f'["{yes_price}", "{no_price}"]',
    }


def _make_parsed_market(
    *,
    target_date: str,
    yes_price: float = 0.50,
    location: str = "New York",
) -> ParsedWeatherMarket:
    """Build a ParsedWeatherMarket with a real city known to resolve_city."""
    return ParsedWeatherMarket(
        market=_make_market_dict(yes_price=yes_price),
        location=location,
        metric="temp_high",
        threshold_low=50.0,
        threshold_high=55.0,
        direction="bucket",
        target_date=target_date,
        unit="fahrenheit",
    )


def _make_cfg(*, use_multi_model: bool = False) -> SimpleNamespace:
    """Minimal config stub covering every attribute _evaluate_market reads."""
    return SimpleNamespace(
        use_multi_model=use_multi_model,
        taker_fee_pct=0.01,
        model_weights={"gfs_seamless": 1.0},
    )


_UNSET = object()


def _make_eval_kwargs(
    wm: ParsedWeatherMarket,
    *,
    diag: dict[str, int],
    live_yes_price: Any = _UNSET,
) -> dict:
    """Build the kwargs dict for _evaluate_market, with mocks for every dep.

    `live_yes_price` controls what `price_fetcher.get_mid_price` returns when
    the Phase 10.B.2 stale-price refetch runs. When unset, defaults to the
    parsed market's YES price so the refetch is a no-op in legacy tests. Pass
    None explicitly to simulate a CLOB fetch failure.
    """
    session = MagicMock(name="aiohttp_session")

    if live_yes_price is _UNSET:
        import json as _json
        _parsed_prices = _json.loads(wm.market["outcomePrices"])
        live_yes_price = float(_parsed_prices[0])
    price_fetcher = MagicMock()
    price_fetcher.get_price = AsyncMock(return_value=live_yes_price)
    price_fetcher.get_mid_price = AsyncMock(return_value=live_yes_price)

    risk_manager = MagicMock()
    risk_manager.size_position = MagicMock(return_value=10.0)

    db_writer = MagicMock()
    db_writer.enqueue = AsyncMock(return_value=None)

    paper_trader = MagicMock()
    paper_trader.open_trade = AsyncMock(return_value=None)

    telegram = MagicMock()
    telegram.alert_edge = AsyncMock(return_value=None)

    return {
        "wm": wm,
        "session": session,
        "price_fetcher": price_fetcher,
        "risk_manager": risk_manager,
        "db_writer": db_writer,
        "paper_trader": paper_trader,
        "cfg": _make_cfg(),
        "telegram": telegram,
        "dedup": None,
        "session_cache": {},
        "diag": diag,
    }


def _past_date() -> str:
    return (datetime.now(UTC).date() - timedelta(days=1)).isoformat()


def _near_future_date() -> str:
    # +1 day: safely in-window (horizon filter rejects >5 days out).
    return (datetime.now(UTC).date() + timedelta(days=1)).isoformat()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_past_date_filter_increments_diag_and_skips() -> None:
    """Market with target_date before today is rejected; diag['past_date']=1."""
    wm = _make_parsed_market(target_date=_past_date())
    diag: dict[str, int] = {}
    kwargs = _make_eval_kwargs(wm, diag=diag)

    result = await main_module._evaluate_market(**kwargs)

    assert result is False
    assert diag.get("past_date") == 1
    # No trade opened
    kwargs["paper_trader"].open_trade.assert_not_called()
    # Critical: we bailed BEFORE the expensive ensemble fetch
    kwargs["price_fetcher"].get_price.assert_not_called()
    kwargs["price_fetcher"].get_mid_price.assert_not_called()


@pytest.mark.asyncio
async def test_buy_no_longshot_blocked_when_yes_price_below_015() -> None:
    """BUY_NO signal with yes_price < 0.15 is rejected (asymmetric trap)."""
    wm = _make_parsed_market(target_date=_near_future_date(), yes_price=0.08)
    diag: dict[str, int] = {}
    kwargs = _make_eval_kwargs(wm, diag=diag)

    # Produce a BUY_NO signal: model says YES is even less likely than market.
    # At confidence=medium, min_raw=0.05. prob=0.01 vs yes=0.08 -> raw_edge=0.07,
    # net_edge=0.06, signal=BUY_NO.
    fake_er = SimpleNamespace(members=[60.0, 61.0, 62.0], member_count=3)

    with (
        patch.object(
            main_module, "fetch_ensemble_result", AsyncMock(return_value=fake_er),
        ),
        patch.object(
            main_module, "ensemble_probability", return_value=(0.01, "medium"),
        ),
    ):
        result = await main_module._evaluate_market(**kwargs)

    assert result is False
    assert diag.get("buy_no_longshot_blocked") == 1
    kwargs["paper_trader"].open_trade.assert_not_called()
    # Ensure sizing never happened — we rejected before risk check
    kwargs["risk_manager"].size_position.assert_not_called()


@pytest.mark.asyncio
async def test_buy_no_longshot_boundary_blocks_exact_015() -> None:
    """Phase 10.D: yes_price == 0.15 must be BLOCKED (<=, not <).

    At YES=0.15 the BUY_NO entry costs 0.85, win pays 0.15, loss costs 0.85.
    Breakeven WR = 0.85 / (0.85 + 0.15) = 85% — same trap as <0.15.
    """
    wm = _make_parsed_market(target_date=_near_future_date(), yes_price=0.15)
    diag: dict[str, int] = {}
    kwargs = _make_eval_kwargs(wm, diag=diag)

    fake_er = SimpleNamespace(members=[60.0, 61.0, 62.0], member_count=3)
    with (
        patch.object(
            main_module, "fetch_ensemble_result", AsyncMock(return_value=fake_er),
        ),
        patch.object(
            main_module, "ensemble_probability", return_value=(0.01, "medium"),
        ),
    ):
        result = await main_module._evaluate_market(**kwargs)

    assert result is False
    assert diag.get("buy_no_longshot_blocked") == 1
    kwargs["risk_manager"].size_position.assert_not_called()


@pytest.mark.asyncio
async def test_buy_no_allowed_just_above_015() -> None:
    """Phase 10.D: yes_price = 0.151 (just above boundary) must NOT trigger filter."""
    wm = _make_parsed_market(target_date=_near_future_date(), yes_price=0.151)
    diag: dict[str, int] = {}
    kwargs = _make_eval_kwargs(wm, diag=diag)

    fake_er = SimpleNamespace(members=[60.0, 61.0, 62.0], member_count=3)

    with (
        patch.object(
            main_module, "fetch_ensemble_result", AsyncMock(return_value=fake_er),
        ),
        patch.object(
            main_module, "ensemble_probability", return_value=(0.05, "medium"),
        ),
    ):
        await main_module._evaluate_market(**kwargs)

    # Filter did not fire
    assert "buy_no_longshot_blocked" not in diag
    # past_date didn't fire either
    assert "past_date" not in diag
    # We progressed past the filter: risk sizing was consulted
    kwargs["risk_manager"].size_position.assert_called_once()


@pytest.mark.asyncio
async def test_buy_no_allowed_when_yes_price_at_or_above_015() -> None:
    """Negative control: yes_price = 0.20 must NOT trigger longshot filter."""
    wm = _make_parsed_market(target_date=_near_future_date(), yes_price=0.20)
    diag: dict[str, int] = {}
    kwargs = _make_eval_kwargs(wm, diag=diag)

    fake_er = SimpleNamespace(members=[60.0, 61.0, 62.0], member_count=3)

    with (
        patch.object(
            main_module, "fetch_ensemble_result", AsyncMock(return_value=fake_er),
        ),
        patch.object(
            main_module, "ensemble_probability", return_value=(0.05, "medium"),
        ),
    ):
        await main_module._evaluate_market(**kwargs)

    # Filter did not fire
    assert "buy_no_longshot_blocked" not in diag
    # past_date didn't fire either
    assert "past_date" not in diag
    # We progressed past the filter: risk sizing was consulted
    kwargs["risk_manager"].size_position.assert_called_once()


@pytest.mark.asyncio
async def test_buy_yes_unaffected_by_buy_no_longshot_filter() -> None:
    """BUY_YES is killed by P10.1 kill switch before longshot filter fires.

    P10.1 CONFIRMED (n=48, WR=2.1%): BUY_YES is permanently blocked.
    """
    wm = _make_parsed_market(target_date=_near_future_date(), yes_price=0.08)
    diag: dict[str, int] = {}
    kwargs = _make_eval_kwargs(wm, diag=diag)

    fake_er = SimpleNamespace(members=[60.0, 61.0, 62.0], member_count=3)
    with (
        patch.object(
            main_module, "fetch_ensemble_result", AsyncMock(return_value=fake_er),
        ),
        patch.object(
            main_module, "ensemble_probability", return_value=(0.90, "medium"),
        ),
    ):
        result = await main_module._evaluate_market(**kwargs)

    assert result is False
    assert diag.get("buy_yes_blocked") == 1
    assert "buy_no_longshot_blocked" not in diag
    kwargs["paper_trader"].open_trade.assert_not_called()


@pytest.mark.asyncio
async def test_buy_yes_blocked_permanently() -> None:
    """BUY_YES with any edge is permanently killed by P10.1.

    Shadow calibration (n=48, WR=2.1%) confirmed GFS systematically
    overestimates P(YES) for weather markets. No capital, no shadow.
    """
    wm = _make_parsed_market(target_date=_near_future_date(), yes_price=0.35)
    diag: dict[str, int] = {}
    kwargs = _make_eval_kwargs(wm, diag=diag)

    fake_er = SimpleNamespace(members=[60.0, 61.0, 62.0], member_count=3)
    with (
        patch.object(
            main_module, "fetch_ensemble_result", AsyncMock(return_value=fake_er),
        ),
        patch.object(
            main_module, "ensemble_probability", return_value=(0.85, "medium"),
        ),
    ):
        result = await main_module._evaluate_market(**kwargs)

    assert result is False, "BUY_YES must be killed"
    assert diag.get("buy_yes_blocked") == 1
    kwargs["paper_trader"].open_trade.assert_not_called()
    kwargs["risk_manager"].size_position.assert_not_called()


# ---------------------------------------------------------------------------
# Phase 10.B.2 — stale price refetch tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_price_rejection_when_live_edge_gone() -> None:
    """Gamma shows a juicy BUY_NO edge; live CLOB price has eaten it.

    Stale yes_price=0.80, prob=0.30 -> stale BUY_NO raw edge=0.50, net=0.49 (passes).
    Live yes_price=0.78 (very small move) -> live BUY_NO raw edge=0.48, net=0.47 (still passes).
    To actually kill the edge we push the live YES down so prob ~ yes_price:
    live yes_price=0.32, prob=0.30 -> raw=0.02, below min_raw for any confidence.
    calculate_edge returns None -> stale_price_edge_gone.
    """
    wm = _make_parsed_market(target_date=_near_future_date(), yes_price=0.80)
    diag: dict[str, int] = {}
    # Live CLOB price has collapsed to 0.32 while Gamma cache still says 0.80
    kwargs = _make_eval_kwargs(wm, diag=diag, live_yes_price=0.32)

    fake_er = SimpleNamespace(members=[60.0, 61.0, 62.0], member_count=3)
    with (
        patch.object(
            main_module, "fetch_ensemble_result", AsyncMock(return_value=fake_er),
        ),
        patch.object(
            main_module, "ensemble_probability", return_value=(0.30, "high"),
        ),
    ):
        result = await main_module._evaluate_market(**kwargs)

    assert result is False
    assert diag.get("stale_price_edge_gone") == 1, f"diag={diag}"
    kwargs["paper_trader"].open_trade.assert_not_called()
    # The live mid-price fetch must have been attempted exactly once (for yes_token)
    kwargs["price_fetcher"].get_mid_price.assert_awaited_once()


@pytest.mark.asyncio
async def test_stale_price_fetch_failure_skips_trade() -> None:
    """If the live CLOB fetch returns None, we must skip with a diag counter."""
    wm = _make_parsed_market(target_date=_near_future_date(), yes_price=0.80)
    diag: dict[str, int] = {}
    kwargs = _make_eval_kwargs(wm, diag=diag, live_yes_price=None)

    fake_er = SimpleNamespace(members=[60.0, 61.0, 62.0], member_count=3)
    with (
        patch.object(
            main_module, "fetch_ensemble_result", AsyncMock(return_value=fake_er),
        ),
        patch.object(
            main_module, "ensemble_probability", return_value=(0.30, "high"),
        ),
    ):
        result = await main_module._evaluate_market(**kwargs)

    assert result is False
    assert diag.get("stale_price_fetch_failed") == 1, f"diag={diag}"
    kwargs["paper_trader"].open_trade.assert_not_called()
    kwargs["risk_manager"].size_position.assert_not_called()


@pytest.mark.asyncio
async def test_stale_price_refetch_uses_live_price_as_entry() -> None:
    """When the live price confirms the edge, the signal entry must be the LIVE price.

    Stale yes_price=0.80, live yes_price=0.75, prob=0.30.
    BUY_NO entry_price = 1 - live_yes = 0.25 (not 1 - 0.80 = 0.20).
    """
    wm = _make_parsed_market(target_date=_near_future_date(), yes_price=0.80)
    diag: dict[str, int] = {}
    kwargs = _make_eval_kwargs(wm, diag=diag, live_yes_price=0.75)

    # Capture the signal handed to open_trade
    fake_trade = SimpleNamespace(trade_id="trade-1")
    kwargs["paper_trader"].open_trade = AsyncMock(return_value=fake_trade)

    fake_er = SimpleNamespace(members=[60.0, 61.0, 62.0], member_count=3)
    with (
        patch.object(
            main_module, "fetch_ensemble_result", AsyncMock(return_value=fake_er),
        ),
        patch.object(
            main_module, "ensemble_probability", return_value=(0.30, "high"),
        ),
    ):
        result = await main_module._evaluate_market(**kwargs)

    assert result is True, f"Trade should open when live edge still holds, diag={diag}"
    assert "stale_price_edge_gone" not in diag
    assert "stale_price_fetch_failed" not in diag

    kwargs["paper_trader"].open_trade.assert_awaited_once()
    signal_arg = kwargs["paper_trader"].open_trade.await_args.args[0]
    # BUY_NO entry = 1 - live_yes_price = 1 - 0.75 = 0.25
    assert abs(signal_arg.market_price - 0.25) < 1e-9, (
        f"signal.market_price={signal_arg.market_price} (expected 0.25 from live price)"
    )
    assert signal_arg.signal_type == SignalType.BUY_NO


@pytest.mark.asyncio
async def test_stale_price_refetch_rejects_wide_spread_book() -> None:
    """Stub book (bid=0.01, ask=0.99) must NOT seed a trade via mid=0.5.

    Regression guard for 2026-04-11 clone-edge incident. get_mid_price must
    return None for wide-spread books; _evaluate_market must treat that as
    stale_price_fetch_failed and refuse to open a trade.
    """
    wm = _make_parsed_market(target_date=_near_future_date(), yes_price=0.80)
    diag: dict[str, int] = {}
    # Simulate what get_mid_price returns AFTER the fix: None for stub books.
    kwargs = _make_eval_kwargs(wm, diag=diag, live_yes_price=None)

    fake_er = SimpleNamespace(members=[60.0, 61.0, 62.0], member_count=3)
    with (
        patch.object(
            main_module, "fetch_ensemble_result", AsyncMock(return_value=fake_er),
        ),
        patch.object(
            main_module, "ensemble_probability", return_value=(0.30, "high"),
        ),
    ):
        result = await main_module._evaluate_market(**kwargs)

    assert result is False
    assert diag.get("stale_price_fetch_failed") == 1, f"diag={diag}"
    # No trade signal emitted, no paper trade opened
    kwargs["paper_trader"].open_trade.assert_not_called()
    kwargs["telegram"].alert_edge.assert_not_called()
    # The live mid-price fetch was attempted exactly once
    kwargs["price_fetcher"].get_mid_price.assert_awaited_once()


# ---------------------------------------------------------------------------
# BUY_NO longshot trap — mirror gate at high yes_price (cheap NO lottery)
# ---------------------------------------------------------------------------
#
# Forensics on paper_trades: 5 BUY_NO trades fired with entry_price < 0.10
# (yes_price > 0.90). WR was 20% (1/5), n=5 is tiny but the mechanism is
# well-understood: at yes_price=0.99 our crude ensemble has essentially
# zero edge — the market already prices in the tail. The gate must block
# symmetrically: yes_price >= 1 - BUY_NO_MIN_YES_PRICE.


@pytest.mark.asyncio
async def test_buy_no_longshot_trap_blocks_high_yes_price() -> None:
    """BUY_NO at yes_price=0.92 (cheap-NO longshot trap) must be blocked."""
    wm = _make_parsed_market(target_date=_near_future_date(), yes_price=0.92)
    diag: dict[str, int] = {}
    kwargs = _make_eval_kwargs(wm, diag=diag)

    # Force BUY_NO: prob=0.70 vs yes=0.92 -> raw_edge=0.22 on NO side.
    fake_er = SimpleNamespace(members=[60.0, 61.0, 62.0], member_count=3)
    with (
        patch.object(
            main_module, "fetch_ensemble_result", AsyncMock(return_value=fake_er),
        ),
        patch.object(
            main_module, "ensemble_probability", return_value=(0.70, "medium"),
        ),
    ):
        result = await main_module._evaluate_market(**kwargs)

    assert result is False
    assert diag.get("buy_no_longshot_blocked") == 1, f"diag={diag}"
    kwargs["paper_trader"].open_trade.assert_not_called()
    # Sizing never happened — rejected before risk check
    kwargs["risk_manager"].size_position.assert_not_called()


@pytest.mark.asyncio
async def test_buy_no_longshot_trap_boundary_blocks_exact_085() -> None:
    """yes_price == 0.85 (1 - BUY_NO_MIN_YES_PRICE) must be BLOCKED (>=)."""
    wm = _make_parsed_market(target_date=_near_future_date(), yes_price=0.85)
    diag: dict[str, int] = {}
    kwargs = _make_eval_kwargs(wm, diag=diag)

    fake_er = SimpleNamespace(members=[60.0, 61.0, 62.0], member_count=3)
    with (
        patch.object(
            main_module, "fetch_ensemble_result", AsyncMock(return_value=fake_er),
        ),
        patch.object(
            main_module, "ensemble_probability", return_value=(0.60, "medium"),
        ),
    ):
        result = await main_module._evaluate_market(**kwargs)

    assert result is False
    assert diag.get("buy_no_longshot_blocked") == 1, f"diag={diag}"
    kwargs["risk_manager"].size_position.assert_not_called()


@pytest.mark.asyncio
async def test_buy_no_allowed_at_mid_yes_price() -> None:
    """yes_price = 0.60 — BUY_NO must NOT be longshot-blocked (mid range)."""
    wm = _make_parsed_market(target_date=_near_future_date(), yes_price=0.60)
    diag: dict[str, int] = {}
    kwargs = _make_eval_kwargs(wm, diag=diag)

    # prob=0.45 vs yes=0.60 -> raw_edge=0.15 on NO side -> BUY_NO with edge.
    fake_er = SimpleNamespace(members=[60.0, 61.0, 62.0], member_count=3)
    with (
        patch.object(
            main_module, "fetch_ensemble_result", AsyncMock(return_value=fake_er),
        ),
        patch.object(
            main_module, "ensemble_probability", return_value=(0.45, "medium"),
        ),
    ):
        await main_module._evaluate_market(**kwargs)

    # Longshot filter did NOT fire
    assert "buy_no_longshot_blocked" not in diag, f"diag={diag}"
    # Progressed past the filter to risk sizing
    kwargs["risk_manager"].size_position.assert_called_once()
