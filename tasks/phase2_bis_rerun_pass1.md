# Phase 2.1-bis — Re-run Pass 1 (signal-only, n=464)

_Pass 1 verdict on the post-2.1-quater backfilled population. **No KILL / 2.3 / 2.4 decision is taken on this pass** — Pass 2 (~5–8 days, n cumulé ≥1500) tranche._

## Verdict global

| Metric | Value |
| --- | --- |
| Markets after dedup | 464 |
| Total rows pre-dedup | 19963 |
| Base rate YES (observed) | 0.1918 (19.2%) |
| Base rate NO (observed) | 0.8082 (80.8%) |
| **Brier — model** | 0.1730 |
| **Brier — baseline P(YES)=0.192** | 0.1550 |
| **Δ (model − baseline)** | +0.0180 |
| ECE (10 equal bins) | 0.1404 |
| MCE (10 equal bins) | 0.5510 |
| Reliability (lower = better) | 0.0264 |
| Resolution (higher = better) | 0.0083 |
| Uncertainty (base-rate term) | 0.1550 |
| Monotonicity — equal bins | 0.6140 |
| Monotonicity — quantile bins | 0.8000 |

**Conclusion (signal-only):** **model LOSES to naive baseline** (Δ Brier > 0.005 against model — RED FLAG).
Brier decomposition shows reliability (0.0264) > resolution (0.0083) — the model's calibration error dominates over its discriminating power. Re-calibration (isotonic / Platt) is a lever; gating on a discriminating sub-segment is harder.

## Distribution par forecast_bucket

| bucket | n | mean_forecast (P(YES)) | obs_freq (YES) | obs_freq (NO) | rel | res |
| --- | --- | --- | --- | --- | --- | --- |
| <0.05 | 301 | 0.030 | 0.159 | 0.841 | 0.0167 | 0.0000 |
| 0.05-0.15 | 45 | 0.080 | 0.156 | 0.844 | 0.0084 | 0.0009 |
| 0.15-0.30 | 47 | 0.213 | 0.213 | 0.787 | 0.0276 | 0.0164 |
| 0.50-0.70 | 19 | 0.601 | 0.263 | 0.737 | 0.1766 | 0.0408 |
| 0.70-0.85 | 8 | 0.784 | 0.375 | 0.625 | 0.1718 | 0.0010 |
| 0.30-0.50 | 36 | 0.384 | 0.306 | 0.694 | 0.0078 | 0.0006 |
| >=0.95 | 6 | 0.970 | 0.667 | 0.333 | 0.0918 | 0.0000 |
| 0.85-0.95 | 2 | — | — | — | — | — |

_G1 reference pattern from Phase D: forecast 3 % → outcome ~42 % YES on n=115. Compare the `<0.05` bucket above to that anchor; if `obs_freq (YES)` is still well above 0.05, the original miscalibration finding holds._

## Segments (signal-only — do NOT decide on these numbers)

**Pass 2 (n cumulé ≥1500) is when these segments become actionable.** n<30 segments are flagged 'wait Pass 2'; numbers shown for orientation only.

### City

| segment | bucket | n | mean_fc | obs_freq | Wilson_lo | Wilson_hi | ECE | rel | res | rank | verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|
| city_top15 | Seattle | 13 | 0.147 | 0.231 | 0.082 | 0.503 | 0.0839 | 0.0185 | 0.1076 | 0.1056 | wait Pass 2 |
| city_top15 | San Francisco | 17 | 0.094 | 0.176 | 0.062 | 0.410 | 0.1105 | 0.0414 | 0.0907 | 0.0871 | wait Pass 2 |
| city_top15 | Warsaw | 11 | 0.110 | 0.182 | 0.051 | 0.477 | 0.2204 | 0.0933 | 0.0708 | 0.0648 | wait Pass 2 |
| city_top15 | Buenos Aires | 13 | 0.138 | 0.231 | 0.082 | 0.503 | 0.2284 | 0.0729 | 0.0544 | 0.0507 | wait Pass 2 |
| city_top15 | Helsinki | 14 | 0.128 | 0.214 | 0.076 | 0.476 | 0.1688 | 0.0351 | 0.0515 | 0.0497 | wait Pass 2 |
| city_top15 | New York | 15 | 0.119 | 0.267 | 0.109 | 0.520 | 0.3131 | 0.1285 | 0.0501 | 0.0444 | wait Pass 2 |
| city_top15 | other | 234 | 0.160 | 0.171 | 0.128 | 0.224 | 0.0903 | 0.0154 | 0.0154 | 0.0152 | candidate |
| city_top15 | Tel Aviv | 19 | 0.099 | 0.263 | 0.118 | 0.488 | 0.3078 | 0.1008 | 0.0130 | 0.0118 | wait Pass 2 |
| city_top15 | Moscow | 12 | 0.146 | 0.250 | 0.089 | 0.532 | 0.2854 | 0.0962 | 0.0129 | 0.0118 | wait Pass 2 |
| city_top15 | Chicago | 12 | 0.172 | 0.250 | 0.089 | 0.532 | 0.3712 | 0.1899 | 0.0125 | 0.0105 | wait Pass 2 |
| city_top15 | Miami | 21 | 0.056 | 0.238 | 0.106 | 0.451 | 0.2424 | 0.0595 | 0.0094 | 0.0089 | wait Pass 2 |
| city_top15 | Madrid | 21 | 0.075 | 0.238 | 0.106 | 0.451 | 0.2554 | 0.0699 | 0.0094 | 0.0088 | wait Pass 2 |
| city_top15 | Atlanta | 15 | 0.107 | 0.200 | 0.070 | 0.452 | 0.2101 | 0.0592 | 0.0089 | 0.0084 | wait Pass 2 |
| city_top15 | Los Angeles | 20 | 0.076 | 0.150 | 0.052 | 0.360 | 0.1076 | 0.0143 | 0.0067 | 0.0066 | wait Pass 2 |
| city_top15 | Denver | 14 | 0.104 | 0.143 | 0.040 | 0.399 | 0.1861 | 0.0551 | 0.0056 | 0.0053 | wait Pass 2 |
| city_top15 | Jakarta | 13 | 0.100 | 0.154 | 0.043 | 0.422 | 0.1981 | 0.0606 | 0.0043 | 0.0041 | wait Pass 2 |

### Horizon

| segment | bucket | n | mean_fc | obs_freq | Wilson_lo | Wilson_hi | ECE | rel | res | rank | verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|
| horizon | 0-24h | 381 | 0.155 | 0.152 | 0.120 | 0.192 | 0.0965 | 0.0200 | 0.0139 | 0.0136 | candidate |
| horizon | na | 83 | 0.037 | 0.373 | 0.277 | 0.481 | 0.3421 | 0.1172 | 0.0021 | 0.0019 | candidate |

### Forecast bucket

| segment | bucket | n | mean_fc | obs_freq | Wilson_lo | Wilson_hi | ECE | rel | res | rank | verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|
| forecast_bucket | 0.50-0.70 | 19 | 0.601 | 0.263 | 0.118 | 0.488 | 0.3381 | 0.1766 | 0.0408 | 0.0347 | wait Pass 2 |
| forecast_bucket | 0.15-0.30 | 47 | 0.213 | 0.213 | 0.120 | 0.349 | 0.1631 | 0.0276 | 0.0164 | 0.0160 | candidate |
| forecast_bucket | 0.70-0.85 | 8 | 0.784 | 0.375 | 0.137 | 0.694 | 0.4091 | 0.1718 | 0.0010 | 0.0009 | wait Pass 2 |
| forecast_bucket | 0.05-0.15 | 45 | 0.080 | 0.156 | 0.077 | 0.288 | 0.0848 | 0.0084 | 0.0009 | 0.0009 | candidate |
| forecast_bucket | 0.30-0.50 | 36 | 0.384 | 0.306 | 0.180 | 0.469 | 0.0783 | 0.0078 | 0.0006 | 0.0005 | candidate |
| forecast_bucket | <0.05 | 301 | 0.030 | 0.159 | 0.122 | 0.205 | 0.1292 | 0.0167 | 0.0000 | 0.0000 | candidate |
| forecast_bucket | >=0.95 | 6 | 0.970 | 0.667 | 0.300 | 0.903 | 0.3030 | 0.0918 | 0.0000 | 0.0000 | wait Pass 2 |

## Décision Pass 1

Verdict signal-only ; **décision finale Pass 2 à J+5–8 sur n cumulé ≥1500**.

- Aucune action KILL / 2.3 / 2.4 prise sur cette base.
- La population continue de croître via le bot data-only ; `scripts/shadow_monitor.py` est l'oracle pour déclencher Pass 2.
- Le `outcome_resolution_age` gate et le `coverage_by_outcome_source` restent les sentinelles d'intégrité du backfill.

## Generated artifacts

- `tasks/phase2_bis/pass1/global_histogram.png`
- `tasks/phase2_bis/pass1/global_reliability.png`
- `tasks/phase2_bis/pass1/reliability_by_city.png`
- `tasks/phase2_bis/pass1/reliability_by_forecast_bucket.png`
- `tasks/phase2_bis/pass1/segment_ranking.csv`
- `tasks/phase2_bis_rerun_pass1.md` (this file)
