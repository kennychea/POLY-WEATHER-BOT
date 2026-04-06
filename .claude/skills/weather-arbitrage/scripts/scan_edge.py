#!/usr/bin/env python3
"""Weather Arbitrage Scanner — Open-Meteo Ensemble vs Polymarket.

Fetches ensemble forecasts from Open-Meteo (free, no key),
compares probability distributions against Polymarket prices,
and identifies arbitrage opportunities.

Real Polymarket weather market formats:
  - Bucket: "Will the highest temperature in NYC be between 40-41°F on April 8?"
  - Exact:  "Will the highest temperature in Madrid be 27°C on April 5?"
  - Threshold: "Will the highest temperature in London be 15°C or below on April 9?"

Usage:
    python scan_edge.py                     # Full scan, all US cities
    python scan_edge.py --city "New York"   # Single city
    python scan_edge.py --min-edge 0.10     # Only edges > 10%
    python scan_edge.py --json              # Machine-readable output
    python scan_edge.py -v                  # Verbose (ensemble details)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

OPEN_METEO_ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
POLYMARKET_GAMMA_URL = "https://gamma-api.polymarket.com/markets"

DEFAULT_MIN_EDGE = 0.05
CACHE_TTL = 1800  # 30 minutes

# US cities — hardcoded coords for speed (no geocoding API call)
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
}

# Aliases to match Polymarket question text → city key
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
}

# Months for date parsing
_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}

# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

_CACHE_DIR = Path(__file__).parent / ".cache"


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
# Open-Meteo Ensemble Fetcher
# ---------------------------------------------------------------------------


def fetch_ensemble(
    city: str,
    lat: float,
    lon: float,
    target_date: str,
    unit: str = "fahrenheit",
) -> dict | None:
    """Fetch ensemble forecast from Open-Meteo (hourly, all members).

    Uses hourly endpoint to get individual ensemble member data,
    then computes daily max/min per member. GFS has 31 members
    (1 control + 30 perturbations).

    Returns dict with:
        temperature_max: list of 31 daily max values (one per member)
        temperature_min: list of 31 daily min values (one per member)
    """
    cache_key = f"ensemble_v2_{city.replace(' ', '_')}_{target_date}_{unit}"
    cached = _cache_get(cache_key)
    if cached:
        return {k: v for k, v in cached.items() if k != "_ts"}

    params = (
        f"?latitude={lat}&longitude={lon}"
        f"&hourly=temperature_2m"
        f"&temperature_unit={unit}"
        f"&start_date={target_date}&end_date={target_date}"
        f"&models=gfs_seamless"
    )

    try:
        req = urllib.request.Request(
            OPEN_METEO_ENSEMBLE_URL + params,
            headers={"User-Agent": "WeatherArbitrageBot/1.0"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        print(f"  ERROR fetching ensemble for {city}: {e}", file=sys.stderr)
        return None

    hourly = data.get("hourly", {})

    # Collect all member keys: temperature_2m (control), temperature_2m_member01..30
    member_keys = ["temperature_2m"]  # control run
    for i in range(1, 31):
        key = f"temperature_2m_member{i:02d}"
        if key in hourly:
            member_keys.append(key)

    # Compute daily max and min per member (each key has 24 hourly values)
    temp_maxes = []
    temp_mins = []
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

    _cache_set(cache_key, result)
    return result


# ---------------------------------------------------------------------------
# Probability Calculator
# ---------------------------------------------------------------------------


def ensemble_probability(
    members: list[float],
    threshold_low: float,
    threshold_high: float | None,
    direction: str,
) -> tuple[float, str]:
    """Calculate probability from ensemble members by counting.

    Handles 3 market types:
      - bucket (between X-Y): count members in [low, high)
      - exact (be X):         count members in [X-0.5, X+0.5)
      - threshold (X or above/below): count members >= or <= X

    Returns (probability, confidence).
    """
    valid = [m for m in members if m is not None]
    n = len(valid)
    if n == 0:
        return 0.0, "none"

    if threshold_high is not None:
        # Bucket or exact: count members in range [low, high]
        count = sum(1 for m in valid if threshold_low <= m <= threshold_high)
    elif direction == "above":
        count = sum(1 for m in valid if m >= threshold_low)
    else:  # below
        count = sum(1 for m in valid if m <= threshold_low)

    prob = count / n

    spread = abs(prob - 0.5)
    if spread > 0.3:
        confidence = "high"
    elif spread > 0.15:
        confidence = "medium"
    else:
        confidence = "low"

    return prob, confidence


# ---------------------------------------------------------------------------
# Polymarket Scanner
# ---------------------------------------------------------------------------


def scan_polymarket_weather() -> list[dict]:
    """Brute-force scan all active Polymarket markets, filter for weather locally.

    The Gamma API text search is unreliable, so we paginate through all
    active markets and filter client-side for 'temperature' keywords.
    """
    cached = _cache_get("polymarket_weather_v2")
    if cached and "markets" in cached:
        return cached["markets"]

    weather_markets = []
    seen_ids = set()

    for offset in range(0, 3000, 100):
        url = (
            f"{POLYMARKET_GAMMA_URL}?limit=100&offset={offset}"
            f"&active=true&closed=false&order=volume&ascending=false"
        )
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "WeatherArbitrageBot/1.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                markets = json.loads(resp.read().decode())
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            break

        if not markets:
            break

        for m in markets:
            q = (m.get("question") or "").lower()
            slug = (m.get("slug") or "").lower()
            cid = m.get("conditionId", "")

            if cid in seen_ids:
                continue
            seen_ids.add(cid)

            # Match weather markets by slug pattern or question keywords
            if "highest-temperature" in slug or "lowest-temperature" in slug:
                weather_markets.append(m)
            elif "highest temperature" in q or "lowest temperature" in q:
                weather_markets.append(m)

    _cache_set("polymarket_weather_v2", {"markets": weather_markets})
    return weather_markets


def parse_weather_question(question: str) -> dict | None:
    """Parse real Polymarket weather question into structured params.

    Handles:
      "...be between 40-41°F on April 8?"     → bucket
      "...be 27°C on April 5?"                → exact
      "...be 15°C or below on April 9?"       → threshold
      "...be 74°F or higher on April 5?"      → threshold
    """
    q = question
    q_lower = q.lower()

    if "temperature" not in q_lower:
        return None

    # --- City ---
    city = None
    for city_name, aliases in CITY_ALIASES.items():
        for alias in aliases:
            if alias in q_lower:
                city = city_name
                break
        if city:
            break
    if city is None:
        return None

    # --- Metric (highest / lowest) ---
    metric = "temp_high"
    if "lowest" in q_lower:
        metric = "temp_low"

    # --- Temperature unit ---
    is_fahrenheit = "°f" in q_lower or "f?" in q_lower.replace(" ", "") or "°f" in q.lower()
    # Also check slug
    if not is_fahrenheit:
        is_fahrenheit = q.endswith("F") or "°F" in q

    unit = "fahrenheit" if is_fahrenheit else "celsius"

    # --- Parse market type + thresholds ---
    # Type 1: Bucket "between X-Y°F"
    bucket_match = re.search(r"between\s+(\d+)-(\d+)\s*°?\s*[°fFcC]", q, re.IGNORECASE)
    if bucket_match:
        threshold_low = float(bucket_match.group(1))
        threshold_high = float(bucket_match.group(2))
        direction = "bucket"
    else:
        threshold_high = None
        # Type 2: Threshold "X°F or below/above/higher"
        thresh_match = re.search(
            r"(\d+)\s*°?\s*[°fFcC]\s+or\s+(below|above|higher|lower|more|less)",
            q, re.IGNORECASE,
        )
        if thresh_match:
            threshold_low = float(thresh_match.group(1))
            word = thresh_match.group(2).lower()
            direction = "below" if word in ("below", "lower", "less") else "above"
        else:
            # Type 3: Exact "be X°C"
            exact_match = re.search(r"be\s+(\d+)\s*°?\s*[°fFcC]", q, re.IGNORECASE)
            if exact_match:
                val = float(exact_match.group(1))
                # Exact = bucket of [val-0.5, val+0.5]
                threshold_low = val - 0.5
                threshold_high = val + 0.5
                direction = "bucket"
            else:
                return None

    # --- Date ---
    target_date = None
    for month_name, month_num in _MONTHS.items():
        if month_name in q_lower:
            day_match = re.search(rf"{month_name}\s+(\d{{1,2}})", q_lower)
            if day_match:
                day = int(day_match.group(1))
                year = datetime.now(timezone.utc).year
                target_date = f"{year}-{month_num:02d}-{day:02d}"
            break

    if target_date is None:
        date_match = re.search(r"(\d{4}-\d{2}-\d{2})", q)
        if date_match:
            target_date = date_match.group(1)

    if target_date is None:
        tomorrow = datetime.now(timezone.utc) + timedelta(days=1)
        target_date = tomorrow.strftime("%Y-%m-%d")

    return {
        "city": city,
        "metric": metric,
        "threshold_low": threshold_low,
        "threshold_high": threshold_high,
        "direction": direction,
        "target_date": target_date,
        "unit": unit,
    }


def get_market_price(market: dict) -> tuple[float, float] | None:
    """Extract YES/NO prices from market dict."""
    prices_raw = market.get("outcomePrices")
    if not prices_raw:
        return None
    try:
        prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
        if len(prices) >= 2:
            return float(prices[0]), float(prices[1])
    except (json.JSONDecodeError, ValueError, IndexError):
        pass
    return None


# ---------------------------------------------------------------------------
# Edge Analyzer
# ---------------------------------------------------------------------------


def analyze_edges(
    markets: list[dict],
    min_edge: float = DEFAULT_MIN_EDGE,
    target_city: str | None = None,
    verbose: bool = False,
) -> list[dict]:
    """Full pipeline: parse market → fetch ensemble → compute edge."""
    opportunities = []
    ensemble_cache: dict[str, dict | None] = {}

    parsed_count = 0
    for market in markets:
        question = market.get("question", "")
        params = parse_weather_question(question)
        if params is None:
            continue

        city = params["city"]
        if target_city and city.lower() != target_city.lower():
            continue

        # Only US cities (we have hardcoded coords)
        if city not in US_CITIES:
            continue

        parsed_count += 1

        prices = get_market_price(market)
        if prices is None:
            continue
        yes_price, no_price = prices

        if yes_price < 0.005 or yes_price > 0.995:
            continue

        # Fetch ensemble (cached per city+date+unit)
        cache_key = f"{city}_{params['target_date']}_{params['unit']}"
        if cache_key not in ensemble_cache:
            lat, lon = US_CITIES[city]
            ensemble_cache[cache_key] = fetch_ensemble(
                city, lat, lon, params["target_date"], params["unit"],
            )
        ensemble = ensemble_cache[cache_key]
        if ensemble is None:
            continue

        # Pick members
        if params["metric"] == "temp_high":
            members = ensemble.get("temperature_max", [])
        else:
            members = ensemble.get("temperature_min", [])

        if not members:
            continue

        # Calculate model probability
        model_prob, confidence = ensemble_probability(
            members,
            params["threshold_low"],
            params["threshold_high"],
            params["direction"],
        )

        # Edge = what model says YES is worth - what market charges
        edge = model_prob - yes_price
        abs_edge = abs(edge)

        if abs_edge < min_edge:
            continue

        signal = "BUY_YES" if edge > 0 else "BUY_NO"

        opp = {
            "question": question,
            "city": city,
            "metric": params["metric"],
            "threshold_low": params["threshold_low"],
            "threshold_high": params["threshold_high"],
            "direction": params["direction"],
            "target_date": params["target_date"],
            "unit": params["unit"],
            "model_prob": round(model_prob, 4),
            "market_yes": round(yes_price, 4),
            "market_no": round(no_price, 4),
            "edge": round(edge, 4),
            "abs_edge": round(abs_edge, 4),
            "signal": signal,
            "confidence": confidence,
            "ensemble_size": len([m for m in members if m is not None]),
            "condition_id": market.get("conditionId", ""),
            "slug": market.get("slug", ""),
        }

        if verbose:
            valid = [m for m in members if m is not None]
            opp["ensemble_stats"] = {
                "mean": round(sum(valid) / len(valid), 1),
                "min": round(min(valid), 1),
                "max": round(max(valid), 1),
            }

        opportunities.append(opp)

    print(f"  Parsed {parsed_count} US weather markets from {len(markets)} total")
    opportunities.sort(key=lambda x: x["abs_edge"], reverse=True)
    return opportunities


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------


def print_results(opps: list[dict], verbose: bool = False) -> None:
    if not opps:
        print("\nNo opportunities found above the minimum edge threshold.")
        print("Try --min-edge 0.03 or check if there are active weather markets.")
        return

    print(f"\n{'='*90}")
    print(f"  WEATHER ARBITRAGE SCANNER — {len(opps)} opportunities")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*90}\n")

    for i, o in enumerate(opps, 1):
        edge_pct = o["edge"] * 100
        model_pct = o["model_prob"] * 100
        market_pct = o["market_yes"] * 100
        unit_sym = "°F" if o["unit"] == "fahrenheit" else "°C"

        # Threshold display
        if o["threshold_high"] is not None:
            thresh_str = f"{o['threshold_low']:.0f}-{o['threshold_high']:.0f}{unit_sym}"
        elif o["direction"] == "above":
            thresh_str = f">={o['threshold_low']:.0f}{unit_sym}"
        else:
            thresh_str = f"<={o['threshold_low']:.0f}{unit_sym}"

        indicator = ">>>" if o["abs_edge"] > 0.15 else (">>" if o["abs_edge"] > 0.10 else ">")

        print(f"  {indicator} #{i} {o['city']} | {o['target_date']} | {thresh_str}")
        print(f"     Model: {model_pct:.1f}%  Market: {market_pct:.1f}%  Edge: {edge_pct:+.1f}%  [{o['signal']}]")
        print(f"     Confidence: {o['confidence'].upper()} | Members: {o['ensemble_size']}")

        if verbose and "ensemble_stats" in o:
            s = o["ensemble_stats"]
            print(f"     Ensemble: mean={s['mean']}  range=[{s['min']}, {s['max']}]")

        if o.get("slug"):
            print(f"     https://polymarket.com/event/{o['slug']}")
        print()

    high = [o for o in opps if o["confidence"] == "high" and o["abs_edge"] > 0.10]
    if high:
        print(f"  ** {len(high)} HIGH-CONFIDENCE edges (>10%, strong consensus) **\n")
    print(f"{'='*90}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Weather Arbitrage Scanner")
    parser.add_argument("--city", help="Filter to a specific city")
    parser.add_argument("--min-edge", type=float, default=DEFAULT_MIN_EDGE)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    print("Scanning Polymarket for weather markets...")
    markets = scan_polymarket_weather()
    print(f"  Found {len(markets)} weather markets")

    if not markets:
        print("\nNo weather markets on Polymarket right now.")
        return

    print("Fetching Open-Meteo ensemble forecasts...")
    opps = analyze_edges(markets, args.min_edge, args.city, args.verbose)

    if args.json:
        print(json.dumps(opps, indent=2))
    else:
        print_results(opps, args.verbose)


if __name__ == "__main__":
    main()
