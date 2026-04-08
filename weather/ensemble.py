"""Open-Meteo Ensemble Fetcher — async version of scan_edge.py:fetch_ensemble().

Fetches hourly ensemble forecasts from Open-Meteo (free, no key needed),
computes daily max/min per member. GFS has 31 members (1 control + 30 perturbations).

Architecture copied from Moon Dev's Polymarket weather scanner.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path

import aiohttp

from infra.types import EnsembleResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# L1 session cache stats (reset per scan cycle via reset_cache_stats)
# ---------------------------------------------------------------------------

_cache_stats: dict[str, int] = {"l1_hits": 0, "l2_hits": 0, "misses": 0}


def get_cache_stats() -> dict[str, int]:
    """Return a copy of current cache hit/miss stats."""
    return dict(_cache_stats)


def reset_cache_stats() -> None:
    """Reset cache stats — call at the start of each scan cycle."""
    _cache_stats["l1_hits"] = 0
    _cache_stats["l2_hits"] = 0
    _cache_stats["misses"] = 0


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

OPEN_METEO_ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
CACHE_TTL = 1800  # 30 minutes

# Rate limiting — Open-Meteo free tier is ~60 req/min
_API_SEMAPHORE = asyncio.Semaphore(2)  # max 2 concurrent requests
_API_DELAY = 0.3  # seconds between requests
_MAX_RETRIES = 3

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
        for attempt in range(_MAX_RETRIES):
            async with _API_SEMAPHORE:
                try:
                    async with session.get(
                        OPEN_METEO_ENSEMBLE_URL,
                        params=params,
                        headers={"User-Agent": "WeatherArbitrageBot/1.0"},
                        timeout=aiohttp.ClientTimeout(total=20),
                    ) as resp:
                        if resp.status == 429:
                            wait = (2 ** attempt) * 1.0
                            logger.warning(
                                "Open-Meteo 429 for %s (model=%s), retry in %.0fs",
                                city, model, wait,
                            )
                            await asyncio.sleep(wait)
                            continue
                        if resp.status != 200:
                            logger.warning("Open-Meteo returned %d for %s", resp.status, city)
                            return None
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
        valid = [t for t in hourly_temps if t is not None]
        if valid:
            temp_maxes.append(max(valid))
            temp_mins.append(min(valid))

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

    # P8.5: Forecast horizon check — skip stale forecasts (>5 days out)
    try:
        from datetime import date as _date
        target = _date.fromisoformat(target_date)
        today = datetime.now(UTC).date()
        days_out = (target - today).days
        if days_out > 5:
            logger.debug("Skipping %s forecast %d days out (max 5)", city, days_out)
            return None
    except (ValueError, TypeError):
        pass  # bad date format — let it proceed, will fail later

    # L1 session cache lookup (must include metric to avoid collisions)
    l1_key = f"{city}_{target_date}_{metric}_{unit}"
    if session_cache is not None and l1_key in session_cache:
        _cache_stats["l1_hits"] += 1
        return session_cache[l1_key]

    lat, lon = US_CITIES[city]
    raw = await fetch_ensemble(city, lat, lon, target_date, unit, session)
    if raw is None:
        return None

    if metric == "temp_high":
        members = raw.get("temperature_max", [])
    else:
        members = raw.get("temperature_min", [])

    if not members:
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

    l1_key = f"multi_{city}_{target_date}_{metric}_{unit}"
    if session_cache is not None and l1_key in session_cache:
        _cache_stats["l1_hits"] += 1
        return session_cache[l1_key]

    lat, lon = US_CITIES[city]
    raw_by_model = await fetch_multi_model_ensemble(
        city, lat, lon, target_date, unit, session,
    )
    if not raw_by_model:
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
