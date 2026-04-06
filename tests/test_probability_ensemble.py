"""Tests for weather/probability.py — ensemble member counting."""

from __future__ import annotations

import pytest

from weather.probability import ensemble_probability


class TestBucketProbability:
    """Bucket: count members in [low, high] range."""

    def test_basic_bucket(self) -> None:
        members = [38.0, 39.0, 40.0, 41.0, 42.0]
        prob, conf = ensemble_probability(members, 40.0, 41.0, "bucket")
        assert prob == pytest.approx(2 / 5)

    def test_all_in_bucket(self) -> None:
        members = [40.0, 40.5, 41.0]
        prob, conf = ensemble_probability(members, 40.0, 41.0, "bucket")
        assert prob == pytest.approx(1.0)
        assert conf == "high"

    def test_none_in_bucket(self) -> None:
        members = [30.0, 31.0, 32.0]
        prob, conf = ensemble_probability(members, 40.0, 41.0, "bucket")
        assert prob == pytest.approx(0.0)
        assert conf == "high"  # High confidence it's NOT in range

    def test_boundary_inclusive(self) -> None:
        members = [40.0, 41.0]
        prob, conf = ensemble_probability(members, 40.0, 41.0, "bucket")
        assert prob == pytest.approx(1.0)


class TestExactProbability:
    """Exact: "be X°C" maps to bucket [X-0.5, X+0.5]."""

    def test_exact_27(self) -> None:
        members = [26.0, 27.0, 27.2, 28.0]
        prob, conf = ensemble_probability(members, 26.5, 27.5, "bucket")
        assert prob == pytest.approx(2 / 4)

    def test_exact_none_match(self) -> None:
        members = [20.0, 21.0, 22.0]
        prob, conf = ensemble_probability(members, 26.5, 27.5, "bucket")
        assert prob == pytest.approx(0.0)


class TestThresholdAbove:
    """Threshold above: count members >= threshold."""

    def test_above_74(self) -> None:
        members = [70.0, 72.0, 74.0, 76.0]
        prob, conf = ensemble_probability(members, 74.0, None, "above")
        assert prob == pytest.approx(2 / 4)

    def test_all_above(self) -> None:
        members = [80.0, 82.0, 84.0]
        prob, conf = ensemble_probability(members, 74.0, None, "above")
        assert prob == pytest.approx(1.0)
        assert conf == "high"

    def test_none_above(self) -> None:
        members = [60.0, 62.0, 64.0]
        prob, conf = ensemble_probability(members, 74.0, None, "above")
        assert prob == pytest.approx(0.0)


class TestThresholdBelow:
    """Threshold below: count members <= threshold."""

    def test_below_72(self) -> None:
        members = [70.0, 72.0, 74.0, 76.0]
        prob, conf = ensemble_probability(members, 72.0, None, "below")
        assert prob == pytest.approx(2 / 4)

    def test_all_below(self) -> None:
        members = [50.0, 52.0, 54.0]
        prob, conf = ensemble_probability(members, 72.0, None, "below")
        assert prob == pytest.approx(1.0)


class TestEdgeCases:
    def test_empty_members(self) -> None:
        prob, conf = ensemble_probability([], 74.0, None, "above")
        assert prob == 0.0
        assert conf == "none"

    def test_all_none_members(self) -> None:
        prob, conf = ensemble_probability([None, None, None], 74.0, None, "above")  # type: ignore[list-item]
        assert prob == 0.0
        assert conf == "none"

    def test_single_member_above(self) -> None:
        prob, conf = ensemble_probability([80.0], 74.0, None, "above")
        assert prob == pytest.approx(1.0)

    def test_single_member_below(self) -> None:
        prob, conf = ensemble_probability([60.0], 74.0, None, "above")
        assert prob == pytest.approx(0.0)

    def test_mixed_with_none(self) -> None:
        members = [70.0, None, 74.0, None, 80.0]  # type: ignore[list-item]
        prob, conf = ensemble_probability(members, 74.0, None, "above")
        # Valid: [70, 74, 80], above 74: [74, 80] = 2/3
        assert prob == pytest.approx(2 / 3)


class TestConfidence:
    def test_high_confidence(self) -> None:
        # prob = 1.0, spread = 0.5 > 0.3
        _, conf = ensemble_probability([80.0, 82.0], 74.0, None, "above")
        assert conf == "high"

    def test_medium_confidence(self) -> None:
        # prob ~= 0.7, spread = 0.2 > 0.15
        members = [73.0, 74.0, 75.0, 76.0, 77.0, 78.0, 79.0, 80.0, 81.0, 82.0]
        prob, conf = ensemble_probability(members, 75.0, None, "above")
        # Above 75: [75,76,77,78,79,80,81,82] = 8/10 = 0.8, spread = 0.3 -> high
        # Let's adjust to get medium
        members2 = [70.0, 72.0, 74.0, 76.0, 78.0]
        prob2, conf2 = ensemble_probability(members2, 74.0, None, "above")
        # Above 74: [74,76,78] = 3/5 = 0.6, spread = 0.1 < 0.15
        assert conf2 == "low"

    def test_low_confidence(self) -> None:
        # prob ~= 0.5, spread ~= 0.0
        members = [73.0, 74.0, 75.0, 76.0]
        prob, conf = ensemble_probability(members, 74.5, None, "above")
        # Above 74.5: [75, 76] = 2/4 = 0.5, spread = 0.0
        assert conf == "low"
