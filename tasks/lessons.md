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
- **BUY_NO price band is critical**: Only NO entry ∈ [0.30, 0.70] (YES ∈ [0.30, 0.70]) is profitable.
  Entry >0.75 has breakeven WR >75% but realized ~60% → guaranteed loss. Entry <0.30 is contrarian
  with 0% WR. At 0.85+ entry, even 86% WR loses money because wins are $1 and losses are $10.
  Backtest: band [0.35,0.65] PF=1.59; band [0.30,0.70] PF=1.32. (Applied Phase 11, 2026-04-15.)
- **One outlier can hide a losing strategy**: +$989.90 from a single lottery ticket masked -$164 on
  all other 68 trades. Always compute PnL with AND without top trade. Always check PF by entry range.
- Gamma condition_id migration breaks resolution for ALL weather trades — need slug-based fallback
- Open-Meteo gridded temps ≠ NWS station temps (Polymarket source) — can differ 3-5°F
- Polymarket weather markets resolve ~J+2 05:00-07:00 UTC (43h after endDate)
- **Never disable a strategy component on n<30** — n=9 losers is noise, not
  signal. Route suspect paths to a shadow/observation table that resolves
  with the same logic but zero bankroll impact, then re-evaluate at n≥30.
  Hard kill switches on tiny samples destroy the data you need to decide
  whether the switch was justified. (Applied to P10.1 BUY_YES on 2026-04-11.)

### Resolution bug (2026-04-09)
- ALL 20 pending Apr 8 trades had Gamma condition_id mismatch — 0/20 auto-resolved
- Root cause: Polymarket migrates condition_ids on weather markets after close
- check_resolution() L1 (cache) misses because closed markets drop from active index
- check_resolution() L2 (API) returns wrong market → added to gamma_mismatch_ids → stuck
- 48h force-resolve loses PnL data (exit=entry, PnL=-fees only)
- Manual resolution via scripts/resolve_april8.py saved real calibration data

### Hong Kong slug bug (2026-04-27, Phase 2.1-quater backfill)
- Slug pattern `highest-temperature-in-hong-kong-on-april-25-2026` does not
  resolve the right condition_id on Gamma. 8/472 (1.7%) markets failed in
  Phase 2.1-quater backfill — entire failure cluster is HK April-25.
- Inspect the real Polymarket slug for HK and extend
  `market_data/indexer.py:_QUESTION_CITY_TO_SLUG` (or the slug builder) if
  HK remains in the bot's scanning scope.
- Non-blocking for Phase 2.1-bis Pass 1 (failed=1.7% << 20% STOP gate), but
  must be fixed before Pass 2 to avoid losing HK calibration data.
