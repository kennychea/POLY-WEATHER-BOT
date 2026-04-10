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
from infra.config import (
    BUY_NO_MIN_YES_PRICE,
    MAX_HORIZON_DAYS,
    Config,
)
from infra.db import DbWriter
from infra.heartbeat import Heartbeat
from infra.telegram import TelegramBot
from infra.types import SignalType, WeatherSignal
from market_data.indexer import MarketIndexer
from market_data.price_fetcher import PriceFetcher
from simulator.paper_trader import WeatherPaperTrader
from weather.ensemble import (
    fetch_ensemble_result,
    fetch_multi_model_result,
    get_cache_stats,
    reset_cache_stats,
    resolve_city,
)
from weather.market_scanner import (
    ParsedWeatherMarket,
    get_market_price,
    scan_weather_markets,
)
from weather.probability import (
    compute_spread_score,
    ensemble_probability,
    multi_model_probability,
)

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

    # Wire up market indexer for event-based resolution
    paper_trader.set_market_indexer(market_indexer)

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
    diag: dict[str, int] = {}
    async with __import__("aiohttp").ClientSession() as session:
        for wm in weather_markets:
            try:
                opened = await _evaluate_market(
                    wm, session, price_fetcher, risk_manager,
                    db_writer, paper_trader, cfg, telegram,
                    dedup=dedup, session_cache=session_cache,
                    diag=diag,
                )
                if opened:
                    trades_opened += 1
            except Exception:
                logger.exception(
                    "Error evaluating market: %s",
                    wm.market.get("question", "?"),
                )
                diag["error"] = diag.get("error", 0) + 1

    elapsed = time.perf_counter() - t0
    stats = get_cache_stats()
    mode = "multi-model" if cfg.use_multi_model else "GFS-only"
    diag_str = " ".join(f"{k}={v}" for k, v in sorted(diag.items()))
    logger.info(
        "SCAN complete: %d markets, %d trades, %.1fs [%s] | cache L1=%d L2=%d miss=%d | %s",
        len(weather_markets), trades_opened, elapsed, mode,
        stats["l1_hits"], stats["l2_hits"], stats["misses"],
        diag_str or "all_filtered",
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
    diag: dict[str, int] | None = None,
) -> bool:
    """Evaluate one weather market using ensemble pipeline. Returns True if trade opened."""
    # 1. Verify city is known for ensemble
    city = resolve_city(wm.location)
    if city is None:
        if diag is not None:
            diag["no_city"] = diag.get("no_city", 0) + 1
        return False

    # 1b. Filter past-date and far-future markets BEFORE expensive API call.
    # Open-Meteo has no forecast for past dates and drops accuracy beyond 5 days.
    try:
        from datetime import date as _date
        target = _date.fromisoformat(wm.target_date)
        today_utc = datetime.now(UTC).date()
        days_out = (target - today_utc).days
        if days_out < 0:
            if diag is not None:
                diag["past_date"] = diag.get("past_date", 0) + 1
            return False
        if days_out > MAX_HORIZON_DAYS:
            if diag is not None:
                diag["horizon_far"] = diag.get("horizon_far", 0) + 1
            return False
    except (ValueError, TypeError):
        pass  # bad date format — let downstream handle it

    # 2. Get token IDs (needed for price fetch and trading)
    token_ids = MarketIndexer.extract_token_ids(wm.market)
    if token_ids is None:
        if diag is not None:
            diag["no_tokens"] = diag.get("no_tokens", 0) + 1
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
            if diag is not None:
                diag["no_price"] = diag.get("no_price", 0) + 1
            return False
        yes_price = fetched

    # Filter extreme prices before expensive ensemble API call
    if yes_price < 0.005 or yes_price > 0.995:
        if diag is not None:
            diag["extreme_price"] = diag.get("extreme_price", 0) + 1
        return False

    # 4. Fetch ensemble forecast
    if cfg.use_multi_model:
        # Multi-model: GFS+ECMWF+ICON weighted average
        multi = await fetch_multi_model_result(
            city=city,
            target_date=wm.target_date,
            metric=wm.metric,
            unit=wm.unit,
            session=session,
            session_cache=session_cache,
        )
        if multi is None:
            return False
        model_members = {name: list(er.members) for name, er in multi.items()}
        prob, confidence = multi_model_probability(
            model_members=model_members,
            threshold_low=wm.threshold_low,
            threshold_high=wm.threshold_high,
            direction=wm.direction,
            weights=cfg.model_weights,
        )
        total_member_count = sum(er.member_count for er in multi.values())
        spread_score = compute_spread_score(model_members, weights=cfg.model_weights)
    else:
        # GFS-only: Moon Dev baseline (proven approach)
        er = await fetch_ensemble_result(
            city=city,
            target_date=wm.target_date,
            metric=wm.metric,
            unit=wm.unit,
            session=session,
            session_cache=session_cache,
        )
        if er is None:
            if diag is not None:
                diag["no_ensemble"] = diag.get("no_ensemble", 0) + 1
            return False
        prob, confidence = ensemble_probability(
            list(er.members), wm.threshold_low, wm.threshold_high, wm.direction,
        )
        total_member_count = er.member_count
        spread_score = 1.0  # Single model = full consensus weight

    # 6. Calculate edge (net of fees)
    edge_result = calculate_edge(
        ensemble_prob=prob,
        market_yes_price=yes_price,
        taker_fee_pct=cfg.taker_fee_pct,
        confidence=confidence,
        spread_score=spread_score,
    )
    if edge_result is None:
        if diag is not None:
            diag["no_edge"] = diag.get("no_edge", 0) + 1
        return False

    # 6b. Dedup check — skip if we already have an active position
    if dedup is not None:
        signal_type_val = edge_result.signal_type.value
        market_id = wm.market.get("conditionId", "")
        if dedup.is_active(market_id, signal_type_val):
            logger.debug("SKIP dedup: %s %s", market_id, signal_type_val)
            if diag is not None:
                diag["dedup"] = diag.get("dedup", 0) + 1
            return False

    # P10.1: Disable BUY_YES entirely — 0/9 win rate, -$90.90 in losses
    # BUY_NO alone is 72% WR and profitable. Revisit after 100+ BUY_NO trades.
    if edge_result.signal_type == SignalType.BUY_YES:
        if diag is not None:
            diag["buy_yes_blocked"] = diag.get("buy_yes_blocked", 0) + 1
        return False
    # P10.B.2 / P10.D: BUY_NO asymmetry trap. At YES <= BUY_NO_MIN_YES_PRICE the
    # NO entry costs ~0.85+, so a losing trade burns most of the stake while a
    # win pays only a sliver. At YES=0.15 breakeven WR is ~85%; realized WR on
    # Apr 8 was 50%. The boundary value is BLOCKED (<=, not <).
    if yes_price <= BUY_NO_MIN_YES_PRICE:
        if diag is not None:
            diag["buy_no_longshot_blocked"] = diag.get("buy_no_longshot_blocked", 0) + 1
        return False
    token_id = no_token

    # P10.B.2 (stale price fix): Gamma API caches for ~5 min. By the time we
    # reach this point, the displayed yes_price may be stale vs CLOB. Re-fetch
    # the live YES price and recompute edge so we only open trades whose edge
    # still holds on the order book right now.
    live_yes_price = await price_fetcher.get_price(yes_token)
    if live_yes_price is None:
        logger.warning(
            "SKIP stale_price_fetch_failed market=%s",
            wm.market.get("question", "?")[:60],
        )
        if diag is not None:
            diag["stale_price_fetch_failed"] = diag.get("stale_price_fetch_failed", 0) + 1
        return False

    # Re-apply longshot guard against the live price (boundary BLOCKED)
    if live_yes_price <= BUY_NO_MIN_YES_PRICE:
        if diag is not None:
            diag["buy_no_longshot_blocked"] = diag.get("buy_no_longshot_blocked", 0) + 1
        return False

    # Recompute edge with the live CLOB price (reuses core/edge_calculator,
    # no math duplication). This enforces the same confidence-adjusted and
    # min-net-edge thresholds as the initial check.
    live_edge_result = calculate_edge(
        ensemble_prob=prob,
        market_yes_price=live_yes_price,
        taker_fee_pct=cfg.taker_fee_pct,
        confidence=confidence,
        spread_score=spread_score,
    )
    if live_edge_result is None:
        logger.info(
            "SKIP stale_price_edge_gone stale=%.4f live=%.4f prob=%.4f market=%s",
            yes_price, live_yes_price, prob,
            wm.market.get("question", "?")[:60],
        )
        if diag is not None:
            diag["stale_price_edge_gone"] = diag.get("stale_price_edge_gone", 0) + 1
        return False

    # If the fresh edge flipped direction to BUY_YES, re-apply the Phase 10.1 block.
    if live_edge_result.signal_type == SignalType.BUY_YES:
        if diag is not None:
            diag["buy_yes_blocked"] = diag.get("buy_yes_blocked", 0) + 1
        return False

    # Adopt the fresh edge result for downstream signal + sizing.
    edge_result = live_edge_result
    yes_price = live_yes_price

    # 7. Risk check (spread_score scales Kelly — tight consensus = bigger bet)
    size = risk_manager.size_position(
        estimated_probability=edge_result.ensemble_prob if edge_result.signal_type == SignalType.BUY_YES else 1.0 - edge_result.ensemble_prob,
        market_price=edge_result.market_price,
        spread_score=edge_result.spread_score,
    )
    if size is None or size <= 0:
        logger.debug(
            "SKIP risk_blocked edge=%.4f market=%s",
            edge_result.net_edge, wm.market.get("question", "?")[:60],
        )
        if diag is not None:
            diag["risk_blocked"] = diag.get("risk_blocked", 0) + 1
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
        ensemble_member_count=total_member_count,
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

    # Log per-model forecasts for Brier score calibration
    if cfg.use_multi_model:
        for model_name, model_er in multi.items():
            model_prob, _ = ensemble_probability(
                list(model_er.members), wm.threshold_low, wm.threshold_high, wm.direction,
            )
            await db_writer.enqueue("forecast_log", {
                "city": wm.location,
                "target_date": wm.target_date,
                "metric": wm.metric,
                "model": model_name,
                "member_count": model_er.member_count,
                "probability": model_prob,
                "actual_outcome": None,
                "brier_score": None,
                "market_id": wm.market.get("conditionId", ""),
                "logged_at": signal.timestamp.isoformat(),
            })
    else:
        await db_writer.enqueue("forecast_log", {
            "city": wm.location,
            "target_date": wm.target_date,
            "metric": wm.metric,
            "model": "gfs_seamless",
            "member_count": total_member_count,
            "probability": prob,
            "actual_outcome": None,
            "brier_score": None,
            "market_id": wm.market.get("conditionId", ""),
            "logged_at": signal.timestamp.isoformat(),
        })

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


def _clear_ensemble_cache() -> None:
    """Clear stale L2 ensemble cache on startup for fresh API data."""
    cache_dir = Path("data/ensemble_cache")
    if not cache_dir.exists():
        return
    removed = 0
    for f in cache_dir.glob("*.json"):
        f.unlink(missing_ok=True)
        removed += 1
    if removed:
        logger.info("Cleared %d stale ensemble cache files on startup", removed)


async def main() -> None:
    """Main entry point."""
    logger.info("polymarket weather-bot starting...")
    # Don't clear L2 cache on startup — stale data is better than 429 storms.
    # Cache has TTL-based expiry anyway.

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
        import weather.ensemble as _ens
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
            # Phase 10.D: wall-clock quota check via public API. is_quota_exhausted()
            # auto-clears after the reset time so laptop sleep can't strand us.
            if _ens.is_quota_exhausted():
                wait = _ens.seconds_until_reset()
                if wait > 0:
                    logger.warning(
                        "Open-Meteo daily quota exhausted, sleeping %.1fh until UTC reset",
                        wait / 3600,
                    )
                    await asyncio.sleep(wait + 60)  # +1 min buffer past reset
                    continue
            # Per-minute rate limit cooldown
            if _ens.is_rate_limit_abort_threshold_reached():
                logger.info("Rate limited last scan, extra 5 min cooldown")
                await asyncio.sleep(300)
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
        "Bot running -- mode=%s, bankroll=$%.2f, ensemble=%s, multi_model=%s",
        cfg.trading_mode,
        cfg.bankroll_usdc,
        cfg.ensemble_model,
        cfg.use_multi_model,
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
