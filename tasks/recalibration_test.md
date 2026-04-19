# Phase D — Hypothesis Testing Report

Counterfactual replay of 72 resolved trades. For each hypothesis, we ask:
*would we have opened this trade, and what PnL would it have produced?*

All results use **leave-one-out cross-validation** where a fitted calibrator
is needed (to avoid in-sample leakage). Filters that don't require fitting
are direct.

**Baseline (current production):** 72 trades, WR 55.6%, PnL −$44.38, PF 0.86.

---

## H1 — Isotonic recalibration (FAIL)

Fit monotone isotonic regression on `(forecast_probability, actual_outcome)` via PAV, then replace raw probability in the bot's edge calculation.

| min_edge threshold | n opened | PnL | WR | PF |
|---|---|---|---|---|
| 0.05 (current) | 60 | −$39.32 | 56.7% | 0.85 |
| 0.08 | 57 | −$87.79 | 54.4% | 0.67 |
| 0.10 | 50 | −$104.43 | 52.0% | 0.57 |
| 0.15 | 31 | −$39.22 | 58.1% | 0.70 |
| 0.20 | 17 | −$39.28 | 52.9% | 0.51 |

**Verdict: REJECTED.** Recalibration alone doesn't flip PnL. Higher edge
thresholds make it worse — because the bot's "strongest signals" (highest
calibrated edge) turn out to be its worst bets. Counterintuitively: the
biggest edges correlate with worse outcomes.

---

## H2 — Side inversion (FAIL, but diagnostic)

What if the bot is so miscalibrated that inverting every trade side would
help? Quick test:

| Strategy | Wins | PnL |
|---|---|---|
| Actual (BUY_NO when bot says low P(YES)) | 40/72 | −$44.38 |
| Inverted (BUY_YES on same signals) | 32/72 | **−$174.20** |

**Verdict: REJECTED.** Inversion is disastrous. The directional signal is
fine in aggregate; the problem is WHICH signals to trust.

---

## H3 — Forecast extremity filter (WINNER)

Observation from Phase B.3: bucket analysis by RAW edge suggested that
moderate-confidence forecasts (forecast ≈ 20%) lose money, while extreme
forecasts (forecast < 10%) held signal. Test: filter by
`forecast_probability < T`.

| Filter | n kept | PnL | WR | PF |
|---|---|---|---|---|
| fc < 5% | 26 | **+$30.12** | 65.4% | **1.33** |
| fc < 8% | 34 | +$1.39 | 58.8% | 1.01 |
| fc < 10% | 39 | −$2.09 | 59.0% | 0.99 |
| fc < 15% | 41 | −$22.29 | 56.1% | 0.88 |
| fc < 20% | 52 | −$16.77 | 57.7% | 0.92 |
| fc < 30% (≈ baseline) | 72 | −$44.38 | 55.6% | 0.86 |

**Verdict: STRONG CANDIDATE.** `fc < 5%` gives a +$74 swing vs baseline
with n=26. Below this threshold, the ensemble model genuinely has signal.
Above it, it's noise.

---

## H3+ — Extremity + entry price gate (BEST)

Combining the forecast filter with a minimum market entry price
(ensures meaningful BUY_NO edge even if calibrated model is close to
market):

| Filter | n kept | PnL | WR | PF |
|---|---|---|---|---|
| **fc < 5% AND entry ≥ 0.55** | **18** | **+$19.91** | **72.2%** | **1.39** |
| fc < 5% AND entry ≥ 0.60 | 16 | +$17.68 | 68.8% | 1.34 |
| fc < 10% AND entry ≥ 0.60 | 21 | +$15.78 | 71.4% | 1.26 |
| fc < 8% AND entry ≥ 0.60 | 19 | +$5.77 | 68.4% | 1.10 |

**Verdict: RECOMMENDED FOR DEPLOYMENT, WITH CAVEATS.**

Best single configuration: `fc < 5% AND entry ≥ 0.55`:
- WR 72.2% (above 59% breakeven)
- PF 1.39 (above 1.3 minimum for tradable)
- Filter rate 75% — most current trades would be SKIPPED

---

## Caveats (mentor mode)

1. **In-sample.** These thresholds were chosen using the same data they're
   measured on. Real out-of-sample performance will be worse. Phase E must
   validate on 50+ NEW trades.

2. **Small n.** 18-26 trades is not enough for tight confidence intervals.
   95% CI on 72% WR with n=18 is [47%, 90%] — wide.

3. **Regime.** All 72 trades are from the CLOB-empty era (gamma_fallback
   executions). The filter may not generalize when CLOB books fill back in.

4. **One-day horizon.** All trades are same-day target dates. Behavior on
   longer horizons unknown until Phase A.3 instrumentation accumulates.

---

## Recommended action

Implement **gate G1**: block any trade where `forecast_probability >= 0.05`.

Optional **gate G2**: additionally require `entry_price >= 0.55` for BUY_NO.

Expected outcome: 75% trade volume reduction, flip from losing to modestly
profitable in paper. Validate over Phase E (50+ new resolved trades).

If Phase E confirms WR ≥ 65% on out-of-sample data, consider live with $50
cap. If WR < 60%, the filter was overfit; return to Phase B.
