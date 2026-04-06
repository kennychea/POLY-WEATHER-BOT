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

## Phase 4 — Live Testing
- [ ] P4.1: Get OpenWeatherMap API key (free tier)
- [ ] P4.2: Run bot, verify weather markets are found
- [ ] P4.3: First paper trade opened
- [ ] P4.4: Monitor 24h, check PnL

## Phase 5 — Optimization
- [ ] P5.1: Add more weather APIs (NOAA, AccuWeather) for cross-validation
- [ ] P5.2: Historical backtesting with past weather data
- [ ] P5.3: Dynamic confidence from forecast model comparison
