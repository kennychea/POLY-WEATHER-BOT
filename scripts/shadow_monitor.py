"""scripts/shadow_monitor.py — Phase 2.1-ter data-only collection monitor.

Reads weather_signals from data/bot.db and reports the 6 exit gates that
decide when to re-run Prompt 2.1-bis on the pre-gate population.

Usage:
    python scripts/shadow_monitor.py [--db PATH] [--json-out PATH]

Outputs:
    - Console: human-readable summary + per-gate status
    - JSON: machine-readable snapshot (default data/shadow_status.json)

Exit gates (all must be true for overall_ready):
    1. n_total >= 500
    2. fill_ratio = n_with_outcome / n_total >= 0.70
    3. >= 5 forecast buckets with n>=30
    4. >= 10 cities with n>=10
    5. >= 3 horizons with n>=30
    6. multi_var_ingestion: >=95% of rows post-step-1.5 have
       humidity_mean NOT NULL (sentinel for silent logging failures)

Bootstrapping: if uptime < 48h OR n_total < 100, days_remaining_estimate
returns the string "bootstrapping" instead of a (noisy) projection.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


FORECAST_BUCKETS: list[tuple[str, float, float]] = [
    ("<0.05", 0.0, 0.05),
    ("0.05-0.15", 0.05, 0.15),
    ("0.15-0.30", 0.15, 0.30),
    ("0.30-0.50", 0.30, 0.50),
    ("0.50-0.70", 0.50, 0.70),
    ("0.70-0.85", 0.70, 0.85),
    ("0.85-0.95", 0.85, 0.95),
    (">0.95", 0.95, 1.01),
]


HORIZON_BUCKETS: list[tuple[str, int, int]] = [
    ("0-24h", 0, 24),
    ("24-48h", 24, 48),
    ("48-72h", 48, 72),
    (">72h", 72, 10_000),
]


# Gate thresholds — adjust in one place
GATE_N_TOTAL = 500
GATE_FILL_RATIO = 0.70
GATE_BUCKETS_MIN = 5
GATE_BUCKET_N = 30
GATE_CITIES_MIN = 10
GATE_CITY_N = 10
GATE_HORIZONS_MIN = 3
GATE_HORIZON_N = 30
GATE_MULTI_VAR_RATIO = 0.95
# Phase 2.1-quater: backfill freshness gate
GATE_OUTCOME_AGE_MAX_DAYS = 5
BOOTSTRAP_MIN_HOURS = 48
BOOTSTRAP_MIN_ROWS = 100


@dataclass
class MonitorSnapshot:
    n_total: int = 0
    n_with_outcome: int = 0
    fill_ratio: float = 0.0
    coverage_by_forecast_bucket: dict[str, int] = field(default_factory=dict)
    coverage_by_city: dict[str, int] = field(default_factory=dict)
    coverage_by_horizon: dict[str, int] = field(default_factory=dict)
    days_remaining_estimate: Any = "bootstrapping"
    uptime_hours: float = 0.0
    ingestion_rate_last_3d: float | None = None
    multi_var_populated_pct: float | None = None
    # Phase 2.1-quater: backfill source attribution + freshness
    coverage_by_outcome_source: dict[str, int] = field(default_factory=dict)
    outcome_resolution_age_days: float | None = None
    exit_gates: dict[str, bool] = field(default_factory=dict)
    overall_ready: bool = False
    warnings: list[str] = field(default_factory=list)


def _bucket_for_prob(prob: float | None) -> str | None:
    if prob is None:
        return None
    for name, lo, hi in FORECAST_BUCKETS:
        if lo <= prob < hi:
            return name
    return None


def _bucket_for_horizon(horizon: int | None) -> str | None:
    if horizon is None:
        return None
    for name, lo, hi in HORIZON_BUCKETS:
        if lo <= horizon < hi:
            return name
    return None


def _load_rows(db_path: Path) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT timestamp, forecast_probability, actual_outcome, "
            "location, horizon_hours, humidity_mean, "
            "forecast_date, outcome_source "
            "FROM weather_signals"
        )
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows
    except sqlite3.OperationalError:
        # Table missing or column missing (pre-migration DB)
        return []


def _parse_timestamp(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def compute_snapshot(rows: list[dict[str, Any]]) -> MonitorSnapshot:
    """Pure function: takes raw weather_signals rows → MonitorSnapshot.

    Kept pure so tests can feed synthetic fixtures without touching SQLite.
    """
    snap = MonitorSnapshot()
    snap.n_total = len(rows)

    if not rows:
        snap.exit_gates = {
            "n_total": False, "fill_ratio": False, "forecast_buckets": False,
            "cities": False, "horizons": False, "multi_var_ingestion": False,
            "outcome_resolution_age": False,
        }
        return snap

    # Outcomes + fill ratio
    snap.n_with_outcome = sum(
        1 for r in rows if r.get("actual_outcome") is not None
    )
    snap.fill_ratio = snap.n_with_outcome / snap.n_total

    # Coverage aggregations
    for r in rows:
        bucket = _bucket_for_prob(r.get("forecast_probability"))
        if bucket:
            snap.coverage_by_forecast_bucket[bucket] = (
                snap.coverage_by_forecast_bucket.get(bucket, 0) + 1
            )
        city = r.get("location") or "unknown"
        snap.coverage_by_city[city] = snap.coverage_by_city.get(city, 0) + 1
        hbucket = _bucket_for_horizon(r.get("horizon_hours"))
        if hbucket:
            snap.coverage_by_horizon[hbucket] = (
                snap.coverage_by_horizon.get(hbucket, 0) + 1
            )

    # Uptime + ingestion rate
    timestamps = [
        ts for ts in (_parse_timestamp(r.get("timestamp")) for r in rows)
        if ts is not None
    ]
    if timestamps:
        first = min(timestamps)
        last = max(timestamps)
        span = (last - first).total_seconds() / 3600.0
        snap.uptime_hours = span
        # Ingestion rate on last 3 days
        cutoff = last - timedelta(days=3)
        recent = [t for t in timestamps if t >= cutoff]
        window_hours = min((last - (recent[0] if recent else last)).total_seconds() / 3600.0, 72.0)
        if window_hours > 0 and len(recent) >= 2:
            snap.ingestion_rate_last_3d = len(recent) / (window_hours / 24.0)

    # days_remaining_estimate with bootstrapping fallback
    if snap.uptime_hours < BOOTSTRAP_MIN_HOURS or snap.n_total < BOOTSTRAP_MIN_ROWS:
        snap.days_remaining_estimate = "bootstrapping"
    elif snap.ingestion_rate_last_3d and snap.ingestion_rate_last_3d > 0:
        needed = max(0, GATE_N_TOTAL - snap.n_total)
        snap.days_remaining_estimate = round(needed / snap.ingestion_rate_last_3d, 1)
    else:
        snap.days_remaining_estimate = "bootstrapping"

    # Multi-var ingestion ratio — sentinel for silent logging failures
    multi_var_rows = [r for r in rows if r.get("humidity_mean") is not None]
    snap.multi_var_populated_pct = len(multi_var_rows) / snap.n_total

    # Phase 2.1-quater: outcome source breakdown
    for r in rows:
        src = r.get("outcome_source")
        if src:
            snap.coverage_by_outcome_source[src] = (
                snap.coverage_by_outcome_source.get(src, 0) + 1
            )

    # Phase 2.1-quater: median age (days) of unresolved past-due rows.
    # Backfill should keep this near zero; a high value flags a stalled backfill loop.
    today = datetime.now(UTC).date()
    unresolved_ages: list[int] = []
    for r in rows:
        if r.get("actual_outcome") is not None:
            continue
        fdate = r.get("forecast_date")
        if not fdate:
            continue
        try:
            d = datetime.fromisoformat(fdate).date()
        except (ValueError, TypeError):
            continue
        age = (today - d).days
        if age > 0:  # past-due
            unresolved_ages.append(age)
    if unresolved_ages:
        unresolved_ages.sort()
        mid = len(unresolved_ages) // 2
        snap.outcome_resolution_age_days = (
            float(unresolved_ages[mid])
            if len(unresolved_ages) % 2 == 1
            else (unresolved_ages[mid - 1] + unresolved_ages[mid]) / 2.0
        )

    # Low-ingestion warning (DATA_ONLY ON but rate dropped)
    if (
        snap.uptime_hours > BOOTSTRAP_MIN_HOURS
        and snap.ingestion_rate_last_3d is not None
        and snap.ingestion_rate_last_3d < 10
    ):
        snap.warnings.append(
            "WARNING: low ingestion rate — check scanner / instrumentation"
        )

    # Exit gates
    buckets_ok = sum(
        1 for n in snap.coverage_by_forecast_bucket.values() if n >= GATE_BUCKET_N
    ) >= GATE_BUCKETS_MIN
    cities_ok = sum(
        1 for n in snap.coverage_by_city.values() if n >= GATE_CITY_N
    ) >= GATE_CITIES_MIN
    horizons_ok = sum(
        1 for n in snap.coverage_by_horizon.values() if n >= GATE_HORIZON_N
    ) >= GATE_HORIZONS_MIN

    # Outcome-age gate passes when no unresolved past-due rows OR median age ≤ threshold.
    age_ok = (
        snap.outcome_resolution_age_days is None
        or snap.outcome_resolution_age_days <= GATE_OUTCOME_AGE_MAX_DAYS
    )

    snap.exit_gates = {
        "n_total": snap.n_total >= GATE_N_TOTAL,
        "fill_ratio": snap.fill_ratio >= GATE_FILL_RATIO,
        "forecast_buckets": buckets_ok,
        "cities": cities_ok,
        "horizons": horizons_ok,
        "multi_var_ingestion": (
            snap.multi_var_populated_pct is not None
            and snap.multi_var_populated_pct >= GATE_MULTI_VAR_RATIO
        ),
        "outcome_resolution_age": age_ok,
    }
    snap.overall_ready = all(snap.exit_gates.values())
    return snap


def snapshot_to_json(snap: MonitorSnapshot) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "n_total": snap.n_total,
        "n_with_outcome": snap.n_with_outcome,
        "fill_ratio": round(snap.fill_ratio, 4),
        "uptime_hours": round(snap.uptime_hours, 2),
        "ingestion_rate_last_3d": snap.ingestion_rate_last_3d,
        "days_remaining_estimate": snap.days_remaining_estimate,
        "multi_var_populated_pct": (
            round(snap.multi_var_populated_pct, 4)
            if snap.multi_var_populated_pct is not None else None
        ),
        "coverage_by_forecast_bucket": snap.coverage_by_forecast_bucket,
        "coverage_by_city": snap.coverage_by_city,
        "coverage_by_horizon": snap.coverage_by_horizon,
        "coverage_by_outcome_source": snap.coverage_by_outcome_source,
        "outcome_resolution_age_days": snap.outcome_resolution_age_days,
        "exit_gates": snap.exit_gates,
        "overall_ready": snap.overall_ready,
        "warnings": snap.warnings,
    }


def print_summary(snap: MonitorSnapshot) -> None:
    print("=" * 60)
    print("SHADOW COLLECT MONITOR — Phase 2.1-ter")
    print("=" * 60)
    print(f"n_total          : {snap.n_total}")
    print(f"n_with_outcome   : {snap.n_with_outcome}")
    print(f"fill_ratio       : {snap.fill_ratio:.2%}")
    print(f"uptime_hours     : {snap.uptime_hours:.1f}h")
    if snap.ingestion_rate_last_3d is not None:
        print(f"rate (3d avg)    : {snap.ingestion_rate_last_3d:.1f} rows/day")
    print(f"ETA to n={GATE_N_TOTAL:<6}: {snap.days_remaining_estimate}")
    if snap.multi_var_populated_pct is not None:
        print(f"multi_var_fill   : {snap.multi_var_populated_pct:.2%}")
    if snap.outcome_resolution_age_days is not None:
        print(f"outcome_age (med): {snap.outcome_resolution_age_days:.1f}d")
    if snap.coverage_by_outcome_source:
        src_str = " ".join(
            f"{k}={v}" for k, v in sorted(snap.coverage_by_outcome_source.items())
        )
        print(f"outcome_source   : {src_str}")
    print()
    print("Exit gates:")
    for name, passed in snap.exit_gates.items():
        status = "PASS" if passed else "FAIL"
        print(f"  [{status}] {name}")
    print()
    print(f"OVERALL_READY : {'YES — run Prompt 2.1-bis again' if snap.overall_ready else 'no'}")
    for w in snap.warnings:
        print(f"  {w}")
    # Top buckets/cities/horizons
    if snap.coverage_by_forecast_bucket:
        print("\nForecast buckets (top):")
        for k, v in sorted(
            snap.coverage_by_forecast_bucket.items(),
            key=lambda kv: -kv[1],
        )[:8]:
            print(f"  {k:<12} {v}")
    if snap.coverage_by_city:
        print("\nCities (top 10):")
        for k, v in sorted(
            snap.coverage_by_city.items(), key=lambda kv: -kv[1]
        )[:10]:
            print(f"  {k:<20} {v}")
    print("=" * 60)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", type=Path, default=Path("data/bot.db"),
        help="Path to bot.db (default: data/bot.db)",
    )
    parser.add_argument(
        "--json-out", type=Path, default=Path("data/shadow_status.json"),
        help="Path to write JSON status (default: data/shadow_status.json)",
    )
    args = parser.parse_args()

    rows = _load_rows(args.db)
    snap = compute_snapshot(rows)

    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(snapshot_to_json(snap), indent=2))
    print_summary(snap)
    return 0


if __name__ == "__main__":
    sys.exit(main())
