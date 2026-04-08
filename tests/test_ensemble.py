"""Tests for weather/ensemble.py — Open-Meteo ensemble fetcher."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from weather.ensemble import (
    CITY_ALIASES,
    MULTI_MODELS,
    US_CITIES,
    _cache_get,
    _cache_stats,
    fetch_ensemble,
    fetch_ensemble_result,
    fetch_multi_model_ensemble,
    fetch_multi_model_result,
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
        assert resolve_city("Timbuktu weather") is None

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
            "Timbuktu", "2026-04-08", "temp_high", "fahrenheit",
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


# ---------------------------------------------------------------------------
# fetch_ensemble with model parameter
# ---------------------------------------------------------------------------


class TestFetchEnsembleModelParam:
    @pytest.mark.asyncio
    async def test_model_param_passed_to_api(self) -> None:
        """Verify the model parameter is sent to Open-Meteo."""
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
            await fetch_ensemble(
                "New York", 40.71, -74.01, "2026-04-08", "fahrenheit",
                mock_session, model="ecmwf_ifs025",
            )

        # Check that the model param was passed in the API call
        call_kwargs = mock_session.get.call_args
        params = call_kwargs.kwargs.get("params") or call_kwargs[1].get("params")
        assert params["models"] == "ecmwf_ifs025"

    @pytest.mark.asyncio
    async def test_default_model_is_gfs(self) -> None:
        """Default model should be gfs_seamless for backward compat."""
        resp_data = _make_hourly_response(n_members=3, base_temp=70.0)

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=resp_data)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)

        with patch("weather.ensemble._cache_get", return_value=None), \
             patch("weather.ensemble._cache_set"):
            await fetch_ensemble(
                "New York", 40.71, -74.01, "2026-04-08", "fahrenheit",
                mock_session,
            )

        call_kwargs = mock_session.get.call_args
        params = call_kwargs.kwargs.get("params") or call_kwargs[1].get("params")
        assert params["models"] == "gfs_seamless"

    @pytest.mark.asyncio
    async def test_cache_key_includes_model(self) -> None:
        """Different models must have different cache keys."""
        with patch("weather.ensemble._cache_get") as mock_cache:
            mock_cache.return_value = {
                "temperature_max": [80.0], "temperature_min": [60.0],
            }
            await fetch_ensemble(
                "New York", 40.71, -74.01, "2026-04-08", "fahrenheit",
                None, model="ecmwf_ifs025",
            )

        cache_key = mock_cache.call_args[0][0]
        assert "ecmwf_ifs025" in cache_key


# ---------------------------------------------------------------------------
# fetch_multi_model_ensemble
# ---------------------------------------------------------------------------


class TestFetchMultiModelEnsemble:
    @pytest.mark.asyncio
    async def test_all_models_succeed(self) -> None:
        """All 3 models return data."""
        raw = {"temperature_max": [78.0, 80.0], "temperature_min": [55.0, 57.0]}

        with patch("weather.ensemble.fetch_ensemble", return_value=raw):
            result = await fetch_multi_model_ensemble(
                "New York", 40.71, -74.01, "2026-04-08",
            )

        assert len(result) == 3
        for model in MULTI_MODELS:
            assert model in result

    @pytest.mark.asyncio
    async def test_partial_failure(self) -> None:
        """One model fails, other 2 succeed."""
        raw = {"temperature_max": [78.0], "temperature_min": [55.0]}

        async def _side_effect(*args, **kwargs):
            if kwargs.get("model") == "icon_seamless":
                return None
            return raw

        with patch("weather.ensemble.fetch_ensemble", side_effect=_side_effect):
            result = await fetch_multi_model_ensemble(
                "New York", 40.71, -74.01, "2026-04-08",
            )

        assert len(result) == 2
        assert "gfs_seamless" in result
        assert "ecmwf_ifs025" in result
        assert "icon_seamless" not in result

    @pytest.mark.asyncio
    async def test_all_fail_returns_empty(self) -> None:
        """All models fail → empty dict."""
        with patch("weather.ensemble.fetch_ensemble", return_value=None):
            result = await fetch_multi_model_ensemble(
                "New York", 40.71, -74.01, "2026-04-08",
            )

        assert result == {}

    @pytest.mark.asyncio
    async def test_exception_handled_gracefully(self) -> None:
        """Exception from one model doesn't crash others."""
        raw = {"temperature_max": [78.0], "temperature_min": [55.0]}

        async def _side_effect(*args, **kwargs):
            if kwargs.get("model") == "ecmwf_ifs025":
                raise TimeoutError("API timeout")
            return raw

        with patch("weather.ensemble.fetch_ensemble", side_effect=_side_effect):
            result = await fetch_multi_model_ensemble(
                "New York", 40.71, -74.01, "2026-04-08",
            )

        assert len(result) == 2
        assert "ecmwf_ifs025" not in result


# ---------------------------------------------------------------------------
# fetch_multi_model_result
# ---------------------------------------------------------------------------


class TestFetchMultiModelResult:
    @pytest.mark.asyncio
    async def test_returns_dict_of_ensemble_results(self) -> None:
        raw = {"temperature_max": [78.0, 80.0, 82.0], "temperature_min": [55.0, 57.0, 59.0]}

        with patch("weather.ensemble.fetch_multi_model_ensemble", return_value={
            "gfs_seamless": raw, "ecmwf_ifs025": raw, "icon_seamless": raw,
        }):
            result = await fetch_multi_model_result(
                "New York", "2026-04-08", "temp_high", "fahrenheit",
            )

        assert result is not None
        assert len(result) == 3
        for model_name, er in result.items():
            assert er.location == "New York"
            assert er.model == model_name
            assert er.members == (78.0, 80.0, 82.0)

    @pytest.mark.asyncio
    async def test_all_fail_returns_none(self) -> None:
        with patch("weather.ensemble.fetch_multi_model_ensemble", return_value={}):
            result = await fetch_multi_model_result(
                "New York", "2026-04-08", "temp_high", "fahrenheit",
            )

        assert result is None

    @pytest.mark.asyncio
    async def test_unknown_city_returns_none(self) -> None:
        result = await fetch_multi_model_result(
            "Timbuktu", "2026-04-08", "temp_high", "fahrenheit",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_session_cache_l1_hit(self) -> None:
        raw = {"temperature_max": [78.0], "temperature_min": [55.0]}
        session_cache: dict = {}
        reset_cache_stats()

        with patch("weather.ensemble.fetch_multi_model_ensemble", return_value={
            "gfs_seamless": raw,
        }) as mock_fetch:
            r1 = await fetch_multi_model_result(
                "New York", "2026-04-08", "temp_high", "fahrenheit",
                session_cache=session_cache,
            )
            r2 = await fetch_multi_model_result(
                "New York", "2026-04-08", "temp_high", "fahrenheit",
                session_cache=session_cache,
            )

        assert r1 is not None
        assert r2 is r1  # exact same object from L1 cache
        assert mock_fetch.call_count == 1
