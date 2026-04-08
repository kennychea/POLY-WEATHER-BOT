"""Tests for core/edge_calculator.py — edge computation with fees."""

from __future__ import annotations

import pytest

from core.edge_calculator import calculate_edge
from infra.types import SignalType


class TestBuyYesSignal:
    """Model says YES is underpriced -> BUY_YES."""

    def test_positive_edge(self) -> None:
        result = calculate_edge(
            ensemble_prob=0.70,
            market_yes_price=0.50,
            taker_fee_pct=0.01,
        )
        assert result is not None
        assert result.signal_type == SignalType.BUY_YES
        assert result.raw_edge == pytest.approx(0.20)
        assert result.ensemble_prob == pytest.approx(0.70)
        assert result.market_price == pytest.approx(0.50)
        # net = 0.20 - 0.50*0.01 - 0.50*0.01 = 0.20 - 0.01 = 0.19
        assert result.net_edge == pytest.approx(0.19)

    def test_small_positive_edge(self) -> None:
        result = calculate_edge(
            ensemble_prob=0.55,
            market_yes_price=0.50,
            taker_fee_pct=0.01,
        )
        assert result is not None
        assert result.signal_type == SignalType.BUY_YES
        # raw = 0.05, fees = 0.50*0.01*2 = 0.01, net = 0.04
        assert result.net_edge == pytest.approx(0.04)


class TestBuyNoSignal:
    """Model says YES is overpriced -> BUY_NO."""

    def test_negative_edge(self) -> None:
        result = calculate_edge(
            ensemble_prob=0.30,
            market_yes_price=0.50,
            taker_fee_pct=0.01,
        )
        assert result is not None
        assert result.signal_type == SignalType.BUY_NO
        assert result.raw_edge == pytest.approx(0.20)
        # Market price for NO = 1 - 0.50 = 0.50
        assert result.market_price == pytest.approx(0.50)
        # net = 0.20 - 0.50*0.01*2 = 0.19
        assert result.net_edge == pytest.approx(0.19)

    def test_small_no_edge(self) -> None:
        result = calculate_edge(
            ensemble_prob=0.40,
            market_yes_price=0.50,
            taker_fee_pct=0.01,
        )
        assert result is not None
        assert result.signal_type == SignalType.BUY_NO
        # raw = 0.10, NO price = 0.50, fees = 0.01, net = 0.09
        assert result.net_edge == pytest.approx(0.09)


class TestEdgeBelowFees:
    """Edge too small to cover fees -> returns None."""

    def test_tiny_edge_filtered(self) -> None:
        result = calculate_edge(
            ensemble_prob=0.505,
            market_yes_price=0.50,
            taker_fee_pct=0.01,
        )
        # raw = 0.005, fees = 0.50*0.01*2 = 0.01, net = -0.005
        assert result is None

    def test_zero_edge(self) -> None:
        result = calculate_edge(
            ensemble_prob=0.50,
            market_yes_price=0.50,
            taker_fee_pct=0.01,
        )
        assert result is None


class TestExtremePrice:
    """Extreme prices (near 0 or 1) should be filtered."""

    def test_price_near_zero(self) -> None:
        result = calculate_edge(ensemble_prob=0.10, market_yes_price=0.003)
        assert result is None

    def test_price_near_one(self) -> None:
        result = calculate_edge(ensemble_prob=0.90, market_yes_price=0.998)
        assert result is None

    def test_price_at_boundary(self) -> None:
        result = calculate_edge(ensemble_prob=0.10, market_yes_price=0.005)
        assert result is None

    def test_price_just_above_boundary(self) -> None:
        result = calculate_edge(
            ensemble_prob=0.80,
            market_yes_price=0.01,
            taker_fee_pct=0.01,
        )
        # Should pass extreme filter (0.01 > 0.005)
        assert result is not None


class TestInvalidInputs:
    def test_prob_out_of_range_high(self) -> None:
        assert calculate_edge(1.5, 0.50) is None

    def test_prob_out_of_range_negative(self) -> None:
        assert calculate_edge(-0.1, 0.50) is None

    def test_prob_exactly_zero(self) -> None:
        # 0.0 is valid (all members say NO)
        result = calculate_edge(0.0, 0.50, taker_fee_pct=0.01)
        # raw_edge = 0.5, signal=BUY_NO, net = 0.5 - 0.50*0.01*2 = 0.49
        assert result is not None
        assert result.signal_type == SignalType.BUY_NO

    def test_prob_exactly_one(self) -> None:
        # 1.0 is valid (all members say YES)
        result = calculate_edge(1.0, 0.50, taker_fee_pct=0.01)
        assert result is not None
        assert result.signal_type == SignalType.BUY_YES


class TestFees:
    def test_zero_fees(self) -> None:
        result = calculate_edge(
            ensemble_prob=0.60,
            market_yes_price=0.50,
            taker_fee_pct=0.0,
        )
        assert result is not None
        assert result.net_edge == pytest.approx(0.10)
        assert result.entry_fee == 0.0
        assert result.exit_fee == 0.0

    def test_high_fees_kill_edge(self) -> None:
        result = calculate_edge(
            ensemble_prob=0.55,
            market_yes_price=0.50,
            taker_fee_pct=0.10,  # 10% fee
        )
        # raw = 0.05, fees = 0.50*0.10*2 = 0.10, net = -0.05
        assert result is None

    def test_confidence_passthrough(self) -> None:
        result = calculate_edge(
            ensemble_prob=0.80,
            market_yes_price=0.50,
            confidence="high",
        )
        assert result is not None
        assert result.confidence == "high"


class TestConfidenceAdjustedEdge:
    """P8.4: Minimum raw edge thresholds by confidence level."""

    def test_high_confidence_passes_3pct_edge(self) -> None:
        """High confidence: 3% raw edge passes (threshold=0.03)."""
        result = calculate_edge(
            ensemble_prob=0.53,
            market_yes_price=0.50,
            confidence="high",
        )
        assert result is not None
        assert result.signal_type == SignalType.BUY_YES

    def test_medium_confidence_rejects_4pct_edge(self) -> None:
        """Medium confidence: 4% raw edge rejected (threshold=0.05)."""
        result = calculate_edge(
            ensemble_prob=0.54,
            market_yes_price=0.50,
            confidence="medium",
        )
        assert result is None

    def test_medium_confidence_passes_5pct_edge(self) -> None:
        """Medium confidence: 5% raw edge passes (threshold=0.05)."""
        result = calculate_edge(
            ensemble_prob=0.55,
            market_yes_price=0.50,
            confidence="medium",
        )
        assert result is not None
        assert result.signal_type == SignalType.BUY_YES

    def test_low_confidence_always_rejected(self) -> None:
        """Low confidence: even 30% raw edge rejected (threshold=1.0)."""
        result = calculate_edge(
            ensemble_prob=0.80,
            market_yes_price=0.50,
            confidence="low",
        )
        assert result is None

    def test_buy_no_confidence_filter(self) -> None:
        """BUY_NO: negative raw_edge with medium conf applies same filter."""
        # raw_edge = 0.44 - 0.50 = -0.06, abs = 0.06 >= medium threshold 0.05
        result = calculate_edge(
            ensemble_prob=0.44,
            market_yes_price=0.50,
            confidence="medium",
        )
        assert result is not None
        assert result.signal_type == SignalType.BUY_NO

    def test_buy_no_medium_rejects_small_edge(self) -> None:
        """BUY_NO: 4% negative edge rejected by medium threshold."""
        # raw_edge = 0.46 - 0.50 = -0.04, abs = 0.04 < 0.05
        result = calculate_edge(
            ensemble_prob=0.46,
            market_yes_price=0.50,
            confidence="medium",
        )
        assert result is None
