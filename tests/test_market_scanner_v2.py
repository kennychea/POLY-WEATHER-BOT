"""Tests for weather/market_scanner.py — Moon Dev parsing."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from weather.market_scanner import (
    ParsedWeatherMarket,
    get_market_price,
    parse_weather_question,
    scan_weather_markets,
)


def _market(question: str, **extra: object) -> dict:
    """Helper to create a minimal market dict."""
    m = {"question": question, "conditionId": "test123"}
    m.update(extra)  # type: ignore[arg-type]
    return m


# ---------------------------------------------------------------------------
# Bucket markets: "between X-Y°F"
# ---------------------------------------------------------------------------


class TestBucketParsing:
    def test_basic_bucket(self) -> None:
        q = "Will the highest temperature in New York City be between 40-41°F on April 8?"
        result = parse_weather_question(_market(q), q)
        assert result is not None
        assert result.location == "New York"
        assert result.metric == "temp_high"
        assert result.threshold_low == 40.0
        assert result.threshold_high == 41.0
        assert result.direction == "bucket"
        assert result.unit == "fahrenheit"
        assert result.target_date.endswith("-04-08")

    def test_bucket_celsius(self) -> None:
        q = "Will the highest temperature in Chicago be between 10-15°C on April 10?"
        result = parse_weather_question(_market(q), q)
        assert result is not None
        assert result.threshold_low == 10.0
        assert result.threshold_high == 15.0
        assert result.unit == "celsius"

    def test_lowest_temperature_bucket(self) -> None:
        q = "Will the lowest temperature in Miami be between 60-65°F on April 12?"
        result = parse_weather_question(_market(q), q)
        assert result is not None
        assert result.metric == "temp_low"
        assert result.direction == "bucket"


# ---------------------------------------------------------------------------
# Threshold markets: "X°F or above/below/higher"
# ---------------------------------------------------------------------------


class TestThresholdParsing:
    def test_or_higher(self) -> None:
        q = "Will the highest temperature in NYC be 74°F or higher on April 5?"
        result = parse_weather_question(_market(q), q)
        assert result is not None
        assert result.threshold_low == 74.0
        assert result.threshold_high is None
        assert result.direction == "above"

    def test_or_below(self) -> None:
        q = "Will the highest temperature in Boston be 15°C or below on April 9?"
        result = parse_weather_question(_market(q), q)
        assert result is not None
        assert result.threshold_low == 15.0
        assert result.threshold_high is None
        assert result.direction == "below"

    def test_or_above(self) -> None:
        q = "Will the highest temperature in Denver be 80°F or above on April 7?"
        result = parse_weather_question(_market(q), q)
        assert result is not None
        assert result.direction == "above"

    def test_or_lower(self) -> None:
        q = "Will the lowest temperature in Seattle be 40°F or lower on April 6?"
        result = parse_weather_question(_market(q), q)
        assert result is not None
        assert result.direction == "below"
        assert result.metric == "temp_low"


# ---------------------------------------------------------------------------
# Exact markets: "be X°C"
# ---------------------------------------------------------------------------


class TestExactParsing:
    def test_exact_fahrenheit(self) -> None:
        q = "Will the highest temperature in Phoenix be 100°F on April 15?"
        result = parse_weather_question(_market(q), q)
        assert result is not None
        assert result.threshold_low == 99.5
        assert result.threshold_high == 100.5
        assert result.direction == "bucket"

    def test_exact_celsius(self) -> None:
        q = "Will the highest temperature in Houston be 27°C on April 5?"
        result = parse_weather_question(_market(q), q)
        assert result is not None
        assert result.threshold_low == 26.5
        assert result.threshold_high == 27.5


# ---------------------------------------------------------------------------
# Date extraction
# ---------------------------------------------------------------------------


class TestDateExtraction:
    @patch("weather.market_scanner.datetime")
    def test_month_and_day(self, mock_dt: object) -> None:
        # Mock datetime.now to return a fixed year
        from unittest.mock import PropertyMock
        from datetime import datetime, timezone

        with patch("weather.market_scanner.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 4, 7, tzinfo=timezone.utc)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            q = "Will the highest temperature in Atlanta be 80°F or above on April 12?"
            result = parse_weather_question(_market(q), q)
            assert result is not None
            assert result.target_date == "2026-04-12"

    def test_iso_date_in_question(self) -> None:
        q = "Will the highest temperature in Dallas be 90°F or above on 2026-04-15?"
        result = parse_weather_question(_market(q), q)
        assert result is not None
        assert result.target_date == "2026-04-15"


# ---------------------------------------------------------------------------
# Rejection cases
# ---------------------------------------------------------------------------


class TestSlugFiltering:
    """Gap 1: Markets with weather slug but no 'temperature' in question text."""

    def test_slug_highest_temperature(self) -> None:
        q = "Will NYC be between 40-41°F on April 8?"
        m = _market(q, slug="highest-temperature-nyc-april-8")
        result = parse_weather_question(m, q)
        assert result is not None
        assert result.location == "New York"

    def test_slug_lowest_temperature(self) -> None:
        q = "Will Chicago be 20°F or below on April 10?"
        m = _market(q, slug="lowest-temperature-chicago-april-10")
        result = parse_weather_question(m, q)
        assert result is not None
        assert result.location == "Chicago"

    def test_no_slug_no_keyword_rejected(self) -> None:
        q = "Will NYC be hot on April 8?"
        m = _market(q, slug="something-else")
        result = parse_weather_question(m, q)
        assert result is None


class TestFahrenheitEdgeCases:
    """Gap 3: Fahrenheit detection for 'f?' pattern."""

    def test_f_question_mark_pattern(self) -> None:
        q = "Will the highest temperature in Miami be 80 F?"
        result = parse_weather_question(_market(q), q)
        assert result is not None
        assert result.unit == "fahrenheit"


class TestDateFallback:
    """Gap 2: Fallback to tomorrow when no date found."""

    def test_no_date_returns_tomorrow(self) -> None:
        q = "Will the highest temperature in NYC be 74°F or higher?"
        result = parse_weather_question(_market(q), q)
        assert result is not None
        # Should be a valid ISO date, not "unknown"
        assert result.target_date != "unknown"
        assert len(result.target_date) == 10  # YYYY-MM-DD format


class TestRejection:
    def test_no_temperature_keyword(self) -> None:
        q = "Will Bitcoin reach $100k by June?"
        result = parse_weather_question(_market(q), q)
        assert result is None

    def test_unknown_city(self) -> None:
        q = "Will the highest temperature in London be 20°C on April 5?"
        result = parse_weather_question(_market(q), q)
        assert result is None

    def test_no_threshold_parseable(self) -> None:
        q = "Will the temperature in Chicago be nice on April 5?"
        result = parse_weather_question(_market(q), q)
        assert result is None

    def test_empty_question(self) -> None:
        result = parse_weather_question(_market(""), "")
        assert result is None


# ---------------------------------------------------------------------------
# scan_weather_markets
# ---------------------------------------------------------------------------


class TestScanWeatherMarkets:
    def test_filters_weather_from_mixed(self) -> None:
        markets = [
            _market("Will the highest temperature in NYC be 74°F or higher on April 5?"),
            _market("Will Bitcoin reach $100k by June?"),
            _market("Will the highest temperature in Miami be between 80-85°F on April 8?"),
            _market(""),
        ]
        results = scan_weather_markets(markets)
        assert len(results) == 2
        assert results[0].location == "New York"
        assert results[1].location == "Miami"


# ---------------------------------------------------------------------------
# get_market_price
# ---------------------------------------------------------------------------


class TestGetMarketPrice:
    def test_json_string_prices(self) -> None:
        m = {"outcomePrices": '[0.45, 0.55]'}
        result = get_market_price(m)
        assert result is not None
        assert result[0] == pytest.approx(0.45)
        assert result[1] == pytest.approx(0.55)

    def test_list_prices(self) -> None:
        m = {"outcomePrices": [0.30, 0.70]}
        result = get_market_price(m)
        assert result is not None
        assert result[0] == pytest.approx(0.30)

    def test_missing_prices(self) -> None:
        assert get_market_price({}) is None

    def test_empty_string_prices(self) -> None:
        assert get_market_price({"outcomePrices": ""}) is None

    def test_invalid_json(self) -> None:
        assert get_market_price({"outcomePrices": "not json"}) is None

    def test_single_price(self) -> None:
        assert get_market_price({"outcomePrices": "[0.5]"}) is None

    def test_string_number_prices(self) -> None:
        m = {"outcomePrices": '["0.45", "0.55"]'}
        result = get_market_price(m)
        assert result is not None
        assert result[0] == pytest.approx(0.45)
