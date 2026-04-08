"""Core data contracts for the Polymarket Weather Bot.

All dataclasses are frozen (immutable) to prevent accidental mutation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


# -- Enums -------------------------------------------------------------------


class TradingMode(StrEnum):
    PAPER = "paper"
    LIVE = "live"


class SignalType(StrEnum):
    BUY_YES = "buy_yes"
    BUY_NO = "buy_no"


# -- Dataclasses -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TradeResult:
    """The outcome of a trade (paper or live)."""

    market_id: str
    signal_type: SignalType
    entry_price: float
    exit_price: float
    size_usdc: float
    pnl: float
    win: bool
    timestamp: datetime


@dataclass(frozen=True, slots=True)
class WeatherForecast:
    """A weather forecast data point from API."""

    location: str           # e.g. "New York"
    lat: float
    lon: float
    date: str               # ISO date "2026-04-10"
    temp_high_f: float      # Forecast high in Fahrenheit
    temp_low_f: float       # Forecast low in Fahrenheit
    precip_prob: float      # Precipitation probability [0-1]
    precip_inches: float    # Expected precipitation in inches
    snow_inches: float      # Expected snowfall in inches
    wind_mph: float         # Max wind speed
    description: str        # "sunny", "rain", etc.
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class WeatherSignal:
    """A trading signal derived from weather forecast vs market price."""

    market_question: str
    market_id: str
    token_id: str
    signal_type: SignalType
    forecast_probability: float  # Our calculated P from weather data
    market_price: float          # Current Polymarket price
    edge: float                  # forecast_prob - market_price
    location: str
    forecast_date: str
    weather_metric: str          # "temp_high", "precip", "snow", etc.
    threshold_value: float       # The threshold in the market question
    timestamp: datetime
    # Ensemble metadata (optional for backward compat with existing code)
    ensemble_member_count: int = 0
    confidence: str = "medium"   # "high", "medium", "low"
    net_edge: float = 0.0        # edge minus fees


@dataclass(frozen=True, slots=True)
class MarketResolution:
    """Resolution data for a closed Polymarket market."""

    condition_id: str = ""
    resolution_price: float = 0.0  # 0.0 = NO, 1.0 = YES
    closed: bool = True
    source: str = ""  # "gamma_resolved", "gamma_cache_fallback", "force_resolved"
    resolved_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class WeatherPaperTrade:
    """A paper trade for weather markets."""

    trade_id: str
    market_question: str
    market_id: str
    token_id: str
    signal_type: SignalType
    size_usdc: float
    entry_price: float
    exit_price: float | None
    fees: float
    pnl: float | None
    status: str  # "pending" | "resolved" | "error"
    opened_at: datetime
    resolved_at: datetime | None
    resolution_source: str = ""


# -- Ensemble & Edge types ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class EnsembleResult:
    """Result from an ensemble forecast query (Open-Meteo GFS/ECMWF)."""

    location: str
    lat: float
    lon: float
    target_date: str              # ISO date "2026-04-10"
    metric: str                   # "temp_high", "temp_low"
    unit: str                     # "fahrenheit" or "celsius"
    members: tuple[float, ...]    # Per-member values (immutable)
    probability: float            # count(condition met) / total members
    member_count: int             # Total ensemble members (e.g. 31 for GFS)
    model: str                    # "gfs_seamless" or "ecmwf_ifs025"
    confidence: str               # "high", "medium", "low"
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class EdgeResult:
    """Calculated edge between ensemble probability and market price."""

    raw_edge: float               # ensemble_prob - market_price (or inverse)
    net_edge: float               # raw_edge - total fees
    ensemble_prob: float          # What our model says
    market_price: float           # What the market charges (implied prob)
    signal_type: SignalType        # BUY_YES or BUY_NO
    entry_fee: float
    exit_fee: float
    confidence: str               # From ensemble spread
    spread_score: float = 1.0     # Ensemble consensus tightness [0.3, 1.0]


@dataclass(frozen=True, slots=True)
class BookDepth:
    """Order book depth analysis for a Polymarket token."""

    best_bid: float
    best_ask: float
    spread: float
    mid: float
    bid_depth_usd: float          # Total USD on bid side
    ask_depth_usd: float          # Total USD on ask side
    slippage_pct: float           # Estimated slippage for target size
    is_liquid: bool               # Meets minimum depth threshold


