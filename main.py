"""
Polymarket Weather Bot -- Entry Point

Pipeline: Ensemble Forecast → Probability Calc → Edge Calc → Risk → Paper Trade
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from core.edge_calculator import calculate_edge
from core.reconciler import Reconciler
from core.risk import RiskManager
from infra.config import Config
from infra.db import DbWriter
from infra.heartbeat import Heartbeat
from infra.telegram import TelegramBot
from infra.types import SignalType, WeatherSignal
from market_data.indexer import MarketIndexer
from market_data.price_fetcher import PriceFetcher
from simulator.paper_trader import WeatherPaperTrader
from weather.ensemble import (
    fetch_ensemble_result,
    get_cache_stats,
    reset_cache_stats,
    resolve_city,
)
from weather.market_scanner import (
    ParsedWeatherMarket,
    get_market_price,
    scan_weather_markets,
)
from weather.probability import ensemble_probability

_LOG_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_LOG_DATE = "%Y-%m-%d %H:%M:%S"


def _setup_logging() -> None:
    import io
    from logging.handlers import RotatingFileHandler

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    formatter = logging.Formatter(_LOG_FMT, datefmt=_LOG_DATE)

    console_stream = io.TextIOWrapper(
        sys.stderr.buffer, encoding="utf-8", errors="replace",
    )
    console = logging.StreamHandler(console_stream)
    console.setFormatter(formatter)
    root.addHandler(console)

    log_dir = Path("data/logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_dir / "bot.log", maxBytes=10 * 1024 * 1024, backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)


_setup_logging()
logger = logging.getLogger("main")


class _TradeDedup:
    """In-memory dedup set to prevent re-opening positions for the same market+direction."""

    def __init__(self) -> None:
        self._active: set[tuple[str, str]] = set()

    def is_active(self, market_id: str, signal_type: str) -> bool:
        return (market_id, signal_type) in self._active

    def add(self, market_id: str, signal_type: str) -> None:
        self._active.add((market_id, signal_type))

    def remove(self, market_id: str, signal_type: str) -> None:
        self._active.discard((market_id, signal_type))

    async def hydrate(self, db_writer: Any) -> None:
        """Load active positions from DB on restart."""
        rows = await db_writer.read_traded_positions("pending")
        for row in rows:
            self._active.add((row["market_id"], row["signal_type"]))
        if self._active:
            logger.info("Dedup: loaded %d active positions from DB", len(self._active))


def build_components() -> dict[str, Any]:
    """Build all bot components from config."""
    load_dotenv()
    cfg = Config.from_env()

    db_path = os.environ.get("DB_PATH", "data/bot.db")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    telegram = TelegramBot(
        token=cfg.telegram_bot_token,
        chat_id=cfg.telegram_chat_id,
    )
    db_writer = DbWriter(db_path, telegram=telegram)

    risk_mgr = RiskManager(
        bankroll=cfg.bankroll_usdc,
        max_bet=cfg.max_bet_usdc,
        max_exposure=cfg.max_exposure_usdc,
        max_edge=cfg.max_edge,
        min_edge=cfg.min_edge_threshold,
    )
    reconciler = Reconciler()

    heartbeat = Heartbeat(telegram=telegram, interval_seconds=300)

    market_indexer = MarketIndexer(
        refresh_interval=cfg.market_refresh_interval,
    )
    price_fetcher = PriceFetcher()

    paper_trader = WeatherPaperTrader(
        price_fetcher=price_fetcher,
        db_writer=db_writer,
        telegram=telegram,
        risk_manager=risk_mgr,
        reconciler=reconciler,
        delay_seconds=cfg.price_check_delay_s,
        taker_fee_pct=cfg.taker_fee_pct,
    )

    return {
        "config": cfg,
        "db_writer": db_writer,
        "telegram": telegram,
        "risk_manager": risk_mgr,
        "reconciler": reconciler,
        "heartbeat": heartbeat,
        "market_indexer": market_indexer,
        "price_fetcher": price_fetcher,
        "paper_trader": paper_trader,
    }


async def scan_and_trade(
    market_indexer: MarketIndexer,
    price_fetcher: PriceFetcher,
    risk_manager: RiskManager,
    db_writer: DbWriter,
    telegram: TelegramBot,
    paper_trader: WeatherPaperTrader,
    cfg: Config,
    dedup: _TradeDedup | None = None,
) -> None:
    """One full scan cycle: parse markets, fetch ensembles, compute edges, trade."""
    t0 = time.perf_counter()
    reset_cache_stats()

    # 1. Find weather markets from indexed Polymarket data
    weather_markets = scan_weather_markets(market_indexer.markets)
    if not weather_markets:
        logger.info("SCAN no weather markets found")
        return

    logger.info("SCAN found %d weather markets", len(weather_markets))

    # 2. For each weather market: ensemble → probability → edge → trade
    # L1 session cache — shared across the entire scan cycle to avoid
    # redundant Open-Meteo API calls for the same city/date/metric.
    session_cache: dict = {}
    trades_opened = 0
    async with __import__("aiohttp").ClientSession() as session:
        for wm in weather_markets:
            try:
                opened = await _evaluate_market(
                    wm, session, price_fetcher, risk_manager,
                    db_writer, paper_trader, cfg, telegram,
                    dedup=dedup, session_cache=session_cache,
                )
                if opened:
                    trades_opened += 1
            except Exception:
                logger.exception(
                    "Error evaluating market: %s",
                    wm.market.get("question", "?"),
                )

    elapsed = time.perf_counter() - t0
    stats = get_cache_stats()
    logger.info(
        "SCAN complete: %d markets, %d trades opened, %.1fs | cache L1=%d L2=%d miss=%d",
        len(weather_markets), trades_opened, elapsed,
        stats["l1_hits"], stats["l2_hits"], stats["misses"],
    )


async def _evaluate_market(
    wm: ParsedWeatherMarket,
    session: Any,
    price_fetcher: PriceFetcher,
    risk_manager: RiskManager,
    db_writer: DbWriter,
    paper_trader: WeatherPaperTrader,
    cfg: Config,
    telegram: TelegramBot | None = None,
    *,
    dedup: _TradeDedup | None = None,
    session_cache: dict | None = None,
) -> bool:
    """Evaluate one weather market using ensemble pipeline. Returns True if trade opened."""
    # 1. Verify city is known for ensemble
    city = resolve_city(wm.location)
    if city is None:
        return False

    # 2. Get token IDs (needed for price fetch and trading)
    token_ids = MarketIndexer.extract_token_ids(wm.market)
    if token_ids is None:
        return False
    yes_token, no_token = token_ids

    # 3. Get market price EARLY — skip ensemble fetch for extreme prices
    prices = get_market_price(wm.market)
    if prices is not None:
        yes_price, _no_price = prices
    else:
        # Fallback: fetch from CLOB orderbook
        fetched = await price_fetcher.get_price(yes_token)
        if fetched is None:
            return False
        yes_price = fetched

    # Filter extreme prices before expensive ensemble API call
    if yes_price < 0.005 or yes_price > 0.995:
        return False

    # 4. Fetch ensemble forecast
    ensemble = await fetch_ensemble_result(
        city=city,
        target_date=wm.target_date,
        metric=wm.metric,
        unit=wm.unit,
        session=session,
        session_cache=session_cache,
    )
    if ensemble is None:
        return False

    # 5. Calculate probability from ensemble members
    prob, confidence = ensemble_probability(
        members=list(ensemble.members),
        threshold_low=wm.threshold_low,
        threshold_high=wm.threshold_high,
        direction=wm.direction,
    )

    # 6. Calculate edge (net of fees)
    edge_result = calculate_edge(
        ensemble_prob=prob,
        market_yes_price=yes_price,
        taker_fee_pct=cfg.taker_fee_pct,
        confidence=confidence,
    )
    if edge_result is None:
        return False

    # 6b. Dedup check — skip if we already have an active position
    if dedup is not None:
        signal_type_val = edge_result.signal_type.value
        market_id = wm.market.get("conditionId", "")
        if dedup.is_active(market_id, signal_type_val):
            logger.debug("SKIP dedup: %s %s", market_id, signal_type_val)
            return False

    if edge_result.signal_type == SignalType.BUY_YES:
        token_id = yes_token
    else:
        token_id = no_token

    # 7. Risk check
    size = risk_manager.size_position(
        estimated_probability=edge_result.ensemble_prob if edge_result.signal_type == SignalType.BUY_YES else 1.0 - edge_result.ensemble_prob,
        market_price=edge_result.market_price,
    )
    if size is None or size <= 0:
        logger.debug(
            "SKIP risk_blocked edge=%.4f market=%s",
            edge_result.net_edge, wm.market.get("question", "?")[:60],
        )
        return False

    # 8. Create signal and open paper trade
    signal = WeatherSignal(
        market_question=wm.market.get("question", ""),
        market_id=wm.market.get("conditionId", ""),
        token_id=token_id,
        signal_type=edge_result.signal_type,
        forecast_probability=prob,
        market_price=edge_result.market_price,
        edge=edge_result.raw_edge,
        location=wm.location,
        forecast_date=wm.target_date,
        weather_metric=wm.metric,
        threshold_value=wm.threshold_low,
        timestamp=datetime.now(UTC),
        ensemble_member_count=ensemble.member_count,
        confidence=confidence,
        net_edge=edge_result.net_edge,
    )

    logger.info(
        "TRADE_SIGNAL %s edge=%.4f net=%.4f prob=%.4f price=%.4f size=$%.2f conf=%s market=%s",
        edge_result.signal_type.value, edge_result.raw_edge, edge_result.net_edge,
        prob, edge_result.market_price, size, confidence,
        wm.market.get("question", "?")[:60],
    )

    # Open paper trade
    trade = await paper_trader.open_trade(signal, size)
    if trade is None:
        # Fallback: just log signal to DB
        await db_writer.enqueue("weather_signals", signal)
        return False

    # Persist dedup state
    if dedup is not None:
        dedup_market_id = wm.market.get("conditionId", "")
        dedup_signal_type = edge_result.signal_type.value
        dedup.add(dedup_market_id, dedup_signal_type)
        await db_writer.enqueue("traded_positions", {
            "market_id": dedup_market_id,
            "signal_type": dedup_signal_type,
            "trade_id": trade.trade_id,
            "opened_at": signal.timestamp.isoformat(),
            "status": "pending",
        })

    if telegram is not None:
        await telegram.alert_edge(
            signal_type=edge_result.signal_type.value,
            edge=edge_result.raw_edge,
            net_edge=edge_result.net_edge,
            market_question=wm.market.get("question", ""),
            location=wm.location,
            price=edge_result.market_price,
        )

    return True


async def _daily_summary_loop(
    telegram: TelegramBot,
    reconciler: Reconciler,
    risk_manager: RiskManager,
    paper_trader: WeatherPaperTrader,
) -> None:
    """Send a daily summary alert at midnight UTC."""
    while True:
        now = datetime.now(UTC)
        tomorrow = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0,
        )
        await asyncio.sleep((tomorrow - now).total_seconds())

        stats = reconciler.stats()
        await telegram.alert_daily_summary(
            trades_opened=paper_trader.pending_count,
            trades_resolved=stats.get("total_trades", 0),
            total_pnl=stats.get("total_pnl", 0.0),
            win_rate=stats.get("win_rate", 0.0),
            bankroll=risk_manager.bankroll,
        )


async def main() -> None:
    """Main entry point."""
    logger.info("polymarket weather-bot starting...")

    components = build_components()
    db = components["db_writer"]
    await db.init_db()
    telegram = components["telegram"]

    heartbeat = components["heartbeat"]
    market_indexer: MarketIndexer = components["market_indexer"]
    reconciler: Reconciler = components["reconciler"]
    price_fetcher: PriceFetcher = components["price_fetcher"]
    risk_manager: RiskManager = components["risk_manager"]
    paper_trader: WeatherPaperTrader = components["paper_trader"]
    cfg: Config = components["config"]

    dedup = _TradeDedup()
    await dedup.hydrate(db)

    paper_trader.set_dedup(dedup)

    await reconciler.hydrate(db)
    await paper_trader.load_pending_trades()

    # Pre-load market index
    try:
        await market_indexer.refresh()
        logger.info("Initial market refresh: %d markets indexed", market_indexer.market_count)
    except Exception:
        logger.warning("Initial market refresh failed, will retry")

    async def _safe_task(coro_factory: Any, name: str) -> None:
        backoff = 1.0
        max_backoff = 60.0
        while True:
            try:
                coro = coro_factory() if callable(coro_factory) else coro_factory
                await coro
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("%s crashed, restarting in %.0fs", name, backoff)
                try:
                    await telegram.send(f"ALERT: {name} crashed! Restarting in {backoff:.0f}s...")
                except Exception:
                    pass
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)

    async def _weather_scan_loop() -> None:
        """Periodically scan for weather trading opportunities."""
        while True:
            try:
                await scan_and_trade(
                    market_indexer=market_indexer,
                    price_fetcher=price_fetcher,
                    risk_manager=risk_manager,
                    db_writer=db,
                    telegram=telegram,
                    paper_trader=paper_trader,
                    cfg=cfg,
                    dedup=dedup,
                )
            except Exception:
                logger.exception("Weather scan failed")
            await asyncio.sleep(cfg.weather_scan_interval)

    async def _trade_resolution_loop() -> None:
        """Periodically check and resolve pending paper trades."""
        while True:
            try:
                await paper_trader.check_pending()
            except Exception:
                logger.exception("Trade resolution failed")
            await asyncio.sleep(10)  # Check every 10s

    tasks = [
        asyncio.create_task(_safe_task(db.run_worker, "DbWriter")),
        asyncio.create_task(
            _safe_task(
                lambda: heartbeat.run(
                    signals_count_fn=lambda: paper_trader.pending_count,
                    win_rate_fn=lambda: reconciler.stats().get("win_rate", 0.0),
                    extra_metrics_fn=lambda: {
                        "Bankroll": f"${risk_manager.bankroll:.2f}",
                        "Exposure": f"${risk_manager.current_exposure:.2f}",
                        "Trades": reconciler.stats().get("total_trades", 0),
                        "PnL": f"${reconciler.stats().get('total_pnl', 0.0):+.2f}",
                        "Pending": paper_trader.pending_count,
                        "DB queue": db.queue_depth,
                    },
                ),
                "Heartbeat",
            ),
        ),
        asyncio.create_task(
            _safe_task(market_indexer.run_refresh_loop, "MarketIndexer"),
        ),
        asyncio.create_task(
            _safe_task(_weather_scan_loop, "WeatherScanner"),
        ),
        asyncio.create_task(
            _safe_task(_trade_resolution_loop, "TradeResolver"),
        ),
        asyncio.create_task(
            _safe_task(
                lambda: _daily_summary_loop(
                    telegram, reconciler, risk_manager, paper_trader,
                ),
                "DailySummary",
            ),
        ),
    ]

    logger.info(
        "Bot running -- mode=%s, bankroll=$%.2f, ensemble=%s",
        cfg.trading_mode,
        cfg.bankroll_usdc,
        cfg.ensemble_model,
    )

    await telegram.send("Weather bot started (paper mode)")

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        logger.info("Tasks cancelled, shutting down...")
    finally:
        heartbeat.stop()
        db.stop()
        for name, coro in [
            ("DbWriter", db.close()),
            ("Telegram", telegram.close()),
            ("PriceFetcher", price_fetcher.close()),
            ("MarketIndexer", market_indexer.close()),
        ]:
            try:
                await coro
            except Exception:
                logger.exception("%s cleanup failed", name)
        logger.info("Shutdown complete.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutdown requested.")
        sys.exit(0)
