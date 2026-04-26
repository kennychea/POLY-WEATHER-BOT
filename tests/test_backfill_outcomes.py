"""Tests for scripts/backfill_weather_signals_outcomes.py.

Phase 2.1-quater. Standalone backfill script — Polymarket Gamma source priority,
METAR fallback, idempotent, DISTINCT-keyed efficiency, no hot-path coupling.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

# Make scripts/ importable as a package
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from infra.types import MarketResolution
from scripts import backfill_weather_signals_outcomes as bf  # type: ignore[import-not-found]


# ── fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
async def synthetic_db(tmp_path: Path) -> str:
    """Build a tiny weather_signals DB matching prod schema, with controlled rows."""
    db_path = tmp_path / "test.db"
    conn = await aiosqlite.connect(str(db_path))
    try:
        await conn.execute(
            """
            CREATE TABLE weather_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_question TEXT NOT NULL,
                market_id TEXT NOT NULL,
                token_id TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                forecast_probability REAL NOT NULL,
                market_price REAL NOT NULL,
                edge REAL NOT NULL,
                location TEXT NOT NULL,
                forecast_date TEXT NOT NULL,
                weather_metric TEXT NOT NULL,
                threshold_value REAL NOT NULL,
                timestamp TEXT NOT NULL,
                ensemble_member_count INTEGER NOT NULL DEFAULT 0,
                confidence TEXT NOT NULL DEFAULT 'medium',
                net_edge REAL NOT NULL DEFAULT 0.0,
                actual_outcome REAL,
                outcome_source TEXT
            )
            """
        )
        await conn.commit()
    finally:
        await conn.close()
    return str(db_path)


def _insert_row(
    conn: aiosqlite.Connection,
    *,
    market_id: str,
    question: str,
    forecast_date: str,
    location: str = "Austin",
    timestamp: str = "2026-04-20T12:00:00Z",
    actual_outcome: float | None = None,
):
    """Build a SQL INSERT with sensible defaults."""
    return conn.execute(
        """INSERT INTO weather_signals
           (market_question, market_id, token_id, signal_type,
            forecast_probability, market_price, edge,
            location, forecast_date, weather_metric, threshold_value,
            timestamp, actual_outcome)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            question, market_id, "tok_" + market_id, "buy_no",
            0.05, 0.50, 0.10,
            location, forecast_date, "highest", 85.0,
            timestamp, actual_outcome,
        ),
    )


# ── resolve_via_polymarket: 4 resolution_price cases ──────────────────


async def test_resolve_via_polymarket_yes() -> None:
    indexer = MagicMock()
    indexer.check_resolution = AsyncMock(
        return_value=MarketResolution(condition_id="x", resolution_price=1.0, closed=True, source="gamma_resolved")
    )
    out, src = await bf.resolve_via_polymarket("x", "Will the highest temperature in Austin be 85\u00b0F on April 28?", indexer)
    assert (out, src) == (1, "polymarket")


async def test_resolve_via_polymarket_no() -> None:
    indexer = MagicMock()
    indexer.check_resolution = AsyncMock(
        return_value=MarketResolution(condition_id="x", resolution_price=0.0, closed=True, source="gamma_resolved")
    )
    out, src = await bf.resolve_via_polymarket("x", "q", indexer)
    assert (out, src) == (0, "polymarket")


async def test_resolve_via_polymarket_split_returns_failed() -> None:
    indexer = MagicMock()
    indexer.check_resolution = AsyncMock(
        return_value=MarketResolution(condition_id="x", resolution_price=0.5, closed=True, source="gamma_resolved")
    )
    out, src = await bf.resolve_via_polymarket("x", "q", indexer)
    assert out is None
    assert src == "failed"


async def test_resolve_via_polymarket_fractional_returns_failed(caplog) -> None:
    indexer = MagicMock()
    indexer.check_resolution = AsyncMock(
        return_value=MarketResolution(condition_id="x", resolution_price=0.42, closed=True, source="gamma_resolved")
    )
    import logging
    with caplog.at_level(logging.WARNING):
        out, src = await bf.resolve_via_polymarket("x", "qsample", indexer)
    assert out is None
    assert src == "failed"
    assert any("fractional" in r.message.lower() for r in caplog.records)


async def test_resolve_via_polymarket_no_resolution_returns_empty() -> None:
    """When indexer.check_resolution returns None, source is empty (not 'failed') so caller can fall back to METAR."""
    indexer = MagicMock()
    indexer.check_resolution = AsyncMock(return_value=None)
    out, src = await bf.resolve_via_polymarket("x", "q", indexer)
    assert out is None
    assert src == ""


# ── resolve_via_metar ─────────────────────────────────────────────────


async def test_resolve_via_metar_unmapped_city_fails() -> None:
    """Cities not in resolution_stations.POLYMARKET_STATIONS resolve as 'failed' without an API call."""
    metar = MagicMock()
    metar.fetch_daily_extremes = AsyncMock()
    from weather.market_question_parser import parse_market_question

    parsed = parse_market_question("Will the highest temperature in Chengdu be 18\u00b0C on April 28?")
    assert parsed is not None
    out, src = await bf.resolve_via_metar(parsed, date(2026, 4, 28), metar)
    assert (out, src) == (None, "failed")
    metar.fetch_daily_extremes.assert_not_called()


async def test_resolve_via_metar_mapped_city_above() -> None:
    metar = MagicMock()
    metar.fetch_daily_extremes = AsyncMock(
        return_value=[{"date": "2026-04-28", "max_tempF": 92.0, "min_tempF": 70.0}]
    )
    from weather.market_question_parser import parse_market_question

    parsed = parse_market_question(
        "Will the highest temperature in Miami be 90\u00b0F or higher on April 28?"
    )
    assert parsed is not None
    out, src = await bf.resolve_via_metar(parsed, date(2026, 4, 28), metar)
    assert (out, src) == (1, "metar")
    # ICAO for Miami is KMIA
    metar.fetch_daily_extremes.assert_awaited_once_with("KMIA", "2026-04-28", "2026-04-28")


async def test_resolve_via_metar_no_data_fails() -> None:
    metar = MagicMock()
    metar.fetch_daily_extremes = AsyncMock(return_value=[])
    from weather.market_question_parser import parse_market_question

    parsed = parse_market_question(
        "Will the highest temperature in Miami be 90\u00b0F or higher on April 28?"
    )
    assert parsed is not None
    out, src = await bf.resolve_via_metar(parsed, date(2026, 4, 28), metar)
    assert (out, src) == (None, "failed")


# ── resolve_outcome (orchestration) ───────────────────────────────────


async def test_resolve_outcome_polymarket_priority_skips_metar() -> None:
    indexer = MagicMock()
    indexer.check_resolution = AsyncMock(
        return_value=MarketResolution(condition_id="x", resolution_price=1.0, closed=True, source="gamma_resolved")
    )
    metar = MagicMock()
    metar.fetch_daily_extremes = AsyncMock()
    out, src = await bf.resolve_outcome(
        market_id="x",
        market_question="Will the highest temperature in Miami be 90\u00b0F or higher on April 28?",
        forecast_date="2026-04-28",
        indexer=indexer,
        metar_fetcher=metar,
    )
    assert (out, src) == (1, "polymarket")
    metar.fetch_daily_extremes.assert_not_called()


async def test_resolve_outcome_metar_fallback_when_polymarket_missing() -> None:
    """When Polymarket returns None (no resolution found), fall back to METAR."""
    indexer = MagicMock()
    indexer.check_resolution = AsyncMock(return_value=None)
    metar = MagicMock()
    metar.fetch_daily_extremes = AsyncMock(
        return_value=[{"date": "2026-04-28", "max_tempF": 95.0, "min_tempF": 70.0}]
    )
    out, src = await bf.resolve_outcome(
        market_id="x",
        market_question="Will the highest temperature in Miami be 90\u00b0F or higher on April 28?",
        forecast_date="2026-04-28",
        indexer=indexer,
        metar_fetcher=metar,
    )
    assert (out, src) == (1, "metar")


async def test_resolve_outcome_polymarket_failed_does_not_try_metar() -> None:
    """0.5 split is a Polymarket-side 'untradeable' verdict — don't fall back to METAR."""
    indexer = MagicMock()
    indexer.check_resolution = AsyncMock(
        return_value=MarketResolution(condition_id="x", resolution_price=0.5, closed=True, source="gamma_resolved")
    )
    metar = MagicMock()
    metar.fetch_daily_extremes = AsyncMock()
    out, src = await bf.resolve_outcome(
        market_id="x",
        market_question="Will the highest temperature in Miami be 90\u00b0F or higher on April 28?",
        forecast_date="2026-04-28",
        indexer=indexer,
        metar_fetcher=metar,
    )
    assert out is None
    assert src == "failed"
    metar.fetch_daily_extremes.assert_not_called()


# ── full backfill driver ──────────────────────────────────────────────


@pytest.fixture
async def populated_db(synthetic_db: str) -> str:
    """100 rows: 60 past-due across 3 markets (20 each), 40 future across 2."""
    yesterday = (date.today() - timedelta(days=2)).isoformat()
    tomorrow = (date.today() + timedelta(days=2)).isoformat()
    conn = await aiosqlite.connect(synthetic_db)
    try:
        # Past-due Polymarket-resolvable
        for _ in range(20):
            await _insert_row(
                conn, market_id="m1",
                question="Will the highest temperature in Miami be 90\u00b0F or higher on April 28?",
                forecast_date=yesterday, location="Miami",
            )
        # Past-due METAR-resolvable (mapped city)
        for _ in range(20):
            await _insert_row(
                conn, market_id="m2",
                question="Will the highest temperature in Austin be between 88-89\u00b0F on April 28?",
                forecast_date=yesterday, location="Austin",
            )
        # Past-due unmapped city → expect 'failed'
        for _ in range(20):
            await _insert_row(
                conn, market_id="m3",
                question="Will the highest temperature in Chengdu be 18\u00b0C on April 28?",
                forecast_date=yesterday, location="Chengdu",
            )
        # Future — should NOT be touched
        for _ in range(20):
            await _insert_row(
                conn, market_id="m4",
                question="Will the highest temperature in Miami be 90\u00b0F or higher on April 28?",
                forecast_date=tomorrow, location="Miami",
            )
        for _ in range(20):
            await _insert_row(
                conn, market_id="m5",
                question="Will the highest temperature in Austin be between 88-89\u00b0F on April 28?",
                forecast_date=tomorrow, location="Austin",
            )
        await conn.commit()
    finally:
        await conn.close()
    return synthetic_db


def _build_mock_indexer(resolutions: dict[str, MarketResolution | None]) -> MagicMock:
    """Build a mock indexer that returns a different MarketResolution per market_id."""
    idx = MagicMock()

    async def _check(condition_id: str, market_question: str = "") -> MarketResolution | None:
        return resolutions.get(condition_id)

    idx.check_resolution = AsyncMock(side_effect=_check)
    return idx


def _build_mock_metar(by_icao: dict[str, list[dict]]) -> MagicMock:
    metar = MagicMock()

    async def _fetch(icao: str, start: str, end: str) -> list[dict]:
        return by_icao.get(icao, [])

    metar.fetch_daily_extremes = AsyncMock(side_effect=_fetch)
    return metar


async def test_backfill_only_past_due(populated_db: str) -> None:
    indexer = _build_mock_indexer({
        "m1": MarketResolution(condition_id="m1", resolution_price=1.0, closed=True, source="gamma_resolved"),
        "m2": None,  # falls through to METAR
        "m3": None,
    })
    metar = _build_mock_metar({
        "KAUS": [{"date": "X", "max_tempF": 88.5, "min_tempF": 70.0}],  # in [88, 89] bucket → YES
    })
    stats = await bf.backfill_all(populated_db, indexer, metar)

    assert stats["n_markets"] == 3  # only past-due distinct
    # Verify DB rows
    conn = await aiosqlite.connect(populated_db)
    try:
        cur = await conn.execute(
            "SELECT actual_outcome, outcome_source, market_id, COUNT(*) FROM weather_signals "
            "GROUP BY actual_outcome, outcome_source, market_id ORDER BY market_id"
        )
        rows = await cur.fetchall()
    finally:
        await conn.close()
    by_mid = {r[2]: r for r in rows}
    assert by_mid["m1"] == (1.0, "polymarket", "m1", 20)
    assert by_mid["m2"] == (1.0, "metar", "m2", 20)
    assert by_mid["m3"] == (None, "failed", "m3", 20)
    # Future rows untouched
    assert by_mid["m4"][0] is None and by_mid["m4"][1] is None
    assert by_mid["m5"][0] is None and by_mid["m5"][1] is None


async def test_backfill_idempotent(populated_db: str) -> None:
    """Running backfill twice produces the same row counts — UPDATE only NULLs."""
    indexer = _build_mock_indexer({
        "m1": MarketResolution(condition_id="m1", resolution_price=1.0, closed=True, source="gamma_resolved"),
        "m2": MarketResolution(condition_id="m2", resolution_price=0.0, closed=True, source="gamma_resolved"),
        "m3": MarketResolution(condition_id="m3", resolution_price=1.0, closed=True, source="gamma_resolved"),
    })
    metar = _build_mock_metar({})

    await bf.backfill_all(populated_db, indexer, metar)
    # First run made 3 distinct calls
    assert indexer.check_resolution.await_count == 3

    await bf.backfill_all(populated_db, indexer, metar)
    # Second run: zero distinct unresolved past-due markets remain
    assert indexer.check_resolution.await_count == 3  # no new calls

    conn = await aiosqlite.connect(populated_db)
    try:
        cur = await conn.execute("SELECT COUNT(*) FROM weather_signals WHERE actual_outcome IS NOT NULL")
        (n,) = await cur.fetchone()
    finally:
        await conn.close()
    assert n == 60  # 20 × 3 past-due markets


async def test_backfill_distinct_efficiency(synthetic_db: str) -> None:
    """1000 rows for 5 distinct markets → 5 indexer calls, not 1000."""
    yesterday = (date.today() - timedelta(days=2)).isoformat()
    conn = await aiosqlite.connect(synthetic_db)
    try:
        for i in range(5):
            for _ in range(200):
                await _insert_row(
                    conn,
                    market_id=f"m{i}",
                    question=f"Will the highest temperature in Miami be {80+i}\u00b0F or higher on April 28?",
                    forecast_date=yesterday,
                    location="Miami",
                )
        await conn.commit()
    finally:
        await conn.close()

    indexer = _build_mock_indexer({
        f"m{i}": MarketResolution(condition_id=f"m{i}", resolution_price=1.0, closed=True, source="gamma_resolved")
        for i in range(5)
    })
    metar = _build_mock_metar({})

    await bf.backfill_all(synthetic_db, indexer, metar)
    assert indexer.check_resolution.await_count == 5  # exactly DISTINCT count


async def test_backfill_by_city_breakdown(populated_db: str) -> None:
    indexer = _build_mock_indexer({
        "m1": MarketResolution(condition_id="m1", resolution_price=1.0, closed=True, source="gamma_resolved"),
        "m2": None,
        "m3": None,
    })
    metar = _build_mock_metar({
        "KAUS": [{"date": "X", "max_tempF": 88.5, "min_tempF": 70.0}],
    })
    stats = await bf.backfill_all(populated_db, indexer, metar)
    by_city = stats["by_city"]
    assert by_city["Miami"]["polymarket"] == 1
    assert by_city["Austin"]["metar"] == 1
    assert by_city["Chengdu"]["failed"] == 1
