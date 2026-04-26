"""Tests for weather/market_question_parser.py.

Phase 2.1-quater. Pure parsing, no I/O. Patterns observed in production DB:
  - exact : "be 18°C on April 28?"          (~58 % of past-due)
  - bucket: "be between 88-89°F on April 28?" (~26 %)
  - above : "be 19°C or higher on April 28?" (~10 %)
  - below : "be 19°C or below on April 28?"  (~ 2 %)
"""

from __future__ import annotations

import pytest

from weather.market_question_parser import ParsedQuestion, parse_market_question


# ── happy path: each of the 4 patterns ────────────────────────────────


def test_parse_exact_celsius() -> None:
    q = "Will the highest temperature in Chengdu be 18\u00b0C on April 28?"
    p = parse_market_question(q)
    assert p == ParsedQuestion(
        metric="highest",
        city="Chengdu",
        month=4,
        day=28,
        direction="exact",
        threshold_low=18.0,
        threshold_high=18.0,
        unit="C",
    )


def test_parse_bucket_fahrenheit() -> None:
    q = "Will the highest temperature in Austin be between 88-89\u00b0F on April 28?"
    p = parse_market_question(q)
    assert p is not None
    assert p.direction == "bucket"
    assert p.threshold_low == 88.0
    assert p.threshold_high == 89.0
    assert p.unit == "F"
    assert p.city == "Austin"


def test_parse_above_celsius() -> None:
    q = "Will the highest temperature in Beijing be 19\u00b0C or higher on April 28?"
    p = parse_market_question(q)
    assert p is not None
    assert p.direction == "above"
    assert p.threshold_low == 19.0
    assert p.threshold_high is None


def test_parse_below_celsius() -> None:
    q = "Will the highest temperature in Tokyo be 12\u00b0C or below on April 28?"
    p = parse_market_question(q)
    assert p is not None
    assert p.direction == "below"
    assert p.threshold_low == 12.0
    assert p.threshold_high is None


# ── city normalization ───────────────────────────────────────────────


def test_parse_handles_city_suffix() -> None:
    q = "Will the highest temperature in New York City be between 52-53\u00b0F on April 19?"
    p = parse_market_question(q)
    assert p is not None
    assert p.city == "New York"  # strip trailing " City" so resolution_stations lookup works


def test_parse_keeps_mexico_city_intact() -> None:
    # "Mexico City" is the actual canonical city name — DO NOT strip
    q = "Will the highest temperature in Mexico City be 25\u00b0C on April 28?"
    p = parse_market_question(q)
    assert p is not None
    assert p.city == "Mexico City"


def test_parse_handles_lowest_metric() -> None:
    q = "Will the lowest temperature in Moscow be -5\u00b0C on April 28?"
    p = parse_market_question(q)
    assert p is not None
    assert p.metric == "lowest"
    assert p.threshold_low == -5.0


# ── unit detection ───────────────────────────────────────────────────


def test_unit_celsius_default() -> None:
    q = "Will the highest temperature in Paris be 17\u00b0C on April 28?"
    p = parse_market_question(q)
    assert p is not None
    assert p.unit == "C"


def test_unit_fahrenheit_marker() -> None:
    q = "Will the highest temperature in Miami be 84\u00b0F on April 28?"
    p = parse_market_question(q)
    assert p is not None
    assert p.unit == "F"


# ── failure modes (None, not raises) ─────────────────────────────────


def test_parse_none_on_empty() -> None:
    assert parse_market_question("") is None


def test_parse_none_on_garbage() -> None:
    assert parse_market_question("Will it rain tomorrow?") is None


def test_parse_none_on_missing_threshold() -> None:
    assert parse_market_question(
        "Will the highest temperature in Tokyo be hot on April 28?"
    ) is None


# ── corrupted unicode (DB stored � for ° in some rows) ───────────────


def test_parse_handles_corrupted_degree_sign() -> None:
    # Some rows stored � (replacement char) instead of ° due to encoding mishaps
    q = "Will the highest temperature in Chengdu be 18\ufffdC on April 28?"
    p = parse_market_question(q)
    assert p is not None
    assert p.threshold_low == 18.0
    assert p.unit == "C"


# ── METAR comparison helper ──────────────────────────────────────────


def test_check_outcome_exact_match() -> None:
    from weather.market_question_parser import check_outcome

    p = parse_market_question("Will the highest temperature in Austin be 85\u00b0F on April 28?")
    assert p is not None
    # Daily high obs (Fahrenheit) — exact whole-degree match
    assert check_outcome(p, observed_high_f=85.0, observed_low_f=70.0) == 1
    assert check_outcome(p, observed_high_f=84.0, observed_low_f=70.0) == 0
    assert check_outcome(p, observed_high_f=86.0, observed_low_f=70.0) == 0


def test_check_outcome_bucket() -> None:
    from weather.market_question_parser import check_outcome

    p = parse_market_question(
        "Will the highest temperature in Austin be between 88-89\u00b0F on April 28?"
    )
    assert p is not None
    assert check_outcome(p, observed_high_f=88.0, observed_low_f=70.0) == 1
    assert check_outcome(p, observed_high_f=89.0, observed_low_f=70.0) == 1
    # Polymarket buckets are exclusive on upper bound: 89-90 is the next bucket.
    # We treat the upper bound as inclusive for v1 (matches scan_edge.py); over-collapse
    # is acceptable because Polymarket source has priority.
    assert check_outcome(p, observed_high_f=87.0, observed_low_f=70.0) == 0
    assert check_outcome(p, observed_high_f=90.0, observed_low_f=70.0) == 0


def test_check_outcome_above() -> None:
    from weather.market_question_parser import check_outcome

    p = parse_market_question(
        "Will the highest temperature in Miami be 90\u00b0F or higher on April 28?"
    )
    assert p is not None
    assert check_outcome(p, observed_high_f=90.0, observed_low_f=70.0) == 1
    assert check_outcome(p, observed_high_f=95.0, observed_low_f=70.0) == 1
    assert check_outcome(p, observed_high_f=89.0, observed_low_f=70.0) == 0


def test_check_outcome_below() -> None:
    from weather.market_question_parser import check_outcome

    p = parse_market_question(
        "Will the highest temperature in Seattle be 50\u00b0F or below on April 28?"
    )
    assert p is not None
    assert check_outcome(p, observed_high_f=50.0, observed_low_f=40.0) == 1
    assert check_outcome(p, observed_high_f=45.0, observed_low_f=40.0) == 1
    assert check_outcome(p, observed_high_f=51.0, observed_low_f=40.0) == 0


def test_check_outcome_celsius_to_fahrenheit_conversion() -> None:
    from weather.market_question_parser import check_outcome

    # 18°C ≈ 64.4°F — METAR returns Fahrenheit, so internal conversion is required
    p = parse_market_question("Will the highest temperature in Tokyo be 18\u00b0C on April 28?")
    assert p is not None
    # Whole-degree C match: obs_F rounded to nearest C should equal 18
    assert check_outcome(p, observed_high_f=64.4, observed_low_f=50.0) == 1
    assert check_outcome(p, observed_high_f=66.2, observed_low_f=50.0) == 0  # 19 °C


def test_check_outcome_uses_lowest_metric_for_lowest_market() -> None:
    """Markets asking about LOWEST temperature must resolve against observed low, not high."""
    from weather.market_question_parser import check_outcome

    p = parse_market_question(
        "Will the lowest temperature in Moscow be -5\u00b0C or below on April 28?"
    )
    assert p is not None
    # -5°C ≈ 23°F. observed_low_f = 23.0 → match, observed_high_f irrelevant
    assert check_outcome(p, observed_high_f=50.0, observed_low_f=23.0) == 1
    assert check_outcome(p, observed_high_f=50.0, observed_low_f=30.0) == 0
