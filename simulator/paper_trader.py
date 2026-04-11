"""Weather paper trader — event-based resolution via Gamma API.

Opens paper trades at market price, polls Gamma for market closure,
then resolves with actual outcome price. 7-day force-resolve fallback.
"""

from __future__ import annotations

import asyncio
import html as html_mod
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from infra.types import (
    MarketResolution,
    SignalType,
    TradeResult,
    WeatherPaperTrade,
    WeatherSignal,
)

_CONFIDENCE_MAP: dict[str, float] = {
    "high": 0.9,
    "medium": 0.5,
    "low": 0.2,
}

_FORCE_RESOLVE_DAYS = 7


@dataclass(frozen=True, slots=True)
class CalibrationRecord:
    """Calibration data logged at trade resolution for edge estimation."""

    trade_id: str
    confidence: float
    estimated_probability: float
    market_price_at_signal: float
    market_price_at_resolution: float
    delay_seconds: int
    pnl: float
    win: bool
    timestamp: datetime
    weather_metric: str = ""
    location: str = ""

logger = logging.getLogger(__name__)


class WeatherPaperTrader:
    """Paper trading engine for weather-based signals."""

    def __init__(
        self,
        price_fetcher: Any,
        db_writer: Any,
        telegram: Any,
        risk_manager: Any,
        reconciler: Any,
        delay_seconds: int = 30,
        taker_fee_pct: float = 0.01,
    ) -> None:
        self._price_fetcher = price_fetcher
        self._db_writer = db_writer
        self._telegram = telegram
        self._risk_manager = risk_manager
        self._reconciler = reconciler
        self._delay_seconds = delay_seconds  # kept for backward compat
        self._taker_fee_pct = taker_fee_pct
        self._pending: list[tuple[WeatherPaperTrade, WeatherSignal, datetime]] = []
        self._pending_lock = asyncio.Lock()
        self._market_indexer: Any = None
        self._dedup: Any = None
        # P10.1 revisit (shadow mode): parallel pending list for BUY_YES
        # signals that bypass real capital. Resolved in the same loop as
        # _pending but via _resolve_shadow_trade (no bankroll side-effects).
        # Each entry: (shadow_row_dict, WeatherSignal, opened_at)
        self._shadow_pending: list[tuple[dict[str, Any], WeatherSignal, datetime]] = []
        self._shadow_pending_lock = asyncio.Lock()

    def set_market_indexer(self, indexer: Any) -> None:
        """Inject the market indexer for resolution polling."""
        self._market_indexer = indexer

    def set_dedup(self, dedup: Any) -> None:
        """Inject the dedup set for position tracking."""
        self._dedup = dedup

    @property
    def pending_count(self) -> int:
        """Number of trades awaiting resolution."""
        return len(self._pending)

    async def load_pending_trades(self) -> int:
        """Reload pending trades from DB after restart. Returns count loaded."""
        rows = await self._db_writer.read_pending_trades()
        loaded = 0
        for row in rows:
            try:
                opened_at = datetime.fromisoformat(row["opened_at"])
                trade = WeatherPaperTrade(
                    trade_id=row["trade_id"],
                    market_question=row["market_question"],
                    market_id=row["market_id"],
                    token_id=row["token_id"],
                    signal_type=SignalType(row["signal_type"]),
                    size_usdc=row["size_usdc"],
                    entry_price=row["entry_price"],
                    exit_price=row.get("exit_price"),
                    fees=row["fees"],
                    pnl=row.get("pnl"),
                    status="pending",
                    opened_at=opened_at,
                    resolved_at=None,
                    resolution_source=row.get("resolution_source", ""),
                )
                # Minimal signal for resolution (calibration uses defaults)
                signal = WeatherSignal(
                    market_question=row["market_question"],
                    market_id=row["market_id"],
                    token_id=row["token_id"],
                    signal_type=trade.signal_type,
                    forecast_probability=row["entry_price"],
                    market_price=row["entry_price"],
                    edge=0.0,
                    location="",
                    forecast_date="",
                    weather_metric="",
                    threshold_value=0.0,
                    timestamp=opened_at,
                )
                self._pending.append((trade, signal, opened_at))
                self._risk_manager.add_exposure(trade.size_usdc)
                loaded += 1
            except Exception:
                logger.exception("Failed to reload trade: %s", row.get("trade_id"))
        if loaded:
            logger.info("Reloaded %d pending trades from DB", loaded)
        return loaded

    async def open_trade(
        self, signal: WeatherSignal, size_usdc: float
    ) -> WeatherPaperTrade | None:
        """Open a paper trade at current market price.

        Returns the pending trade, or None if price fetch fails.
        """
        # Use signal's market price for entry (consistent with edge calculation).
        # The CLOB best_ask can diverge wildly on illiquid markets.
        price = signal.market_price

        trade_id = f"paper_{uuid.uuid4().hex[:12]}"
        now = datetime.now(UTC)
        entry_fee = size_usdc * self._taker_fee_pct
        fees = entry_fee  # exit fee added at resolution

        trade = WeatherPaperTrade(
            trade_id=trade_id,
            market_question=signal.market_question,
            market_id=signal.market_id,
            token_id=signal.token_id,
            signal_type=signal.signal_type,
            size_usdc=size_usdc,
            entry_price=price,
            exit_price=None,
            fees=fees,
            pnl=None,
            status="pending",
            opened_at=now,
            resolved_at=None,
            resolution_source="",
        )

        await self._db_writer.enqueue("paper_trades", trade)
        async with self._pending_lock:
            self._pending.append((trade, signal, now))
        self._risk_manager.add_exposure(size_usdc)

        await self._telegram.send(
            f"PAPER TRADE OPENED\n"
            f"Signal: {signal.signal_type.value}\n"
            f"Market: {html_mod.escape(signal.market_question[:80])}\n"
            f"Entry: ${price:.4f}\n"
            f"Size: ${size_usdc:.2f}\n"
            f"Location: {signal.location} | Metric: {signal.weather_metric}"
        )

        logger.info(
            "Paper trade opened: %s %s @ %.4f ($%.2f)",
            trade_id, signal.signal_type.value, price, size_usdc,
        )
        return trade

    async def open_shadow_trade(
        self, signal: WeatherSignal, size_usdc: float
    ) -> dict[str, Any] | None:
        """Open a SHADOW paper trade — observes outcome without touching capital.

        P10.1 revisit: BUY_YES signals used to be hard-blocked on n=9 losers.
        Instead, route them here so we can collect a statistically honest win
        rate over n>=30 samples before deciding to re-enable. This method
        never calls risk_manager (no exposure, no bankroll), never touches
        paper_trades, and never updates the reconciler. Its only side effect
        is an insert into the shadow_trades table.

        Returns the shadow row dict on success, None if inputs are invalid.
        """
        price = signal.market_price
        if price <= 0 or price >= 1:
            logger.warning(
                "open_shadow_trade rejected invalid price=%.4f", price,
            )
            return None

        trade_id = f"shadow_{uuid.uuid4().hex[:12]}"
        now = datetime.now(UTC)
        confidence_str = getattr(signal, "confidence", "medium")

        shadow_row: dict[str, Any] = {
            "trade_id": trade_id,
            "opened_at": now.isoformat(),
            "market_question": signal.market_question,
            "market_condition_id": signal.market_id,
            "yes_token": signal.token_id,
            "side": signal.signal_type.value,
            "edge": float(signal.edge),
            "prob": float(signal.forecast_probability),
            "price": float(price),
            "size_usd": float(size_usdc),
            "confidence": confidence_str,
            "status": "pending",
            "resolved_at": None,
            "outcome": None,
            "pnl_usd": None,
        }

        await self._db_writer.enqueue("shadow_trades", shadow_row)
        async with self._shadow_pending_lock:
            self._shadow_pending.append((shadow_row, signal, now))

        logger.info(
            "SHADOW TRADE opened: %s %s @ %.4f ($%.2f) edge=%.4f",
            trade_id, signal.signal_type.value, price, size_usdc,
            signal.edge,
        )
        return shadow_row

    async def load_pending_shadow_trades(self) -> int:
        """Reload pending shadow trades from DB after restart. Returns count."""
        try:
            rows = await self._db_writer.read_pending_shadow_trades()
        except Exception:
            logger.exception("Failed to read pending shadow trades")
            return 0
        loaded = 0
        for row in rows:
            try:
                opened_at = datetime.fromisoformat(row["opened_at"])
                signal = WeatherSignal(
                    market_question=row["market_question"],
                    market_id=row["market_condition_id"],
                    token_id=row["yes_token"],
                    signal_type=SignalType(row["side"]),
                    forecast_probability=row["prob"],
                    market_price=row["price"],
                    edge=row["edge"],
                    location="",
                    forecast_date="",
                    weather_metric="",
                    threshold_value=0.0,
                    timestamp=opened_at,
                    confidence=row.get("confidence", "medium"),
                )
                self._shadow_pending.append((dict(row), signal, opened_at))
                loaded += 1
            except Exception:
                logger.exception(
                    "Failed to reload shadow trade: %s", row.get("trade_id"),
                )
        if loaded:
            logger.info("Reloaded %d pending shadow trades from DB", loaded)
        return loaded

    async def check_pending(self) -> None:
        """Check all pending trades: poll Gamma for closure, force-resolve after 7 days.

        Also resolves shadow trades (BUY_YES observations, P10.1 revisit) using
        the same Gamma resolution data — but with zero bankroll impact.
        """
        now = datetime.now(UTC)
        resolved_ids: set[str] = set()

        async with self._pending_lock:
            snapshot = list(self._pending)

        for trade, signal, opened_at in snapshot:
            try:
                # Poll Gamma for market resolution
                resolution: MarketResolution | None = None
                if self._market_indexer is not None:
                    resolution = await self._market_indexer.check_resolution(
                        trade.market_id,
                        market_question=trade.market_question,
                    )

                if resolution is not None:
                    # Market closed — resolve with actual outcome
                    did_resolve = await self._resolve_trade(
                        trade, signal, now, resolution,
                    )
                    if did_resolve:
                        resolved_ids.add(trade.trade_id)
                elif (now - opened_at) > timedelta(days=_FORCE_RESOLVE_DAYS):
                    # Stuck too long — force-resolve
                    await self._force_resolve(trade, signal, now)
                    resolved_ids.add(trade.trade_id)
                elif (
                    self._market_indexer is not None
                    and trade.market_id in self._market_indexer.gamma_mismatch_ids
                    and (now - opened_at) > timedelta(hours=48)
                ):
                    # Market migrated on Polymarket — will never resolve via Gamma
                    logger.info(
                        "Force-resolving orphaned trade %s (Gamma mismatch, 48h+)",
                        trade.trade_id,
                    )
                    await self._force_resolve(trade, signal, now)
                    resolved_ids.add(trade.trade_id)
                # else: market still open, keep pending (no retry counting)
            except Exception:
                logger.exception(
                    "Failed to resolve %s, keeping pending", trade.trade_id,
                )

        if resolved_ids:
            async with self._pending_lock:
                self._pending = [
                    item for item in self._pending
                    if item[0].trade_id not in resolved_ids
                ]

        # P10.1 revisit: resolve shadow trades using same Gamma data but
        # with zero bankroll / exposure / reconciler impact.
        async with self._shadow_pending_lock:
            shadow_snapshot = list(self._shadow_pending)

        resolved_shadow_ids: set[str] = set()
        for shadow_row, shadow_signal, shadow_opened_at in shadow_snapshot:
            try:
                shadow_resolution: MarketResolution | None = None
                if self._market_indexer is not None:
                    shadow_resolution = await self._market_indexer.check_resolution(
                        shadow_row["market_condition_id"],
                        market_question=shadow_row["market_question"],
                    )
                if shadow_resolution is not None:
                    await self._resolve_shadow_trade(
                        shadow_row, shadow_signal, now, shadow_resolution,
                    )
                    resolved_shadow_ids.add(shadow_row["trade_id"])
                elif (now - shadow_opened_at) > timedelta(days=_FORCE_RESOLVE_DAYS):
                    await self._force_resolve_shadow(shadow_row, now)
                    resolved_shadow_ids.add(shadow_row["trade_id"])
            except Exception:
                logger.exception(
                    "Failed to resolve shadow trade %s, keeping pending",
                    shadow_row.get("trade_id"),
                )

        if resolved_shadow_ids:
            async with self._shadow_pending_lock:
                self._shadow_pending = [
                    item for item in self._shadow_pending
                    if item[0]["trade_id"] not in resolved_shadow_ids
                ]

    async def _resolve_shadow_trade(
        self,
        shadow_row: dict[str, Any],
        signal: WeatherSignal,
        now: datetime,
        resolution: MarketResolution,
    ) -> None:
        """Resolve a shadow trade with PnL BUT NO bankroll side-effects.

        This is deliberately a parallel path to _resolve_trade. It must NEVER
        call risk_manager.* or reconciler.analyze — the whole point of shadow
        mode is to observe without touching capital.
        """
        # Exit price logic mirrors _resolve_trade
        exit_price = resolution.resolution_price
        side = shadow_row["side"]
        if side == SignalType.BUY_NO.value:
            exit_price = 1.0 - resolution.resolution_price
        exit_price = max(0.0, min(1.0, exit_price))

        entry_price = shadow_row["price"]
        size_usdc = shadow_row["size_usd"]
        # Use same entry-fee-only convention as paper trades
        entry_fee = size_usdc * self._taker_fee_pct
        pnl = self._compute_pnl(
            signal_type=SignalType(side),
            entry_price=entry_price,
            exit_price=exit_price,
            size_usdc=size_usdc,
            fees=entry_fee,
        )
        win = pnl > 0

        resolved_row: dict[str, Any] = {
            **shadow_row,
            "status": "resolved",
            "resolved_at": now.isoformat(),
            "outcome": "win" if win else "loss",
            "pnl_usd": pnl,
        }
        await self._db_writer.enqueue("shadow_trades", resolved_row)

        logger.info(
            "SHADOW TRADE resolved: %s PnL=$%.4f (%s) source=%s [no bankroll impact]",
            shadow_row["trade_id"], pnl, "WIN" if win else "LOSS",
            resolution.source,
        )
        # INTENTIONAL: no risk_manager, no reconciler, no dedup, no telegram
        # alert. Shadow trades are pure observations.

    async def _force_resolve_shadow(
        self, shadow_row: dict[str, Any], now: datetime,
    ) -> None:
        """Force-resolve a stuck shadow trade after 7 days. PnL = -entry_fee."""
        logger.critical(
            "Force-resolving stuck SHADOW trade %s after %d days",
            shadow_row["trade_id"], _FORCE_RESOLVE_DAYS,
        )
        entry_fee = shadow_row["size_usd"] * self._taker_fee_pct
        resolved_row: dict[str, Any] = {
            **shadow_row,
            "status": "force_resolved",
            "resolved_at": now.isoformat(),
            "outcome": "force_resolved",
            "pnl_usd": -entry_fee,
        }
        await self._db_writer.enqueue("shadow_trades", resolved_row)

    async def _resolve_trade(
        self,
        trade: WeatherPaperTrade,
        signal: WeatherSignal,
        now: datetime,
        resolution: MarketResolution,
    ) -> bool:
        """Resolve a single pending trade using market resolution data.

        Returns True if resolved successfully.
        """
        # Exit price from resolution — for BUY_NO, invert
        exit_price = resolution.resolution_price
        if trade.signal_type == SignalType.BUY_NO:
            exit_price = 1.0 - resolution.resolution_price

        # Validate exit price
        exit_price = max(0.0, min(1.0, exit_price))

        # Compute shares — no exit fee on Polymarket resolution (redemption at face value)
        shares = trade.size_usdc / trade.entry_price if trade.entry_price > 0 else 0.0
        total_fees = trade.fees  # entry fee only; resolution has no taker fee

        # Compute PnL: (exit - entry) * shares - fees
        pnl = self._compute_pnl(
            signal_type=signal.signal_type,
            entry_price=trade.entry_price,
            exit_price=exit_price,
            size_usdc=trade.size_usdc,
            fees=total_fees,
        )

        # Create resolved trade
        resolved = WeatherPaperTrade(
            trade_id=trade.trade_id,
            market_question=trade.market_question,
            market_id=trade.market_id,
            token_id=trade.token_id,
            signal_type=trade.signal_type,
            size_usdc=trade.size_usdc,
            entry_price=trade.entry_price,
            exit_price=exit_price,
            fees=total_fees,
            pnl=pnl,
            status="resolved",
            opened_at=trade.opened_at,
            resolved_at=now,
            resolution_source=resolution.source,
        )

        # Persist to DB
        await self._db_writer.enqueue("paper_trades", resolved)

        # Log calibration data for edge estimation
        win = pnl > 0
        confidence_str = getattr(signal, "confidence", "medium")
        confidence_val = _CONFIDENCE_MAP.get(confidence_str, 0.5)
        delay = int((now - trade.opened_at).total_seconds())

        cal = CalibrationRecord(
            trade_id=trade.trade_id,
            confidence=confidence_val,
            estimated_probability=signal.forecast_probability,
            market_price_at_signal=signal.market_price,
            market_price_at_resolution=exit_price,
            delay_seconds=delay,
            pnl=pnl,
            win=win,
            timestamp=now,
            weather_metric=signal.weather_metric,
            location=signal.location,
        )
        await self._db_writer.enqueue("calibration_log", cal)

        # Release exposure and update bankroll
        self._risk_manager.release_exposure(trade.size_usdc)
        self._risk_manager.update_bankroll(pnl)

        # Report to reconciler
        trade_result = TradeResult(
            market_id=trade.market_id,
            signal_type=trade.signal_type,
            entry_price=trade.entry_price,
            exit_price=exit_price,
            size_usdc=trade.size_usdc,
            pnl=pnl,
            win=win,
            timestamp=now,
        )
        self._reconciler.analyze(trade_result)

        # Clean up dedup
        if self._dedup is not None:
            self._dedup.remove(trade.market_id, trade.signal_type.value)
            await self._db_writer.mark_position_resolved(
                trade.market_id, trade.signal_type.value,
            )

        # Telegram alert
        if self._telegram is not None:
            await self._telegram.alert_resolution(
                market_question=trade.market_question,
                signal_type=trade.signal_type.value,
                entry_price=trade.entry_price,
                exit_price=exit_price,
                pnl=pnl,
                win=win,
                resolution_source=resolution.source,
            )

        logger.info(
            "Paper trade resolved: %s PnL=$%.4f (%s) source=%s",
            trade.trade_id, pnl, "WIN" if win else "LOSS", resolution.source,
        )
        return True

    async def _force_resolve(
        self,
        trade: WeatherPaperTrade,
        signal: WeatherSignal,
        now: datetime,
    ) -> None:
        """Force-resolve a stuck trade after 7 days. PnL = -fees (exit at entry)."""
        logger.critical(
            "Force-resolving stuck trade %s after %d days",
            trade.trade_id, _FORCE_RESOLVE_DAYS,
        )

        resolution = MarketResolution(
            source="force_resolved",
            resolution_price=trade.entry_price,
            resolved_at=now,
        )

        # Exit at entry price means no price move, just fees lost
        exit_price = trade.entry_price
        total_fees = trade.fees  # no exit fee since exit_value ~ entry_value is small

        resolved = WeatherPaperTrade(
            trade_id=trade.trade_id,
            market_question=trade.market_question,
            market_id=trade.market_id,
            token_id=trade.token_id,
            signal_type=trade.signal_type,
            size_usdc=trade.size_usdc,
            entry_price=trade.entry_price,
            exit_price=exit_price,
            fees=total_fees,
            pnl=-total_fees,
            status="force_resolved",
            opened_at=trade.opened_at,
            resolved_at=now,
            resolution_source=resolution.source,
        )
        await self._db_writer.enqueue("paper_trades", resolved)

        self._risk_manager.release_exposure(trade.size_usdc)
        self._risk_manager.update_bankroll(-total_fees)

        # Clean up dedup
        if self._dedup is not None:
            self._dedup.remove(trade.market_id, trade.signal_type.value)
            await self._db_writer.mark_position_resolved(
                trade.market_id, trade.signal_type.value,
            )

        if self._telegram is not None:
            await self._telegram.alert_resolution(
                market_question=trade.market_question,
                signal_type=trade.signal_type.value,
                entry_price=trade.entry_price,
                exit_price=exit_price,
                pnl=-total_fees,
                win=False,
                resolution_source="force_resolved",
            )

    @staticmethod
    def _compute_pnl(
        signal_type: SignalType,
        entry_price: float,
        exit_price: float,
        size_usdc: float,
        fees: float,
    ) -> float:
        """Compute PnL for a paper trade.

        Both BUY_YES and BUY_NO are long positions on their respective tokens.
        Profit when the bought token's price goes up: (exit - entry) * shares - fees
        """
        if entry_price <= 0:
            return -fees

        shares = size_usdc / entry_price
        raw_pnl = (exit_price - entry_price) * shares

        return raw_pnl - fees
