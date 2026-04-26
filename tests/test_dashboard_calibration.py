"""Calibration panel tests."""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from weather import dashboard_widgets as dw


def _build_db_with_outcomes(tmp_path: Path, rows: list[dict]) -> str:
    db = tmp_path / "cal_test.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """CREATE TABLE weather_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_question TEXT NOT NULL, market_id TEXT NOT NULL,
            token_id TEXT NOT NULL, signal_type TEXT NOT NULL,
            forecast_probability REAL NOT NULL, market_price REAL NOT NULL,
            edge REAL NOT NULL, location TEXT NOT NULL,
            forecast_date TEXT NOT NULL, weather_metric TEXT NOT NULL,
            threshold_value REAL NOT NULL, timestamp TEXT NOT NULL,
            ensemble_member_count INTEGER DEFAULT 0,
            confidence TEXT DEFAULT 'medium', net_edge REAL DEFAULT 0.0,
            actual_outcome REAL, humidity_mean REAL, horizon_hours INTEGER,
            outcome_source TEXT
        )""",
    )
    for r in rows:
        conn.execute(
            """INSERT INTO weather_signals
               (market_question, market_id, token_id, signal_type,
                forecast_probability, market_price, edge, location, forecast_date,
                weather_metric, threshold_value, timestamp, actual_outcome, outcome_source)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (r["market_question"], r.get("market_id", "M"), "T", "buy_no",
             r["forecast_probability"], 0.5, 0.1, "City", "2026-04-25",
             "highest", 85.0, r.get("timestamp", "2026-04-25T12:00:00+00:00"),
             r.get("actual_outcome"), "polymarket"),
        )
    conn.commit(); conn.close()
    return str(db)


def test_empty_db_returns_empty_marker(tmp_path: Path) -> None:
    db = _build_db_with_outcomes(tmp_path, [])
    data = dw.compute_calibration_data(db, use_cache=False)
    assert data["empty"] is True
    panel = dw.build_calibration_panel(data)
    summary = dw.build_calibration_summary(data)
    assert panel is not None and summary is not None


def test_baseline_brier_calibrated_population_beats_naive(tmp_path: Path) -> None:
    """100 markets, 80% NO base rate, perfectly calibrated forecasts → brier_model < brier_baseline."""
    rows: list[dict] = []
    # 80 markets where actual = 0 (NO) and forecast says P(YES)=0.05 (correctly low)
    for i in range(80):
        rows.append({"market_question": f"NO{i}", "market_id": f"NM{i}",
                     "forecast_probability": 0.05, "actual_outcome": 0.0})
    # 20 markets where actual = 1 (YES) and forecast says P(YES)=0.95 (correctly high)
    for i in range(20):
        rows.append({"market_question": f"YES{i}", "market_id": f"YM{i}",
                     "forecast_probability": 0.95, "actual_outcome": 1.0})
    db = _build_db_with_outcomes(tmp_path, rows)
    data = dw.compute_calibration_data(db, use_cache=False)
    assert data["empty"] is False
    assert data["n"] == 100
    assert data["brier_model"] < data["brier_baseline"]
    # Calibrated forecasts: model Brier ≈ 0.0025 (5% off perfect), baseline ≈ 0.16
    assert data["brier_model"] < 0.10
    assert 0.10 < data["brier_baseline"] < 0.20


def test_dedup_keeps_latest_forecast_per_market(tmp_path: Path) -> None:
    """Two rows for same market_question, only the latest timestamp counts toward Brier."""
    rows = [
        {"market_question": "Q1", "market_id": "M1",
         "forecast_probability": 0.10, "actual_outcome": 0.0,
         "timestamp": "2026-04-20T10:00:00+00:00"},
        {"market_question": "Q1", "market_id": "M1",
         "forecast_probability": 0.50, "actual_outcome": 0.0,
         "timestamp": "2026-04-25T10:00:00+00:00"},  # latest, keeps this row
    ]
    db = _build_db_with_outcomes(tmp_path, rows)
    data = dw.compute_calibration_data(db, use_cache=False)
    assert data["n"] == 1  # dedup'd to single market
    # Latest forecast was 0.50 vs actual 0.0 → squared error 0.25
    assert data["brier_model"] == pytest.approx(0.25, abs=1e-6)


def test_calibration_cache_within_ttl(tmp_path: Path) -> None:
    rows = [{"market_question": "Q", "market_id": "M",
             "forecast_probability": 0.10, "actual_outcome": 0.0}]
    db = _build_db_with_outcomes(tmp_path, rows)
    # Clear module cache
    dw._calibration_cache.clear()
    a = dw.compute_calibration_data(db)
    b = dw.compute_calibration_data(db)
    assert a is b  # same object → cache hit


def test_ascii_reliability_diagram_renders_for_diverse_data(tmp_path: Path) -> None:
    rows = []
    for p, n in [(0.05, 30), (0.30, 20), (0.70, 10), (0.95, 5)]:
        for i in range(n):
            actual = 1.0 if i % 2 else 0.0
            rows.append({
                "market_question": f"Q_{p}_{i}", "market_id": f"M_{p}_{i}",
                "forecast_probability": p, "actual_outcome": actual,
            })
    db = _build_db_with_outcomes(tmp_path, rows)
    data = dw.compute_calibration_data(db, use_cache=False)
    panel = dw.build_calibration_panel(data)
    assert panel is not None
