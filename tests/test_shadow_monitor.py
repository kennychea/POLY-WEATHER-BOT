"""Tests for scripts/shadow_monitor.py — Phase 2.1-ter exit gates."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
import sys

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from scripts import shadow_monitor as sm


def _row(
    *,
    ts: datetime | None = None,
    prob: float | None = 0.05,
    outcome: float | None = 1.0,
    city: str = "New York",
    horizon: int | None = 12,
    humidity_mean: float | None = 65.0,
    forecast_date: str | None = None,
    outcome_source: str | None = None,
) -> dict:
    if ts is None:
        ts = datetime.now(UTC) - timedelta(hours=12)
    return {
        "timestamp": ts.isoformat(),
        "forecast_probability": prob,
        "actual_outcome": outcome,
        "location": city,
        "horizon_hours": horizon,
        "humidity_mean": humidity_mean,
        "forecast_date": forecast_date,
        "outcome_source": outcome_source,
    }


def test_empty_db_all_gates_fail() -> None:
    snap = sm.compute_snapshot([])
    assert snap.n_total == 0
    assert snap.fill_ratio == 0.0
    assert not snap.overall_ready
    assert snap.days_remaining_estimate == "bootstrapping"
    assert all(not v for v in snap.exit_gates.values())


def test_bootstrapping_when_uptime_low() -> None:
    """<48h uptime with <100 rows → days_remaining_estimate is the string
    'bootstrapping', not a noisy huge-integer projection."""
    now = datetime.now(UTC)
    rows = [_row(ts=now - timedelta(hours=i)) for i in range(50)]
    snap = sm.compute_snapshot(rows)
    assert snap.days_remaining_estimate == "bootstrapping"
    assert not snap.overall_ready


def test_bootstrapping_when_few_rows_even_with_long_uptime() -> None:
    """<100 rows with >48h uptime still returns bootstrapping."""
    now = datetime.now(UTC)
    rows = [_row(ts=now - timedelta(hours=72)),
            _row(ts=now - timedelta(hours=1))]
    snap = sm.compute_snapshot(rows)
    assert snap.days_remaining_estimate == "bootstrapping"


def test_gates_all_pass_when_population_is_healthy() -> None:
    """Synthetic healthy population — all 6 gates pass, overall_ready True."""
    now = datetime.now(UTC)
    rows: list[dict] = []
    # 10 cities × 5 forecast buckets × 4 horizons × 3 repeats = 600 rows
    cities = [f"City_{i}" for i in range(10)]
    prob_buckets = [0.03, 0.10, 0.22, 0.40, 0.60]  # hits 5 buckets
    horizons = [6, 30, 60, 90]  # hits 4 horizon buckets
    for i, c in enumerate(cities):
        for pb in prob_buckets:
            for h in horizons:
                for rep in range(3):
                    ts = now - timedelta(hours=72 + (i * 40 + rep))
                    rows.append(_row(
                        ts=ts,
                        prob=pb,
                        outcome=1.0 if rep % 2 == 0 else 0.0,
                        city=c,
                        horizon=h,
                        humidity_mean=60.0 + i,
                    ))
    snap = sm.compute_snapshot(rows)
    assert snap.n_total >= 500
    # All resolved → fill_ratio = 1.0
    assert snap.fill_ratio == 1.0
    assert snap.exit_gates["n_total"]
    assert snap.exit_gates["fill_ratio"]
    assert snap.exit_gates["forecast_buckets"]
    assert snap.exit_gates["cities"]
    assert snap.exit_gates["horizons"]
    assert snap.exit_gates["multi_var_ingestion"]
    assert snap.overall_ready


def test_gate_n_total_only_missing() -> None:
    """Everything healthy but <500 rows → overall_ready False."""
    now = datetime.now(UTC)
    rows = []
    # 200 rows spread across gates (would pass bucket/city/horizon distribution
    # for larger n, but n_total fails)
    for i in range(200):
        rows.append(_row(
            ts=now - timedelta(hours=72 + i),
            prob=0.10,
            outcome=1.0,
            city="New York",
            horizon=30,
            humidity_mean=70.0,
        ))
    snap = sm.compute_snapshot(rows)
    assert snap.n_total == 200
    assert not snap.exit_gates["n_total"]
    assert not snap.overall_ready


def test_multi_var_gate_fails_when_most_humidity_null() -> None:
    """>5% rows with humidity_mean NULL → multi_var gate fails,
    silent-logging-failure sentinel."""
    now = datetime.now(UTC)
    rows = []
    # 120 rows, 90% humidity NULL (logging bug simulation)
    for i in range(120):
        rows.append(_row(
            ts=now - timedelta(hours=72 + i),
            humidity_mean=None if i % 10 != 0 else 70.0,
        ))
    snap = sm.compute_snapshot(rows)
    assert snap.multi_var_populated_pct is not None
    assert snap.multi_var_populated_pct < sm.GATE_MULTI_VAR_RATIO
    assert not snap.exit_gates["multi_var_ingestion"]


def test_multi_var_gate_passes_at_98pct_populated() -> None:
    """>=95% humidity populated → gate passes."""
    now = datetime.now(UTC)
    rows = []
    for i in range(120):
        rows.append(_row(
            ts=now - timedelta(hours=72 + i),
            humidity_mean=70.0 if i < 118 else None,  # ~98.3%
        ))
    snap = sm.compute_snapshot(rows)
    assert snap.multi_var_populated_pct >= sm.GATE_MULTI_VAR_RATIO
    assert snap.exit_gates["multi_var_ingestion"]


def test_fill_ratio_gate_fails_when_few_outcomes() -> None:
    """High n_total but few resolutions → fill_ratio gate fails."""
    now = datetime.now(UTC)
    rows = []
    # 600 rows, only 50 resolved = 8.3% < 70%
    for i in range(600):
        rows.append(_row(
            ts=now - timedelta(hours=72 + i),
            outcome=1.0 if i < 50 else None,
        ))
    snap = sm.compute_snapshot(rows)
    assert snap.exit_gates["n_total"]
    assert not snap.exit_gates["fill_ratio"]
    assert not snap.overall_ready


def test_snapshot_to_json_shape() -> None:
    """JSON output contains all expected keys."""
    snap = sm.compute_snapshot([_row()])
    j = sm.snapshot_to_json(snap)
    for key in (
        "generated_at", "n_total", "n_with_outcome", "fill_ratio",
        "uptime_hours", "days_remaining_estimate",
        "coverage_by_forecast_bucket", "coverage_by_city",
        "coverage_by_horizon", "exit_gates", "overall_ready",
    ):
        assert key in j


def test_outcome_source_breakdown_recorded() -> None:
    """coverage_by_outcome_source aggregates polymarket / metar / failed counts."""
    now = datetime.now(UTC)
    rows = [
        _row(ts=now - timedelta(hours=1), outcome_source="polymarket"),
        _row(ts=now - timedelta(hours=1), outcome_source="polymarket"),
        _row(ts=now - timedelta(hours=1), outcome_source="metar"),
        _row(ts=now - timedelta(hours=1), outcome_source="failed", outcome=None),
        _row(ts=now - timedelta(hours=1), outcome_source=None),  # unbackfilled — not counted
    ]
    snap = sm.compute_snapshot(rows)
    assert snap.coverage_by_outcome_source == {
        "polymarket": 2, "metar": 1, "failed": 1,
    }


def test_outcome_age_gate_passes_when_no_unresolved_past_due() -> None:
    """Healthy state: every past-due row has actual_outcome → gate passes (median undefined)."""
    now = datetime.now(UTC)
    yesterday = (now.date() - timedelta(days=1)).isoformat()
    rows = [
        _row(ts=now - timedelta(hours=12), outcome=1.0, forecast_date=yesterday),
        _row(ts=now - timedelta(hours=12), outcome=0.0, forecast_date=yesterday),
    ]
    snap = sm.compute_snapshot(rows)
    assert snap.outcome_resolution_age_days is None
    assert snap.exit_gates["outcome_resolution_age"] is True


def test_outcome_age_gate_fails_when_median_age_exceeds_threshold() -> None:
    """Unresolved past-due rows older than 5 days flip the gate to FAIL."""
    now = datetime.now(UTC)
    very_old = (now.date() - timedelta(days=10)).isoformat()
    rows = [
        _row(ts=now - timedelta(hours=12), outcome=None, forecast_date=very_old),
        _row(ts=now - timedelta(hours=12), outcome=None, forecast_date=very_old),
        _row(ts=now - timedelta(hours=12), outcome=None, forecast_date=very_old),
    ]
    snap = sm.compute_snapshot(rows)
    assert snap.outcome_resolution_age_days == 10.0
    assert snap.exit_gates["outcome_resolution_age"] is False


def test_outcome_age_gate_passes_when_median_within_threshold() -> None:
    now = datetime.now(UTC)
    two_days_ago = (now.date() - timedelta(days=2)).isoformat()
    rows = [
        _row(ts=now - timedelta(hours=12), outcome=None, forecast_date=two_days_ago),
    ]
    snap = sm.compute_snapshot(rows)
    assert snap.outcome_resolution_age_days == 2.0
    assert snap.exit_gates["outcome_resolution_age"] is True


def test_bucket_for_prob_edges() -> None:
    assert sm._bucket_for_prob(0.0) == "<0.05"
    assert sm._bucket_for_prob(0.049) == "<0.05"
    assert sm._bucket_for_prob(0.05) == "0.05-0.15"
    assert sm._bucket_for_prob(0.999) == ">0.95"
    assert sm._bucket_for_prob(None) is None


def test_bucket_for_horizon_edges() -> None:
    assert sm._bucket_for_horizon(0) == "0-24h"
    assert sm._bucket_for_horizon(23) == "0-24h"
    assert sm._bucket_for_horizon(24) == "24-48h"
    assert sm._bucket_for_horizon(100) == ">72h"
    assert sm._bucket_for_horizon(None) is None
