"""Open-Meteo Ensemble Fetcher — async version of scan_edge.py:fetch_ensemble().

Fetches hourly ensemble forecasts from Open-Meteo (free, no key needed),
computes daily max/min per member. GFS has 31 members (1 control + 30 perturbations).

Architecture copied from Moon Dev's Polymarket weather scanner.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiohttp

from infra.config import (
    ENSEMBLE_CACHE_TTL_SECS,
    MAX_HORIZON_DAYS,
    RATE_LIMIT_ABORT_THRESHOLD,
)
from infra.types import EnsembleResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# L1 session cache stats (reset per scan cycle via reset_cache_stats)
# ---------------------------------------------------------------------------

_cache_stats: dict[str, int] = {
    "l1_hits": 0,
    "l1_neg": 0,   # negative cache hits (prior call returned None this cycle)
    "l2_hits": 0,
    "misses": 0,
}


def get_cache_stats() -> dict[str, int]:
    """Return a copy of current cache hit/miss stats."""
    return dict(_cache_stats)


def reset_cache_stats() -> None:
    """Reset cache stats AND per-cycle rate-limit state.

    NOTE: daily-quota state is NOT reset here — it persists across scan cycles
    until the UTC quota reset time is reached (Open-Meteo resets at 00:00 UTC,
    we wait until 00:05 for safety).
    """
    _cache_stats["l1_hits"] = 0
    _cache_stats["l1_neg"] = 0
    _cache_stats["l2_hits"] = 0
    _cache_stats["misses"] = 0
    _STATE.reset_cycle()


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

OPEN_METEO_ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
CACHE_TTL = ENSEMBLE_CACHE_TTL_SECS  # 4h default — GFS updates every 6h

# Rate limiting — Open-Meteo free tier is ~10,000 req/day, ~60 req/min
_API_SEMAPHORE = asyncio.Semaphore(2)  # max 2 concurrent requests
_API_DELAY = 0.5  # seconds between requests (was 0.3, too aggressive for 31 cities)
_MAX_RETRIES = 2  # reduced from 3 — fail fast on 429 storms
_RATE_LIMIT_UNTIL: float = 0.0  # global cooldown: all requests pause until this timestamp

# Threshold for consecutive 429s before aborting the rest of a scan cycle.
# Imported from infra/config so tests and ops can tune via env without
# touching code. Kept as a module alias for backward compatibility.
_429_ABORT_THRESHOLD = RATE_LIMIT_ABORT_THRESHOLD


# ---------------------------------------------------------------------------
# Phase 10.D — RateLimitState: encapsulated rate-limit / quota tracker.
#
# Why a class? Phase 10.B used five module-level globals that main.py reached
# into via `_ens._DAILY_QUOTA_EXHAUSTED` etc. Two problems:
#   1. Wall-clock vs monotonic: `_QUOTA_RESET_AT` was a `time.monotonic()`
#      offset. On Windows laptop sleep/hibernate, monotonic pauses while
#      wall-clock advances — the bot got stuck "exhausted" past the real
#      00:05 UTC reset.
#   2. Private-attribute coupling between modules.
#
# Both fixed here: state is a single `_STATE` instance with a public API,
# and times are stored as timezone-aware `datetime` in UTC.
# ---------------------------------------------------------------------------


@dataclass
class _RateLimitState:
    failed_cities: set[str] = field(default_factory=set)
    rate_limit_until: datetime | None = None      # short-term 429 cooldown
    daily_quota_exhausted: bool = False
    quota_reset_at: datetime | None = None        # wall-clock UTC reset time
    rate_429_count: int = 0

    # ---- queries -----------------------------------------------------

    def is_quota_exhausted(self) -> bool:
        """Return True if daily quota is currently exhausted.

        Auto-clears the flag if the wall-clock reset time has passed —
        handles the laptop-sleep case where monotonic clocks would lie.
        """
        if not self.daily_quota_exhausted:
            return False
        if self.quota_reset_at is not None and datetime.now(UTC) >= self.quota_reset_at:
            self.daily_quota_exhausted = False
            self.quota_reset_at = None
            return False
        return True

    def seconds_until_reset(self) -> float:
        """Seconds until the daily quota resets (0 if not exhausted or already past)."""
        if not self.daily_quota_exhausted or self.quota_reset_at is None:
            return 0.0
        delta = (self.quota_reset_at - datetime.now(UTC)).total_seconds()
        return max(0.0, delta)

    def should_skip_city(self, city: str) -> bool:
        return city in self.failed_cities

    def is_abort_threshold_reached(self) -> bool:
        return self.rate_429_count >= _429_ABORT_THRESHOLD

    # ---- mutations ---------------------------------------------------

    def reset_cycle(self) -> None:
        """Clear per-cycle state at the start of each scan.

        Daily-quota fields persist across cycles intentionally.
        """
        self.failed_cities.clear()
        self.rate_429_count = 0

    def mark_city_failed(self, city: str) -> None:
        self.failed_cities.add(city)

    def mark_429(self) -> None:
        self.rate_429_count += 1

    def reset_429_count(self) -> None:
        self.rate_429_count = 0

    def mark_daily_quota_exhausted(self, reset_at: datetime) -> None:
        if reset_at.tzinfo is None:
            reset_at = reset_at.replace(tzinfo=UTC)
        self.daily_quota_exhausted = True
        self.quota_reset_at = reset_at


_STATE = _RateLimitState()


# ---------------------------------------------------------------------------
# Public rate-limit API — main.py and tests use these instead of touching
# the singleton attributes directly.
# ---------------------------------------------------------------------------


def is_quota_exhausted() -> bool:
    """True if Open-Meteo daily quota is currently exhausted."""
    return _STATE.is_quota_exhausted()


def seconds_until_reset() -> float:
    """Seconds remaining until daily quota resets (0 if not exhausted)."""
    return _STATE.seconds_until_reset()


def is_rate_limit_abort_threshold_reached() -> bool:
    """True if consecutive 429 count crossed the abort threshold this cycle."""
    return _STATE.is_abort_threshold_reached()


# NWP models to fetch — equal-weight averaging across models
MULTI_MODELS: tuple[str, ...] = ("gfs_seamless", "ecmwf_ifs025", "icon_seamless")

# Known cities — hardcoded coords for speed (no geocoding API call needed)
US_CITIES: dict[str, tuple[float, float]] = {
    "New York":      (40.7128, -74.0060),
    "Los Angeles":   (34.0522, -118.2437),
    "Chicago":       (41.8781, -87.6298),
    "Miami":         (25.7617, -80.1918),
    "Washington DC": (38.9072, -77.0369),
    "Houston":       (29.7604, -95.3698),
    "Phoenix":       (33.4484, -112.0740),
    "Denver":        (39.7392, -104.9903),
    "Seattle":       (47.6062, -122.3321),
    "Boston":        (42.3601, -71.0589),
    "Atlanta":       (33.7490, -84.3880),
    "Dallas":        (32.7767, -96.7970),
    "San Francisco": (37.7749, -122.4194),
    "Minneapolis":   (44.9778, -93.2650),
    "Detroit":       (42.3314, -83.0458),
    "Austin":        (30.2672, -97.7431),
    # International cities
    "London":        (51.5074, -0.1278),
    "Paris":         (48.8566,  2.3522),
    "Madrid":        (40.4168, -3.7038),
    "Berlin":        (52.5200, 13.4050),
    "Rome":          (41.9028, 12.4964),
    "Tokyo":         (35.6762, 139.6503),
    "Seoul":         (37.5665, 126.9780),
    "Shanghai":      (31.2304, 121.4737),
    "Beijing":       (39.9042, 116.4074),
    "Hong Kong":     (22.3193, 114.1694),
    "Sydney":        (-33.8688, 151.2093),
    "Mumbai":        (19.0760, 72.8777),
    # Polymarket active weather cities (discovered 2026-04-09)
    "Toronto":       (43.6532, -79.3832),
    "Buenos Aires":  (-34.6037, -58.3816),
    "Wellington":    (-41.2866, 174.7756),
    "Moscow":        (55.7558, 37.6173),
    "Mexico City":   (19.4326, -99.1332),
    "Singapore":     (1.3521, 103.8198),
    "Jakarta":       (-6.2088, 106.8456),
    "Kuala Lumpur":  (3.1390, 101.6869),
    "Sao Paulo":     (-23.5505, -46.6333),
    "Amsterdam":     (52.3676, 4.9041),
    "Taipei":        (25.0330, 121.5654),
    "Istanbul":      (41.0082, 28.9784),
    # Phase 10.C: additional Polymarket weather cities (added 2026-04-10)
    "Tel Aviv":      (32.0853, 34.7818),
    "Panama City":   (8.9824, -79.5199),
    "Warsaw":        (52.2297, 21.0122),
    "Munich":        (48.1351, 11.5820),
    "Helsinki":      (60.1699, 24.9384),
    "Lucknow":       (26.8467, 80.9462),
    "Ankara":        (39.9334, 32.8597),
    "Milan":         (45.4642, 9.1900),
    "Shenzhen":      (22.5431, 114.0579),
    "Chongqing":     (29.4316, 106.9123),
    "Wuhan":         (30.5928, 114.3055),
    "Busan":         (35.1796, 129.0756),
    "Chengdu":       (30.5728, 104.0668),
}

# Aliases to match Polymarket question text -> city key
CITY_ALIASES: dict[str, list[str]] = {
    "New York":      ["new york city", "new york", "nyc"],
    "Los Angeles":   ["los angeles"],
    "Chicago":       ["chicago"],
    "Miami":         ["miami"],
    "Washington DC": ["washington dc", "washington d.c."],
    "Houston":       ["houston"],
    "Phoenix":       ["phoenix"],
    "Denver":        ["denver"],
    "Seattle":       ["seattle"],
    "Boston":        ["boston"],
    "Atlanta":       ["atlanta"],
    "Dallas":        ["dallas"],
    "San Francisco": ["san francisco"],
    "Minneapolis":   ["minneapolis"],
    "Detroit":       ["detroit"],
    "Austin":        ["austin"],
    # International
    "London":        ["london"],
    "Paris":         ["paris"],
    "Madrid":        ["madrid"],
    "Berlin":        ["berlin"],
    "Rome":          ["rome"],
    "Tokyo":         ["tokyo"],
    "Seoul":         ["seoul"],
    "Shanghai":      ["shanghai"],
    "Beijing":       ["beijing"],
    "Hong Kong":     ["hong kong"],
    "Sydney":        ["sydney"],
    "Mumbai":        ["mumbai"],
    "Toronto":       ["toronto"],
    "Buenos Aires":  ["buenos aires"],
    "Wellington":    ["wellington"],
    "Moscow":        ["moscow"],
    "Mexico City":   ["mexico city"],
    "Singapore":     ["singapore"],
    "Jakarta":       ["jakarta"],
    "Kuala Lumpur":  ["kuala lumpur"],
    "Sao Paulo":     ["sao paulo", "são paulo"],
    "Amsterdam":     ["amsterdam"],
    "Taipei":        ["taipei"],
    "Istanbul":      ["istanbul"],
    # Phase 10.C additions
    "Tel Aviv":      ["tel aviv"],
    "Panama City":   ["panama city"],
    "Warsaw":        ["warsaw"],
    "Munich":        ["munich"],
    "Helsinki":      ["helsinki"],
    "Lucknow":       ["lucknow"],
    "Ankara":        ["ankara"],
    "Milan":         ["milan"],
    "Shenzhen":      ["shenzhen"],
    "Chongqing":     ["chongqing"],
    "Wuhan":         ["wuhan"],
    "Busan":         ["busan"],
    "Chengdu":       ["chengdu"],
}

# ---------------------------------------------------------------------------
# Cache (file-based, same as scan_edge.py)
# ---------------------------------------------------------------------------

_CACHE_DIR = Path("data/ensemble_cache")


def _cache_get(key: str) -> dict | None:
    path = _CACHE_DIR / f"{key}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - data.get("_ts", 0) < CACHE_TTL:
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return None


def _cache_set(key: str, data: dict) -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    data["_ts"] = time.time()
    (_CACHE_DIR / f"{key}.json").write_text(json.dumps(data), encoding="utf-8")


# ---------------------------------------------------------------------------
# Ensemble Fetcher
# ---------------------------------------------------------------------------


async def fetch_ensemble(
    city: str,
    lat: float,
    lon: float,
    target_date: str,
    unit: str = "fahrenheit",
    session: aiohttp.ClientSession | None = None,
    model: str = "gfs_seamless",
) -> dict | None:
    """Fetch ensemble forecast from Open-Meteo (hourly, all members).

    Uses hourly endpoint to get individual ensemble member data,
    then computes daily max/min per member. Member count varies by model:
    GFS=31, ECMWF=51, ICON=40.

    Returns dict with:
        temperature_max: list of daily max values (one per member)
        temperature_min: list of daily min values (one per member)
    """
    cache_key = f"ensemble_v2_{city.replace(' ', '_')}_{target_date}_{unit}_{model}"
    cached = _cache_get(cache_key)
    if cached:
        _cache_stats["l2_hits"] += 1
        return {k: v for k, v in cached.items() if k != "_ts"}

    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_2m",
        "temperature_unit": unit,
        "start_date": target_date,
        "end_date": target_date,
        "models": model,
    }

    close_session = False
    if session is None:
        session = aiohttp.ClientSession()
        close_session = True

    data = None
    try:
        global _RATE_LIMIT_UNTIL
        # Daily quota exhausted? fast-return until wall-clock UTC reset.
        # is_quota_exhausted() auto-clears the flag if reset time has passed,
        # which handles laptop sleep/hibernate that monotonic clocks miss.
        if _STATE.is_quota_exhausted():
            return None
        # Per-city failure tracking — skip cities that already 429'd this cycle
        if _STATE.should_skip_city(city):
            return None
        # Safety net: catastrophic burst of 429s (e.g., quota issues)
        if _STATE.is_abort_threshold_reached():
            return None
        for attempt in range(_MAX_RETRIES):
            # Global cooldown: if ANY request got 429, ALL requests pause
            now = time.monotonic()
            if now < _RATE_LIMIT_UNTIL:
                await asyncio.sleep(_RATE_LIMIT_UNTIL - now)
            async with _API_SEMAPHORE:
                try:
                    async with session.get(
                        OPEN_METEO_ENSEMBLE_URL,
                        params=params,
                        headers={"User-Agent": "WeatherArbitrageBot/1.0"},
                        timeout=aiohttp.ClientTimeout(total=20),
                    ) as resp:
                        if resp.status == 429:
                            _STATE.mark_429()
                            _STATE.mark_city_failed(city)  # skip this city rest of cycle

                            # Parse body to detect DAILY quota vs per-minute limit
                            reason = ""
                            try:
                                body = await resp.json()
                                reason = str(body.get("reason", "")).lower()
                            except Exception:
                                pass

                            if "daily" in reason:
                                now_utc = datetime.now(UTC)
                                tomorrow_utc = (now_utc + timedelta(days=1)).replace(
                                    hour=0, minute=5, second=0, microsecond=0,
                                )
                                sleep_secs = (tomorrow_utc - now_utc).total_seconds()
                                _STATE.mark_daily_quota_exhausted(tomorrow_utc)
                                logger.critical(
                                    "Open-Meteo DAILY quota exhausted (%s): "
                                    "cooling %.1fh until %s UTC",
                                    reason, sleep_secs / 3600, tomorrow_utc.isoformat(),
                                )
                                return None

                            if _STATE.is_abort_threshold_reached():
                                logger.warning(
                                    "Open-Meteo rate limited (%d consecutive 429s), "
                                    "aborting remaining requests this cycle",
                                    _STATE.rate_429_count,
                                )
                                return None
                            wait = (2 ** attempt) * 2.0 + 3.0  # 5s, 7s
                            _RATE_LIMIT_UNTIL = time.monotonic() + wait
                            logger.warning(
                                "Open-Meteo 429 for %s (model=%s), global cooldown %.0fs [%d/%d]",
                                city, model, wait, _STATE.rate_429_count, _429_ABORT_THRESHOLD,
                            )
                            await asyncio.sleep(wait)
                            continue
                        if resp.status != 200:
                            logger.warning("Open-Meteo returned %d for %s", resp.status, city)
                            return None
                        # Success — reset consecutive 429 counter
                        _STATE.reset_429_count()
                        data = await resp.json()
                        break
                except (aiohttp.ClientError, TimeoutError) as e:
                    if attempt < _MAX_RETRIES - 1:
                        await asyncio.sleep((2 ** attempt) * 0.5)
                        continue
                    logger.warning("Error fetching ensemble for %s: %s", city, e)
                    return None
                finally:
                    await asyncio.sleep(_API_DELAY)
    finally:
        if close_session:
            await session.close()

    if data is None:
        return None

    hourly = data.get("hourly", {})

    # Collect all member keys: temperature_2m (control), temperature_2m_memberNN
    # Member count varies by model (GFS=30, ECMWF=50, ICON=39 perturbed)
    member_keys = ["temperature_2m"]  # control run
    for i in range(1, 51):  # max 50 perturbed members (ECMWF)
        key = f"temperature_2m_member{i:02d}"
        if key in hourly:
            member_keys.append(key)

    # Compute daily max and min per member (each key has 24 hourly values)
    temp_maxes: list[float] = []
    temp_mins: list[float] = []
    for key in member_keys:
        hourly_temps = hourly.get(key, [])
        valid = [t for t in hourly_temps if t is not None and math.isfinite(t)]
        if valid:
            temp_maxes.append(max(valid))
            temp_mins.append(min(valid))

    # Warn if member count is unexpectedly low (GFS=31, ECMWF=51, ICON=40)
    expected = len(member_keys)
    actual = len(temp_maxes)
    if actual < expected:
        logger.warning(
            "Ensemble %s %s: only %d/%d members produced valid temps",
            city, target_date, actual, expected,
        )

    result = {
        "temperature_max": temp_maxes,
        "temperature_min": temp_mins,
    }

    _cache_stats["misses"] += 1
    _cache_set(cache_key, result)
    return result


async def fetch_ensemble_result(
    city: str,
    target_date: str,
    metric: str = "temp_high",
    unit: str = "fahrenheit",
    session: aiohttp.ClientSession | None = None,
    session_cache: dict | None = None,
) -> EnsembleResult | None:
    """High-level wrapper that returns an EnsembleResult dataclass.

    Args:
        session_cache: Optional L1 in-memory cache dict, shared across a
            single scan cycle. When provided, results are stored/retrieved
            here *before* hitting the L2 file cache or API.
    """
    if city not in US_CITIES:
        return None

    # P8.5: Forecast horizon check — skip stale forecasts beyond MAX_HORIZON_DAYS
    try:
        from datetime import date as _date
        target = _date.fromisoformat(target_date)
        today = datetime.now(UTC).date()
        days_out = (target - today).days
        if days_out > MAX_HORIZON_DAYS:
            logger.debug(
                "Skipping %s forecast %d days out (max %d)",
                city, days_out, MAX_HORIZON_DAYS,
            )
            return None
    except (ValueError, TypeError):
        pass  # bad date format — let it proceed, will fail later

    # L1 session cache lookup (must include metric to avoid collisions).
    # Supports negative caching: if a prior call this cycle returned None,
    # the cache stores None so subsequent calls short-circuit without API.
    l1_key = f"{city}_{target_date}_{metric}_{unit}"
    if session_cache is not None and l1_key in session_cache:
        cached_value = session_cache[l1_key]
        if cached_value is None:
            _cache_stats["l1_neg"] += 1
            return None
        _cache_stats["l1_hits"] += 1
        return cached_value

    lat, lon = US_CITIES[city]
    raw = await fetch_ensemble(city, lat, lon, target_date, unit, session)
    if raw is None:
        # Negative cache: prevent re-fetching the same (city, date, metric)
        # for the rest of this scan cycle.
        if session_cache is not None:
            session_cache[l1_key] = None
        return None

    if metric == "temp_high":
        members = raw.get("temperature_max", [])
    else:
        members = raw.get("temperature_min", [])

    if not members:
        if session_cache is not None:
            session_cache[l1_key] = None
        return None

    result = EnsembleResult(
        location=city,
        lat=lat,
        lon=lon,
        target_date=target_date,
        metric=metric,
        unit=unit,
        members=tuple(members),
        probability=0.0,  # Calculated later by ensemble_probability
        member_count=len(members),
        model="gfs_seamless",
        confidence="medium",
        fetched_at=datetime.now(UTC),
    )

    # Store in L1 session cache for this scan cycle
    if session_cache is not None:
        session_cache[l1_key] = result

    return result


# ---------------------------------------------------------------------------
# Multi-model ensemble fetching
# ---------------------------------------------------------------------------


async def fetch_multi_model_ensemble(
    city: str,
    lat: float,
    lon: float,
    target_date: str,
    unit: str = "fahrenheit",
    session: aiohttp.ClientSession | None = None,
    models: tuple[str, ...] = MULTI_MODELS,
) -> dict[str, dict]:
    """Fetch ensemble data from multiple NWP models in parallel.

    Returns: {model_name: {"temperature_max": [...], "temperature_min": [...]}}
    Only includes models that succeeded.
    """
    tasks = [
        fetch_ensemble(city, lat, lon, target_date, unit, session, model=m)
        for m in models
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    combined: dict[str, dict] = {}
    for model_name, result in zip(models, results):
        if isinstance(result, BaseException):
            logger.warning("Model %s raised %s for %s", model_name, result, city)
            continue
        if result is None:
            logger.warning("Model %s returned None for %s", model_name, city)
            continue
        combined[model_name] = result
    return combined


async def fetch_multi_model_result(
    city: str,
    target_date: str,
    metric: str = "temp_high",
    unit: str = "fahrenheit",
    session: aiohttp.ClientSession | None = None,
    session_cache: dict | None = None,
) -> dict[str, EnsembleResult] | None:
    """High-level wrapper returning {model: EnsembleResult} for each model.

    Uses L1 session cache. Returns None if ALL models fail.
    """
    if city not in US_CITIES:
        return None

    # Forecast horizon check — skip stale forecasts beyond MAX_HORIZON_DAYS
    try:
        from datetime import date as _date
        target = _date.fromisoformat(target_date)
        today = datetime.now(UTC).date()
        days_out = (target - today).days
        if days_out > MAX_HORIZON_DAYS:
            logger.debug(
                "Skipping multi-model %s forecast %d days out (max %d)",
                city, days_out, MAX_HORIZON_DAYS,
            )
            return None
    except (ValueError, TypeError):
        pass

    l1_key = f"multi_{city}_{target_date}_{metric}_{unit}"
    if session_cache is not None and l1_key in session_cache:
        cached_value = session_cache[l1_key]
        if cached_value is None:
            _cache_stats["l1_neg"] += 1
            return None
        _cache_stats["l1_hits"] += 1
        return cached_value

    lat, lon = US_CITIES[city]
    raw_by_model = await fetch_multi_model_ensemble(
        city, lat, lon, target_date, unit, session,
    )
    if not raw_by_model:
        if session_cache is not None:
            session_cache[l1_key] = None
        return None

    result: dict[str, EnsembleResult] = {}
    for model_name, raw in raw_by_model.items():
        if metric == "temp_high":
            members = raw.get("temperature_max", [])
        else:
            members = raw.get("temperature_min", [])
        if not members:
            continue
        result[model_name] = EnsembleResult(
            location=city,
            lat=lat,
            lon=lon,
            target_date=target_date,
            metric=metric,
            unit=unit,
            members=tuple(members),
            probability=0.0,
            member_count=len(members),
            model=model_name,
            confidence="medium",
            fetched_at=datetime.now(UTC),
        )

    if not result:
        if session_cache is not None:
            session_cache[l1_key] = None
        return None

    models_str = ", ".join(f"{m}({er.member_count})" for m, er in result.items())
    logger.info("Multi-model %s %s %s: %s", city, metric, target_date, models_str)

    if session_cache is not None:
        session_cache[l1_key] = result

    return result


def resolve_city(text: str) -> str | None:
    """Match text to a known US city using aliases."""
    text_lower = text.lower()
    for city_name, aliases in CITY_ALIASES.items():
        for alias in aliases:
            if alias in text_lower:
                return city_name
    return None
