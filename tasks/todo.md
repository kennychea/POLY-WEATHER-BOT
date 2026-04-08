# Task Tracking — Weather-Based Polymarket Bot

## Phase 1 — Scaffolding DONE
- [x] Project structure copied from news bot
- [x] infra/ — db.py, telegram.py, heartbeat.py, types.py, config.py (adapted)
- [x] core/ — risk.py, reconciler.py
- [x] market_data/ — price_fetcher.py, ws_price_feed.py, indexer.py
- [x] weather/fetcher.py — OpenWeatherMap 5-day forecast
- [x] weather/market_scanner.py — Parse Polymarket weather markets
- [x] weather/probability.py — Forecast → probability (normal CDF)
- [x] main.py — Weather scan loop pipeline

## Phase 1.5 — News-bot Cleanup DONE
- [x] infra/db.py — Schema adapte (weather_signals, paper_trades avec market_question)
- [x] simulator/paper_trader.py — WeatherPaperTrader (remplace NewsPaperTrader)
- [x] tests/test_db.py — Tous les tests adaptes pour weather types
- [x] core/risk.py — size_news_position() → size_position()
- [x] tests/test_risk.py — References news supprimees
- [x] weather/market_scanner.py — Annee dynamique (plus de hardcode 2026)
- [x] market_data/indexer.py — Categorie "weather" ajoutee
- [x] pyproject.toml — Renomme polymarket-weather-bot
- [x] 96/96 tests verts, 0 references news-bot restantes

## Phase 2 — Ensemble Engine DONE
- [x] P2.1: weather/ensemble.py — Async Open-Meteo fetcher (31 GFS members)
- [x] P2.2: weather/probability.py — ensemble_probability() member counting
- [x] P2.3: weather/market_scanner.py — Moon Dev bucket/exact/threshold parsing
- [x] P2.4: core/edge_calculator.py — Edge calc net of fees → EdgeResult
- [x] P2.5: main.py — Ensemble pipeline wired (scan→ensemble→prob→edge→trade)
- [x] P2.6: Paper trader wired into scan loop + trade resolution loop
- [x] P2.7: Tests — test_ensemble, test_probability_ensemble, test_market_scanner_v2, test_edge_calculator
- [x] 168/168 tests pass (96 existing + 72 new)

## Phase 2.5 — Moon Dev Gap Closure DONE
- [x] P2.8: Slug filtering (highest-temperature/lowest-temperature in slug)
- [x] P2.9: Date fallback to tomorrow (was "unknown", broke ensemble fetch)
- [x] P2.10: Fahrenheit "f?" pattern detection aligned with scan_edge.py
- [x] P2.11: Early price filtering before ensemble fetch (saves API calls)
- [x] P2.12: weather/cli_scanner.py — Standalone CLI (--city, --min-edge, --json, -v)
- [x] P2.13: Tests for gaps + CLI display
- [x] 177/177 tests pass

## Phase 3 — Paper Trader Adaptation (mostly done in Phase 2)
- [x] P3.1: simulator/paper_trader.py already adapted for WeatherSignal
- [x] P3.2: Paper trader wired into main.py scan loop + resolution loop
- [ ] P3.3: DB schema verification for weather_signals + paper_trades

## Phase 4 — Live Testing DONE
- [x] P4.1: CLI scanner verified — 307 weather markets, 41 edges
- [x] P4.2: Bot finds 144 weather markets from 4500+ indexed
- [x] P4.3: First paper trades opened (8 per cycle)
- [x] P4.4: 16 resolved trades, PnL=+$11.34, 4W/12L

## Phase 5 — Optimization + Real Resolution DONE
- [x] P5.1.1: DB dedup table (traded_positions) + CRUD methods
- [x] P5.3.1: Session-level L1 ensemble cache + cache stats logging
- [x] P5.1.2: In-memory dedup set in scan loop + DB persistence
- [x] P5.5.1: Telegram alerts (edge, resolution, daily summary)
- [x] P5.2.1: MarketIndexer.check_resolution() + MarketResolution type
- [x] P5.2.2: Event-based trade resolution (Gamma closed check, 7-day force-resolve)
- [x] P5.4.1: Backtest framework (Brier score, confidence bucketing, edge correlation)
- [x] 217/217 tests pass

## Phase 6 — Multi-Model Ensemble Integration DONE
- [x] P6.1: Refactor fetch_ensemble() to accept model parameter
- [x] P6.2: Add fetch_multi_model_ensemble() — parallel GFS+ECMWF+ICON via asyncio.gather
- [x] P6.3: Add fetch_multi_model_result() — high-level wrapper with L1 cache
- [x] P6.4: Add multi_model_probability() — per-model averaging + disagreement confidence
- [x] P6.5: Wire multi-model into main.py pipeline (_evaluate_market)
- [x] P6.6: Wire multi-model into cli_scanner.py
- [x] P6.7: Tests — 19 new tests (model param, multi-fetch, partial failure, probability blending)
- [x] 236/236 tests pass

## Phase 7 — Fix Bot: Zero Trades, Rate Limiting, Continuous Running
- [x] P7.1: Switch to GFS-only pipeline by default (multi-model kills signal)
- [x] P7.2: Add USE_MULTI_MODEL config toggle (default=false, keep multi-model code)
- [x] P7.3: Add diagnostic logging (SCAN_DIAG: no_city, extreme_price, no_edge, risk_blocked, dedup)
- [x] P7.4: Rate limiting for Open-Meteo API (semaphore=2, 300ms delay, 429 retry with backoff)
- [x] P7.5: Persistent Gamma mismatch suppression (don't clear on refresh)
- [x] P7.6: Force-resolve orphaned trades (Gamma mismatch + 48h old)
- [x] P7.7: Auto-restart run_bot.bat (restart loop, visible console)
- [x] 256/256 tests pass

## Phase 8 — Calibration + Bug Fixes DONE
- [x] P8.1: BUG FIX — Removed `_check_near_settled()` (false early resolution killed BUY_NO trades)
- [x] P8.2: MIN_EDGE 3%→5% in config, added `_MIN_NET_EDGE=0.02` in edge_calculator
- [x] P8.3: Laplace smoothing `(count+1)/(n+2)` in ensemble_probability(), passthrough in multi_model
- [x] P8.4: Confidence-adjusted edge: high≥3%, medium≥5%, low=rejected
- [x] P8.5: Forecast horizon ≤5 days in fetch_ensemble_result()
- [x] P8.6: Tighter confidence thresholds: high>0.35, medium>0.20 (was 0.30/0.15)
- [x] 258/258 tests pass

### Phase 8 Audit Results (2026-04-08)
- **Tests**: 258/258 pass
- **Bug fix confirmed**: No "near_settled" in bot resolution logs
- **Bot scan**: 760 markets, 0 new trades (53 pending), diagnostics: extreme_price=317, no_edge=273, risk_blocked=141, dedup=29
- **CLI scanner**: 27 edges found at 5% threshold (was ~41 before)
- **Bug found & fixed**: `fetch_multi_model_result()` missing horizon check — added
- **Known discrepancy**: CLI scanner always uses multi-model + no fee/confidence filter (diagnostic tool, not trading path)

## Phase 9 — Clean Paper Trading Evaluation IN PROGRESS
- [x] P9.1: Audit found .env override: MIN_EDGE_THRESHOLD=0.03 (Phase 8's 5% was NEVER active!)
- [x] P9.2: Archived contaminated DB → data/bot_archive_pre_phase9.db (53 resolved, 97 pending, all pre-P8)
- [x] P9.3: Fixed .env: MIN_EDGE=0.05, MAX_EXPOSURE=200, MAX_BET=10
- [x] P9.4: CLI scanner switched to GFS-only (was multi-model)
- [x] P9.5: Added horizon check to fetch_multi_model_result() (bug from audit)
- [x] P9.6: Added 10 new tests — Laplace smoothing (3), confidence-adjusted edge (6), multi-model horizon (1)
- [x] 268/268 tests pass
- [ ] P9.7: Run bot fresh with clean DB — accumulate Phase 8+ trades
- [ ] P9.8: Checkpoint 1 at 5 clean trades — verify Laplace, confidence, edge ≥5%
- [ ] P9.9: Checkpoint 2 at 15 clean trades — win rate trend, edge correlation
- [ ] P9.10: Checkpoint 3 at 30 clean trades — Go/No-Go for live (target 60%+)
