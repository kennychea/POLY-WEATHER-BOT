"""Backfill weather_signals.actual_outcome — Phase 2.1-quater.

Standalone, idempotent, decoupled from the trading hot path.

Sources, in priority order:
  1. Polymarket Gamma API — slug-based event lookup via MarketIndexer.check_resolution.
     Maps MarketResolution.resolution_price → outcome:
        1.0 → 1 (YES)        source='polymarket'
        0.0 → 0 (NO)         source='polymarket'
        0.5 → None           source='failed'   (untradeable / split)
        other fractional     source='failed'   (warn — Gamma quirk)
        None (no resolution) → fall through to METAR fallback
  2. METAR — observed daily extremes from IEM ASOS, compared against the
     parsed market_question. Cities not in resolution_stations.POLYMARKET_STATIONS
     resolve as 'failed' without an API call.

Dedup key: market_question (1:1 with market_id in production data).

Usage:
    python scripts/backfill_weather_signals_outcomes.py --all
    python scripts/backfill_weather_signals_outcomes.py --db data/bot.db --all
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

import aiosqlite

# Make project root importable when run as a script
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from infra.types import MarketResolution
from market_data.indexer import MarketIndexer
from weather.market_question_parser import (
    ParsedQuestion,
    check_outcome,
    parse_market_question,
)
from weather.metar_fetcher import MetarFetcher
from weather.resolution_stations import get_station

logger = logging.getLogger("backfill_outcomes")


# ── source 1: Polymarket Gamma ────────────────────────────────────────


async def resolve_via_polymarket(
    market_id: str,
    market_question: str,
    indexer: Any,
) -> tuple[int | None, str]:
    """Try to resolve via Polymarket Gamma (slug-based event lookup).

    Returns (outcome, source):
        (1 or 0, "polymarket")  — clean YES/NO resolution
        (None,   "failed")      — split (0.5) or fractional (warn, Gamma quirk)
        (None,   "")            — no resolution found (caller should try METAR)
    """
    res: MarketResolution | None = await indexer.check_resolution(market_id, market_question)
    if res is None or not res.closed:
        return (None, "")
    p = res.resolution_price
    if p == 1.0:
        return (1, "polymarket")
    if p == 0.0:
        return (0, "polymarket")
    if p == 0.5:
        logger.warning("polymarket split (0.5) — untradeable: %s", market_question[:60])
        return (None, "failed")
    logger.warning(
        "polymarket fractional %.4f (Gamma quirk) — failing: %s", p, market_question[:60]
    )
    return (None, "failed")


# ── source 2: METAR fallback ──────────────────────────────────────────


async def resolve_via_metar(
    parsed: ParsedQuestion,
    target_date: date,
    metar_fetcher: Any,
) -> tuple[int | None, str]:
    """Fall back to METAR observed extremes. Skips unmapped cities."""
    station = get_station(parsed.city)
    if station is None:
        return (None, "failed")
    icao, _ = station
    iso = target_date.isoformat()
    obs_list = await metar_fetcher.fetch_daily_extremes(icao, iso, iso)
    if not obs_list:
        return (None, "failed")
    obs = obs_list[0]
    high = obs.get("max_tempF")
    low = obs.get("min_tempF")
    if high is None or low is None:
        return (None, "failed")
    return (check_outcome(parsed, observed_high_f=float(high), observed_low_f=float(low)), "metar")


# ── orchestration ─────────────────────────────────────────────────────


async def resolve_outcome(
    *,
    market_id: str,
    market_question: str,
    forecast_date: str,
    indexer: Any,
    metar_fetcher: Any,
) -> tuple[int | None, str]:
    """Polymarket priority, then METAR fallback. 'failed' from Polymarket short-circuits."""
    out, src = await resolve_via_polymarket(market_id, market_question, indexer)
    if src in ("polymarket", "failed"):
        return (out, src)

    parsed = parse_market_question(market_question)
    if parsed is None:
        return (None, "failed")
    try:
        target = date.fromisoformat(forecast_date)
    except (ValueError, TypeError):
        return (None, "failed")
    return await resolve_via_metar(parsed, target, metar_fetcher)


# ── DB helpers ────────────────────────────────────────────────────────


async def _fetch_unresolved_past_due(
    db_path: str,
) -> list[tuple[str, str, str]]:
    """Distinct (market_id, market_question, forecast_date) for past-due unresolved markets.

    Uses market_id as the dedup key. In prod, market_id is 1:1 with market_question,
    but using market_id avoids any cross-contamination if a question is ever shared
    across distinct conditionIds.
    """
    async with aiosqlite.connect(db_path) as conn:
        cur = await conn.execute(
            """
            SELECT market_id, market_question, forecast_date
            FROM weather_signals
            WHERE actual_outcome IS NULL
              AND forecast_date IS NOT NULL
              AND forecast_date < date('now')
            GROUP BY market_id
            ORDER BY forecast_date DESC
            """
        )
        rows = await cur.fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


async def _update_outcome(
    db_path: str,
    market_id: str,
    actual_outcome: int | None,
    outcome_source: str,
) -> int:
    """UPDATE all rows of a market — only those still NULL (idempotent). Returns rows affected."""
    async with aiosqlite.connect(db_path) as conn:
        cur = await conn.execute(
            """
            UPDATE weather_signals
            SET actual_outcome = ?, outcome_source = ?
            WHERE market_id = ? AND actual_outcome IS NULL
            """,
            (
                None if actual_outcome is None else float(actual_outcome),
                outcome_source,
                market_id,
            ),
        )
        await conn.commit()
        return cur.rowcount or 0


# ── driver ────────────────────────────────────────────────────────────


async def backfill_all(
    db_path: str,
    indexer: Any,
    metar_fetcher: Any,
) -> dict[str, Any]:
    """Resolve every distinct past-due unresolved market in weather_signals.

    Returns a stats dict:
        {
            "n_markets":        int,
            "n_rows_updated":   int,
            "elapsed_seconds":  float,
            "by_source":        {"polymarket": int, "metar": int, "failed": int},
            "by_city":          {city: {"polymarket": int, "metar": int, "failed": int, "n_markets": int}},
            "outcome_distribution": {"yes": int, "no": int, "failed": int},
            "gamma_mismatches": [(city, forecast_date, market_question), ...],
        }
    """
    t0 = time.monotonic()
    rows = await _fetch_unresolved_past_due(db_path)

    by_source = {"polymarket": 0, "metar": 0, "failed": 0}
    by_city: dict[str, dict[str, int]] = defaultdict(
        lambda: {"polymarket": 0, "metar": 0, "failed": 0, "n_markets": 0}
    )
    outcome_dist = {"yes": 0, "no": 0, "failed": 0}
    n_rows_updated = 0
    gamma_mismatches: list[tuple[str, str, str]] = []

    for market_id, market_question, forecast_date in rows:
        outcome, source = await resolve_outcome(
            market_id=market_id,
            market_question=market_question,
            forecast_date=forecast_date,
            indexer=indexer,
            metar_fetcher=metar_fetcher,
        )

        parsed = parse_market_question(market_question)
        city = parsed.city if parsed else "?"

        # Bucket
        bucket = source if source in by_source else "failed"
        by_source[bucket] += 1
        by_city[city]["n_markets"] += 1
        by_city[city][bucket] += 1
        if outcome == 1:
            outcome_dist["yes"] += 1
        elif outcome == 0:
            outcome_dist["no"] += 1
        else:
            outcome_dist["failed"] += 1

        # Track Gamma mismatches: when Polymarket failed but METAR (or any) source resolved cleanly
        if source == "metar":
            gamma_mismatches.append((city, forecast_date, market_question))

        n_rows_updated += await _update_outcome(db_path, market_id, outcome, bucket)

    elapsed = time.monotonic() - t0
    return {
        "n_markets": len(rows),
        "n_rows_updated": n_rows_updated,
        "elapsed_seconds": elapsed,
        "by_source": by_source,
        "by_city": dict(by_city),
        "outcome_distribution": outcome_dist,
        "gamma_mismatches": gamma_mismatches,
    }


# ── CLI entry point ───────────────────────────────────────────────────


async def _cli(db_path: str) -> int:
    """One-shot CLI runner. Bootstraps a real MarketIndexer (no refresh) + MetarFetcher."""
    indexer = MarketIndexer()
    metar = MetarFetcher()
    try:
        stats = await backfill_all(db_path, indexer, metar)
    finally:
        await indexer.close()
        await metar.close()

    src = stats["by_source"]
    total = max(1, sum(src.values()))
    logger.info(
        "Backfill done — %d markets / %d rows updated in %.1fs",
        stats["n_markets"], stats["n_rows_updated"], stats["elapsed_seconds"],
    )
    logger.info(
        "  source: polymarket=%d (%.1f%%) metar=%d (%.1f%%) failed=%d (%.1f%%)",
        src["polymarket"], 100 * src["polymarket"] / total,
        src["metar"], 100 * src["metar"] / total,
        src["failed"], 100 * src["failed"] / total,
    )
    od = stats["outcome_distribution"]
    logger.info("  outcomes: YES=%d NO=%d failed=%d", od["yes"], od["no"], od["failed"])
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill actual_outcome on weather_signals.")
    ap.add_argument("--db", default="data/bot.db", help="SQLite path (default: data/bot.db)")
    ap.add_argument("--all", action="store_true", help="Resolve every past-due unresolved market.")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    if not args.all:
        ap.error("v1 supports --all only.")
    return asyncio.run(_cli(args.db))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
