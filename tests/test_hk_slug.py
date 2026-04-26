"""Slug round-trip and HK extreme-price fallback tests.

Phase 2.1-quater HK fix. Verifies that:
  1. Every alias in `_QUESTION_CITY_TO_SLUG` round-trips correctly through
     `_build_slug_from_question` (regression guard against future slug-map
     edits silently breaking resolution).
  2. The extreme-price fallback added to `backfill_weather_signals_outcomes`
     handles the 5 scenarios spec'd in tasks/prompt_hk_fix_GO.md.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from market_data.indexer import MarketIndexer, _QUESTION_CITY_TO_SLUG
from scripts import backfill_weather_signals_outcomes as bf  # type: ignore[import-not-found]


# ── slug roundtrip regression guard ────────────────────────────────────


def _synthetic_question(city_alias: str) -> str:
    """Build a Polymarket-style question that contains the given city alias."""
    # Title-case the alias for the question text; the parser is case-insensitive.
    pretty = city_alias.title()
    return f"Will the highest temperature in {pretty} be 25\u00b0C on April 25?"


def test_every_question_city_alias_roundtrips_to_expected_slug() -> None:
    """If `_QUESTION_CITY_TO_SLUG` says alias X maps to slug Y, then a synthetic
    question containing X must produce exactly slug Y when run through
    `_build_slug_from_question`. Regression guard for future edits to the map."""
    failures: list[tuple[str, str, str | None]] = []
    for alias, expected_slug_fragment in _QUESTION_CITY_TO_SLUG.items():
        # Skip alias that's a strict substring of a longer alias to avoid the
        # longer-first match hijacking (e.g., "nyc" gets resolved by "new york city" first).
        # The map is intentionally ordered long→short, so just trust it as-is.
        question = _synthetic_question(alias)
        built = MarketIndexer._build_slug_from_question(question)
        if built is None:
            failures.append((alias, "(None)", None))
            continue
        # Built form: "{metric}-temperature-in-{city_slug}-on-{month}-{day}-{year}"
        # We assert the city slug fragment appears between "in-" and "-on-".
        if f"in-{expected_slug_fragment}-on-" not in built:
            failures.append((alias, expected_slug_fragment, built))
    assert not failures, (
        "Slug roundtrip failures: "
        + ", ".join(f"{a}->expected={e},built={b}" for a, e, b in failures)
    )


def test_hong_kong_slug_builds_canonical_form() -> None:
    """Direct anchor for the HK case that motivated this whole fix."""
    q = "Will the highest temperature in Hong Kong be 23\u00b0C on April 25?"
    built = MarketIndexer._build_slug_from_question(q)
    assert built is not None
    assert built.startswith("highest-temperature-in-hong-kong-on-april-25-")


# ── extreme-price fallback (orchestration helper) ─────────────────────


def _make_event_markets(question: str, yes_price: float) -> list[dict]:
    """Build a single-element 'markets' list mimicking Gamma /events response."""
    return [{
        "question": question,
        "closed": False,  # the whole point of extreme-price: closed=False but settled
        "outcomePrices": f'["{yes_price}", "{1 - yes_price}"]',
    }]


async def test_extreme_price_resolves_yes_when_price_high_and_past_due() -> None:
    q = "Will the highest temperature in Hong Kong be 23\u00b0C on April 25?"
    today = date(2026, 4, 27)  # April 25 < April 26 (today-1) → guard passes
    fetcher = AsyncMock(return_value=_make_event_markets(q, 0.9995))
    out, src = await bf.resolve_via_extreme_price(
        market_question=q, forecast_date="2026-04-25", today=today,
        fetch_event_markets=fetcher,
    )
    assert (out, src) == (1, "polymarket_extreme_price")
    fetcher.assert_awaited_once()


async def test_extreme_price_resolves_no_when_price_low_and_past_due() -> None:
    q = "Will the highest temperature in Hong Kong be 30\u00b0C on April 25?"
    today = date(2026, 4, 27)
    fetcher = AsyncMock(return_value=_make_event_markets(q, 0.0005))
    out, src = await bf.resolve_via_extreme_price(
        market_question=q, forecast_date="2026-04-25", today=today,
        fetch_event_markets=fetcher,
    )
    assert (out, src) == (0, "polymarket_extreme_price")


async def test_extreme_price_falls_through_on_mid_price() -> None:
    q = "Will the highest temperature in Hong Kong be 25\u00b0C on April 25?"
    today = date(2026, 4, 27)
    fetcher = AsyncMock(return_value=_make_event_markets(q, 0.50))
    out, src = await bf.resolve_via_extreme_price(
        market_question=q, forecast_date="2026-04-25", today=today,
        fetch_event_markets=fetcher,
    )
    assert (out, src) == (None, "")  # caller will try METAR


async def test_extreme_price_falls_through_at_subthreshold_yes() -> None:
    """0.95 is not 'extreme enough' — must be ≥0.99 to count as settled."""
    q = "Will the highest temperature in Hong Kong be 27\u00b0C on April 25?"
    today = date(2026, 4, 27)
    fetcher = AsyncMock(return_value=_make_event_markets(q, 0.95))
    out, src = await bf.resolve_via_extreme_price(
        market_question=q, forecast_date="2026-04-25", today=today,
        fetch_event_markets=fetcher,
    )
    assert (out, src) == (None, "")


async def test_extreme_price_blocked_by_temporal_guard_on_yesterday() -> None:
    """target_date == today-1 → guard fails, gives Polymarket 24h to formally close."""
    q = "Will the highest temperature in Hong Kong be 25\u00b0C on April 26?"
    today = date(2026, 4, 27)  # target_date 2026-04-26 == today-1 → guard FAILS
    fetcher = AsyncMock(return_value=_make_event_markets(q, 0.99))
    out, src = await bf.resolve_via_extreme_price(
        market_question=q, forecast_date="2026-04-26", today=today,
        fetch_event_markets=fetcher,
    )
    assert (out, src) == (None, "")
    fetcher.assert_not_called()  # short-circuit before HTTP


async def test_extreme_price_handles_question_not_in_event() -> None:
    """If the slug event has no market matching our question, return empty."""
    today = date(2026, 4, 27)
    other_q = "Will the highest temperature in Hong Kong be 99\u00b0C on April 25?"
    fetcher = AsyncMock(return_value=_make_event_markets(other_q, 0.9995))
    out, src = await bf.resolve_via_extreme_price(
        market_question="Will the highest temperature in Hong Kong be 23\u00b0C on April 25?",
        forecast_date="2026-04-25", today=today,
        fetch_event_markets=fetcher,
    )
    assert (out, src) == (None, "")


async def test_extreme_price_handles_no_event_returned() -> None:
    today = date(2026, 4, 27)
    fetcher = AsyncMock(return_value=None)
    out, src = await bf.resolve_via_extreme_price(
        market_question="Q",
        forecast_date="2026-04-25", today=today,
        fetch_event_markets=fetcher,
    )
    assert (out, src) == (None, "")


# ── full orchestration: extreme-price slot in resolve_outcome ─────────


async def test_resolve_outcome_polymarket_closed_skips_extreme_price() -> None:
    """When polymarket source returns a clean closed resolution, extreme-price is skipped."""
    from infra.types import MarketResolution
    from unittest.mock import MagicMock

    indexer = MagicMock()
    indexer.check_resolution = AsyncMock(
        return_value=MarketResolution(condition_id="x", resolution_price=1.0, closed=True, source="gamma_resolved")
    )
    metar = MagicMock()
    metar.fetch_daily_extremes = AsyncMock()
    fetcher = AsyncMock()  # extreme-price fetcher

    out, src = await bf.resolve_outcome(
        market_id="x",
        market_question="Q",
        forecast_date="2026-04-25",
        indexer=indexer,
        metar_fetcher=metar,
        fetch_event_markets=fetcher,
        today=date(2026, 4, 27),
    )
    assert (out, src) == (1, "polymarket")
    fetcher.assert_not_called()
    metar.fetch_daily_extremes.assert_not_called()


async def test_resolve_outcome_extreme_price_resolves_when_polymarket_missing() -> None:
    """polymarket → None falls through to extreme-price, which resolves."""
    from unittest.mock import MagicMock

    indexer = MagicMock()
    indexer.check_resolution = AsyncMock(return_value=None)
    metar = MagicMock()
    metar.fetch_daily_extremes = AsyncMock()
    q = "Will the highest temperature in Hong Kong be 23\u00b0C on April 25?"
    fetcher = AsyncMock(return_value=_make_event_markets(q, 0.9995))

    out, src = await bf.resolve_outcome(
        market_id="x",
        market_question=q,
        forecast_date="2026-04-25",
        indexer=indexer,
        metar_fetcher=metar,
        fetch_event_markets=fetcher,
        today=date(2026, 4, 27),
    )
    assert (out, src) == (1, "polymarket_extreme_price")
    metar.fetch_daily_extremes.assert_not_called()


async def test_resolve_outcome_metar_after_polymarket_and_extreme_price_both_miss() -> None:
    from unittest.mock import MagicMock

    indexer = MagicMock()
    indexer.check_resolution = AsyncMock(return_value=None)
    q = "Will the highest temperature in Miami be 90\u00b0F or higher on April 25?"
    fetcher = AsyncMock(return_value=_make_event_markets(q, 0.50))  # mid-price → extreme-price falls through
    metar = MagicMock()
    metar.fetch_daily_extremes = AsyncMock(
        return_value=[{"date": "2026-04-25", "max_tempF": 95.0, "min_tempF": 70.0}]
    )

    out, src = await bf.resolve_outcome(
        market_id="x",
        market_question=q,
        forecast_date="2026-04-25",
        indexer=indexer,
        metar_fetcher=metar,
        fetch_event_markets=fetcher,
        today=date(2026, 4, 27),
    )
    assert (out, src) == (1, "metar")
