"""Backtest framework — analyzes resolved trades from calibration_log.

CLI: python -m weather.backtest [db_path]
Pure math, no LLM.
"""

from __future__ import annotations

import asyncio
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiosqlite


# ── Result dataclass ───────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class BacktestResult:
    """Aggregated backtest metrics from calibration_log."""

    total_trades: int = 0
    win_rate: float = 0.0
    total_pnl: float = 0.0
    avg_pnl: float = 0.0
    brier_score: float = 0.0
    # Confidence buckets
    high_conf_trades: int = 0
    high_conf_win_rate: float = 0.0
    medium_conf_trades: int = 0
    medium_conf_win_rate: float = 0.0
    low_conf_trades: int = 0
    low_conf_win_rate: float = 0.0
    # Edge analysis
    avg_predicted_edge: float = 0.0
    avg_realized_edge: float = 0.0
    edge_correlation: float = 0.0


# ── Pure-math helpers ──────────────────────────────────────────────


def compute_brier_score(predictions: list[float], outcomes: list[float]) -> float:
    """Mean squared error between predicted probabilities and binary outcomes.

    Returns 0.0 for empty inputs.
    """
    if not predictions:
        return 0.0
    return sum((p - o) ** 2 for p, o in zip(predictions, outcomes)) / len(predictions)


def compute_edge_correlation(predicted: list[float], realized: list[float]) -> float:
    """Pearson correlation coefficient between predicted and realized edges.

    Returns 0.0 for n < 2.
    """
    n = len(predicted)
    if n < 2:
        return 0.0

    mean_p = sum(predicted) / n
    mean_r = sum(realized) / n

    cov = sum((p - mean_p) * (r - mean_r) for p, r in zip(predicted, realized))
    std_p = math.sqrt(sum((p - mean_p) ** 2 for p in predicted))
    std_r = math.sqrt(sum((r - mean_r) ** 2 for r in realized))

    if std_p == 0.0 or std_r == 0.0:
        return 0.0

    return cov / (std_p * std_r)


# ── Confidence bucketing helper ────────────────────────────────────


def _bucket_confidence(confidence: float) -> str:
    """Classify confidence into high / medium / low."""
    if confidence >= 0.7:
        return "high"
    if confidence >= 0.3:
        return "medium"
    return "low"


def _win_rate(trades: list[dict[str, Any]]) -> float:
    if not trades:
        return 0.0
    return sum(1 for t in trades if t["win"]) / len(trades)


# ── Core backtest ──────────────────────────────────────────────────


async def run_backtest(db_path: Path) -> BacktestResult:
    """Read calibration_log and compute all backtest metrics."""
    rows: list[dict[str, Any]] = []

    # Ensure parent directory exists so sqlite can create the file
    db_path.parent.mkdir(parents=True, exist_ok=True)

    async with aiosqlite.connect(str(db_path)) as conn:
        # Ensure table exists so empty DB doesn't crash
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS calibration_log ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "trade_id TEXT NOT NULL,"
            "confidence REAL NOT NULL,"
            "estimated_probability REAL NOT NULL,"
            "market_price_at_signal REAL NOT NULL,"
            "market_price_at_resolution REAL NOT NULL,"
            "delay_seconds INTEGER NOT NULL,"
            "pnl REAL NOT NULL,"
            "win INTEGER NOT NULL,"
            "timestamp TEXT NOT NULL,"
            "weather_metric TEXT NOT NULL DEFAULT '',"
            "location TEXT NOT NULL DEFAULT ''"
            ")"
        )
        cursor = await conn.execute(
            "SELECT * FROM calibration_log ORDER BY timestamp DESC"
        )
        raw_rows = await cursor.fetchall()
        cols = [desc[0] for desc in cursor.description]
        rows = [dict(zip(cols, row, strict=False)) for row in raw_rows]

    if not rows:
        return BacktestResult()

    # Basic stats
    total = len(rows)
    wins = sum(1 for r in rows if r["win"])
    total_pnl = sum(r["pnl"] for r in rows)

    # Predicted edge = estimated_probability - market_price_at_signal
    predicted_edges = [
        r["estimated_probability"] - r["market_price_at_signal"] for r in rows
    ]
    realized_edges = [r["pnl"] for r in rows]

    # Brier score: use estimated_probability as prediction, win as outcome
    predictions = [r["estimated_probability"] for r in rows]
    outcomes = [float(r["win"]) for r in rows]

    # Confidence buckets
    buckets: dict[str, list[dict[str, Any]]] = {"high": [], "medium": [], "low": []}
    for r in rows:
        bucket = _bucket_confidence(r["confidence"])
        buckets[bucket].append(r)

    return BacktestResult(
        total_trades=total,
        win_rate=wins / total,
        total_pnl=total_pnl,
        avg_pnl=total_pnl / total,
        brier_score=compute_brier_score(predictions, outcomes),
        high_conf_trades=len(buckets["high"]),
        high_conf_win_rate=_win_rate(buckets["high"]),
        medium_conf_trades=len(buckets["medium"]),
        medium_conf_win_rate=_win_rate(buckets["medium"]),
        low_conf_trades=len(buckets["low"]),
        low_conf_win_rate=_win_rate(buckets["low"]),
        avg_predicted_edge=sum(predicted_edges) / total,
        avg_realized_edge=sum(realized_edges) / total,
        edge_correlation=compute_edge_correlation(predicted_edges, realized_edges),
    )


# ── Report formatting ─────────────────────────────────────────────


def format_report(result: BacktestResult) -> str:
    """Format backtest result as an ASCII report."""
    lines = [
        "=" * 60,
        "  BACKTEST REPORT",
        "=" * 60,
        "",
        "--- Overview ---",
        f"  Total trades:      {result.total_trades}",
        f"  Win rate:          {result.win_rate:.1%}",
        f"  Total PnL:         ${result.total_pnl:+.4f}",
        f"  Avg PnL/trade:     ${result.avg_pnl:+.4f}",
        f"  Brier score:       {result.brier_score:.4f}",
        "",
        "--- Confidence Buckets ---",
        f"  High  (>=0.7):  {result.high_conf_trades:>4} trades,"
        f"  win rate {result.high_conf_win_rate:.1%}",
        f"  Medium (0.3-0.7): {result.medium_conf_trades:>4} trades,"
        f"  win rate {result.medium_conf_win_rate:.1%}",
        f"  Low    (<0.3):  {result.low_conf_trades:>4} trades,"
        f"  win rate {result.low_conf_win_rate:.1%}",
        "",
        "--- Edge Analysis ---",
        f"  Avg predicted edge:  {result.avg_predicted_edge:+.4f}",
        f"  Avg realized edge:   {result.avg_realized_edge:+.4f}",
        f"  Edge correlation:    {result.edge_correlation:+.4f}",
        "",
        "=" * 60,
    ]
    return "\n".join(lines)


# ── CLI entry ──────────────────────────────────────────────────────


async def _main() -> None:
    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/bot.db")
    result = await run_backtest(db_path)
    print(format_report(result))


if __name__ == "__main__":
    asyncio.run(_main())
