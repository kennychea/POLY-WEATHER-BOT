"""Weather data fetcher — OpenWeatherMap API.

Fetches 5-day forecasts for configured locations.
Free tier: 1000 calls/day, 3-hour intervals.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import aiohttp

from infra.types import WeatherForecast

logger = logging.getLogger(__name__)

# OpenWeatherMap 5-day/3-hour forecast
OWM_FORECAST_URL = "https://api.openweathermap.org/data/2.5/forecast"
# OpenWeatherMap geocoding
OWM_GEO_URL = "http://api.openweathermap.org/geo/1.0/direct"


class WeatherFetcher:
    """Fetches weather forecasts from OpenWeatherMap."""

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._session: aiohttp.ClientSession | None = None
        # Cache: location -> (lat, lon)
        self._geo_cache: dict[str, tuple[float, float]] = {}

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
            )
        return self._session

    async def geocode(self, location: str) -> tuple[float, float] | None:
        """Convert city name to (lat, lon). Cached."""
        if location in self._geo_cache:
            return self._geo_cache[location]

        session = await self._get_session()
        params = {"q": location, "limit": 1, "appid": self._api_key}
        try:
            async with session.get(OWM_GEO_URL, params=params) as resp:
                if resp.status != 200:
                    logger.warning("Geocode failed for %s: HTTP %d", location, resp.status)
                    return None
                data = await resp.json()
                if not data:
                    logger.warning("Geocode returned empty for %s", location)
                    return None
                lat, lon = data[0]["lat"], data[0]["lon"]
                self._geo_cache[location] = (lat, lon)
                return (lat, lon)
        except Exception:
            logger.exception("Geocode error for %s", location)
            return None

    async def fetch_forecast(self, location: str) -> list[WeatherForecast]:
        """Fetch 5-day forecast for a location. Returns daily summaries."""
        coords = await self.geocode(location)
        if coords is None:
            return []

        lat, lon = coords
        session = await self._get_session()
        params = {
            "lat": lat,
            "lon": lon,
            "appid": self._api_key,
            "units": "imperial",  # Fahrenheit
        }

        try:
            async with session.get(OWM_FORECAST_URL, params=params) as resp:
                if resp.status != 200:
                    logger.warning("Forecast failed for %s: HTTP %d", location, resp.status)
                    return []
                data = await resp.json()
        except Exception:
            logger.exception("Forecast fetch error for %s", location)
            return []

        return self._parse_forecast(data, location, lat, lon)

    def _parse_forecast(
        self,
        data: dict[str, Any],
        location: str,
        lat: float,
        lon: float,
    ) -> list[WeatherForecast]:
        """Parse OWM 3-hour forecast into daily summaries."""
        daily: dict[str, dict[str, Any]] = {}

        for item in data.get("list", []):
            dt_txt = item.get("dt_txt", "")
            date = dt_txt[:10]  # "2026-04-10"
            if not date:
                continue

            main = item.get("main", {})
            temp = main.get("temp", 0)
            rain_3h = item.get("rain", {}).get("3h", 0)
            snow_3h = item.get("snow", {}).get("3h", 0)
            wind = item.get("wind", {}).get("speed", 0)
            pop = item.get("pop", 0)  # Probability of precipitation
            desc = ""
            weather_list = item.get("weather", [])
            if weather_list:
                desc = weather_list[0].get("description", "")

            if date not in daily:
                daily[date] = {
                    "temps": [],
                    "rain": 0.0,
                    "snow": 0.0,
                    "wind_max": 0.0,
                    "pop_max": 0.0,
                    "desc": desc,
                }

            d = daily[date]
            d["temps"].append(temp)
            d["rain"] += rain_3h / 25.4  # mm to inches
            d["snow"] += snow_3h / 25.4
            d["wind_max"] = max(d["wind_max"], wind)
            d["pop_max"] = max(d["pop_max"], pop)

        now = datetime.now(UTC)
        forecasts = []
        for date, d in sorted(daily.items()):
            temps = d["temps"]
            if not temps:
                continue
            forecasts.append(WeatherForecast(
                location=location,
                lat=lat,
                lon=lon,
                date=date,
                temp_high_f=max(temps),
                temp_low_f=min(temps),
                precip_prob=d["pop_max"],
                precip_inches=d["rain"],
                snow_inches=d["snow"],
                wind_mph=d["wind_max"],
                description=d["desc"],
                fetched_at=now,
            ))

        logger.info(
            "Fetched %d-day forecast for %s (high=%.0f°F)",
            len(forecasts),
            location,
            forecasts[0].temp_high_f if forecasts else 0,
        )
        return forecasts

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None
