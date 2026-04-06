"""Edge calculator — ensemble probability vs market price.

Computes the edge (model advantage) between what the ensemble forecast
says the probability is and what Polymarket is pricing it at, net of fees.

Architecture from Moon Dev's scan_edge.py analyze_edges().
"""

from __future__ import annotations

import logging

from infra.types import EdgeResult, SignalType

logger = logging.getLogger(__name__)

# Extreme prices to skip (too close to 0 or 1 = illiquid / resolved)
_EXTREME_LOW = 0.005
_EXTREME_HIGH = 0.995


def calculate_edge(
    ensemble_prob: float,
    market_yes_price: float,
    taker_fee_pct: float = 0.01,
    confidence: str = "medium",
) -> EdgeResult | None:
    """Calculate edge between ensemble probability and market price.

    If our model says YES is more likely than the market thinks -> BUY_YES.
    If our model says YES is less likely than the market thinks -> BUY_NO.

    Returns EdgeResult with net_edge (after fees), or None if no edge.
    """
    if market_yes_price <= _EXTREME_LOW or market_yes_price >= _EXTREME_HIGH:
        return None

    if not 0.0 <= ensemble_prob <= 1.0:
        return None

    raw_edge = ensemble_prob - market_yes_price

    if raw_edge > 0:
        # Model says YES is underpriced -> BUY YES
        signal_type = SignalType.BUY_YES
        entry_price = market_yes_price
    else:
        # Model says YES is overpriced -> BUY NO
        signal_type = SignalType.BUY_NO
        entry_price = 1.0 - market_yes_price
        raw_edge = -raw_edge  # Make positive (edge on NO side)

    # Fee estimation: entry fee + exit fee (both sides of the trade)
    entry_fee = entry_price * taker_fee_pct
    exit_fee = entry_price * taker_fee_pct  # Conservative: assume exit at same price
    net_edge = raw_edge - entry_fee - exit_fee

    if net_edge <= 0:
        return None

    return EdgeResult(
        raw_edge=raw_edge,
        net_edge=net_edge,
        ensemble_prob=ensemble_prob,
        market_price=entry_price,
        signal_type=signal_type,
        entry_fee=entry_fee,
        exit_fee=exit_fee,
        confidence=confidence,
    )
