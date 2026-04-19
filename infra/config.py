"""Configuration loader — reads .env and exposes typed Config."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from infra.types import TradingMode

# Default model weights — ECMWF is highest-skill global NWP model
DEFAULT_MODEL_WEIGHTS: dict[str, float] = {
    "ecmwf_ifs025": 0.5,
    "gfs_seamless": 0.3,
    "icon_seamless": 0.2,
}

# ---------------------------------------------------------------------------
# Phase 10.D — module-level trading thresholds (env-overridable)
# These intentionally live OUTSIDE the Config dataclass: they're hot-path
# constants imported by main.py and weather/ensemble.py, not per-process
# settings the user is expected to tune via .env. Defaults match the
# previously hardcoded values in the call sites — no .env update required.
# ---------------------------------------------------------------------------

# BUY_NO price band guard (symmetric). Restricts BUY_NO trades to
# YES ∈ [0.30, 0.70] — the only zone where risk/reward is profitable.
#
# At YES <= 0.30 the NO entry is 0.70+: breakeven WR ~70%, realized ~60%.
# At YES >= 0.70 the NO entry is 0.30-: cheap contrarian bet, realized WR ~0%.
# Sweet spot YES ∈ [0.35, 0.65] has PF=1.59 on n=24; the 0.30 band keeps
# volume for data collection and still achieves PF=1.32 on n=31.
#
# Mirror gate uses 1 - BUY_NO_MIN_YES_PRICE automatically.
BUY_NO_MIN_YES_PRICE: float = float(os.environ.get("BUY_NO_MIN_YES_PRICE", "0.30"))

# Open-Meteo loses skill beyond ~5 days. Skip forecasts further out.
MAX_HORIZON_DAYS: int = int(os.environ.get("MAX_HORIZON_DAYS", "5"))

# Phase 12.D.H3: forecast extremity gate. Ensemble model is only well-calibrated
# when it predicts very low P(YES). Above 5%, observed PnL is -$44 (n=46, WR 51%).
# Below 5%, PnL is +$30 (n=26, WR 65%). Block any BUY_NO trade where the model
# says P(YES) >= this threshold. BUY_YES side is already kill-switched elsewhere.
# Set to 1.0 to disable the gate entirely.
MAX_FORECAST_PROB_FOR_TRADE: float = float(
    os.environ.get("MAX_FORECAST_PROB_FOR_TRADE", "0.05")
)

# L2 file cache TTL for ensemble forecasts. GFS updates every 6h; 4h cuts
# API load roughly in half without serving truly stale data.
ENSEMBLE_CACHE_TTL_SECS: int = int(os.environ.get("ENSEMBLE_CACHE_TTL_SECS", "14400"))

# Consecutive-429 safety net. Per-city failure tracking handles the common
# case; this only kicks in for catastrophic bursts (e.g., daily quota issues).
RATE_LIMIT_ABORT_THRESHOLD: int = int(os.environ.get("RATE_LIMIT_ABORT_THRESHOLD", "15"))


def _require(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        msg = f"Missing required environment variable: {key}"
        raise ValueError(msg)
    return val


def _get(key: str, default: str = "") -> str:
    val = os.environ.get(key)
    return val if val else default


@dataclass(frozen=True, slots=True)
class Config:
    """Immutable application configuration loaded from environment."""

    # Polymarket
    polymarket_api_key: str
    polymarket_api_secret: str
    polymarket_api_passphrase: str
    polymarket_wallet_address: str
    polymarket_private_key: str

    # Telegram Bot (alerts)
    telegram_bot_token: str
    telegram_chat_id: str

    # Trading
    trading_mode: TradingMode
    bankroll_usdc: float
    max_bet_usdc: float
    min_edge_threshold: float
    max_exposure_usdc: float
    max_edge: float
    taker_fee_pct: float
    price_check_delay_s: int

    # Weather
    openweather_api_key: str
    weather_scan_interval: int      # seconds between forecast scans
    market_refresh_interval: int    # seconds between Polymarket refresh
    forecast_locations: list[str]   # cities to track

    # Ensemble forecast
    ensemble_model: str             # "gfs_seamless" or "ecmwf_ifs025"
    ensemble_cache_ttl: int         # cache duration in seconds

    # Multi-model toggle — False = GFS-only (Moon Dev baseline), True = GFS+ECMWF+ICON
    use_multi_model: bool = False

    # Model weights for multi-model averaging (only used when use_multi_model=True)
    model_weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_MODEL_WEIGHTS))

    # Risk tuning
    kelly_fraction: float = 0.25            # fraction of Kelly (0.25 = quarter-Kelly)
    max_position_pct: float = 0.15          # max single position as % of bankroll
    min_book_depth_usd: float = 50.0        # minimum orderbook depth to trade

    def __post_init__(self) -> None:
        errors = []
        if self.bankroll_usdc <= 0:
            errors.append("BANKROLL_USDC must be > 0")
        if self.max_bet_usdc <= 0:
            errors.append("MAX_BET_USDC must be > 0")
        if self.max_exposure_usdc <= 0:
            errors.append("MAX_EXPOSURE_USDC must be > 0")
        if self.max_bet_usdc > self.bankroll_usdc:
            errors.append(
                f"MAX_BET_USDC ({self.max_bet_usdc}) > BANKROLL ({self.bankroll_usdc})"
            )
        if self.max_bet_usdc > self.max_exposure_usdc:
            errors.append(
                f"MAX_BET_USDC ({self.max_bet_usdc}) > MAX_EXPOSURE ({self.max_exposure_usdc})"
            )
        if self.max_edge <= 0 or self.max_edge > 1.0:
            errors.append("MAX_EDGE must be between 0 and 1")
        if self.min_edge_threshold > self.max_edge:
            errors.append(
                f"MIN_EDGE ({self.min_edge_threshold}) > MAX_EDGE ({self.max_edge})"
            )
        if not 0 < self.kelly_fraction <= 1.0:
            errors.append("KELLY_FRACTION must be between 0 and 1")
        if not 0 < self.max_position_pct <= 1.0:
            errors.append("MAX_POSITION_PCT must be between 0 and 1")
        if self.min_book_depth_usd < 0:
            errors.append("MIN_BOOK_DEPTH_USD must be >= 0")
        if errors:
            raise ValueError("Invalid config: " + "; ".join(errors))

    @classmethod
    def from_env(cls) -> Config:
        mode = TradingMode(_get("TRADING_MODE", "paper"))
        poly_get = _require if mode == TradingMode.LIVE else _get
        return cls(
            polymarket_api_key=poly_get("POLYMARKET_API_KEY"),
            polymarket_api_secret=poly_get("POLYMARKET_API_SECRET"),
            polymarket_api_passphrase=poly_get("POLYMARKET_API_PASSPHRASE"),
            polymarket_wallet_address=poly_get("POLYMARKET_WALLET_ADDRESS"),
            polymarket_private_key=poly_get("POLYMARKET_PRIVATE_KEY"),
            telegram_bot_token=_require("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=_require("TELEGRAM_CHAT_ID"),
            trading_mode=mode,
            bankroll_usdc=float(_get("BANKROLL_USDC", "100")),
            max_bet_usdc=float(_get("MAX_BET_USDC", "10")),
            min_edge_threshold=float(_get("MIN_EDGE_THRESHOLD", "0.05")),
            max_exposure_usdc=float(_get("MAX_EXPOSURE_USDC", "50")),
            max_edge=float(_get("MAX_EDGE", "0.15")),
            taker_fee_pct=float(_get("TAKER_FEE_PCT", "0.01")),
            price_check_delay_s=int(_get("PRICE_CHECK_DELAY_S", "60")),
            openweather_api_key=_get("OPENWEATHER_API_KEY", ""),
            weather_scan_interval=int(_get("WEATHER_SCAN_INTERVAL", "900")),
            market_refresh_interval=int(_get("MARKET_REFRESH_INTERVAL", "300")),
            forecast_locations=_parse_locations(
                _get("FORECAST_LOCATIONS", "New York,Los Angeles,Chicago,Miami,Washington DC")
            ),
            # Ensemble
            ensemble_model=_get("ENSEMBLE_MODEL", "gfs_seamless"),
            ensemble_cache_ttl=int(_get("ENSEMBLE_CACHE_TTL", "1800")),
            # Multi-model toggle
            use_multi_model=_get("USE_MULTI_MODEL", "false").lower() in ("true", "1", "yes"),
            # Model weights
            model_weights=_parse_model_weights(_get("MODEL_WEIGHTS", "")),
            # Risk tuning
            kelly_fraction=float(_get("KELLY_FRACTION", "0.25")),
            max_position_pct=float(_get("MAX_POSITION_PCT", "0.15")),
            min_book_depth_usd=float(_get("MIN_BOOK_DEPTH_USD", "50")),
        )


def _parse_model_weights(raw: str) -> dict[str, float]:
    """Parse MODEL_WEIGHTS env var (JSON string) or return defaults."""
    if not raw:
        return dict(DEFAULT_MODEL_WEIGHTS)
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict) and all(
            isinstance(k, str) and isinstance(v, (int, float))
            for k, v in parsed.items()
        ):
            return {k: float(v) for k, v in parsed.items()}
    except (json.JSONDecodeError, TypeError):
        pass
    return dict(DEFAULT_MODEL_WEIGHTS)


def _parse_locations(raw: str) -> list[str]:
    if not raw:
        return []
    return [loc.strip() for loc in raw.split(",") if loc.strip()]
