"""Tests for weather/ensemble.py — Open-Meteo ensemble fetcher."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from weather.ensemble import (
    CITY_ALIASES,
    US_CITIES,
    _cache_get,
    _cache_stats,
    fetch_ensemble,
    fetch_ensemble_result,
    get_cache_stats,
    reset_cache_stats,
    resolve_city,
)


# ---------------------------------------------------------------------------
# resolve_city
# ---------------------------------------------------------------------------


class TestResolveCity:
    def test_exact_alias(self) -> None:
        assert resolve_city("new york city") == "New York"

    def test_alias_nyc(self) -> None:
        assert resolve_city("Will the temperature in NYC be...") == "New York"

    def test_alias_chicago(self) -> None:
        assert resolve_city("chicago weather tomorrow") == "Chicago"

    def test_unknown_city(self) -> None:
        assert resolve_city("London weather") is None

    def test_case_insensitive(self) -> None:
        assert resolve_city("MIAMI is hot") == "Miami"

    def test_all_cities_have_coords(self) -> None:
        for city_name in CITY_ALIASES:
            assert city_name in US_CITIES, f"{city_name} missing from US_CITIES"


# ---------------------------------------------------------------------------
# fetch_ensemble (mocked HTTP)
# ---------------------------------------------------------------------------

def _make_hourly_response(n_members: int = 3, base_temp: float = 70.0) -> dict:
    """Build a realistic Open-Meteo hourly response with n_members."""
    hourly: dict[str, list[float]] = {}
    # Control run
    hourly["time"] = [f"2026-04-08T{h:02d}:00" for h in range(24)]
    hourly["temperature_2m"] = [base_temp + h * 0.5 for h in range(24)]
    # Perturbed members
    for i in range(1, n_members):
        key = f"temperature_2m_member{i:02d}"
        hourly[key] = [base_temp + h * 0.5 + i for h in range(24)]
    return {"hourly": hourly}


class TestFetchEnsemble:
    @pytest.mark.asyncio
    async def test_fetch_parses_members(self) -> None:
        resp_data = _make_hourly_response(n_members=4, base_temp=60.0)

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=resp_data)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)

        with patch("weather.ensemble._cache_get", return_value=None), \
             patch("weather.ensemble._cache_set"):
            result = await fetch_ensemble(
                "New York", 40.71, -74.01, "2026-04-08", "fahrenheit", mock_session,
            )

        assert result is not None
        assert "temperature_max" in result
        assert "temperature_min" in result
        assert len(result["temperature_max"]) == 4  # control + 3 perturbed
        assert len(result["temperature_min"]) == 4

    @pytest.mark.asyncio
    async def test_fetch_returns_none_on_http_error(self) -> None:
        mock_resp = AsyncMock()
        mock_resp.status = 500
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)

        with patch("weather.ensemble._cache_get", return_value=None):
            result = await fetch_ensemble(
                "New York", 40.71, -74.01, "2026-04-08", "fahrenheit", mock_session,
            )
        assert result is None

    @pytest.mark.asyncio
    async def test_fetch_uses_cache(self) -> None:
        cached = {
            "temperature_max": [80.0, 82.0, 81.0],
            "temperature_min": [60.0, 61.0, 59.0],
        }
        with patch("weather.ensemble._cache_get", return_value={**cached, "_ts": 0}):
            result = await fetch_ensemble(
                "New York", 40.71, -74.01, "2026-04-08", "fahrenheit", None,
            )
        assert result is not None
        assert result["temperature_max"] == [80.0, 82.0, 81.0]
        # _ts should be stripped
        assert "_ts" not in result


# ---------------------------------------------------------------------------
# fetch_ensemble_result
# ---------------------------------------------------------------------------


class TestFetchEnsembleResult:
    @pytest.mark.asyncio
    async def test_returns_ensemble_result(self) -> None:
        raw = {
            "temperature_max": [78.0, 80.0, 82.0],
            "temperature_min": [55.0, 57.0, 59.0],
        }
        with patch("weather.ensemble.fetch_ensemble", return_value=raw):
            result = await fetch_ensemble_result(
                "New York", "2026-04-08", "temp_high", "fahrenheit",
            )
        assert result is not None
        assert result.location == "New York"
        assert result.members == (78.0, 80.0, 82.0)
        assert result.member_count == 3
        assert result.metric == "temp_high"
        assert result.model == "gfs_seamless"

    @pytest.mark.asyncio
    async def test_temp_low_metric(self) -> None:
        raw = {
            "temperature_max": [78.0, 80.0],
            "temperature_min": [55.0, 57.0],
        }
        with patch("weather.ensemble.fetch_ensemble", return_value=raw):
            result = await fetch_ensemble_result(
                "Chicago", "2026-04-08", "temp_low", "fahrenheit",
            )
        assert result is not None
        assert result.members == (55.0, 57.0)

    @pytest.mark.asyncio
    async def test_unknown_city_returns_none(self) -> None:
        result = await fetch_ensemble_result(
            "London", "2026-04-08", "temp_high", "fahrenheit",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_fetch_failure_returns_none(self) -> None:
        with patch("weather.ensemble.fetch_ensemble", return_value=None):
            result = await fetch_ensemble_result(
                "New York", "2026-04-08", "temp_high", "fahrenheit",
            )
        assert result is None


# ---------------------------------------------------------------------------
# L1 session cache
# ---------------------------------------------------------------------------


class TestSessionCacheL1Hit:
    """Second call with same params returns cached result without API."""

    @pytest.mark.asyncio
    async def test_session_cache_l1_hit(self) -> None:
        raw = {
            "temperature_max": [78.0, 80.0, 82.0],
            "temperature_min": [55.0, 57.0, 59.0],
        }
        session_cache: dict = {}
        reset_cache_stats()

        with patch("weather.ensemble.fetch_ensemble", return_value=raw) as mock_fetch:
            # First call — populates L1
            r1 = await fetch_ensemble_result(
                "New York", "2026-04-08", "temp_high", "fahrenheit",
                session_cache=session_cache,
            )
            # Second call — should hit L1, no API call
            r2 = await fetch_ensemble_result(
                "New York", "2026-04-08", "temp_high", "fahrenheit",
                session_cache=session_cache,
            )

        assert r1 is not None
        assert r2 is r1  # exact same object from cache
        assert mock_fetch.call_count == 1  # only one API call
        stats = get_cache_stats()
        assert stats["l1_hits"] >= 1


class TestSessionCacheDifferentCityMisses:
    """Different city bypasses L1 cache."""

    @pytest.mark.asyncio
    async def test_session_cache_different_city_misses(self) -> None:
        raw = {
            "temperature_max": [78.0, 80.0, 82.0],
            "temperature_min": [55.0, 57.0, 59.0],
        }
        session_cache: dict = {}
        reset_cache_stats()

        with patch("weather.ensemble.fetch_ensemble", return_value=raw) as mock_fetch:
            r1 = await fetch_ensemble_result(
                "New York", "2026-04-08", "temp_high", "fahrenheit",
                session_cache=session_cache,
            )
            r2 = await fetch_ensemble_result(
                "Chicago", "2026-04-08", "temp_high", "fahrenheit",
                session_cache=session_cache,
            )

        assert r1 is not None
        assert r2 is not None
        assert r1 is not r2
        assert mock_fetch.call_count == 2  # both hit API
        stats = get_cache_stats()
        assert stats["l1_hits"] == 0


class TestSessionCacheNoneDisablesL1:
    """session_cache=None still works (backward compat)."""

    @pytest.mark.asyncio
    async def test_session_cache_none_disables_l1(self) -> None:
        raw = {
            "temperature_max": [78.0, 80.0, 82.0],
            "temperature_min": [55.0, 57.0, 59.0],
        }
        reset_cache_stats()

        with patch("weather.ensemble.fetch_ensemble", return_value=raw) as mock_fetch:
            r1 = await fetch_ensemble_result(
                "New York", "2026-04-08", "temp_high", "fahrenheit",
                session_cache=None,
            )
            r2 = await fetch_ensemble_result(
                "New York", "2026-04-08", "temp_high", "fahrenheit",
                # No session_cache arg — defaults to None
            )

        assert r1 is not None
        assert r2 is not None
        assert mock_fetch.call_count == 2  # no L1, both hit API
        stats = get_cache_stats()
        assert stats["l1_hits"] == 0


class TestCacheStatsIncrement:
    """Stats counters update correctly across L1, L2, and misses."""

    @pytest.mark.asyncio
    async def test_cache_stats_increment(self) -> None:
        raw = {
            "temperature_max": [78.0, 80.0, 82.0],
            "temperature_min": [55.0, 57.0, 59.0],
        }
        session_cache: dict = {}
        reset_cache_stats()

        with patch("weather.ensemble.fetch_ensemble", return_value=raw):
            # Miss — first fetch, no L1 or L2
            await fetch_ensemble_result(
                "New York", "2026-04-08", "temp_high", "fahrenheit",
                session_cache=session_cache,
            )
            # L1 hit — same params, session_cache populated
            await fetch_ensemble_result(
                "New York", "2026-04-08", "temp_high", "fahrenheit",
                session_cache=session_cache,
            )
            # Another L1 hit
            await fetch_ensemble_result(
                "New York", "2026-04-08", "temp_high", "fahrenheit",
                session_cache=session_cache,
            )

        stats = get_cache_stats()
        assert stats["l1_hits"] == 2
        # misses counted at fetch_ensemble level (mocked), so 0 here
        # but l1_hits are the important metric for this test
        assert stats["l1_hits"] > stats["l2_hits"]

        # Verify reset works
        reset_cache_stats()
        stats_after = get_cache_stats()
        assert stats_after == {"l1_hits": 0, "l2_hits": 0, "misses": 0}
