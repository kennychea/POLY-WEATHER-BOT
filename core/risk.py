"""Risk manager — position sizing and exposure limits.

Uses quarter-Kelly criterion for sizing, with hard caps on
max bet and max total exposure.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class RiskManager:
    """Manages position sizing and exposure limits."""

    def __init__(
        self,
        bankroll: float,
        max_bet: float,
        max_exposure: float,
        kelly_fraction: float = 0.25,
        max_edge: float = 0.15,
        min_edge: float = 0.0,
    ) -> None:
        self.bankroll = bankroll
        self._max_bet = max_bet
        self._max_exposure = max_exposure
        self._kelly_fraction = kelly_fraction
        self._max_edge = max_edge
        self._min_edge = min_edge
        self.current_exposure: float = 0.0

    def add_exposure(self, amount: float) -> None:
        """Add to current exposure."""
        self.current_exposure += amount

    def release_exposure(self, amount: float) -> None:
        """Release exposure after trade completion."""
        if amount > self.current_exposure:
            logger.warning(
                "Releasing %.2f but only %.2f exposed (possible double-release)",
                amount, self.current_exposure,
            )
        self.current_exposure = max(0.0, self.current_exposure - amount)

    def update_bankroll(self, pnl: float) -> None:
        """Update bankroll after a resolved trade. Floor at 0."""
        self.bankroll = max(0.0, self.bankroll + pnl)

    def size_position(
        self, estimated_probability: float, market_price: float,
    ) -> float:
        """Calculate position size using quarter-Kelly.

        Uses estimated_probability as the fair value.
        Edge is capped at max_edge to prevent overconfident sizing
        until the model is calibrated from real trade data.
        """
        remaining = self._max_exposure - self.current_exposure
        if remaining <= 0:
            return 0.0

        if market_price <= 0 or market_price >= 1.0:
            return 0.0

        if estimated_probability <= 0 or estimated_probability >= 1.0:
            return 0.0

        # Filter: skip if edge below minimum threshold
        raw_edge = estimated_probability - market_price
        if raw_edge < self._min_edge:
            return 0.0

        # Cap the edge to prevent overconfident positions
        if raw_edge > self._max_edge:
            logger.info(
                "Edge capped: %.4f -> %.4f for price=%.4f",
                raw_edge, self._max_edge, market_price,
            )
        capped_edge = min(raw_edge, self._max_edge)
        effective_prob = min(market_price + capped_edge, 0.99)

        kelly = (effective_prob - market_price) / (1.0 - market_price)
        kelly = max(0.0, kelly)

        size = self.bankroll * kelly * self._kelly_fraction
        size = min(size, self._max_bet, remaining)
        return round(size, 2)

