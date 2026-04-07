# Phase 5 — Optimization + Real Resolution

## Goal
Transform the paper trading bot from a demo into a realistic 24h runner: one position per market, real resolution when markets close, efficient API usage, and verifiable PnL.

## Requirements

### P5.1: Trade Deduplication
- Track conditionId + signal_type pairs already traded
- Don't re-open if position is still pending
- In-memory set in scan loop + DB check for persistence across restarts
- Key: `(market_id, signal_type)` — one position per market per direction

### P5.2: Real Market Resolution
- Check Gamma API for market closed=true and resolutionPrice
- Replace current fake "exit = Gamma price 30s later" approach
- Resolution loop should poll resolved markets, not just time-delay
- Record resolution_source for each trade (gamma_resolved, gamma_cache_fallback)
- Handle edge cases: market not yet resolved → keep pending, market disappeared → force-resolve with entry price

### P5.3: Ensemble Cache Optimization
- Don't re-fetch Open-Meteo if target_date + city combo already fetched this cycle
- Session-level in-memory cache passed through scan_and_trade()
- Existing file cache (30min TTL) stays as L2 cache
- Log cache hits/misses for monitoring

### P5.4: Backtest Framework
- Compare predicted edges vs actual outcomes on resolved markets
- Query calibration_log for trades with known resolution
- Compute: hit rate by confidence level, edge accuracy, Brier score
- CLI command: `python -m weather.backtest` or method on Reconciler

### P5.5: Telegram Alerts (Optional)
- Send alert when new edge found above threshold
- Send alert when trade resolves (win/loss + PnL)
- Daily summary: trades opened, resolved, PnL, win rate
- Skip if no Telegram credentials configured

## Current Architecture (key files)
- `main.py:126-167` — scan_and_trade() loop, _evaluate_market()
- `simulator/paper_trader.py:117-172` — open_trade(), 174-313 — resolution
- `market_data/indexer.py:120-159` — market refresh cycle
- `weather/ensemble.py:74-94` — file-based cache, 101-182 — fetch
- `infra/types.py:62-99` — WeatherSignal, WeatherPaperTrade
- `infra/config.py:96-128` — trading parameters
- `infra/db.py:22-89` — schema, 245-265 — pending/resolved queries

## Constraints
- Don't break existing 177 tests
- Don't modify infra/types.py frozen dataclasses unnecessarily — extend via new fields with defaults
- Gamma API is the only reliable price source for weather markets (CLOB spread ~0.998)
- Keep paper mode working without Telegram or Polymarket API keys
