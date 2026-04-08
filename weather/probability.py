"""Ensemble probability calculator — direct member counting.

Replaces the old Normal CDF approach with Moon Dev's method:
COUNT how many ensemble members fall into the target range.
This is more robust and directly interpretable.

Handles 3 market types:
  - bucket (between X-Y): count members in [low, high]
  - exact (be X):         count members in [X-0.5, X+0.5]
  - threshold (X or above/below): count members >= or <= X
"""

from __future__ import annotations

import logging
import statistics

logger = logging.getLogger(__name__)

# Max plausible std dev for temperature in °F — used to normalize spread score.
# Range ~0-120°F → std of uniform ~35. We use 30 as a practical ceiling.
_MAX_TEMP_STD: float = 30.0

# Floor for spread score — never zero-size a bet completely.
_MIN_SPREAD_SCORE: float = 0.3


def ensemble_probability(
    members: list[float],
    threshold_low: float,
    threshold_high: float | None,
    direction: str,
    smoothing: bool = True,
) -> tuple[float, str]:
    """Calculate probability from ensemble members by counting.

    Args:
        members: List of ensemble member values (e.g., 31 temperature forecasts)
        threshold_low: Lower threshold value
        threshold_high: Upper threshold (for bucket/exact) or None (for threshold)
        direction: "above", "below", or "bucket"
        smoothing: Apply Laplace smoothing to shrink extremes toward 0.5

    Returns:
        (probability, confidence) where confidence is "high", "medium", or "low"
    """
    valid = [m for m in members if m is not None]
    n = len(valid)
    if n == 0:
        return 0.0, "none"

    if threshold_high is not None:
        # Bucket or exact: count members in range [low, high]
        count = sum(1 for m in valid if threshold_low <= m <= threshold_high)
    elif direction == "above":
        count = sum(1 for m in valid if m >= threshold_low)
    else:  # below
        count = sum(1 for m in valid if m <= threshold_low)

    # Laplace smoothing: (count + 1) / (n + 2) — shrinks extreme probs toward 0.5
    if smoothing:
        prob = (count + 1) / (n + 2)
    else:
        prob = count / n

    # Confidence based on how decisive the consensus is (P8.6: tighter thresholds)
    spread = abs(prob - 0.5)
    if spread > 0.35:
        confidence = "high"
    elif spread > 0.20:
        confidence = "medium"
    else:
        confidence = "low"

    return prob, confidence


def multi_model_probability(
    model_members: dict[str, list[float]],
    threshold_low: float,
    threshold_high: float | None,
    direction: str,
    weights: dict[str, float] | None = None,
    smoothing: bool = True,
) -> tuple[float, str]:
    """Weighted-average probability across multiple NWP models.

    Calculates per-model probability using ensemble_probability(),
    then computes a weighted average. When weights are provided,
    models with higher weights contribute more. Weights auto-normalize
    to sum=1.0 for whatever models are actually present.

    Args:
        model_members: {model_name: list_of_member_values}
        weights: {model_name: weight} — if None, uses equal weights

    Returns:
        (averaged_probability, confidence)
    """
    if not model_members:
        return 0.0, "none"

    per_model_probs: dict[str, float] = {}
    for model_name, members in model_members.items():
        prob, _ = ensemble_probability(members, threshold_low, threshold_high, direction, smoothing=smoothing)
        per_model_probs[model_name] = prob

    # Weighted average (auto-normalizes for present models)
    if weights:
        weighted_sum = 0.0
        total_weight = 0.0
        for model_name, prob in per_model_probs.items():
            w = weights.get(model_name, 1.0 / len(per_model_probs))
            weighted_sum += prob * w
            total_weight += w
        avg_prob = weighted_sum / total_weight if total_weight > 0 else 0.0
    else:
        avg_prob = sum(per_model_probs.values()) / len(per_model_probs)

    # Confidence from spread (how decisive) AND agreement (how consistent)
    probs_list = list(per_model_probs.values())
    spread = abs(avg_prob - 0.5)
    disagreement = max(probs_list) - min(probs_list)

    if spread > 0.3 and disagreement < 0.15:
        confidence = "high"      # decisive AND models agree
    elif spread > 0.15 and disagreement < 0.30:
        confidence = "medium"    # moderate signal, reasonable agreement
    else:
        confidence = "low"       # indecisive OR models disagree

    return avg_prob, confidence


def compute_spread_score(
    model_members: dict[str, list[float]],
    weights: dict[str, float] | None = None,
    max_std: float = _MAX_TEMP_STD,
) -> float:
    """Compute a [0.3, 1.0] score reflecting ensemble consensus tightness.

    Tight spread (low std dev) → score near 1.0 → bigger Kelly bet.
    Wide spread (high std dev) → score near 0.3 → smaller Kelly bet.

    Per-model scores are weighted-averaged using the same model weights
    as probability calculation.
    """
    if not model_members:
        return _MIN_SPREAD_SCORE

    per_model_scores: dict[str, float] = {}
    for model_name, members in model_members.items():
        valid = [m for m in members if m is not None]
        if len(valid) < 2:
            per_model_scores[model_name] = _MIN_SPREAD_SCORE
            continue
        std = statistics.stdev(valid)
        raw_score = max(0.0, 1.0 - std / max_std)
        per_model_scores[model_name] = max(_MIN_SPREAD_SCORE, raw_score)

    # Weighted average of per-model scores
    if weights:
        weighted_sum = 0.0
        total_weight = 0.0
        for model_name, score in per_model_scores.items():
            w = weights.get(model_name, 1.0 / len(per_model_scores))
            weighted_sum += score * w
            total_weight += w
        avg_score = weighted_sum / total_weight if total_weight > 0 else _MIN_SPREAD_SCORE
    else:
        avg_score = sum(per_model_scores.values()) / len(per_model_scores)

    return max(_MIN_SPREAD_SCORE, min(1.0, avg_score))
