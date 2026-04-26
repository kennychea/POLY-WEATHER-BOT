"""Alert engine tests — 8 rules + history snapshot rolling."""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from weather import dashboard_alerts as da


def _build_minimal_db(tmp_path: Path,
                     *, weather_rows: list[dict] | None = None,
                     paper_trades: list[dict] | None = None) -> str:
    db = tmp_path / "alert.db"
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
            actual_outcome REAL, outcome_source TEXT
        )""",
    )
    conn.execute(
        """CREATE TABLE paper_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id TEXT NOT NULL, market_question TEXT NOT NULL,
            market_id TEXT NOT NULL, token_id TEXT NOT NULL,
            signal_type TEXT NOT NULL, size_usdc REAL NOT NULL,
            entry_price REAL NOT NULL, exit_price REAL,
            fees REAL NOT NULL, pnl REAL, status TEXT NOT NULL,
            opened_at TEXT NOT NULL, resolved_at TEXT,
            resolution_source TEXT NOT NULL DEFAULT '',
            forecast_probability REAL, ensemble_std REAL,
            regime TEXT, horizon_hours INTEGER
        )""",
    )
    for r in (weather_rows or []):
        conn.execute(
            "INSERT INTO weather_signals (market_question, market_id, token_id, "
            "signal_type, forecast_probability, market_price, edge, location, "
            "forecast_date, weather_metric, threshold_value, timestamp, "
            "actual_outcome, outcome_source) "
            "VALUES ('Q','M','T','buy_no',0.05,0.5,0.1,'C','2026-04-25','highest',85.0,?,?,?)",
            (r["timestamp"], r.get("actual_outcome"), r.get("outcome_source")),
        )
    for t in (paper_trades or []):
        conn.execute(
            "INSERT INTO paper_trades (trade_id, market_question, market_id, "
            "token_id, signal_type, size_usdc, entry_price, fees, pnl, status, "
            "opened_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (t.get("trade_id", "T"), t.get("market_question", "Q"), "M", "TT",
             t.get("signal_type", "buy_no"), 10.0, t.get("entry_price", 0.5),
             0.1, t.get("pnl"), t.get("status", "pending"), t["opened_at"]),
        )
    conn.commit(); conn.close()
    return str(db)


# ── individual rules ────────────────────────────────────────────────


def test_data_only_off_alert_fires(monkeypatch) -> None:
    monkeypatch.setenv("DATA_ONLY_MODE", "0")
    a = da.alert_data_only_mode_off()
    assert a is not None
    assert a.severity == "CRITICAL"


def test_data_only_off_alert_silent_when_on(monkeypatch) -> None:
    monkeypatch.setenv("DATA_ONLY_MODE", "1")
    assert da.alert_data_only_mode_off() is None


def test_new_paper_trades_in_window_fires(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DATA_ONLY_MODE", "1")
    now_iso = datetime.now(UTC).isoformat()
    db = _build_minimal_db(tmp_path, paper_trades=[
        {"trade_id": "T1", "opened_at": now_iso, "status": "pending"},
        {"trade_id": "T2", "opened_at": now_iso, "status": "pending"},
    ])
    a = da.alert_new_paper_trades_in_window(db)
    assert a is not None and a.severity == "CRITICAL"


def test_ingestion_dropped_alert_fires(tmp_path: Path) -> None:
    """Empty weather_signals → 0 rows/h → alert."""
    db = _build_minimal_db(tmp_path)
    a = da.alert_ingestion_dropped(db)
    assert a is not None and a.severity == "CRITICAL"


def test_ingestion_alert_silent_when_healthy(tmp_path: Path) -> None:
    """800 rows distributed within the last 6h window → ~133 rows/h above threshold."""
    now = datetime.now(UTC)
    # Pack 800 rows across the last 6 hours = 360 minutes → seconds-spaced
    rows = [{"timestamp": (now - timedelta(seconds=i * 27)).isoformat()}
            for i in range(800)]
    db = _build_minimal_db(tmp_path, weather_rows=rows)
    a = da.alert_ingestion_dropped(db)
    assert a is None


def test_pending_resolution_lag_alerts(tmp_path: Path) -> None:
    now_iso = datetime.now(UTC).isoformat()
    pendings = [{"trade_id": f"T{i}", "opened_at": now_iso, "status": "pending"}
                for i in range(150)]
    db = _build_minimal_db(tmp_path, paper_trades=pendings)
    a = da.alert_pending_resolution_lag(db)
    assert a is not None and a.severity == "WARNING"


def test_extreme_price_share_alerts(tmp_path: Path) -> None:
    """7 polymarket_extreme_price out of 10 today → 70% > threshold."""
    today_iso = datetime.now(UTC).isoformat()
    rows = []
    for i in range(7):
        rows.append({"timestamp": today_iso, "actual_outcome": 1.0,
                     "outcome_source": "polymarket_extreme_price"})
    for i in range(3):
        rows.append({"timestamp": today_iso, "actual_outcome": 0.0,
                     "outcome_source": "polymarket"})
    db = _build_minimal_db(tmp_path, weather_rows=[
        {**r, "actual_outcome": r["actual_outcome"], "outcome_source": r["outcome_source"]}
        for r in rows
    ])
    # All same market_id "M" by fixture default → distinct count is 1.
    # We need to vary market_id; rebuild with explicit ids.
    db = tmp_path / "ep.db"
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
            actual_outcome REAL, outcome_source TEXT
        )""",
    )
    for i in range(7):
        conn.execute(
            "INSERT INTO weather_signals (market_question, market_id, token_id, "
            "signal_type, forecast_probability, market_price, edge, location, "
            "forecast_date, weather_metric, threshold_value, timestamp, "
            "actual_outcome, outcome_source) "
            "VALUES ('Q', ?, 'T', 'buy_no', 0.05, 0.5, 0.1, 'C', '2026-04-25', "
            "'highest', 85.0, ?, 1.0, 'polymarket_extreme_price')",
            (f"EP{i}", today_iso),
        )
    for i in range(3):
        conn.execute(
            "INSERT INTO weather_signals (market_question, market_id, token_id, "
            "signal_type, forecast_probability, market_price, edge, location, "
            "forecast_date, weather_metric, threshold_value, timestamp, "
            "actual_outcome, outcome_source) "
            "VALUES ('Q', ?, 'T', 'buy_no', 0.05, 0.5, 0.1, 'C', '2026-04-25', "
            "'highest', 85.0, ?, 0.0, 'polymarket')",
            (f"P{i}", today_iso),
        )
    conn.commit(); conn.close()
    a = da.alert_extreme_price_share_high(str(db))
    assert a is not None and a.severity == "INFO"


def test_new_market_outage_fires_when_no_recent_markets(tmp_path: Path) -> None:
    db = _build_minimal_db(tmp_path)
    a = da.alert_new_market_outage(db)
    assert a is not None and a.severity == "INFO"


# ── orchestration + rendering ───────────────────────────────────────


def test_compute_all_alerts_orders_by_severity(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DATA_ONLY_MODE", "0")
    db = _build_minimal_db(tmp_path)
    alerts = da.compute_all_alerts(db, history_path=None)
    if len(alerts) >= 2:
        order = [da.SEVERITY_ORDER[a.severity] for a in alerts]
        assert order == sorted(order)


def test_render_alerts_block_returns_panel_even_when_empty() -> None:
    panel = da.render_alerts_block([])
    assert panel is not None


# ── history rolling ─────────────────────────────────────────────────


def test_append_snapshot_writes_and_prunes(tmp_path: Path) -> None:
    history_path = tmp_path / "h.json"
    now = datetime.now(UTC)
    # Inject 10 snapshots, oldest 30 days back
    history_path.write_text(json.dumps([
        {"timestamp": (now - timedelta(days=i * 4)).isoformat(),
         "brier_delta": 0.01 * i}
        for i in range(10)
    ]))
    fresh = da.append_snapshot(history_path, {
        "timestamp": now.isoformat(), "brier_delta": 0.05,
    })
    # All snapshots older than 7 days pruned
    assert all(
        (now - datetime.fromisoformat(s["timestamp"].replace("Z", "+00:00"))).days <= 7
        for s in fresh
    )
    assert len(fresh) <= 4  # only ones within 7 days


def test_brier_diverging_no_baseline_returns_warning() -> None:
    a = da.alert_brier_diverging([])
    assert a is not None and a.severity == "WARNING"
    assert "no baseline" in a.message
