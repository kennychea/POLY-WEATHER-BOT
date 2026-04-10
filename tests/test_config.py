"""Tests for infra/config.py — validation and env parsing."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from infra.config import Config, _parse_model_weights


# ── Validation tests ──────────────────────────────────────────────


_VALID_ENV = {
    "POLYMARKET_API_KEY": "k",
    "POLYMARKET_API_SECRET": "s",
    "POLYMARKET_API_PASSPHRASE": "p",
    "POLYMARKET_WALLET_ADDRESS": "w",
    "POLYMARKET_PRIVATE_KEY": "pk",
    "TELEGRAM_BOT_TOKEN": "tbt",
    "TELEGRAM_CHAT_ID": "tci",
    "TRADING_MODE": "paper",
    "BANKROLL_USDC": "100",
    "MAX_BET_USDC": "10",
    "MIN_EDGE_THRESHOLD": "0.05",
    "MAX_EXPOSURE_USDC": "50",
    "MAX_EDGE": "0.15",
    "TAKER_FEE_PCT": "0.01",
    "PRICE_CHECK_DELAY_S": "60",
    "OPENWEATHER_API_KEY": "owm",
    "WEATHER_SCAN_INTERVAL": "900",
    "MARKET_REFRESH_INTERVAL": "300",
    "FORECAST_LOCATIONS": "New York,Miami",
    "ENSEMBLE_MODEL": "gfs_seamless",
    "ENSEMBLE_CACHE_TTL": "1800",
}


class TestConfigValidation:
    def test_valid_config_from_env(self) -> None:
        with patch.dict(os.environ, _VALID_ENV, clear=False):
            cfg = Config.from_env()
            assert cfg.bankroll_usdc == 100.0
            assert cfg.max_bet_usdc == 10.0
            assert cfg.trading_mode.value == "paper"

    def test_bankroll_must_be_positive(self) -> None:
        env = {**_VALID_ENV, "BANKROLL_USDC": "0"}
        with patch.dict(os.environ, env, clear=False):
            with pytest.raises(ValueError, match="BANKROLL_USDC must be > 0"):
                Config.from_env()

    def test_max_bet_must_be_positive(self) -> None:
        env = {**_VALID_ENV, "MAX_BET_USDC": "-5"}
        with patch.dict(os.environ, env, clear=False):
            with pytest.raises(ValueError, match="MAX_BET_USDC must be > 0"):
                Config.from_env()

    def test_max_bet_cannot_exceed_bankroll(self) -> None:
        env = {**_VALID_ENV, "MAX_BET_USDC": "200", "MAX_EXPOSURE_USDC": "500"}
        with patch.dict(os.environ, env, clear=False):
            with pytest.raises(ValueError, match="MAX_BET_USDC.*> BANKROLL"):
                Config.from_env()

    def test_max_bet_cannot_exceed_max_exposure(self) -> None:
        env = {**_VALID_ENV, "MAX_BET_USDC": "60", "MAX_EXPOSURE_USDC": "50", "BANKROLL_USDC": "500"}
        with patch.dict(os.environ, env, clear=False):
            with pytest.raises(ValueError, match="MAX_BET_USDC.*> MAX_EXPOSURE"):
                Config.from_env()

    def test_max_edge_must_be_valid(self) -> None:
        env = {**_VALID_ENV, "MAX_EDGE": "2.0"}
        with patch.dict(os.environ, env, clear=False):
            with pytest.raises(ValueError, match="MAX_EDGE must be between"):
                Config.from_env()

    def test_min_edge_cannot_exceed_max_edge(self) -> None:
        env = {**_VALID_ENV, "MIN_EDGE_THRESHOLD": "0.20", "MAX_EDGE": "0.10"}
        with patch.dict(os.environ, env, clear=False):
            with pytest.raises(ValueError, match="MIN_EDGE.*> MAX_EDGE"):
                Config.from_env()

    def test_kelly_fraction_bounds(self) -> None:
        env = {**_VALID_ENV, "KELLY_FRACTION": "1.5"}
        with patch.dict(os.environ, env, clear=False):
            with pytest.raises(ValueError, match="KELLY_FRACTION"):
                Config.from_env()


# ── Model weights parsing ─────────────────────────────────────────


class TestModelWeights:
    def test_empty_returns_defaults(self) -> None:
        result = _parse_model_weights("")
        assert "ecmwf_ifs025" in result
        assert "gfs_seamless" in result

    def test_valid_json(self) -> None:
        result = _parse_model_weights('{"gfs_seamless": 1.0}')
        assert result == {"gfs_seamless": 1.0}

    def test_invalid_json_returns_defaults(self) -> None:
        result = _parse_model_weights("not json")
        assert "ecmwf_ifs025" in result

    def test_wrong_type_returns_defaults(self) -> None:
        result = _parse_model_weights('[1, 2, 3]')
        assert "ecmwf_ifs025" in result
