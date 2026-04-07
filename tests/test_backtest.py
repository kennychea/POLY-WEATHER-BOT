"""Tests for weather.backtest module."""

from __future__ import annotations

import math
from pathlib import Path

import aiosqlite
import pytest

from weather.backtest import (
    BacktestResult,
    compute_brier_score,
    compute_edge_correlation,
    format_report,
    run_backtest,
)

# ── Brier score tests ──────────────────────────────────────────────


def test_brier_score_perfect():
    """Perfect predictions (matching outcomes) → score near 0."""
    preds = [1.0, 0.0, 1.0, 0.0]
    outcomes = [1.0, 0.0, 1.0, 0.0]
    assert compute_brier_score(preds, outcomes) == pytest.approx(0.0, abs=1e-9)


def test_brier_score_random():
    """All 0.5 predictions vs binary outcomes → ~0.25."""
    preds = [0.5, 0.5, 0.5, 0.5]
    outcomes = [1.0, 0.0, 1.0, 0.0]
    assert compute_brier_score(preds, outcomes) == pytest.approx(0.25, abs=1e-9)


def test_brier_score_empty():
    """Empty list → 0.0."""
    assert compute_brier_score([], []) == 0.0


# ── Edge correlation tests ─────────────────────────────────────────


def test_edge_correlation_perfect():
    """Identical lists → correlation 1.0."""
    vals = [0.1, 0.2, 0.3, 0.4, 0.5]
    assert compute_edge_correlation(vals, vals) == pytest.approx(1.0, abs=1e-9)


def test_edge_correlation_uncorrelated():
    """Uncorrelated data → correlation near 0."""
    predicted = [1.0, -1.0, 1.0, -1.0, 1.0, -1.0]
    realized = [1.0, 1.0, -1.0, -1.0, 1.0, 1.0]
    r = compute_edge_correlation(predicted, realized)
    assert abs(r) < 0.5  # not strongly correlated


# ── run_backtest tests ─────────────────────────────────────────────

_SCHEMA = (
    "CREATE TABLE calibration_log ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT,"
    "trade_id TEXT NOT NULL,"
    "confidence REAL NOT NULL,"
    "estimated_probability REAL NOT NULL,"
    "market_price_at_signal REAL NOT NULL,"
    "market_price_at_resolution REAL NOT NULL,"
    "delay_seconds INTEGER NOT NULL,"
    "pnl REAL NOT NULL,"
    "win INTEGER NOT NULL,"
    "timestamp TEXT NOT NULL,"
    "weather_metric TEXT NOT NULL DEFAULT '',"
    "location TEXT NOT NULL DEFAULT ''"
    ")"
)


@pytest.mark.asyncio
async def test_run_backtest_empty_db(tmp_path: Path):
    """Empty calibration_log → zeroed BacktestResult."""
    db_path = tmp_path / "empty.db"
    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.execute(_SCHEMA)
        await conn.commit()

    result = await run_backtest(db_path)
    assert result.total_trades == 0
    assert result.win_rate == 0.0
    assert result.total_pnl == 0.0
    assert result.brier_score == 0.0
    assert result.edge_correlation == 0.0


@pytest.mark.asyncio
async def test_run_backtest_with_data(tmp_path: Path):
    """Seeded calibration data → correct aggregated metrics."""
    db_path = tmp_path / "seeded.db"
    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.execute(_SCHEMA)
        # 4 trades: 2 wins, 2 losses
        trades = [
            # high confidence win
            ("t1", 0.85, 0.80, 0.50, 0.90, 60, 0.10, 1, "2026-01-01T00:00:00", "temp_high", "NYC"),
            # high confidence loss
            ("t2", 0.75, 0.70, 0.55, 0.40, 120, -0.05, 0, "2026-01-02T00:00:00", "temp_high", "NYC"),
            # medium confidence win
            ("t3", 0.50, 0.60, 0.40, 0.70, 90, 0.08, 1, "2026-01-03T00:00:00", "precip", "LA"),
            # low confidence loss
            ("t4", 0.20, 0.30, 0.25, 0.20, 30, -0.03, 0, "2026-01-04T00:00:00", "snow", "CHI"),
        ]
        await conn.executemany(
            "INSERT INTO calibration_log "
            "(trade_id, confidence, estimated_probability, market_price_at_signal,"
            " market_price_at_resolution, delay_seconds, pnl, win, timestamp,"
            " weather_metric, location)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            trades,
        )
        await conn.commit()

    result = await run_backtest(db_path)

    assert result.total_trades == 4
    assert result.win_rate == pytest.approx(0.5)
    assert result.total_pnl == pytest.approx(0.10)  # 0.10 - 0.05 + 0.08 - 0.03
    assert result.avg_pnl == pytest.approx(0.025)

    # Confidence buckets
    assert result.high_conf_trades == 2
    assert result.high_conf_win_rate == pytest.approx(0.5)
    assert result.medium_conf_trades == 1
    assert result.medium_conf_win_rate == pytest.approx(1.0)
    assert result.low_conf_trades == 1
    assert result.low_conf_win_rate == pytest.approx(0.0)

    # Predicted edges: (0.80-0.50, 0.70-0.55, 0.60-0.40, 0.30-0.25) = (0.30, 0.15, 0.20, 0.05)
    assert result.avg_predicted_edge == pytest.approx(0.175)
    # Realized edges (pnl): (0.10, -0.05, 0.08, -0.03)
    assert result.avg_realized_edge == pytest.approx(0.025)


# ── format_report test ─────────────────────────────────────────────


def test_format_report_contains_key_fields():
    """Report output contains all expected section headers and fields."""
    result = BacktestResult(
        total_trades=10,
        win_rate=0.6,
        total_pnl=0.5,
        avg_pnl=0.05,
        brier_score=0.15,
        high_conf_trades=3,
        high_conf_win_rate=0.8,
        medium_conf_trades=5,
        medium_conf_win_rate=0.6,
        low_conf_trades=2,
        low_conf_win_rate=0.0,
        avg_predicted_edge=0.12,
        avg_realized_edge=0.05,
        edge_correlation=0.45,
    )
    report = format_report(result)

    assert "BACKTEST REPORT" in report
    assert "Total trades" in report
    assert "Win rate" in report
    assert "Brier score" in report
    assert "Confidence Buckets" in report
    assert "Edge Analysis" in report
    assert "Edge correlation" in report
    assert "Avg predicted edge" in report
    assert "Avg realized edge" in report
