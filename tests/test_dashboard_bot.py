"""Bot operational panel tests."""

from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from weather import dashboard_widgets as dw


def _build_db_minimal(tmp_path: Path, recent_rows: int = 0) -> str:
    db = tmp_path / "bot_test.db"
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
    now = datetime.now(UTC)
    for i in range(recent_rows):
        conn.execute(
            """INSERT INTO weather_signals (market_question, market_id, token_id,
                signal_type, forecast_probability, market_price, edge, location,
                forecast_date, weather_metric, threshold_value, timestamp)
                VALUES ('Q', 'M', 'T', 'buy_no', 0.05, 0.5, 0.1, 'C',
                        '2026-04-25', 'highest', 85.0, ?)""",
            ((now - timedelta(minutes=i * 5)).isoformat(),),
        )
    conn.commit(); conn.close()
    return str(db)


def test_no_log_file_panel_renders_without_crash(tmp_path: Path, monkeypatch) -> None:
    """No log file at any conventional location → 'no recent activity' rather than crash."""
    monkeypatch.delenv("BOT_LOG_PATH", raising=False)
    monkeypatch.chdir(tmp_path)
    db = _build_db_minimal(tmp_path)
    data = dw.compute_bot_data(db, log_path=None)
    assert data["log_path"] is None
    assert data["recent_cycles"] == []
    panel = dw.build_bot_panel(data)
    assert panel is not None


def test_explicit_log_path_with_scan_cycles(tmp_path: Path) -> None:
    """When log file is provided, recent SCAN complete lines are surfaced."""
    log = tmp_path / "bot.log"
    log.write_text(
        "2026-04-26 12:00 INFO main: SCAN complete: 700 markets, 0 trades, 8.1s [...]\n"
        "2026-04-26 12:01 INFO main: SCAN no weather markets found\n"
        "2026-04-26 12:02 INFO main: SCAN complete: 705 markets, 1 trades, 8.5s [...]\n",
        encoding="utf-8",
    )
    db = _build_db_minimal(tmp_path)
    data = dw.compute_bot_data(db, log_path=str(log))
    assert len(data["recent_cycles"]) == 2  # only 2 SCAN-complete lines
    panel = dw.build_bot_panel(data)
    assert panel is not None


def test_data_only_alert_when_rate_low(monkeypatch, tmp_path: Path) -> None:
    """DATA_ONLY=on + zero recent rows → 'low ingestion rate' alert."""
    monkeypatch.setenv("DATA_ONLY_MODE", "1")
    db = _build_db_minimal(tmp_path, recent_rows=0)
    data = dw.compute_bot_data(db, log_path=None)
    assert data["data_only_mode"] is True
    assert any("low ingestion rate" in a for a in data["alerts"])


def test_summary_panel_renders(tmp_path: Path) -> None:
    db = _build_db_minimal(tmp_path)
    data = dw.compute_bot_data(db)
    summary = dw.build_bot_summary(data)
    assert summary is not None
