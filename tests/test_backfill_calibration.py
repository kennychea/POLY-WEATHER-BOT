"""Tests for Phase 12.A.5 backfill script."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.backfill_calibration import backfill, infer_yes_outcome


def _seed_db(db_path: Path) -> None:
    """Minimal schema + seed data for backfill tests."""
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE paper_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id TEXT, market_question TEXT, market_id TEXT,
            token_id TEXT, signal_type TEXT, size_usdc REAL,
            entry_price REAL, exit_price REAL, fees REAL, pnl REAL,
            status TEXT, opened_at TEXT, resolved_at TEXT,
            resolution_source TEXT, forecast_probability REAL,
            ensemble_std REAL, regime TEXT, horizon_hours INTEGER
        );
        CREATE TABLE forecast_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            city TEXT, target_date TEXT, metric TEXT, model TEXT,
            member_count INTEGER, probability REAL,
            actual_outcome INTEGER, brier_score REAL,
            market_id TEXT, logged_at TEXT
        );
        """
    )
    # Resolved BUY_YES winning trade
    cur.execute(
        """INSERT INTO paper_trades
           (trade_id, market_question, market_id, token_id, signal_type,
            size_usdc, entry_price, exit_price, fees, pnl, status,
            opened_at, resolved_at, resolution_source)
           VALUES ('t1', 'q1', 'mkt_A', 'tok1', 'buy_yes', 10.0, 0.55,
                   1.0, 0.10, 4.50, 'resolved',
                   '2026-04-10T12:00:00+00:00',
                   '2026-04-11T12:00:00+00:00', 'gamma_closed')"""
    )
    # Resolved BUY_NO winning trade (exit=1.0 means YES resolved false)
    cur.execute(
        """INSERT INTO paper_trades
           (trade_id, market_question, market_id, token_id, signal_type,
            size_usdc, entry_price, exit_price, fees, pnl, status,
            opened_at, resolved_at, resolution_source)
           VALUES ('t2', 'q2', 'mkt_B', 'tok2', 'buy_no', 10.0, 0.60,
                   1.0, 0.10, 6.50, 'resolved',
                   '2026-04-10T12:00:00+00:00',
                   '2026-04-11T12:00:00+00:00', 'gamma_closed')"""
    )
    # Matching forecast_log rows (same market_id, earlier timestamp)
    cur.execute(
        """INSERT INTO forecast_log
           (city, target_date, metric, model, member_count, probability,
            market_id, logged_at)
           VALUES ('NYC', '2026-04-11', 'temp_high', 'gfs_seamless',
                   31, 0.70, 'mkt_A', '2026-04-10T11:00:00+00:00')"""
    )
    cur.execute(
        """INSERT INTO forecast_log
           (city, target_date, metric, model, member_count, probability,
            market_id, logged_at)
           VALUES ('LA', '2026-04-11', 'temp_high', 'gfs_seamless',
                   31, 0.35, 'mkt_B', '2026-04-10T11:00:00+00:00')"""
    )
    conn.commit()
    conn.close()


def test_infer_yes_outcome_buy_yes_win():
    # BUY_YES + exit_price=1.0 means YES resolved true
    assert infer_yes_outcome("buy_yes", 1.0) == 1


def test_infer_yes_outcome_buy_yes_loss():
    # BUY_YES + exit_price=0.0 means YES resolved false
    assert infer_yes_outcome("buy_yes", 0.0) == 0


def test_infer_yes_outcome_buy_no_win():
    # BUY_NO + exit_price=1.0 means YES resolved false (trade wins)
    assert infer_yes_outcome("buy_no", 1.0) == 0


def test_infer_yes_outcome_buy_no_loss():
    # BUY_NO + exit_price=0.0 means YES resolved true (trade loses)
    assert infer_yes_outcome("buy_no", 0.0) == 1


def test_infer_yes_outcome_ambiguous():
    # force_resolved trades have exit_price == entry_price — can't infer outcome
    assert infer_yes_outcome("buy_yes", 0.55) is None
    assert infer_yes_outcome("buy_yes", None) is None


def test_backfill_links_trades_and_resolves_forecasts(tmp_path: Path):
    db = tmp_path / "test.db"
    _seed_db(db)

    backfill(db, dry_run=False)

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    # Both trades should be linked
    cur.execute(
        "SELECT trade_id, forecast_probability, horizon_hours FROM paper_trades"
    )
    rows = {r["trade_id"]: dict(r) for r in cur.fetchall()}
    assert rows["t1"]["forecast_probability"] == 0.70
    assert rows["t2"]["forecast_probability"] == 0.35
    assert rows["t1"]["horizon_hours"] == 24  # noon-to-noon = exactly 24h

    # forecast_log should have actual_outcome + brier
    cur.execute(
        "SELECT market_id, actual_outcome, brier_score FROM forecast_log"
    )
    out = {r["market_id"]: dict(r) for r in cur.fetchall()}
    # mkt_A: BUY_YES exit=1.0 → YES true → actual=1; brier = (0.70-1)^2 = 0.09
    assert out["mkt_A"]["actual_outcome"] == 1
    assert abs(out["mkt_A"]["brier_score"] - 0.09) < 1e-6
    # mkt_B: BUY_NO exit=1.0 → YES false → actual=0; brier = (0.35-0)^2 = 0.1225
    assert out["mkt_B"]["actual_outcome"] == 0
    assert abs(out["mkt_B"]["brier_score"] - 0.1225) < 1e-6
    conn.close()


def test_backfill_dry_run_makes_no_changes(tmp_path: Path):
    db = tmp_path / "test.db"
    _seed_db(db)

    backfill(db, dry_run=True)

    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM paper_trades WHERE forecast_probability IS NOT NULL"
    )
    assert cur.fetchone()[0] == 0
    cur.execute("SELECT COUNT(*) FROM forecast_log WHERE actual_outcome IS NOT NULL")
    assert cur.fetchone()[0] == 0
    conn.close()
