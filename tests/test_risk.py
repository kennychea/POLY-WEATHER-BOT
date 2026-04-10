"""Tests for core/risk.py — risk manager (position sizing, exposure limits)."""

import pytest


@pytest.mark.asyncio
async def test_risk_manager_creation():
    """Should create a RiskManager with config."""
    from core.risk import RiskManager
    rm = RiskManager(
        bankroll=1000.0,
        max_bet=50.0,
        max_exposure=200.0,
    )
    assert rm.bankroll == 1000.0


@pytest.mark.asyncio
async def test_risk_manager_release_exposure():
    """Should be able to release exposure after trade completes."""
    from core.risk import RiskManager
    rm = RiskManager(bankroll=1000.0, max_bet=50.0, max_exposure=100.0)
    rm.add_exposure(50.0)
    assert rm.current_exposure == 50.0
    rm.release_exposure(30.0)
    assert rm.current_exposure == 20.0


# ── Bankroll update tests ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_bankroll_win():
    """Winning trade should increase bankroll."""
    from core.risk import RiskManager
    rm = RiskManager(bankroll=1000.0, max_bet=50.0, max_exposure=200.0)
    rm.update_bankroll(6.67)
    assert rm.bankroll == pytest.approx(1006.67, abs=0.01)


@pytest.mark.asyncio
async def test_update_bankroll_loss():
    """Losing trade should decrease bankroll."""
    from core.risk import RiskManager
    rm = RiskManager(bankroll=1000.0, max_bet=50.0, max_exposure=200.0)
    rm.update_bankroll(-10.0)
    assert rm.bankroll == pytest.approx(990.0, abs=0.01)


@pytest.mark.asyncio
async def test_update_bankroll_floor_at_zero():
    """Bankroll should never go below zero."""
    from core.risk import RiskManager
    rm = RiskManager(bankroll=5.0, max_bet=50.0, max_exposure=200.0)
    rm.update_bankroll(-100.0)
    assert rm.bankroll == 0.0


@pytest.mark.asyncio
async def test_update_bankroll_zero_pnl():
    """Zero PnL should not change bankroll."""
    from core.risk import RiskManager
    rm = RiskManager(bankroll=1000.0, max_bet=50.0, max_exposure=200.0)
    rm.update_bankroll(0.0)
    assert rm.bankroll == 1000.0


# ── Position sizing tests ───────────────────────────────────────


@pytest.mark.asyncio
async def test_size_position_basic():
    """Should size a position using Kelly formula."""
    from core.risk import RiskManager
    rm = RiskManager(bankroll=1000.0, max_bet=50.0, max_exposure=200.0)
    size = rm.size_position(estimated_probability=0.85, market_price=0.60)
    assert size > 0
    assert size <= 50.0


@pytest.mark.asyncio
async def test_size_position_no_edge():
    """Should return 0 when confidence <= market_price (no edge)."""
    from core.risk import RiskManager
    rm = RiskManager(bankroll=1000.0, max_bet=50.0, max_exposure=200.0)
    size = rm.size_position(estimated_probability=0.50, market_price=0.60)
    assert size == 0.0


@pytest.mark.asyncio
async def test_size_position_invalid_price():
    """Should return 0 for invalid market prices."""
    from core.risk import RiskManager
    rm = RiskManager(bankroll=1000.0, max_bet=50.0, max_exposure=200.0)
    assert rm.size_position(estimated_probability=0.85, market_price=0.0) == 0.0
    assert rm.size_position(estimated_probability=0.85, market_price=1.0) == 0.0


@pytest.mark.asyncio
async def test_size_position_respects_exposure():
    """Should respect exposure limits."""
    from core.risk import RiskManager
    rm = RiskManager(bankroll=1000.0, max_bet=50.0, max_exposure=10.0)
    rm.add_exposure(10.0)
    size = rm.size_position(estimated_probability=0.85, market_price=0.60)
    assert size == 0.0


@pytest.mark.asyncio
async def test_size_position_caps_edge():
    """Edge should be capped at max_edge to prevent overconfident sizing."""
    from core.risk import RiskManager
    rm = RiskManager(bankroll=1000.0, max_bet=500.0, max_exposure=500.0, max_edge=0.15)
    # est_prob=0.95, price=0.30 → raw edge=0.65, capped to 0.15
    # effective_prob = 0.30 + 0.15 = 0.45
    # kelly = (0.45 - 0.30) / (1 - 0.30) = 0.2143
    # size = 1000 * 0.2143 * 0.25 = 53.57
    size_capped = rm.size_position(estimated_probability=0.95, market_price=0.30)

    rm2 = RiskManager(bankroll=1000.0, max_bet=500.0, max_exposure=500.0, max_edge=1.0)
    # kelly = (0.95 - 0.30) / (1 - 0.30) = 0.9286
    # size = 1000 * 0.9286 * 0.25 = 232.14
    size_uncapped = rm2.size_position(estimated_probability=0.95, market_price=0.30)

    assert size_capped < size_uncapped


@pytest.mark.asyncio
async def test_size_position_default_max_edge():
    """Default max_edge should be 0.15 (15%)."""
    from core.risk import RiskManager
    rm = RiskManager(bankroll=1000.0, max_bet=50.0, max_exposure=200.0)
    assert rm._max_edge == 0.15


@pytest.mark.asyncio
async def test_size_position_min_edge_noop():
    """P10.C: RiskManager.min_edge is a no-op (forced to 0).

    The edge gate lives in core/edge_calculator.py (confidence-tiered:
    high>=3%, medium>=5%, low rejected). RiskManager should NOT re-filter
    by raw edge, since that double-penalized high-confidence trades.
    """
    from core.risk import RiskManager
    # Even when the caller passes min_edge=0.05, it's ignored.
    rm = RiskManager(bankroll=1000, max_bet=50, max_exposure=200, min_edge=0.05)
    assert rm._min_edge == 0.0
    # Tiny edge (0.02) that would have been filtered before — now sized.
    size = rm.size_position(estimated_probability=0.55, market_price=0.53)
    assert size > 0.0


@pytest.mark.asyncio
async def test_size_position_passes_with_edge():
    """Should size normally when there is a positive edge."""
    from core.risk import RiskManager
    rm = RiskManager(bankroll=1000, max_bet=50, max_exposure=200)
    # Edge = 0.65 - 0.55 = 0.10
    size = rm.size_position(estimated_probability=0.65, market_price=0.55)
    assert size > 0.0


# ── Invalid probability tests (P3a.5 fix) ──────────────────────


@pytest.mark.asyncio
async def test_size_invalid_probability():
    """Should return 0 for invalid estimated_probability values (P3a.5)."""
    from core.risk import RiskManager
    rm = RiskManager(bankroll=1000, max_bet=50, max_exposure=200)
    assert rm.size_position(estimated_probability=0.0, market_price=0.50) == 0.0
    assert rm.size_position(estimated_probability=-0.5, market_price=0.50) == 0.0
    assert rm.size_position(estimated_probability=1.0, market_price=0.50) == 0.0
    assert rm.size_position(estimated_probability=1.5, market_price=0.50) == 0.0


@pytest.mark.asyncio
async def test_size_price_zero():
    """Should return 0 when market_price is 0."""
    from core.risk import RiskManager
    rm = RiskManager(bankroll=1000, max_bet=50, max_exposure=200)
    assert rm.size_position(estimated_probability=0.80, market_price=0.0) == 0.0


@pytest.mark.asyncio
async def test_size_price_one():
    """Should return 0 when market_price is 1.0 (no edge possible)."""
    from core.risk import RiskManager
    rm = RiskManager(bankroll=1000, max_bet=50, max_exposure=200)
    assert rm.size_position(estimated_probability=0.80, market_price=1.0) == 0.0


# ── Spread score sizing tests (P7.3) ────────────────────────────


@pytest.mark.asyncio
async def test_size_position_spread_scales_down():
    """Low spread_score should reduce position size."""
    from core.risk import RiskManager
    rm = RiskManager(bankroll=1000.0, max_bet=500.0, max_exposure=500.0)
    size_full = rm.size_position(estimated_probability=0.85, market_price=0.60, spread_score=1.0)
    size_half = rm.size_position(estimated_probability=0.85, market_price=0.60, spread_score=0.5)
    assert size_full > 0
    assert size_half > 0
    assert size_half == pytest.approx(size_full * 0.5, abs=0.02)


@pytest.mark.asyncio
async def test_size_position_spread_floor():
    """spread_score below 0.3 should be clamped to 0.3."""
    from core.risk import RiskManager
    rm = RiskManager(bankroll=1000.0, max_bet=500.0, max_exposure=500.0)
    size_zero = rm.size_position(estimated_probability=0.85, market_price=0.60, spread_score=0.0)
    size_floor = rm.size_position(estimated_probability=0.85, market_price=0.60, spread_score=0.3)
    assert size_zero == size_floor  # 0.0 clamped to 0.3


@pytest.mark.asyncio
async def test_size_position_spread_default():
    """Default spread_score=1.0 should not change sizing vs explicit 1.0."""
    from core.risk import RiskManager
    rm = RiskManager(bankroll=1000.0, max_bet=500.0, max_exposure=500.0)
    size_default = rm.size_position(estimated_probability=0.85, market_price=0.60)
    size_explicit = rm.size_position(estimated_probability=0.85, market_price=0.60, spread_score=1.0)
    assert size_default == size_explicit
