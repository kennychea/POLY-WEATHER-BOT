"""Trade history panel tests + backward-compat for --pending / --resolved."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from weather import dashboard_widgets as dw


def _build_paper_trades_db(tmp_path: Path, trades: list[dict]) -> str:
    db = tmp_path / "trades_test.db"
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
                fees, pnl, status, opened_at, resolved_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                t.get("trade_id", f"T{i}"), t.get("market_question", "Q"),
                t.get("market_id", "M"), "TT",
                t.get("signal_type", "buy_no"),
                t.get("size_usdc", 10.0), t.get("entry_price", 0.5),
                t.get("exit_price"), t.get("fees", 0.1),
                t.get("pnl"), t.get("status", "resolved"),
                "2026-04-20T12:00:00+00:00",
                t.get("resolved_at", "2026-04-22T12:00:00+00:00"),
            ),
        )
    conn.commit(); conn.close()
    return str(db)


def test_outlier_trade_pnl_with_and_without_top(tmp_path: Path) -> None:
    """20 trades with 1 outlier (+$1000) → ex-top PnL much smaller than total."""
    trades = []
    for i in range(19):
        trades.append({"pnl": -2.0})  # 19 small losses
    trades.append({"pnl": 1000.0})  # one outlier win
    db = _build_paper_trades_db(tmp_path, trades)
    data = dw.compute_trades_data(db)
    assert data["n_resolved"] == 20
    assert data["pnl_total"] == pytest.approx(1000 - 38)
    assert data["pnl_ex_top"] == pytest.approx(-38)  # without outlier: pure loss
    assert data["top_pnl_trade"] == 1000.0


def test_pf_per_signal_type(tmp_path: Path) -> None:
    """buy_no losses + buy_yes wins → distinct PF per signal_type."""
    trades = [
        {"signal_type": "buy_no", "pnl": 5.0},
        {"signal_type": "buy_no", "pnl": -2.0},
        {"signal_type": "buy_yes", "pnl": 10.0},
        {"signal_type": "buy_yes", "pnl": -5.0},
        {"signal_type": "buy_yes", "pnl": 7.0},
    ]
    db = _build_paper_trades_db(tmp_path, trades)
    data = dw.compute_trades_data(db)
    assert data["pf_by_signal"]["buy_no"] == pytest.approx(2.5)  # 5 / 2
    assert data["pf_by_signal"]["buy_yes"] == pytest.approx((10 + 7) / 5)


def test_force_resolved_status_counted(tmp_path: Path) -> None:
    trades = [
        {"status": "force_resolved", "pnl": 1.0},
        {"status": "resolved", "pnl": 2.0},
        {"status": "pending", "pnl": None},
    ]
    db = _build_paper_trades_db(tmp_path, trades)
    data = dw.compute_trades_data(db)
    assert data["n_resolved"] == 2  # both resolved + force_resolved counted
    assert data["n_pending"] == 1


def test_panel_renders_for_each_mode(tmp_path: Path) -> None:
    db = _build_paper_trades_db(tmp_path, [{"pnl": 1.0}, {"pnl": -1.0}])
    data = dw.compute_trades_data(db)
    for mode in ("all", "pending", "resolved"):
        panel = dw.build_trades_panel(data, mode=mode)
        assert panel is not None


def test_summary_panel_renders(tmp_path: Path) -> None:
    db = _build_paper_trades_db(tmp_path, [{"pnl": 5.0}, {"pnl": -2.0}])
    data = dw.compute_trades_data(db)
    summary = dw.build_trades_summary(data)
    assert summary is not None


def test_empty_db(tmp_path: Path) -> None:
    db = _build_paper_trades_db(tmp_path, [])
    data = dw.compute_trades_data(db)
    assert data["n_resolved"] == 0
    panel = dw.build_trades_panel(data)
    assert panel is not None
