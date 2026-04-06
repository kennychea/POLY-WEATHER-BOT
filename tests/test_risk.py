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
async def test_size_position_min_edge_filter():
    """Should return 0 when edge < min_edge threshold (P1.4 fix)."""
    from core.risk import RiskManager
    rm = RiskManager(bankroll=1000, max_bet=50, max_exposure=200, min_edge=0.05)
    # Edge = 0.55 - 0.53 = 0.02 < min_edge=0.05
    size = rm.size_position(estimated_probability=0.55, market_price=0.53)
    assert size == 0.0


@pytest.mark.asyncio
async def test_size_position_passes_min_edge():
    """Should size normally when edge >= min_edge."""
    from core.risk import RiskManager
    rm = RiskManager(bankroll=1000, max_bet=50, max_exposure=200, min_edge=0.05)
    # Edge = 0.65 - 0.55 = 0.10 >= min_edge=0.05
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
