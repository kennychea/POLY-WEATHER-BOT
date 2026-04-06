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

logger = logging.getLogger(__name__)


def ensemble_probability(
    members: list[float],
    threshold_low: float,
    threshold_high: float | None,
    direction: str,
) -> tuple[float, str]:
    """Calculate probability from ensemble members by counting.

    Args:
        members: List of ensemble member values (e.g., 31 temperature forecasts)
        threshold_low: Lower threshold value
        threshold_high: Upper threshold (for bucket/exact) or None (for threshold)
        direction: "above", "below", or "bucket"

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

    prob = count / n

    # Confidence based on how decisive the consensus is
    spread = abs(prob - 0.5)
    if spread > 0.3:
        confidence = "high"
    elif spread > 0.15:
        confidence = "medium"
    else:
        confidence = "low"

    return prob, confidence
