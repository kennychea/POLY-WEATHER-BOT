"""Phase 2 panel tests — synthetic DB fixture, no network, no hot-path coupling."""

from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from weather import dashboard_widgets as dw


def _build_db(tmp_path: Path, rows: list[dict]) -> str:
    db = tmp_path / "phase2_test.db"
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
                weather_metric, threshold_value, timestamp, actual_outcome,
                humidity_mean, horizon_hours, outcome_source)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (r.get("market_question", "Q"), r.get("market_id", "M"), "T", "buy_no",
             r.get("forecast_probability", 0.05), 0.5, 0.1,
             r.get("location", "Miami"), r.get("forecast_date", "2026-04-25"),
             "highest", 85.0, r.get("timestamp", "2026-04-25T12:00:00+00:00"),
             r.get("actual_outcome"), r.get("humidity_mean", 65.0),
             r.get("horizon_hours", 12), r.get("outcome_source")),
        )
    conn.commit(); conn.close()
    return str(db)


def test_phase2_panel_empty_db(tmp_path: Path) -> None:
    """No rows → snapshot empty, panel renders without crash."""
    db = _build_db(tmp_path, [])
    data = dw.compute_phase2_data(db)
    assert data["snapshot"].n_total == 0
    assert data["n_unique_markets_resolved"] == 0
    panel = dw.build_phase2_panel(data)
    assert panel is not None  # Rich Panel renders


def test_phase2_panel_with_synthetic_population(tmp_path: Path) -> None:
    """50 synthetic rows, mix of resolved/unresolved → numbers + render correct."""
    now = datetime.now(UTC)
    rows = []
    for i in range(50):
        rows.append({
            "market_question": f"Q{i % 10}",  # 10 unique markets
            "market_id": f"M{i % 10}",
            "location": f"City{i % 5}",
            "forecast_date": (now - timedelta(days=2)).date().isoformat(),
            "timestamp": (now - timedelta(hours=i)).isoformat(),
            "actual_outcome": float(i % 2) if i < 30 else None,
            "outcome_source": "polymarket" if i < 30 else None,
            "horizon_hours": 12,
            "humidity_mean": 65.0,
        })
    db = _build_db(tmp_path, rows)
    data = dw.compute_phase2_data(db)
    assert data["snapshot"].n_total == 50
    # Resolved markets: 30 rows → ≤10 unique (we used Q0..Q9 cycling)
    assert data["n_unique_markets_resolved"] <= 10
    panel = dw.build_phase2_panel(data)
    summary = dw.build_phase2_summary(data)
    assert panel is not None
    assert summary is not None


def test_phase2_summary_panel_under_5_lines(tmp_path: Path) -> None:
    """Condensed summary must stay tight enough for the 4-panel grid view."""
    db = _build_db(tmp_path, [])
    data = dw.compute_phase2_data(db)
    summary = dw.build_phase2_summary(data)
    # Render to string; ensure not too tall
    from rich.console import Console
    from io import StringIO
    buf = StringIO()
    Console(file=buf, force_terminal=False, width=60).print(summary)
    lines = buf.getvalue().splitlines()
    assert 3 <= len(lines) <= 8  # panel border + body
