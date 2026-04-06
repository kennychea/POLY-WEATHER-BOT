"""Weather market scanner — finds and parses Polymarket weather markets.

Parses real Polymarket weather question formats:
  - Bucket: "Will the highest temperature in NYC be between 40-41°F on April 8?"
  - Exact:  "Will the highest temperature in Madrid be 27°C on April 5?"
  - Threshold: "Will the highest temperature in London be 15°C or below on April 9?"
  - Threshold: "Will the highest temperature in NYC be 74°F or higher on April 5?"

Architecture ported from Moon Dev's scan_edge.py.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

# City aliases for matching Polymarket question text
CITY_ALIASES: dict[str, list[str]] = {
    "New York":      ["new york city", "new york", "nyc"],
    "Los Angeles":   ["los angeles"],
    "Chicago":       ["chicago"],
    "Miami":         ["miami"],
    "Washington DC": ["washington dc", "washington d.c."],
    "Houston":       ["houston"],
    "Phoenix":       ["phoenix"],
    "Denver":        ["denver"],
    "Seattle":       ["seattle"],
    "Boston":        ["boston"],
    "Atlanta":       ["atlanta"],
    "Dallas":        ["dallas"],
    "San Francisco": ["san francisco"],
    "Minneapolis":   ["minneapolis"],
    "Detroit":       ["detroit"],
    "Austin":        ["austin"],
}

# Month names for date extraction
_MONTHS: dict[str, int] = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}


@dataclass(frozen=True, slots=True)
class ParsedWeatherMarket:
    """A parsed weather market with extracted parameters."""

    market: dict[str, Any]      # Raw Polymarket market dict
    location: str               # Normalized city name
    metric: str                 # "temp_high" or "temp_low"
    threshold_low: float        # Lower threshold
    threshold_high: float | None  # Upper threshold (bucket/exact) or None (threshold)
    direction: str              # "above", "below", or "bucket"
    target_date: str            # ISO date "2026-04-08"
    unit: str                   # "fahrenheit" or "celsius"


def scan_weather_markets(markets: list[dict[str, Any]]) -> list[ParsedWeatherMarket]:
    """Scan indexed markets and extract weather-related ones."""
    results = []
    for m in markets:
        question = m.get("question", "")
        if not question:
            continue

        parsed = parse_weather_question(m, question)
        if parsed is not None:
            results.append(parsed)

    logger.info("Found %d weather markets out of %d total", len(results), len(markets))
    return results


def parse_weather_question(
    market: dict[str, Any],
    question: str,
) -> ParsedWeatherMarket | None:
    """Parse a Polymarket weather question into structured params.

    Handles:
      "...be between 40-41°F on April 8?"     -> bucket
      "...be 27°C on April 5?"                -> exact
      "...be 15°C or below on April 9?"       -> threshold
      "...be 74°F or higher on April 5?"      -> threshold
    """
    q_lower = question.lower()
    slug = (market.get("slug") or "").lower()

    # Match by question text OR slug (scan_edge.py checks both)
    has_temp_keyword = "temperature" in q_lower
    has_temp_slug = "highest-temperature" in slug or "lowest-temperature" in slug
    if not has_temp_keyword and not has_temp_slug:
        return None

    # --- City ---
    city = _extract_city(q_lower)
    if city is None:
        return None

    # --- Metric (highest / lowest) ---
    metric = "temp_low" if "lowest" in q_lower else "temp_high"

    # --- Temperature unit ---
    is_fahrenheit = (
        "°f" in q_lower
        or "°f" in question
        or "f?" in q_lower.replace(" ", "")
    )
    if not is_fahrenheit:
        is_fahrenheit = question.rstrip("?").rstrip().endswith("F")
    unit = "fahrenheit" if is_fahrenheit else "celsius"

    # --- Parse market type + thresholds ---
    # Type 1: Bucket "between X-Y°F"
    bucket_match = re.search(
        r"between\s+(\d+)-(\d+)\s*°?\s*[°fFcC]", question, re.IGNORECASE,
    )
    if bucket_match:
        threshold_low = float(bucket_match.group(1))
        threshold_high = float(bucket_match.group(2))
        direction = "bucket"
    else:
        threshold_high = None
        # Type 2: Threshold "X°F or below/above/higher/lower"
        thresh_match = re.search(
            r"(\d+)\s*°?\s*[°fFcC]\s+or\s+(below|above|higher|lower|more|less)",
            question, re.IGNORECASE,
        )
        if thresh_match:
            threshold_low = float(thresh_match.group(1))
            word = thresh_match.group(2).lower()
            direction = "below" if word in ("below", "lower", "less") else "above"
        else:
            # Type 3: Exact "be X°C" or "be X°F"
            exact_match = re.search(
                r"be\s+(\d+)\s*°?\s*[°fFcC]", question, re.IGNORECASE,
            )
            if exact_match:
                val = float(exact_match.group(1))
                threshold_low = val - 0.5
                threshold_high = val + 0.5
                direction = "bucket"
            else:
                return None

    # --- Date ---
    target_date = _extract_date(q_lower)

    return ParsedWeatherMarket(
        market=market,
        location=city,
        metric=metric,
        threshold_low=threshold_low,
        threshold_high=threshold_high,
        direction=direction,
        target_date=target_date,
        unit=unit,
    )


def get_market_price(market: dict[str, Any]) -> tuple[float, float] | None:
    """Extract YES/NO prices from market dict's outcomePrices field."""
    prices_raw = market.get("outcomePrices")
    if not prices_raw:
        return None
    try:
        prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
        if len(prices) >= 2:
            return float(prices[0]), float(prices[1])
    except (json.JSONDecodeError, ValueError, IndexError, TypeError):
        pass
    return None


def _extract_city(q_lower: str) -> str | None:
    """Extract city name from question text."""
    for city_name, aliases in CITY_ALIASES.items():
        for alias in aliases:
            if alias in q_lower:
                return city_name
    return None


def _extract_date(q_lower: str) -> str:
    """Extract target date from question. Returns ISO date string."""
    current_year = datetime.now(timezone.utc).year

    for month_name, month_num in _MONTHS.items():
        if month_name in q_lower:
            day_match = re.search(rf"{month_name}\s+(\d{{1,2}})", q_lower)
            if day_match:
                day = int(day_match.group(1))
                return f"{current_year}-{month_num:02d}-{day:02d}"
            return f"{current_year}-{month_num:02d}"

    # Try ISO date
    date_match = re.search(r"(\d{4}-\d{2}-\d{2})", q_lower)
    if date_match:
        return date_match.group(1)

    # Fallback: tomorrow (same as scan_edge.py)
    tomorrow = datetime.now(timezone.utc) + timedelta(days=1)
    return tomorrow.strftime("%Y-%m-%d")
