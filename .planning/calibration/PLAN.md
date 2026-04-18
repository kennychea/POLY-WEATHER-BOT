# Calibration Plan — Fix a Losing Bot

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Transform the current losing paper-trade bot (WR 54.9%, PF 0.85 on n=71) into a calibrated, measurably-profitable bot via instrumentation → diagnosis → hypothesis testing → validation.

**Architecture:** Five phases gated by measurable success criteria. Phase A (instrumentation) is a hard prerequisite — the bot currently cannot be diagnosed because the forecast-to-outcome data linkage is broken. Phase B (diagnosis) uses real production data once instrumentation is fixed. Phase C (backtest) enables rapid hypothesis testing offline. Phase D (calibration) iterates on hypotheses. Phase E validates in paper before any live discussion.

**Tech Stack:** Python 3.11+, aiosqlite, pytest (TDD), existing ensemble cache (Open-Meteo GFS), Polymarket Gamma API historical prices.

---

## Current State — What's Broken

**Confirmed bugs blocking calibration:**

1. `weather_signals` table has 0 rows across 162 opened trades. Reason: `main.py:486` only enqueues in the fallback path (`trade is None`). Every successful trade bypasses signal logging.
2. `forecast_log.actual_outcome` is NULL for all 91 rows. Reason: `infra/db.py:443 resolve_forecast()` exists but is never called by the trade resolution code.
3. `paper_trades` schema lacks forecast_probability, ensemble_std, regime, horizon_hours — cannot link a trade's outcome to the forecast that triggered it.
4. 71 already-resolved trades have orphaned forecasts in `forecast_log` (same `market_id` but no backlink).

**Confirmed performance problems (diagnosed after instrumentation):**

- WR 54.9% on n=71, breakeven 59.0% (avg gain $7.03 vs avg loss $10.10)
- Tranches chronologiques dégradent: 75% → 50% → 45% → 45%
- Bucket `[0.50-0.59]`: n=21, WR 42.9%, PnL −$52.83
- Bucket `[0.60-0.69]`: n=41, WR 61.0%, PnL −$29.35 (breakeven-losing)
- Exposition pending $460 vs MAX_EXPOSURE_USDC=200 (separate bug to fix)

---

## Phase A — Instrumentation (HARD PREREQUISITE)

**Goal:** Every trade logs its triggering forecast and resolves with outcome linked back. No calibration possible without this.

**Success criteria (all must be true):**
- [ ] `SELECT COUNT(*) FROM weather_signals` > 0 after any successful trade
- [ ] `SELECT COUNT(*) FROM forecast_log WHERE actual_outcome IS NOT NULL` >= 60 (backfill target)
- [ ] `paper_trades` has columns `forecast_probability`, `ensemble_std`, `regime`, `horizon_hours` populated for new trades
- [ ] Backfill script links 71 existing resolved trades to their forecast where possible
- [ ] Brier score computable: `SELECT AVG(brier_score) FROM forecast_log` returns a number

### Task A.1 — Always log `weather_signals` on successful trade

**Files:**
- Modify: `main.py` around line 482-487 (the conditional enqueue in fallback)
- Test: `tests/test_main.py` (new test)

- [ ] **Step 1: Write failing test**

```python
# tests/test_main.py — add test
async def test_successful_trade_logs_weather_signal(tmp_db, mock_paper_trader):
    """Every successful trade must also log to weather_signals."""
    # Arrange: mock paper_trader.open_trade returns a valid trade
    mock_paper_trader.open_trade = AsyncMock(return_value=FakeTrade(trade_id="t1"))
    db_writer = MockDBWriter()
    signal = make_test_signal(market_id="m1")

    # Act: run the same code path that main.py uses
    await execute_trade_open(signal, db_writer, mock_paper_trader)

    # Assert: weather_signals enqueued regardless of trade success
    signal_enqueues = [i for i in db_writer.enqueued if i[0] == "weather_signals"]
    assert len(signal_enqueues) == 1
    assert signal_enqueues[0][1]["market_id"] == "m1"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_main.py::test_successful_trade_logs_weather_signal -v
```
Expected: FAIL (signal not enqueued on happy path)

- [ ] **Step 3: Fix main.py to always enqueue weather_signals**

Move `await db_writer.enqueue("weather_signals", signal)` OUT of the `if trade is None` block so it runs for both successful and failed trade opens. Current code around line 482-487:

```python
# BEFORE
trade = await paper_trader.open_trade(signal, size)
if trade is None:
    await db_writer.enqueue("weather_signals", signal)
    return False

# AFTER
trade = await paper_trader.open_trade(signal, size)
# Always log signal for calibration analysis
await db_writer.enqueue("weather_signals", signal)
if trade is None:
    return False
```

- [ ] **Step 4: Run test to verify pass**

```bash
python -m pytest tests/test_main.py::test_successful_trade_logs_weather_signal -v
```
Expected: PASS

- [ ] **Step 5: Run full suite (no regression)**

```bash
python -m pytest tests/ -q
```
Expected: 369 passed (was 368, +1 new)

- [ ] **Step 6: Commit**

```bash
git add tests/test_main.py main.py
git commit -m "fix(calibration): always log weather_signals on trade open

Previously only logged in fallback path (trade is None), leaving
weather_signals empty across 162 successful trades. This blocks
calibration analysis."
```

---

### Task A.2 — Add forecast metadata columns to `paper_trades`

**Files:**
- Modify: `infra/db.py:130-145` (paper_trades CREATE TABLE) and `_COLUMNS["paper_trades"]` around line 156-165
- Test: `tests/test_db.py` (new test)

- [ ] **Step 1: Write failing test**

```python
# tests/test_db.py
async def test_paper_trades_has_calibration_columns(tmp_path):
    db = DB(tmp_path / "test.db")
    await db.init()
    async with db._get_conn() as conn:
        cur = await conn.execute("PRAGMA table_info(paper_trades)")
        cols = {row[1] for row in await cur.fetchall()}
    required = {"forecast_probability", "ensemble_std", "regime", "horizon_hours"}
    assert required.issubset(cols), f"missing: {required - cols}"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_db.py::test_paper_trades_has_calibration_columns -v
```
Expected: FAIL (columns missing)

- [ ] **Step 3: Add columns to schema in `infra/db.py`**

Update the `paper_trades` CREATE TABLE in `_SCHEMA` dict to include:

```sql
CREATE TABLE IF NOT EXISTS paper_trades (
    ... existing columns ...,
    forecast_probability REAL,
    ensemble_std REAL,
    regime TEXT,
    horizon_hours INTEGER
)
```

Update `_COLUMNS["paper_trades"]` list to include the 4 new columns.

Add idempotent migration in `DB.init()` after `CREATE TABLE`:

```python
# Calibration columns (idempotent migration for existing DBs)
for col, typ in [
    ("forecast_probability", "REAL"),
    ("ensemble_std", "REAL"),
    ("regime", "TEXT"),
    ("horizon_hours", "INTEGER"),
]:
    try:
        await conn.execute(f"ALTER TABLE paper_trades ADD COLUMN {col} {typ}")
    except Exception:
        pass  # column exists
```

- [ ] **Step 4: Run test to verify pass**

```bash
python -m pytest tests/test_db.py::test_paper_trades_has_calibration_columns -v
```
Expected: PASS

- [ ] **Step 5: Run migration on live DB**

```bash
python -c "
import asyncio
from infra.db import DB
asyncio.run(DB('./data/bot.db').init())
"
python -c "
import sqlite3
c = sqlite3.connect('./data/bot.db').cursor()
c.execute('PRAGMA table_info(paper_trades)')
print([r[1] for r in c.fetchall()])
"
```
Expected: list contains forecast_probability, ensemble_std, regime, horizon_hours

- [ ] **Step 6: Commit**

```bash
git add infra/db.py tests/test_db.py
git commit -m "feat(db): add calibration columns to paper_trades

forecast_probability, ensemble_std, regime, horizon_hours enable
post-hoc analysis linking trade outcomes to their triggering forecast."
```

---

### Task A.3 — Populate calibration columns at trade open

**Files:**
- Modify: `simulator/paper_trader.py` `open_trade()` signature
- Modify: `main.py` call site around line 483 passing metadata
- Test: `tests/test_paper_trader.py` (new test)

- [ ] **Step 1: Write failing test**

```python
async def test_open_trade_persists_calibration_metadata(tmp_db):
    trader = WeatherPaperTrader(tmp_db, bankroll=1000.0, max_bet=10.0)
    signal = make_test_signal(forecast_probability=0.65)
    metadata = {
        "forecast_probability": 0.65,
        "ensemble_std": 0.12,
        "regime": "clob_full",
        "horizon_hours": 24,
    }
    trade = await trader.open_trade(signal, size_usdc=10.0, metadata=metadata)
    async with tmp_db._get_conn() as conn:
        cur = await conn.execute(
            "SELECT forecast_probability, ensemble_std, regime, horizon_hours "
            "FROM paper_trades WHERE trade_id = ?", (trade.trade_id,))
        row = await cur.fetchone()
    assert row == (0.65, 0.12, "clob_full", 24)
```

- [ ] **Step 2: Run test to verify it fails**

Expected: FAIL (open_trade doesn't accept metadata kwarg)

- [ ] **Step 3: Add `metadata` kwarg to `open_trade()` in paper_trader.py**

Accept optional `metadata: dict | None = None` and include its fields in the INSERT. Default to None for all 4 columns when absent.

- [ ] **Step 4: Update main.py caller to pass metadata**

Around line 483, compute regime (from price_fetcher — "clob_full" if spread < 0.20 else "gamma_fallback"), ensemble_std (from `members` list before `ensemble_probability()`), horizon_hours (from `wm.target_date` vs now), and pass as `metadata=...`.

- [ ] **Step 5: Run test + full suite**

```bash
python -m pytest tests/test_paper_trader.py::test_open_trade_persists_calibration_metadata -v
python -m pytest tests/ -q
```

- [ ] **Step 6: Commit**

```bash
git commit -m "feat(trader): persist calibration metadata on open_trade"
```

---

### Task A.4 — Call `resolve_forecast()` on trade resolution

**Files:**
- Modify: wherever trades get resolved — `grep -n "status = .resolved.\|update_trade_status\|resolve" simulator/paper_trader.py core/reconciler.py` to find
- Test: `tests/test_reconciler.py` (new test)

- [ ] **Step 1: Identify resolution code path**

```bash
grep -rn "status.*resolved\|UPDATE paper_trades" simulator/ core/ main.py
```

- [ ] **Step 2: Write failing test**

```python
async def test_resolution_updates_forecast_log(tmp_db):
    # Arrange: forecast_log entry exists with actual_outcome=NULL
    await tmp_db._get_conn().execute("INSERT INTO forecast_log (...) VALUES (...)")
    # Act: resolve a trade for that market_id with outcome=1
    await resolve_trade(tmp_db, market_id="m1", outcome=1)
    # Assert: forecast_log.actual_outcome populated + brier_score computed
    cur = await tmp_db._get_conn().execute(
        "SELECT actual_outcome, brier_score FROM forecast_log WHERE market_id = ?",
        ("m1",))
    row = await cur.fetchone()
    assert row[0] == 1
    assert row[1] is not None
```

- [ ] **Step 3: Add `resolve_forecast()` call in the resolution path**

In the reconciler (or wherever trades get resolved), after updating `paper_trades.status = 'resolved'`, call `await db.resolve_forecast(market_id, actual_outcome)`. `actual_outcome` is 1 if the YES resolved true, 0 otherwise — derive from `outcome` field.

- [ ] **Step 4: Run test + full suite**

- [ ] **Step 5: Commit**

```bash
git commit -m "fix(calibration): populate forecast_log.actual_outcome on resolution"
```

---

### Task A.5 — Backfill existing 71 resolved trades

**Files:**
- Create: `scripts/backfill_calibration.py`
- Test: `tests/test_backfill_calibration.py`

- [ ] **Step 1: Write failing test**

```python
async def test_backfill_links_trades_to_forecasts(tmp_db):
    # Arrange: insert 1 resolved paper_trade (forecast_probability=NULL)
    # Insert matching forecast_log row for same market_id
    # Act: run backfill
    # Assert: paper_trade.forecast_probability now matches forecast_log.probability
    # Assert: forecast_log.actual_outcome populated from trade outcome
```

- [ ] **Step 2: Write backfill script**

```python
# scripts/backfill_calibration.py
"""One-shot: link existing resolved paper_trades to their forecasts.

For each resolved paper_trade:
  1. Lookup matching forecast_log row by market_id (most recent before opened_at)
  2. Copy probability into paper_trades.forecast_probability
  3. Infer outcome (1 if exit_price=1.0 and side=buy_yes, etc.) and UPDATE forecast_log
"""
```

- [ ] **Step 3: Run on live DB (after backup)**

```bash
cp data/bot.db data/bot.db.pre_backfill_20260419
python scripts/backfill_calibration.py
```

- [ ] **Step 4: Verify**

```bash
python -c "
import sqlite3
c = sqlite3.connect('./data/bot.db').cursor()
c.execute('SELECT COUNT(*) FROM paper_trades WHERE status=\"resolved\" AND forecast_probability IS NOT NULL')
print('Trades with forecast:', c.fetchone()[0])
c.execute('SELECT COUNT(*) FROM forecast_log WHERE actual_outcome IS NOT NULL')
print('Forecasts with outcome:', c.fetchone()[0])
"
```
Expected: both numbers >= 60 (allow some trades to not match a forecast)

- [ ] **Step 5: Commit**

```bash
git add scripts/backfill_calibration.py tests/test_backfill_calibration.py
git commit -m "chore(calibration): backfill 71 resolved trades with forecast data"
```

---

### Task A.6 — Phase A exit gate

- [ ] **Step 1: Verify all success criteria**

```bash
python -c "
import sqlite3
c = sqlite3.connect('./data/bot.db').cursor()
c.execute('SELECT COUNT(*) FROM weather_signals'); print('weather_signals:', c.fetchone()[0])
c.execute('SELECT COUNT(*) FROM forecast_log WHERE actual_outcome IS NOT NULL'); print('forecasts resolved:', c.fetchone()[0])
c.execute('SELECT COUNT(*) FROM paper_trades WHERE status=\"resolved\" AND forecast_probability IS NOT NULL'); print('trades linked:', c.fetchone()[0])
"
```

All three numbers must be > 0. `forecasts resolved` and `trades linked` must be >= 60.

- [ ] **Step 2: Run full test suite**

```bash
python -m pytest tests/ -q
```
Expected: all green, at least 4 new tests (A.1-A.5)

- [ ] **Step 3: Let the live bot run 24h and re-verify**

Ensure newly-opened trades are populating all calibration fields correctly.

---

## Phase B — Diagnosis (after Phase A complete)

**Status:** Detailed task breakdown deferred until Phase A outputs are known. See `.planning/calibration/PHASE-B-DIAGNOSIS.md` to be written when Phase A.6 gate passes.

**Goal:** Produce `tasks/diagnostic.md` with quantified answers to:

- **B.1 Calibration reliability** — reliability diagram per 10% bucket; compute Expected Calibration Error (ECE)
- **B.2 PnL attribution** — decompose losses into {fees, slippage, asymmetric payout, miscalibration}
- **B.3 Regime segmentation** — WR/PF split by: {CLOB-full vs gamma_fallback, horizon tranche, city, ensemble_std bucket}

**Success gate:** Three charts + ranked list of top 3 suspected drivers of the −$49 PnL.

---

## Phase C — Backtest Framework (parallelizable with B)

**Status:** Detailed task breakdown deferred. See `.planning/calibration/PHASE-C-BACKTEST.md` to be written at Phase A.6 gate.

**Goal:** Extend `weather/backtest.py` (219 lines exists) to replay historical forecasts against historical Polymarket prices, enabling rapid hypothesis testing without burning paper capital.

**Success gate:** `python -m weather.backtest --replay --from 2026-04-14 --to 2026-04-19` reproduces real PnL ($-48.99) within ±5%.

---

## Phase D — Calibration Hypotheses (after B and C)

**Status:** Detailed task breakdown deferred. Hypotheses to test:

- **H1 — Isotonic recalibration** of raw ensemble probability
- **H2 — Edge threshold scaling** by `ensemble_std`
- **H3 — Fractional Kelly × reliability score** for position sizing
- **H4 — Regime gating** based on Phase B.3 findings

**Success gate:** At least one hypothesis produces backtested PF >= 1.4 and WR >= 62% across full replay period.

---

## Phase E — Paper Validation

**Status:** Runs after D deploys winning config.

**Goal:** Confirm calibrated bot meets go-live criteria.

**Success gate:**

| Metric | Threshold |
|---|---|
| Resolved trades | >= 50 |
| WR | >= 62% |
| IC 95% lower bound | > 55% |
| Profit factor | >= 1.4 |
| Max drawdown | < 15% bankroll |
| Brier score | < 0.20 |

All six must be met simultaneously over a single continuous window. If any fail → return to Phase B with new data.

---

## What This Plan Refuses

- Tuning `MIN_EDGE_THRESHOLD` on n=71 without a backtest (curve-fitting on noise)
- Adding multi-model (ECMWF/ICON) before proving GFS is the bottleneck
- Increasing position size to chase losses
- Going live at any point before Phase E gate is met
- Skipping Phase A because "it's just plumbing" — it's the ONLY thing that enables calibration

---

## Estimated Timeline

| Phase | Duration | Blocker |
|---|---|---|
| A — Instrumentation | 1-2 days | — |
| B — Diagnosis | 1 day | A complete |
| C — Backtest | 2-3 days | A complete (parallel with B) |
| D — Calibration | 2-3 days | B + C complete |
| E — Paper validation | 3-4 weeks | D complete |
| **Total to live discussion** | **~5 weeks** | — |
