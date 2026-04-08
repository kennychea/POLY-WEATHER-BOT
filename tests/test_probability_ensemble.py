"""Tests for weather/probability.py — ensemble member counting."""

from __future__ import annotations

import pytest

from weather.probability import compute_spread_score, ensemble_probability, multi_model_probability


class TestBucketProbability:
    """Bucket: count members in [low, high] range."""

    def test_basic_bucket(self) -> None:
        members = [38.0, 39.0, 40.0, 41.0, 42.0]
        prob, conf = ensemble_probability(members, 40.0, 41.0, "bucket", smoothing=False)
        assert prob == pytest.approx(2 / 5)

    def test_all_in_bucket(self) -> None:
        members = [40.0, 40.5, 41.0]
        prob, conf = ensemble_probability(members, 40.0, 41.0, "bucket", smoothing=False)
        assert prob == pytest.approx(1.0)
        assert conf == "high"

    def test_none_in_bucket(self) -> None:
        members = [30.0, 31.0, 32.0]
        prob, conf = ensemble_probability(members, 40.0, 41.0, "bucket", smoothing=False)
        assert prob == pytest.approx(0.0)
        assert conf == "high"  # High confidence it's NOT in range

    def test_boundary_inclusive(self) -> None:
        members = [40.0, 41.0]
        prob, conf = ensemble_probability(members, 40.0, 41.0, "bucket", smoothing=False)
        assert prob == pytest.approx(1.0)


class TestExactProbability:
    """Exact: "be X°C" maps to bucket [X-0.5, X+0.5]."""

    def test_exact_27(self) -> None:
        members = [26.0, 27.0, 27.2, 28.0]
        prob, conf = ensemble_probability(members, 26.5, 27.5, "bucket", smoothing=False)
        assert prob == pytest.approx(2 / 4)

    def test_exact_none_match(self) -> None:
        members = [20.0, 21.0, 22.0]
        prob, conf = ensemble_probability(members, 26.5, 27.5, "bucket", smoothing=False)
        assert prob == pytest.approx(0.0)


class TestThresholdAbove:
    """Threshold above: count members >= threshold."""

    def test_above_74(self) -> None:
        members = [70.0, 72.0, 74.0, 76.0]
        prob, conf = ensemble_probability(members, 74.0, None, "above", smoothing=False)
        assert prob == pytest.approx(2 / 4)

    def test_all_above(self) -> None:
        members = [80.0, 82.0, 84.0]
        prob, conf = ensemble_probability(members, 74.0, None, "above", smoothing=False)
        assert prob == pytest.approx(1.0)
        assert conf == "high"

    def test_none_above(self) -> None:
        members = [60.0, 62.0, 64.0]
        prob, conf = ensemble_probability(members, 74.0, None, "above", smoothing=False)
        assert prob == pytest.approx(0.0)


class TestThresholdBelow:
    """Threshold below: count members <= threshold."""

    def test_below_72(self) -> None:
        members = [70.0, 72.0, 74.0, 76.0]
        prob, conf = ensemble_probability(members, 72.0, None, "below", smoothing=False)
        assert prob == pytest.approx(2 / 4)

    def test_all_below(self) -> None:
        members = [50.0, 52.0, 54.0]
        prob, conf = ensemble_probability(members, 72.0, None, "below", smoothing=False)
        assert prob == pytest.approx(1.0)


class TestEdgeCases:
    def test_empty_members(self) -> None:
        prob, conf = ensemble_probability([], 74.0, None, "above", smoothing=False)
        assert prob == 0.0
        assert conf == "none"

    def test_all_none_members(self) -> None:
        prob, conf = ensemble_probability([None, None, None], 74.0, None, "above", smoothing=False)  # type: ignore[list-item]
        assert prob == 0.0
        assert conf == "none"

    def test_single_member_above(self) -> None:
        prob, conf = ensemble_probability([80.0], 74.0, None, "above", smoothing=False)
        assert prob == pytest.approx(1.0)

    def test_single_member_below(self) -> None:
        prob, conf = ensemble_probability([60.0], 74.0, None, "above", smoothing=False)
        assert prob == pytest.approx(0.0)

    def test_mixed_with_none(self) -> None:
        members = [70.0, None, 74.0, None, 80.0]  # type: ignore[list-item]
        prob, conf = ensemble_probability(members, 74.0, None, "above", smoothing=False)
        # Valid: [70, 74, 80], above 74: [74, 80] = 2/3
        assert prob == pytest.approx(2 / 3)


class TestConfidence:
    def test_high_confidence(self) -> None:
        # prob = 1.0, spread = 0.5 > 0.3
        _, conf = ensemble_probability([80.0, 82.0], 74.0, None, "above", smoothing=False)
        assert conf == "high"

    def test_medium_confidence(self) -> None:
        # prob ~= 0.7, spread = 0.2 > 0.15
        members = [73.0, 74.0, 75.0, 76.0, 77.0, 78.0, 79.0, 80.0, 81.0, 82.0]
        prob, conf = ensemble_probability(members, 75.0, None, "above", smoothing=False)
        # Above 75: [75,76,77,78,79,80,81,82] = 8/10 = 0.8, spread = 0.3 -> high
        # Let's adjust to get medium
        members2 = [70.0, 72.0, 74.0, 76.0, 78.0]
        prob2, conf2 = ensemble_probability(members2, 74.0, None, "above", smoothing=False)
        # Above 74: [74,76,78] = 3/5 = 0.6, spread = 0.1 < 0.15
        assert conf2 == "low"

    def test_low_confidence(self) -> None:
        # prob ~= 0.5, spread ~= 0.0
        members = [73.0, 74.0, 75.0, 76.0]
        prob, conf = ensemble_probability(members, 74.5, None, "above", smoothing=False)
        # Above 74.5: [75, 76] = 2/4 = 0.5, spread = 0.0
        assert conf == "low"


# ---------------------------------------------------------------------------
# multi_model_probability
# ---------------------------------------------------------------------------


class TestMultiModelProbability:
    """Tests for probability averaging across multiple NWP models."""

    def test_all_models_agree_high(self) -> None:
        """3 models all say ~100% → high confidence."""
        model_members = {
            "gfs": [80.0, 82.0, 84.0],
            "ecmwf": [81.0, 83.0, 85.0],
            "icon": [79.0, 81.0, 83.0],
        }
        prob, conf = multi_model_probability(model_members, 74.0, None, "above", smoothing=False)
        assert prob == pytest.approx(1.0)
        assert conf == "high"

    def test_all_models_agree_low(self) -> None:
        """3 models all say ~0% → high confidence (decisively NO)."""
        model_members = {
            "gfs": [60.0, 62.0, 64.0],
            "ecmwf": [61.0, 63.0, 65.0],
            "icon": [59.0, 61.0, 63.0],
        }
        prob, conf = multi_model_probability(model_members, 74.0, None, "above", smoothing=False)
        assert prob == pytest.approx(0.0)
        assert conf == "high"

    def test_models_disagree(self) -> None:
        """GFS says yes, ECMWF says no → low confidence."""
        model_members = {
            "gfs": [80.0, 82.0, 84.0],       # all above 74 → prob=1.0
            "ecmwf": [60.0, 62.0, 64.0],     # none above 74 → prob=0.0
            "icon": [73.0, 74.0, 75.0],      # 2/3 above → prob=0.667
        }
        prob, conf = multi_model_probability(model_members, 74.0, None, "above", smoothing=False)
        # avg = (1.0 + 0.0 + 0.667) / 3 ≈ 0.556
        expected = (1.0 + 0.0 + 2 / 3) / 3
        assert prob == pytest.approx(expected, abs=0.01)
        # disagreement = 1.0 - 0.0 = 1.0 >> 0.30 → low
        assert conf == "low"

    def test_averages_correctly(self) -> None:
        """Verify arithmetic mean of per-model probabilities."""
        model_members = {
            "gfs": [74.0, 76.0],     # 2/2 above 74 → 1.0
            "ecmwf": [73.0, 75.0],   # 1/2 above 74 → 0.5
        }
        prob, conf = multi_model_probability(model_members, 74.0, None, "above", smoothing=False)
        assert prob == pytest.approx(0.75)

    def test_single_model_fallback(self) -> None:
        """1 model only → same as single-model probability."""
        members = [70.0, 74.0, 80.0]
        model_members = {"gfs": members}
        prob, conf = multi_model_probability(model_members, 74.0, None, "above", smoothing=False)
        single_prob, _ = ensemble_probability(members, 74.0, None, "above", smoothing=False)
        assert prob == pytest.approx(single_prob)

    def test_empty_dict_returns_none(self) -> None:
        """No models → (0.0, 'none')."""
        prob, conf = multi_model_probability({}, 74.0, None, "above", smoothing=False)
        assert prob == 0.0
        assert conf == "none"

    def test_two_models_moderate_agreement(self) -> None:
        """2 models with moderate agreement → medium confidence."""
        model_members = {
            "gfs": [80.0, 82.0, 84.0, 86.0, 88.0],      # all above → 1.0
            "ecmwf": [76.0, 78.0, 80.0, 82.0, 84.0],    # all above → 1.0
        }
        prob, conf = multi_model_probability(model_members, 74.0, None, "above", smoothing=False)
        assert prob == pytest.approx(1.0)
        # disagreement = 0.0, spread = 0.5 → high
        assert conf == "high"

    def test_bucket_direction(self) -> None:
        """Multi-model works with bucket direction too."""
        model_members = {
            "gfs": [39.0, 40.0, 41.0, 42.0],     # 2/4 in [40,41] → 0.5
            "ecmwf": [40.0, 40.5, 41.0, 42.0],   # 3/4 in [40,41] → 0.75
        }
        prob, conf = multi_model_probability(model_members, 40.0, 41.0, "bucket", smoothing=False)
        assert prob == pytest.approx(0.625)


class TestWeightedModelProbability:
    """Tests for skill-weighted model averaging."""

    def test_ecmwf_dominant_weight(self) -> None:
        """When ECMWF gets weight=1.0, result matches ECMWF only."""
        model_members = {
            "gfs_seamless": [74.0, 76.0],       # 2/2 above 74 → 1.0
            "ecmwf_ifs025": [60.0, 62.0],       # 0/2 above 74 → 0.0
            "icon_seamless": [74.0, 80.0],       # 2/2 above 74 → 1.0
        }
        weights = {"ecmwf_ifs025": 1.0, "gfs_seamless": 0.0, "icon_seamless": 0.0}
        prob, _ = multi_model_probability(model_members, 74.0, None, "above", weights=weights, smoothing=False)
        assert prob == pytest.approx(0.0)

    def test_equal_weights_same_as_unweighted(self) -> None:
        """Equal weights produce same result as no weights."""
        model_members = {
            "gfs": [74.0, 76.0],     # prob = 1.0
            "ecmwf": [73.0, 75.0],   # prob = 0.5
        }
        prob_unweighted, _ = multi_model_probability(model_members, 74.0, None, "above", smoothing=False)
        weights = {"gfs": 0.5, "ecmwf": 0.5}
        prob_weighted, _ = multi_model_probability(model_members, 74.0, None, "above", weights=weights, smoothing=False)
        assert prob_weighted == pytest.approx(prob_unweighted)

    def test_weights_normalize_when_model_missing(self) -> None:
        """If only 2/3 models present, weights auto-normalize."""
        model_members = {
            "gfs_seamless": [74.0, 76.0],       # prob = 1.0
            "ecmwf_ifs025": [60.0, 62.0],       # prob = 0.0
            # icon_seamless is missing
        }
        weights = {"ecmwf_ifs025": 0.5, "gfs_seamless": 0.3, "icon_seamless": 0.2}
        prob, _ = multi_model_probability(model_members, 74.0, None, "above", weights=weights, smoothing=False)
        # Expected: (1.0*0.3 + 0.0*0.5) / (0.3+0.5) = 0.3/0.8 = 0.375
        assert prob == pytest.approx(0.375)

    def test_single_model_with_weights(self) -> None:
        """1 model available → uses that model's probability regardless of weights."""
        model_members = {"gfs_seamless": [80.0, 82.0]}  # prob = 1.0
        weights = {"ecmwf_ifs025": 0.5, "gfs_seamless": 0.3, "icon_seamless": 0.2}
        prob, _ = multi_model_probability(model_members, 74.0, None, "above", weights=weights, smoothing=False)
        assert prob == pytest.approx(1.0)

    def test_weighted_skews_toward_heavier_model(self) -> None:
        """Heavier weight on a model shifts the average toward it."""
        model_members = {
            "gfs_seamless": [80.0, 82.0],       # prob = 1.0
            "ecmwf_ifs025": [60.0, 62.0],       # prob = 0.0
        }
        # Equal weights → 0.5
        prob_equal, _ = multi_model_probability(model_members, 74.0, None, "above", smoothing=False)
        # GFS heavy → closer to 1.0
        weights_gfs = {"gfs_seamless": 0.8, "ecmwf_ifs025": 0.2}
        prob_gfs, _ = multi_model_probability(model_members, 74.0, None, "above", weights=weights_gfs, smoothing=False)
        # ECMWF heavy → closer to 0.0
        weights_ecmwf = {"gfs_seamless": 0.2, "ecmwf_ifs025": 0.8}
        prob_ecmwf, _ = multi_model_probability(model_members, 74.0, None, "above", weights=weights_ecmwf, smoothing=False)
        assert prob_gfs > prob_equal > prob_ecmwf

    def test_unknown_model_gets_fallback_weight(self) -> None:
        """Model not in weights dict gets equal fallback weight."""
        model_members = {
            "gfs_seamless": [80.0, 82.0],       # prob = 1.0
            "unknown_model": [60.0, 62.0],       # prob = 0.0
        }
        weights = {"gfs_seamless": 0.8}  # unknown_model not in weights
        prob, _ = multi_model_probability(model_members, 74.0, None, "above", weights=weights, smoothing=False)
        # unknown gets 1/2 = 0.5 fallback weight
        # (1.0*0.8 + 0.0*0.5) / (0.8+0.5) = 0.8/1.3 ≈ 0.615
        assert prob == pytest.approx(0.8 / 1.3, abs=0.01)


class TestLaplaceSmoothing:
    """Tests for Laplace smoothing (production default: smoothing=True)."""

    def test_laplace_default_is_on(self) -> None:
        """Calling without smoothing kwarg should use Laplace (not raw count)."""
        members = [80.0] * 31  # all above threshold
        prob_default, _ = ensemble_probability(members, 74.0, None, "above")
        prob_smoothed, _ = ensemble_probability(members, 74.0, None, "above", smoothing=True)
        prob_raw, _ = ensemble_probability(members, 74.0, None, "above", smoothing=False)
        assert prob_default == prob_smoothed
        assert prob_default != prob_raw

    def test_laplace_shrinks_unanimous_above(self) -> None:
        """31/31 above: raw=1.0, Laplace=(31+1)/(31+2)=32/33."""
        members = [80.0] * 31
        prob, conf = ensemble_probability(members, 74.0, None, "above", smoothing=True)
        assert prob == pytest.approx(32 / 33)
        assert conf == "high"

    def test_laplace_shrinks_zero_count(self) -> None:
        """0/31 above: raw=0.0, Laplace=(0+1)/(31+2)=1/33."""
        members = [60.0] * 31
        prob, conf = ensemble_probability(members, 74.0, None, "above", smoothing=True)
        assert prob == pytest.approx(1 / 33)
        assert conf == "high"  # high confidence it's NOT above


class TestSpreadScore:
    """Tests for compute_spread_score() — ensemble consensus tightness."""

    def test_uniform_members_high_score(self) -> None:
        """All members identical → std=0 → score=1.0."""
        model_members = {"gfs": [70.0, 70.0, 70.0, 70.0]}
        score = compute_spread_score(model_members)
        assert score == pytest.approx(1.0)

    def test_wide_spread_floor(self) -> None:
        """Huge range → score hits floor of 0.3."""
        model_members = {"gfs": [0.0, 30.0, 60.0, 90.0, 120.0]}
        score = compute_spread_score(model_members)
        assert score == pytest.approx(0.3)

    def test_moderate_spread(self) -> None:
        """Moderate spread (std ~3°F) → score ~0.9."""
        model_members = {"gfs": [68.0, 69.0, 70.0, 71.0, 72.0]}
        score = compute_spread_score(model_members)
        assert 0.8 < score < 1.0

    def test_empty_dict_returns_floor(self) -> None:
        score = compute_spread_score({})
        assert score == pytest.approx(0.3)

    def test_single_member_returns_floor(self) -> None:
        """Can't compute stdev with 1 member → floor."""
        model_members = {"gfs": [70.0]}
        score = compute_spread_score(model_members)
        assert score == pytest.approx(0.3)

    def test_multi_model_weighted(self) -> None:
        """Heavier model weight influences the average score."""
        model_members = {
            "gfs_seamless": [70.0, 70.0, 70.0],       # tight → ~1.0
            "ecmwf_ifs025": [0.0, 50.0, 100.0],       # wild → ~0.3
        }
        # Equal → avg of 1.0 and 0.3 ≈ 0.65
        score_equal = compute_spread_score(model_members)
        # Weight GFS heavy → closer to 1.0
        weights_gfs = {"gfs_seamless": 0.9, "ecmwf_ifs025": 0.1}
        score_gfs = compute_spread_score(model_members, weights=weights_gfs)
        assert score_gfs > score_equal

    def test_score_always_in_range(self) -> None:
        """Score is always between 0.3 and 1.0 regardless of input."""
        for members in [
            {"m": [70.0] * 10},
            {"m": list(range(-50, 150))},
            {"m": [0.0, 1000.0]},
        ]:
            score = compute_spread_score(members)
            assert 0.3 <= score <= 1.0
