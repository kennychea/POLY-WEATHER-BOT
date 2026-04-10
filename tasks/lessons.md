# Lessons Learned

_Updated after every correction or mistake._

## From News Bot (imported)

### Windows compatibility
- ALWAYS set `encoding="utf-8"` on file handlers and `errors="replace"` on console handlers
- Always mock aiohttp.ClientSession at module level in tests
- Use `StrEnum`, `datetime.UTC`, `collections.abc.Callable`
- pathlib.Path partout

### Async patterns
- asyncio.gather with return_exceptions=True for independent tasks
- Wrap background tasks in _safe_task with auto-restart + backoff
- Never await a non-async method
- Requeue items in except block after flush failure

### Trading logic
- Never resolve paper trades using signal direction — use actual price
- Model both entry and exit fees
- Every resource acquired must have a corresponding release
- Validate prices are in valid range (0, 1)
- BUY_YES longshots (entry < 0.10) lose 100% of stake on loss — asymmetric risk kills profitability
- Gamma condition_id migration breaks resolution for ALL weather trades — need slug-based fallback
- Open-Meteo gridded temps ≠ NWS station temps (Polymarket source) — can differ 3-5°F
- Polymarket weather markets resolve ~J+2 05:00-07:00 UTC (43h after endDate)

### Resolution bug (2026-04-09)
- ALL 20 pending Apr 8 trades had Gamma condition_id mismatch — 0/20 auto-resolved
- Root cause: Polymarket migrates condition_ids on weather markets after close
- check_resolution() L1 (cache) misses because closed markets drop from active index
- check_resolution() L2 (API) returns wrong market → added to gamma_mismatch_ids → stuck
- 48h force-resolve loses PnL data (exit=entry, PnL=-fees only)
- Manual resolution via scripts/resolve_april8.py saved real calibration data
