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
- [x] P9.7: Bot running, 20 trades opened Apr 8, manually resolved (10W/10L, -$47)
- [ ] P9.8: Checkpoint 1 at 5 clean trades — verify Laplace, confidence, edge ≥5%
- [ ] P9.9: Checkpoint 2 at 15 clean trades — win rate trend, edge correlation
- [ ] P9.10: Checkpoint 3 at 30 clean trades — Go/No-Go for live (target 60%+)

## Phase 10 — Critical Fixes (PRIORITY)

### P10.1: Filter BUY_YES longshots DONE → REPLACED BY SHADOW MODE (2026-04-11)
- [x] In `main.py` `_evaluate_market()` line 337: reject BUY_YES when yes_price < 0.10
- [x] Add `diag["longshot_filtered"]` counter
- [x] Apr 8 data: 7/7 BUY_YES trades LOST = -$70.70 (all longshots entry 0.005-0.075)
- [x] **Superseded** by shadow mode (P10.1.SHADOW below). n=9 was statistically
      indefensible; hard kill switch replaced with observation-only routing.

### P10.1.SHADOW: BUY_YES shadow mode (2026-04-11)
- [x] New `shadow_trades` table in `infra/db.py` (no bankroll impact)
- [x] `WeatherPaperTrader.open_shadow_trade()` — observes outcome, never
      touches `risk_manager` / `reconciler` / `paper_trades`
- [x] `main.py` `_evaluate_market`: both BUY_YES sites (pre- and post-stale-
      price refetch) now call `open_shadow_trade` and increment
      `diag["shadow_buy_yes_opened"]` instead of `buy_yes_blocked`
- [x] `check_pending()` resolves shadow trades with same Gamma logic but no
      bankroll side-effects; 7-day force-resolve path included
- [x] `load_pending_shadow_trades()` wired into startup so restarts don't
      orphan pending shadow observations
- [x] SCAN complete log auto-includes `shadow_buy_yes_opened=X` via diag_str
- [x] 5 new tests (1 main + 2 db + 2 resolution); 361/361 tests green
- [x] Goal: collect n≥30 BUY_YES samples for honest WR calibration before
      deciding to re-enable or permanently kill

### P10.2: Fix resolution via slug fallback DONE
- [x] `check_resolution()` now accepts `market_question` param (backward compatible)
- [x] When condition_id is in `gamma_mismatch_ids`, builds event slug from question text
- [x] `_build_slug_from_question()`: extracts metric/city/date → slug string
- [x] `_resolve_via_slug()`: fetches event by slug, matches sub-market by question text
- [x] Also triggers slug resolution on first-time L2 API mismatch (immediate, no wait)
- [x] `paper_trader.py` passes `trade.market_question` to `check_resolution()`
- [x] 13 new tests: 9 slug builder + 4 resolution integration
- [x] 325/325 tests pass

### P10.3: Dashboard + cities (DONE in this session)
- [x] Moon Dev-style scanner panel (City, 24h Vol, Bucket, Forecast, Mkt Fav, Edge)
- [x] Pending trades show full market names (no truncation)
- [x] Resolved trades panel added
- [x] Scrollable CLI: --pending, --resolved, --trades
- [x] 12 new cities added (31 total): Toronto, Buenos Aires, Wellington, Moscow,
      Mexico City, Singapore, Jakarta, Kuala Lumpur, Sao Paulo, Amsterdam, Taipei, Istanbul
- [x] Files changed: dashboard.py, ensemble.py, market_scanner.py, indexer.py
- [x] 29/29 dashboard tests pass

### P10.4: Stale price fix (Phase 10.B.2) DONE
- [x] `_evaluate_market()` re-fetches live CLOB YES price via `PriceFetcher.get_price(yes_token)`
      immediately after the BUY_NO longshot guard and before risk sizing.
- [x] Edge recomputation reuses `core.edge_calculator.calculate_edge` — zero math duplication.
      Same confidence-adjusted thresholds and `_MIN_NET_EDGE` apply to the live check.
- [x] Three new diag counters + exit paths:
      * `stale_price_fetch_failed` — CLOB returned None (book empty / request failed)
      * `stale_price_edge_gone` — live edge dropped below threshold
      * flipped-direction BUY_YES re-routes into existing `buy_yes_blocked`
- [x] Live longshot guard re-applied — if live yes_price < 0.15, still a trap.
- [x] Signal now carries the LIVE `edge_result.market_price` as entry (not stale Gamma).
- [x] 3 new tests in `tests/test_main.py`: rejection, fetch failure, happy-path with live entry.
- [x] Existing `_make_eval_kwargs` fixture fixed — `price_fetcher.get_price` now returns a
      float (was returning the raw JSON string, a latent bug the refetch exposed).
- [x] 351/351 tests pass (was 341 baseline).

### P10.C: Coverage gaps (DONE 2026-04-10)
Diagnostic showed 636/1062 markets skipped as "no_ensemble" — city mismatches
and a regex that couldn't parse negative temps. Three independent fixes:

- [x] **Fix 1 — Negative-temperature regex** (`weather/market_scanner.py`):
      bucket / threshold / exact patterns now match `-?\d+` so Moscow winter
      markets like "-1°C or lower" parse correctly. Bucket separator also
      accepts "to" in addition to "-".
- [x] **Fix 2 — 13 new cities** added to ensemble.py (US_CITIES + CITY_ALIASES),
      market_scanner.py (CITY_ALIASES), indexer.py (_WEATHER_CITY_SLUGS +
      _QUESTION_CITY_TO_SLUG), and dashboard.py (_WEATHER_CITY_SLUGS):
      Tel Aviv, Panama City, Warsaw, Munich, Helsinki, Lucknow, Ankara,
      Milan, Shenzhen, Chongqing, Wuhan, Busan, Chengdu. Total 44 cities.
- [x] **Fix 3 — Removed risk.py min_edge double-filter**: `core/risk.py`
      now forces `self._min_edge = 0.0` regardless of constructor arg.
      `core/edge_calculator.py` already enforces confidence-tiered gates
      (P8.4: high >=3%, medium >=5%, low rejected). The second raw-edge
      check in risk.py was defeating the high-confidence 3% band because
      `main.py` was passing `cfg.min_edge_threshold=0.05`. Test
      `test_size_position_min_edge_noop` documents the new behavior.
- [x] Tests: 351 → 351 (added 3 negative-temp tests + renamed 2 risk tests;
      P10.4 already moved the baseline to 351, and new tests offset renames).
      All green.

### Phase 10.D — Rate-limit encapsulation + wall-clock quota (DONE 2026-04-10)
Code-review pass surfaced four interrelated defects in the Phase 10.B
quota-awareness layer. All four shipped in one cohesive commit because
they touch the same module state.

- [x] **FIX 1 — Wall-clock quota reset** (`weather/ensemble.py`):
      `_QUOTA_RESET_AT` was a `time.monotonic()` offset. On Windows
      laptop sleep/hibernate the monotonic clock pauses while wall-clock
      advances, stranding the bot in `daily_quota_exhausted=True` past
      the real 00:05 UTC reset. Now stored as a UTC `datetime` and
      compared against `datetime.now(UTC)`. `is_quota_exhausted()`
      auto-clears the flag when reset time has passed.

- [x] **FIX 2 — BUY_NO longshot off-by-one** (`main.py`): comment said
      "86% breakeven at YES=0.15" but code used `< 0.15`. Switched to
      `<= BUY_NO_MIN_YES_PRICE` (0.15 BLOCKED). At YES=0.15 the math
      gives breakeven WR = 0.85/(0.85+0.15) = 85%, same trap. Test
      `test_buy_no_longshot_boundary_blocks_exact_015` and
      `test_buy_no_allowed_just_above_015` pin the boundary.

- [x] **FIX 3 — Encapsulate rate-limit state** (`weather/ensemble.py`,
      `main.py`, `tests/test_ensemble.py`): replaced 5 module globals
      with a single `_RateLimitState` dataclass `_STATE`. Public API:
      `is_quota_exhausted()`, `seconds_until_reset()`,
      `is_rate_limit_abort_threshold_reached()`, plus `_STATE` methods
      `should_skip_city`, `mark_city_failed`, `mark_429`,
      `reset_429_count`, `mark_daily_quota_exhausted`, `reset_cycle`.
      `main.py`'s `_weather_scan_loop` no longer reaches into
      `_ens._DAILY_QUOTA_EXHAUSTED` etc. Test fixture rewritten to
      reset `_STATE` directly.

- [x] **FIX 4 — Constants to `infra/config.py`**: added 4 env-overridable
      module-level constants (defaults match prior hardcoded values, so
      no `.env` migration is needed):
      * `BUY_NO_MIN_YES_PRICE = 0.15`
      * `MAX_HORIZON_DAYS = 5`
      * `ENSEMBLE_CACHE_TTL_SECS = 14400`
      * `RATE_LIMIT_ABORT_THRESHOLD = 15`
      Imported by `weather/ensemble.py` (CACHE_TTL, MAX_HORIZON_DAYS,
      `_429_ABORT_THRESHOLD`) and `main.py` (BUY_NO_MIN_YES_PRICE,
      MAX_HORIZON_DAYS).

- [x] Tests: 351 → 353 (added 2 boundary tests, kept all Phase 10.B
      tests passing after fixture migration). All green.

## Session 2026-04-09 Summary
- Shutdown recovery: bot restarted fine, 20 trades reloaded from DB
- Manual resolution: scripts/resolve_april8.py resolved 20 stuck trades with real Polymarket data
- Results: 10W/10L, -$47.06, 50% WR, profit factor 0.53
- Root cause: Gamma condition_id migration + BUY_YES longshot asymmetry
- Dashboard upgraded: Moon Dev scanner, full trade names, scrollable CLI, 12 new cities
