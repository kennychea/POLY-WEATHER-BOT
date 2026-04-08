# Phase 7 PLAN — Advanced Weather Models

## Goal
Upgrade probability engine from naive single-model counting to calibrated multi-model system with spread-aware bet sizing.

## Success Criteria
- [ ] main.py uses multi-model (GFS+ECMWF+ICON) instead of single-model GFS
- [ ] Model weights configurable via .env (default: ECMWF 0.5, GFS 0.3, ICON 0.2)
- [ ] Ensemble spread feeds into Kelly fraction (tight spread → bigger bet)
- [ ] Precipitation markets parsed and traded
- [ ] Brier score logged per model for future weight tuning
- [ ] All existing tests still pass + new tests for each subtask

---

## P7.1 — Wire Multi-Model into main.py Pipeline
**Priority: CRITICAL (fixes Phase 6 gap)**
**Files:** `main.py`, `weather/probability.py`
**Risk:** Low — cli_scanner.py already proves this works

### Changes
1. In `_evaluate_market()`: replace `fetch_ensemble_result()` with `fetch_multi_model_result()`
2. Replace `ensemble_probability()` call with `multi_model_probability()`
3. Update signal metadata: `ensemble_member_count` = sum of all model members
4. Update log format to show model count

### Tests
- `test_evaluate_market_uses_multi_model` — verify multi-model is called
- Existing scan_and_trade tests still pass

---

## P7.2 — Skill-Weighted Model Averaging
**Priority: HIGH**
**Files:** `weather/probability.py`, `infra/config.py`
**Risk:** Medium — weights need validation

### Changes
1. Add `MODEL_WEIGHTS` config: `{"ecmwf_ifs025": 0.5, "gfs_seamless": 0.3, "icon_seamless": 0.2}`
2. New env var: `MODEL_WEIGHTS` (JSON string, optional, defaults above)
3. Update `multi_model_probability()` → weighted average instead of simple mean
4. Normalize weights to sum=1.0 when a model fails/is missing
5. When only 1 model available: use single-model probability (graceful degradation)

### Rationale
ECMWF IFS is generally the highest-skill global NWP model (see ECMWF verification reports). GFS is solid but lower resolution. ICON is a good third opinion but DWD-optimized for Europe.

### Tests
- `test_weighted_probability_ecmwf_dominant` — ECMWF gets more weight
- `test_weighted_probability_normalization` — weights normalize when model missing
- `test_weighted_probability_single_model_fallback`
- `test_config_model_weights_from_env`

---

## P7.3 — Spread-Based Confidence → Kelly Adjustment
**Priority: HIGH**
**Files:** `weather/probability.py`, `core/risk.py`, `infra/types.py`
**Risk:** Medium — needs careful tuning to not kill sizing

### Changes
1. Add `spread_score` to probability return: normalized [0,1] where 1 = tight consensus
   - Formula: `1.0 - (std_dev / max_possible_std)` per model, then weighted average
2. Add `spread_score` field to `EdgeResult`
3. In `RiskManager.size_position()`: multiply Kelly by `spread_score`
   - Tight ensemble (spread_score=0.9) → 90% of Kelly bet
   - Loose ensemble (spread_score=0.3) → 30% of Kelly bet
4. Log spread_score in trade signals

### Rationale
When all 122 members agree, we should bet bigger. When they disagree wildly, we should bet smaller. This is the ensemble equivalent of "confidence-weighted Kelly."

### Tests
- `test_spread_score_tight_ensemble` — all same value → score ~1.0
- `test_spread_score_wide_ensemble` — huge range → score ~0.3
- `test_risk_sizing_scales_with_spread`
- `test_spread_score_in_edge_result`

---

## P7.4 — Precipitation Ensemble + Market Parsing
**Priority: MEDIUM**
**Files:** `weather/ensemble.py`, `weather/market_scanner.py`, `weather/probability.py`
**Risk:** Medium — new weather variable, new parsing patterns

### Changes
1. `fetch_ensemble()`: add `precipitation_sum` hourly variable to Open-Meteo query
   - Returns `precipitation_total` per member (daily sum in mm or inches)
2. `market_scanner.py`: add precipitation question patterns
   - "Will it rain in NYC on April 10?"
   - "Will precipitation in NYC exceed 0.5 inches on April 10?"
   - Slug patterns: "rain-", "precipitation-"
3. New metric type: `"precip"` alongside `"temp_high"` / `"temp_low"`
4. `ensemble_probability()` already works for threshold direction — just wire new metric

### Tests
- `test_fetch_ensemble_precipitation` — verify precip members returned
- `test_parse_precipitation_market` — question parsing
- `test_probability_precipitation_threshold`

---

## P7.5 — Historical Calibration Tracking
**Priority: MEDIUM (foundation for future weight tuning)**
**Files:** `infra/db.py`, new `weather/calibration.py`
**Risk:** Low — logging only, no behavioral change

### Changes
1. New DB table: `forecast_log` (city, date, metric, model, members_json, prob, actual_outcome, brier_score)
2. After each trade resolution: log the forecast that generated it + actual outcome
3. `weather/calibration.py`: `log_forecast()` and `compute_brier_scores()` functions
4. CLI addition: `python -m weather.calibration --report` shows per-model Brier scores
5. Future: use Brier scores to auto-tune MODEL_WEIGHTS (Phase 8)

### Tests
- `test_log_forecast` — insert + retrieve
- `test_brier_score_calculation` — known inputs/outputs
- `test_brier_report_by_model`

---

## Execution Order
```
P7.1 (critical gap fix, no deps)
  ↓
P7.2 (depends on P7.1 — needs multi-model in pipeline)
  ↓
P7.3 (depends on P7.2 — needs spread from weighted calc)
  ↓
P7.4 (independent, can run parallel to P7.3)
  ↓
P7.5 (depends on P7.1 — needs multi-model trades to log)
```

## Risk Mitigation
- Each subtask is independently deployable (no big-bang)
- P7.1 is a near-copy of cli_scanner.py logic — proven code
- Model weights start conservative (ECMWF only 50%, not 100%)
- Spread scaling starts with floor of 0.3 (never kills sizing completely)
- Precipitation is additive (new markets, doesn't change temp logic)
