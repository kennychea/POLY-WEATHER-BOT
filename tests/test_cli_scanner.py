"""Tests for weather/cli_scanner.py — standalone CLI scanner."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from weather.cli_scanner import print_results


class TestPrintResults:
    def test_empty_results(self, capsys: pytest.CaptureFixture[str]) -> None:
        print_results([])
        out = capsys.readouterr().out
        assert "No opportunities found" in out

    def test_formats_buy_yes(self, capsys: pytest.CaptureFixture[str]) -> None:
        opps = [{
            "question": "Will NYC be 74°F or higher?",
            "city": "New York",
            "metric": "temp_high",
            "threshold_low": 74.0,
            "threshold_high": None,
            "direction": "above",
            "target_date": "2026-04-08",
            "unit": "fahrenheit",
            "model_prob": 0.80,
            "market_yes": 0.50,
            "market_no": 0.50,
            "edge": 0.30,
            "abs_edge": 0.30,
            "signal": "BUY_YES",
            "confidence": "high",
            "ensemble_size": 31,
            "slug": "highest-temperature-nyc",
            "condition_id": "abc",
        }]
        print_results(opps)
        out = capsys.readouterr().out
        assert "New York" in out
        assert "BUY_YES" in out
        assert "HIGH" in out
        assert ">>>" in out  # abs_edge > 0.15

    def test_formats_bucket(self, capsys: pytest.CaptureFixture[str]) -> None:
        opps = [{
            "question": "Will NYC be between 40-41°F?",
            "city": "New York",
            "metric": "temp_high",
            "threshold_low": 40.0,
            "threshold_high": 41.0,
            "direction": "bucket",
            "target_date": "2026-04-08",
            "unit": "fahrenheit",
            "model_prob": 0.10,
            "market_yes": 0.30,
            "market_no": 0.70,
            "edge": -0.20,
            "abs_edge": 0.20,
            "signal": "BUY_NO",
            "confidence": "high",
            "ensemble_size": 31,
            "slug": "",
            "condition_id": "def",
        }]
        print_results(opps)
        out = capsys.readouterr().out
        assert "40-41" in out
        assert "BUY_NO" in out

    def test_verbose_shows_stats(self, capsys: pytest.CaptureFixture[str]) -> None:
        opps = [{
            "question": "Test",
            "city": "Miami",
            "metric": "temp_high",
            "threshold_low": 80.0,
            "threshold_high": None,
            "direction": "above",
            "target_date": "2026-04-08",
            "unit": "fahrenheit",
            "model_prob": 0.70,
            "market_yes": 0.50,
            "market_no": 0.50,
            "edge": 0.20,
            "abs_edge": 0.20,
            "signal": "BUY_YES",
            "confidence": "medium",
            "ensemble_size": 31,
            "slug": "",
            "condition_id": "ghi",
            "ensemble_stats": {"mean": 82.3, "min": 75.1, "max": 89.4},
        }]
        print_results(opps, verbose=True)
        out = capsys.readouterr().out
        assert "mean=82.3" in out
        assert "75.1" in out
        assert "89.4" in out
