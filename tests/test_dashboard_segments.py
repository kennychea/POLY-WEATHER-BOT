"""Segment ranking tests — Wilson CI + GATE flag classification."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from weather import dashboard_segments as ds


def _build_db(tmp_path: Path, trades: list[dict]) -> str:
    db = tmp_path / "seg.db"
    conn = sqlite3.connect(str(db))
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
    for i, t in enumerate(trades):
        conn.execute(
            """INSERT INTO paper_trades (trade_id, market_question, market_id,
                token_id, signal_type, size_usdc, entry_price, exit_price,
                fees, pnl, status, opened_at, resolved_at,
                forecast_probability, horizon_hours)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                f"T{i}", t.get("market_question", "Will the highest temperature in Miami be 80°F on April 25?"),
                f"M{i}", "TT",
                t.get("signal_type", "buy_no"), 10.0,
                t.get("entry_price", 0.55), None, 0.1,
                t.get("pnl"), t.get("status", "resolved"),
                "2026-04-20T12:00:00+00:00", "2026-04-22T12:00:00+00:00",
                t.get("forecast_probability"), t.get("horizon_hours"),
            ),
        )
    conn.commit(); conn.close()
    return str(db)


def test_classify_gate_keep_when_wilson_lower_above_breakeven() -> None:
    """Wilson lower 0.70 > breakeven 0.55 → GATE_KEEP."""
    assert ds._classify(0.70, 0.85, 0.55) == "GATE_KEEP"


def test_classify_gate_out_when_wilson_upper_below_breakeven() -> None:
    """Wilson upper 0.40 < breakeven 0.55 → GATE_OUT."""
    assert ds._classify(0.30, 0.40, 0.55) == "GATE_OUT"


def test_classify_watch_when_breakeven_within_ci() -> None:
    """Breakeven 0.55 inside [0.40, 0.70] → WATCH."""
    assert ds._classify(0.40, 0.70, 0.55) == "WATCH"


def test_segments_filters_n_below_threshold(tmp_path: Path) -> None:
    """Cities with <10 trades excluded."""
    trades = []
    for i in range(5):  # 5 trades for City1 — should be filtered out
        trades.append({"market_question": f"Will the highest temperature in C1 be 80°F on April 25?",
                       "pnl": 1.0})
    for i in range(15):  # 15 trades for City2 — should appear
        trades.append({"market_question": f"Will the highest temperature in C2 be 80°F on April 25?",
                       "pnl": 0.5})
    db = _build_db(tmp_path, trades)
    from weather.dashboard_widgets import _fetch_paper_trades
    rows = _fetch_paper_trades(db)
    segments = ds.compute_segments_by_city(rows)
    labels = [s.label for s in segments]
    assert any("C2" in lb for lb in labels)
    assert not any("C1" in lb for lb in labels)


def test_segments_sorted_pnl_per_trade_ascending(tmp_path: Path) -> None:
    """Worst (most negative PnL/trade) first."""
    trades = []
    for _ in range(10):  # CityWin: +1 per trade
        trades.append({"market_question": "Will the highest temperature in CityWin be 80°F on April 25?",
                       "pnl": 1.0})
    for _ in range(10):  # CityLose: -2 per trade
        trades.append({"market_question": "Will the highest temperature in CityLose be 80°F on April 25?",
                       "pnl": -2.0})
    db = _build_db(tmp_path, trades)
    from weather.dashboard_widgets import _fetch_paper_trades
    segments = ds.compute_segments_by_city(_fetch_paper_trades(db))
    assert segments[0].pnl_per_trade < segments[-1].pnl_per_trade
    assert "citylose" in segments[0].label.lower()


def test_horizon_segments_buckets_correctly(tmp_path: Path) -> None:
    trades = []
    for _ in range(12):
        trades.append({"horizon_hours": 12, "pnl": 0.5})  # 0-24h
    for _ in range(15):
        trades.append({"horizon_hours": 36, "pnl": -1.0})  # 24-48h
    db = _build_db(tmp_path, trades)
    from weather.dashboard_widgets import _fetch_paper_trades
    segments = ds.compute_segments_by_horizon(_fetch_paper_trades(db))
    labels = [s.label for s in segments]
    assert "0-24h" in labels
    assert "24-48h" in labels


def test_bucket_segments_classifies_forecast_correctly(tmp_path: Path) -> None:
    trades = []
    for _ in range(12):
        trades.append({"forecast_probability": 0.03, "pnl": 1.0})  # <0.05
    for _ in range(15):
        trades.append({"forecast_probability": 0.45, "pnl": -1.0})  # 0.30-0.50
    db = _build_db(tmp_path, trades)
    from weather.dashboard_widgets import _fetch_paper_trades
    segments = ds.compute_segments_by_bucket(_fetch_paper_trades(db))
    labels = [s.label for s in segments]
    assert "<0.05" in labels
    assert "0.30-0.50" in labels


def test_focused_panel_renders_for_each_dim(tmp_path: Path) -> None:
    trades = [{"pnl": 0.5} for _ in range(15)]
    db = _build_db(tmp_path, trades)
    for dim in ("city", "horizon", "bucket"):
        panel = ds.build_segments_focused_panel(db, dim)
        assert panel is not None


def test_full_panel_renders_with_empty_db(tmp_path: Path) -> None:
    db = _build_db(tmp_path, [])
    panel = ds.build_segments_panel(db)
    assert panel is not None
