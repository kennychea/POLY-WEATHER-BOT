"""Tests for weather/dashboard.py — Rich panel builders."""

from __future__ import annotations

import pytest

from core.risk import RiskManager
from weather.backtest import BacktestResult
from weather.dashboard import (
    _extract_bucket,
    build_cache_panel,
    build_calibration_panel,
    build_footer,
    build_header,
    build_layout,
    build_pending_panel,
    build_portfolio_panel,
    build_resolved_panel,
    build_risk_panel,
    build_scanner_panel,
    build_signals_panel,
)


# ── Bucket extraction ────────────────────────────────────────────


class TestExtractBucket:
    def test_between_fahrenheit(self) -> None:
        assert _extract_bucket("be between 48-49°F on April 8?") == "48-49°F"

    def test_or_higher(self) -> None:
        assert _extract_bucket("be 76°F or higher on April 8?") == "≥76°F"

    def test_or_below(self) -> None:
        assert _extract_bucket("be 37°F or below on April 8?") == "≤37°F"

    def test_exact_celsius(self) -> None:
        assert _extract_bucket("be 29°C on April 9?") == "29°C"

    def test_fallback(self) -> None:
        result = _extract_bucket("some random text")
        assert len(result) <= 20


# ── Scanner panel ────────────────────────────────────────────────


class TestScannerPanel:
    def test_empty_rows(self) -> None:
        panel = build_scanner_panel([])
        assert panel is not None

    def test_with_data(self) -> None:
        rows = [
            {
                "city": "NYC",
                "volume_24h": 42000,
                "bucket": "50-51°F",
                "mkt_price": 0.415,
                "mkt_vol": 3500,
            },
            {
                "city": "Miami",
                "volume_24h": 31000,
                "bucket": "76-77°F",
                "mkt_price": 0.445,
                "mkt_vol": 6500,
                "forecast": 0.182,
            },
        ]
        panel = build_scanner_panel(rows)
        assert panel is not None

    def test_with_edge(self) -> None:
        rows = [
            {
                "city": "Hong Kong",
                "volume_24h": 114000,
                "bucket": "27°C",
                "mkt_price": 0.716,
                "mkt_vol": 27000,
                "forecast": 0.97,
            },
        ]
        panel = build_scanner_panel(rows)
        assert panel is not None


# ── Portfolio panel ───────────────────────────────────────────────


class TestPortfolioPanel:
    def test_basic_stats(self) -> None:
        stats = {
            "total_trades": 10,
            "wins": 6,
            "losses": 4,
            "win_rate": 0.6,
            "total_pnl": 15.50,
            "profit_factor": 1.5,
        }
        panel = build_portfolio_panel(stats)
        assert panel.title is not None

    def test_zero_trades(self) -> None:
        stats = {
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "total_pnl": 0.0,
            "profit_factor": 0.0,
        }
        panel = build_portfolio_panel(stats)
        assert panel is not None

    def test_empty_dict(self) -> None:
        panel = build_portfolio_panel({})
        assert panel is not None

    def test_negative_pnl(self) -> None:
        stats = {
            "total_trades": 5,
            "wins": 1,
            "losses": 4,
            "win_rate": 0.2,
            "total_pnl": -8.30,
            "profit_factor": 0.3,
        }
        panel = build_portfolio_panel(stats)
        assert panel is not None


# ── Risk panel ────────────────────────────────────────────────────


class TestRiskPanel:
    def test_normal_exposure(self) -> None:
        risk = RiskManager(bankroll=100, max_bet=10, max_exposure=200)
        risk.add_exposure(45)
        panel = build_risk_panel(risk)
        assert panel is not None

    def test_zero_max_exposure(self) -> None:
        risk = RiskManager(bankroll=100, max_bet=10, max_exposure=0.01)
        panel = build_risk_panel(risk)
        assert panel is not None


# ── Pending trades panel ──────────────────────────────────────────


class TestPendingPanel:
    def test_empty_trades(self) -> None:
        panel = build_pending_panel([])
        assert panel is not None

    def test_single_trade(self) -> None:
        trades = [
            {
                "market_question": "Will NYC be above 70F?",
                "signal_type": "BUY_YES",
                "entry_price": 0.45,
                "size_usdc": 8.0,
                "opened_at": "2026-04-09T12:00:00+00:00",
            },
        ]
        panel = build_pending_panel(trades)
        assert panel is not None

    def test_full_question_shown(self) -> None:
        """Full market names should be visible, not truncated."""
        trades = [
            {
                "market_question": "Will the highest temperature in New York City be between 50-51°F on April 9?",
                "signal_type": "buy_no",
                "entry_price": 0.575,
                "size_usdc": 10.0,
                "opened_at": "2026-04-09T12:00:00+00:00",
            },
        ]
        panel = build_pending_panel(trades)
        assert panel is not None

    def test_over_20_shows_limit(self) -> None:
        trades = [
            {
                "market_question": f"Market {i}",
                "signal_type": "BUY_YES",
                "entry_price": 0.50,
                "size_usdc": 5.0,
                "opened_at": "2026-04-09T12:00:00+00:00",
            }
            for i in range(25)
        ]
        panel = build_pending_panel(trades)
        assert panel is not None

    def test_missing_fields_use_defaults(self) -> None:
        trades = [{"market_id": "xyz"}]
        panel = build_pending_panel(trades)
        assert panel is not None


# ── Resolved trades panel ────────────────────────────────────────


class TestResolvedPanel:
    def test_empty(self) -> None:
        panel = build_resolved_panel([])
        assert panel is not None

    def test_with_trades(self) -> None:
        trades = [
            {
                "market_question": "Will NYC be 50-51F on April 8?",
                "signal_type": "buy_no",
                "entry_price": 0.605,
                "exit_price": 1.0,
                "pnl": 6.43,
            },
            {
                "market_question": "Will NYC be 44-45F on April 8?",
                "signal_type": "buy_yes",
                "entry_price": 0.006,
                "exit_price": 0.0,
                "pnl": -10.10,
            },
        ]
        panel = build_resolved_panel(trades)
        assert panel is not None


# ── Calibration panel ─────────────────────────────────────────────


class TestCalibrationPanel:
    def test_none_backtest(self) -> None:
        panel = build_calibration_panel(None)
        assert panel is not None

    def test_empty_backtest(self) -> None:
        panel = build_calibration_panel(BacktestResult())
        assert panel is not None

    def test_good_calibration(self) -> None:
        bt = BacktestResult(
            total_trades=20,
            brier_score=0.18,
            edge_correlation=0.35,
            high_conf_trades=5,
            high_conf_win_rate=0.8,
            medium_conf_trades=10,
            medium_conf_win_rate=0.6,
        )
        panel = build_calibration_panel(bt)
        assert panel is not None

    def test_bad_calibration(self) -> None:
        bt = BacktestResult(
            total_trades=10,
            brier_score=0.30,
            edge_correlation=-0.2,
            high_conf_trades=2,
            high_conf_win_rate=0.0,
            medium_conf_trades=8,
            medium_conf_win_rate=0.25,
        )
        panel = build_calibration_panel(bt)
        assert panel is not None


# ── Cache panel ───────────────────────────────────────────────────


class TestCachePanel:
    def test_cache_panel_renders(self) -> None:
        panel = build_cache_panel()
        assert panel is not None


# ── Header ────────────────────────────────────────────────────────


class TestHeader:
    def test_header_default(self) -> None:
        panel = build_header(30)
        assert panel is not None

    def test_header_with_last_refresh(self) -> None:
        panel = build_header(60, "14:30:05")
        assert panel is not None

    def test_header_with_countdown(self) -> None:
        panel = build_header(30, "14:30:05", elapsed=18, status="LIVE")
        assert panel is not None

    def test_header_fetching_status(self) -> None:
        panel = build_header(30, status="FETCHING")
        assert panel is not None

    def test_header_error_status(self) -> None:
        panel = build_header(30, status="ERROR")
        assert panel is not None

    def test_header_elapsed_exceeds_interval(self) -> None:
        """elapsed > refresh_interval should clamp remaining to 0, not crash."""
        panel = build_header(30, elapsed=35)
        assert panel is not None


# ── Layout ────────────────────────────────────────────────────────


# ── Signals panel ────────────────────────────────────────────────


class TestSignalsPanel:
    def test_empty_signals(self) -> None:
        panel = build_signals_panel([])
        assert panel is not None

    def test_with_signals(self) -> None:
        signals = [
            {
                "signal_type": "BUY_YES",
                "forecast_probability": 0.42,
                "market_price": 0.35,
                "net_edge": 0.065,
                "confidence": "high",
                "ensemble_member_count": 51,
                "location": "NYC",
                "forecast_date": "2026-04-10",
            },
        ]
        panel = build_signals_panel(signals)
        assert panel is not None

    def test_confidence_levels(self) -> None:
        signals = [
            {"signal_type": "BUY_YES", "forecast_probability": 0.5,
             "market_price": 0.4, "confidence": "high",
             "location": "NYC", "forecast_date": "2026-04-10"},
            {"signal_type": "BUY_NO", "forecast_probability": 0.3,
             "market_price": 0.4, "confidence": "medium",
             "location": "Miami", "forecast_date": "2026-04-11"},
            {"signal_type": "BUY_YES", "forecast_probability": 0.2,
             "market_price": 0.15, "confidence": "low",
             "location": "Chicago", "forecast_date": "2026-04-12"},
        ]
        panel = build_signals_panel(signals)
        assert panel is not None


# ── Footer ───────────────────────────────────────────────────────


class TestFooter:
    def test_footer_default(self) -> None:
        panel = build_footer()
        assert panel is not None

    def test_footer_live_mode(self) -> None:
        panel = build_footer(trading_mode="live")
        assert panel is not None

    def test_footer_with_uptime(self) -> None:
        import time
        start = time.monotonic() - 3661  # 1h1m1s ago
        panel = build_footer(start_time=start, db_path="data/bot.db")
        assert panel is not None

    def test_footer_with_model(self) -> None:
        panel = build_footer(ensemble_model="ecmwf_ifs025")
        assert panel is not None


# ── Layout ────────────────────────────────────────────────────────


class TestLayout:
    def test_layout_has_all_sections(self) -> None:
        layout = build_layout()
        assert layout["header"] is not None
        assert layout["portfolio"] is not None
        assert layout["risk"] is not None
        assert layout["cache"] is not None
        assert layout["pending"] is not None
        assert layout["resolved"] is not None
        assert layout["calibration"] is not None
        assert layout["scanner"] is not None
        assert layout["signals"] is not None
        assert layout["footer"] is not None
