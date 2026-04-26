"""Default condensed view + CLI dispatch tests."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from weather import dashboard_widgets as dw


def test_render_condensed_default_with_empty_db_does_not_crash(tmp_path: Path) -> None:
    """Empty DB → all 4 summary panels still render + footer prints."""
    import sqlite3

    db = tmp_path / "empty.db"
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
    conn.commit(); conn.close()

    from io import StringIO
    from rich.console import Console

    buf = StringIO()
    console = Console(file=buf, force_terminal=False, width=100)
    dw.render_condensed_default(console, str(db))
    out = buf.getvalue()
    # All 4 panel titles must appear
    for title in ("Phase 2", "Calibration", "Bot", "Trades"):
        assert title in out
    # Footer must mention Pass 2
    assert "Pass 2" in out


def test_dashboard_cli_help_lists_all_new_flags() -> None:
    """Sanity: argparse --help mentions the new mutually exclusive flags + back-compat."""
    proc = subprocess.run(
        [sys.executable, "-m", "weather.dashboard", "--help"],
        capture_output=True, text=True, timeout=15,
        cwd=Path(__file__).resolve().parents[1],
        encoding="utf-8", errors="replace",
    )
    assert proc.returncode == 0
    out = proc.stdout
    for flag in ("--phase2", "--calibration", "--bot", "--trades",
                 "--pending", "--resolved", "--scanner"):
        assert flag in out, f"--help missing: {flag}"
