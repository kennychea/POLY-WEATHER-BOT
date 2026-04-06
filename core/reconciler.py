"""Reconciler — post-mortem analysis of completed trades."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from infra.types import SignalType, TradeResult

logger = logging.getLogger(__name__)


class Reconciler:
    """Analyzes completed trades and tracks cumulative statistics."""

    def __init__(self) -> None:
        self._results: list[TradeResult] = []

    async def hydrate(self, db_writer: Any) -> int:
        """Load resolved trades from DB to restore stats after restart."""
        rows = await db_writer.read_resolved_trades()
        loaded = 0
        for row in rows:
            try:
                if row["exit_price"] is None:
                    logger.warning(
                        "Trade %s has no exit_price, skipping hydration",
                        row.get("trade_id"),
                    )
                    continue
                result = TradeResult(
                    market_id=row["market_id"],
                    signal_type=SignalType(row["signal_type"]),
                    entry_price=row["entry_price"],
                    exit_price=row["exit_price"] or 0.0,
                    size_usdc=row["size_usdc"],
                    pnl=row["pnl"] or 0.0,
                    win=(row["pnl"] or 0.0) > 0,
                    timestamp=datetime.fromisoformat(
                        row.get("resolved_at") or row["opened_at"],
                    ),
                )
                self._results.append(result)
                loaded += 1
            except Exception:
                logger.exception("Failed to hydrate trade: %s", row.get("trade_id"))
        if loaded:
            logger.info("Hydrated reconciler with %d resolved trades", loaded)
        return loaded

    def analyze(self, result: TradeResult) -> dict[str, Any]:
        """Analyze a single trade result and add to history."""
        self._results.append(result)

        return_pct = (
            (result.pnl / result.size_usdc) * 100 if result.size_usdc > 0 else 0.0
        )

        return {
            "market_id": result.market_id,
            "outcome": "win" if result.win else "loss",
            "pnl": result.pnl,
            "return_pct": round(return_pct, 2),
            "entry": result.entry_price,
            "exit": result.exit_price,
        }

    def stats(self) -> dict[str, Any]:
        """Return cumulative statistics."""
        total = len(self._results)
        if total == 0:
            return {
                "total_trades": 0,
                "wins": 0,
                "losses": 0,
                "win_rate": 0.0,
                "total_pnl": 0.0,
                "profit_factor": 0.0,
            }

        wins = sum(1 for r in self._results if r.win)
        losses = total - wins
        total_pnl = sum(r.pnl for r in self._results)
        gross_wins = sum(r.pnl for r in self._results if r.pnl > 0)
        gross_losses = abs(sum(r.pnl for r in self._results if r.pnl < 0))
        profit_factor = (
            gross_wins / gross_losses if gross_losses > 0 else 999.0
        )

        return {
            "total_trades": total,
            "wins": wins,
            "losses": losses,
            "win_rate": wins / total,
            "total_pnl": round(total_pnl, 2),
            "profit_factor": round(profit_factor, 3),
        }
