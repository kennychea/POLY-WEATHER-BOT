"""Weather paper trader — T0 entry, T+30s resolution, PnL tracking.

Opens paper trades at market price, waits delay_seconds, then fetches
the new price to compute PnL including taker fees.
"""

from __future__ import annotations

import asyncio
import html as html_mod
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from infra.types import WeatherPaperTrade, WeatherSignal, SignalType, TradeResult


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
        self._delay_seconds = delay_seconds
        self._taker_fee_pct = taker_fee_pct
        self._pending: list[tuple[WeatherPaperTrade, WeatherSignal, datetime]] = []
        self._pending_lock = asyncio.Lock()
        self._retry_counts: dict[str, int] = {}
        self._max_retries: int = 10

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
        price = await self._price_fetcher.get_price(signal.token_id)
        if price is None:
            logger.warning("Cannot open trade: price unavailable for %s", signal.token_id)
            return None

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

    async def check_pending(self) -> None:
        """Check all pending trades and resolve those past the delay."""
        now = datetime.now(UTC)
        resolved_ids: set[str] = set()

        async with self._pending_lock:
            snapshot = list(self._pending)

        for trade, signal, opened_at in snapshot:
            elapsed = (now - opened_at).total_seconds()
            if elapsed < self._delay_seconds:
                continue

            try:
                did_resolve = await self._resolve_trade(trade, signal, now)
                if did_resolve:
                    resolved_ids.add(trade.trade_id)
                    self._retry_counts.pop(trade.trade_id, None)
                else:
                    count = self._retry_counts.get(trade.trade_id, 0) + 1
                    self._retry_counts[count] = count
                    if count >= self._max_retries:
                        await self._force_resolve(trade, signal, now)
                        resolved_ids.add(trade.trade_id)
                        self._retry_counts.pop(trade.trade_id, None)
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

    async def _resolve_trade(
        self,
        trade: WeatherPaperTrade,
        signal: WeatherSignal,
        now: datetime,
    ) -> bool:
        """Resolve a single pending trade. Returns True if resolved, False to keep pending."""
        try:
            exit_price = await asyncio.wait_for(
                self._price_fetcher.get_price(signal.token_id),
                timeout=10.0,
            )
        except TimeoutError:
            logger.error(
                "Timeout fetching exit price for %s, keeping pending",
                trade.trade_id,
            )
            return False
        if exit_price is None:
            logger.warning(
                "T+30s price unavailable for %s, keeping pending", trade.trade_id,
            )
            return False
        if exit_price <= 0.0 or exit_price >= 1.0:
            logger.warning(
                "Invalid exit_price=%.4f for %s, keeping pending",
                exit_price, trade.trade_id,
            )
            return False

        # Compute total fees (entry + exit)
        shares = trade.size_usdc / trade.entry_price if trade.entry_price > 0 else 0
        exit_value = exit_price * shares
        exit_fee = exit_value * self._taker_fee_pct
        total_fees = trade.fees + exit_fee

        # Compute PnL
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
        )

        await self._db_writer.enqueue("paper_trades", resolved)

        # Log calibration data for edge estimation
        win = pnl > 0
        cal = CalibrationRecord(
            trade_id=trade.trade_id,
            confidence=float(signal.confidence) if signal.confidence.replace(".", "").isdigit() else 0.5,
            estimated_probability=signal.forecast_probability,
            market_price_at_signal=signal.market_price,
            market_price_at_resolution=exit_price,
            delay_seconds=self._delay_seconds,
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

        emoji = "WIN" if win else "LOSS"
        await self._telegram.send(
            f"{emoji} PAPER TRADE RESOLVED\n"
            f"Trade: {trade.trade_id}\n"
            f"Entry: ${trade.entry_price:.4f} -> Exit: ${exit_price:.4f}\n"
            f"PnL: ${pnl:+.4f}\n"
            f"Market: {html_mod.escape(trade.market_question[:80])}"
        )

        logger.info(
            "Paper trade resolved: %s PnL=$%.4f (%s)",
            trade.trade_id, pnl, "WIN" if win else "LOSS",
        )
        return True

    async def _force_resolve(
        self,
        trade: WeatherPaperTrade,
        signal: WeatherSignal,
        now: datetime,
    ) -> None:
        """Force-resolve a stuck trade after max retries. PnL = -fees."""
        logger.critical(
            "Force-resolving stuck trade %s after %d retries",
            trade.trade_id, self._max_retries,
        )
        resolved = WeatherPaperTrade(
            trade_id=trade.trade_id,
            market_question=trade.market_question,
            market_id=trade.market_id,
            token_id=trade.token_id,
            signal_type=trade.signal_type,
            size_usdc=trade.size_usdc,
            entry_price=trade.entry_price,
            exit_price=trade.entry_price,
            fees=trade.fees,
            pnl=-trade.fees,
            status="error",
            opened_at=trade.opened_at,
            resolved_at=now,
        )
        await self._db_writer.enqueue("paper_trades", resolved)
        self._risk_manager.release_exposure(trade.size_usdc)
        self._risk_manager.update_bankroll(-trade.fees)
        await self._telegram.send(
            f"STUCK TRADE FORCE-RESOLVED\n"
            f"Trade: {trade.trade_id}\n"
            f"PnL: ${-trade.fees:+.4f} (fees only)\n"
            f"Reason: price unavailable after {self._max_retries} retries"
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
