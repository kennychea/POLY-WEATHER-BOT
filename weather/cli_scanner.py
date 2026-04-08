"""Standalone CLI weather arbitrage scanner — async version of scan_edge.py.

Scans Polymarket for weather markets, fetches Open-Meteo ensemble forecasts,
and identifies arbitrage opportunities. Reuses all bot modules.

Usage:
    python -m weather.cli_scanner                     # Full scan, all US cities
    python -m weather.cli_scanner --city "New York"   # Single city
    python -m weather.cli_scanner --min-edge 0.10     # Only edges > 10%
    python -m weather.cli_scanner --json              # Machine-readable output
    python -m weather.cli_scanner -v                  # Verbose (ensemble details)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

import aiohttp

from weather.ensemble import US_CITIES, fetch_ensemble_result
from weather.market_scanner import (
    get_market_price,
    parse_weather_question,
)
from weather.probability import ensemble_probability

logger = logging.getLogger(__name__)

POLYMARKET_GAMMA_URL = "https://gamma-api.polymarket.com/markets"
DEFAULT_MIN_EDGE = 0.05


# ---------------------------------------------------------------------------
# Polymarket scanner (brute-force, same as scan_edge.py)
# ---------------------------------------------------------------------------


def scan_polymarket_weather() -> list[dict]:
    """Brute-force scan all active Polymarket markets, filter for weather.

    Paginates through Gamma API and filters client-side for temperature
    markets by slug or question text.
    """
    weather_markets: list[dict] = []
    seen_ids: set[str] = set()

    for offset in range(0, 3000, 100):
        url = (
            f"{POLYMARKET_GAMMA_URL}?limit=100&offset={offset}"
            f"&active=true&closed=false&order=volume&ascending=false"
        )
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "WeatherArbitrageBot/1.0"},
            )
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

            if "highest-temperature" in slug or "lowest-temperature" in slug:
                weather_markets.append(m)
            elif "highest temperature" in q or "lowest temperature" in q:
                weather_markets.append(m)

    return weather_markets


# ---------------------------------------------------------------------------
# Edge analysis pipeline
# ---------------------------------------------------------------------------


async def analyze_edges(
    markets: list[dict],
    min_edge: float = DEFAULT_MIN_EDGE,
    target_city: str | None = None,
    verbose: bool = False,
) -> list[dict]:
    """Full async pipeline: parse → ensemble → probability → edge."""
    opportunities: list[dict] = []
    parsed_count = 0

    async with aiohttp.ClientSession() as session:
        for market in markets:
            question = market.get("question", "")
            parsed = parse_weather_question(market, question)
            if parsed is None:
                continue

            city = parsed.location
            if target_city and city.lower() != target_city.lower():
                continue

            if city not in US_CITIES:
                continue

            parsed_count += 1

            # Get market price
            prices = get_market_price(market)
            if prices is None:
                continue
            yes_price, no_price = prices

            if yes_price < 0.005 or yes_price > 0.995:
                continue

            # Fetch GFS-only ensemble (31 members)
            er = await fetch_ensemble_result(
                city=city,
                target_date=parsed.target_date,
                metric=parsed.metric,
                unit=parsed.unit,
                session=session,
            )
            if er is None:
                continue

            members = list(er.members)
            if not members:
                continue

            # Calculate probability from GFS ensemble
            model_prob, confidence = ensemble_probability(
                members,
                threshold_low=parsed.threshold_low,
                threshold_high=parsed.threshold_high,
                direction=parsed.direction,
            )

            # Edge = model says YES is worth X, market charges Y
            edge = model_prob - yes_price
            abs_edge = abs(edge)

            if abs_edge < min_edge:
                continue

            signal = "BUY_YES" if edge > 0 else "BUY_NO"

            opp: dict = {
                "question": question,
                "city": city,
                "metric": parsed.metric,
                "threshold_low": parsed.threshold_low,
                "threshold_high": parsed.threshold_high,
                "direction": parsed.direction,
                "target_date": parsed.target_date,
                "unit": parsed.unit,
                "model_prob": round(model_prob, 4),
                "market_yes": round(yes_price, 4),
                "market_no": round(no_price, 4),
                "edge": round(edge, 4),
                "abs_edge": round(abs_edge, 4),
                "signal": signal,
                "confidence": confidence,
                "ensemble_size": len(members),
                "models_used": 1,
                "condition_id": market.get("conditionId", ""),
                "slug": market.get("slug", ""),
            }

            if verbose:
                valid = [m for m in members if m is not None]
                if valid:
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
# Display (matches scan_edge.py format)
# ---------------------------------------------------------------------------


def print_results(opps: list[dict], verbose: bool = False) -> None:
    """Print formatted results matching scan_edge.py's display."""
    if not opps:
        print("\nNo opportunities found above the minimum edge threshold.")
        print("Try --min-edge 0.03 or check if there are active weather markets.")
        return

    print(f"\n{'=' * 90}")
    print(f"  WEATHER ARBITRAGE SCANNER — {len(opps)} opportunities")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'=' * 90}\n")

    for i, o in enumerate(opps, 1):
        edge_pct = o["edge"] * 100
        model_pct = o["model_prob"] * 100
        market_pct = o["market_yes"] * 100
        unit_sym = "\u00b0F" if o["unit"] == "fahrenheit" else "\u00b0C"

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
        print(f"     Confidence: {o['confidence'].upper()} | Members: {o['ensemble_size']} ({o.get('models_used', 1)} models)")

        if verbose and "ensemble_stats" in o:
            s = o["ensemble_stats"]
            print(f"     Ensemble: mean={s['mean']}  range=[{s['min']}, {s['max']}]")

        if o.get("slug"):
            print(f"     https://polymarket.com/event/{o['slug']}")
        print()

    high = [o for o in opps if o["confidence"] == "high" and o["abs_edge"] > 0.10]
    if high:
        print(f"  ** {len(high)} HIGH-CONFIDENCE edges (>10%, strong consensus) **\n")
    print(f"{'=' * 90}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Weather Arbitrage Scanner — Polymarket vs Open-Meteo Ensemble",
    )
    parser.add_argument("--city", help="Filter to a specific city")
    parser.add_argument("--min-edge", type=float, default=DEFAULT_MIN_EDGE)
    parser.add_argument("--json", action="store_true", help="Machine-readable JSON output")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show ensemble stats")
    args = parser.parse_args()

    print("Scanning Polymarket for weather markets...")
    markets = scan_polymarket_weather()
    print(f"  Found {len(markets)} weather markets")

    if not markets:
        print("\nNo weather markets on Polymarket right now.")
        return

    print("Fetching Open-Meteo ensemble forecasts...")
    opps = asyncio.run(
        analyze_edges(markets, args.min_edge, args.city, args.verbose),
    )

    if args.json:
        print(json.dumps(opps, indent=2))
    else:
        print_results(opps, args.verbose)


if __name__ == "__main__":
    main()
