"""Tests for scripts/purge_stub_mid_fakes.py.

Uses a temp sqlite file (not :memory: because the script opens its own
connection via path). Never touches data/bot.db.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from scripts.purge_stub_mid_fakes import CANCELLED_STATUS, purge


def _create_schema(db_path: Path) -> None:
    """Create a minimal paper_trades table matching the prod schema fields
    the purge script touches."""
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """
            CREATE TABLE paper_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id TEXT NOT NULL,
                market_question TEXT NOT NULL,
                market_id TEXT NOT NULL,
                token_id TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                size_usdc REAL NOT NULL,
                entry_price REAL NOT NULL,
                exit_price REAL,
                fees REAL NOT NULL,
                pnl REAL,
                status TEXT NOT NULL,
                opened_at TEXT NOT NULL,
                resolved_at TEXT,
                resolution_source TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.commit()


def _insert_row(
    db_path: Path,
    *,
    trade_id: str,
    signal_type: str,
    entry_price: float,
    status: str,
    opened_at: str,
    question: str = "Will NYC be 60-61F on Apr 11?",
) -> None:
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """
            INSERT INTO paper_trades (
                trade_id, market_question, market_id, token_id, signal_type,
                size_usdc, entry_price, fees, status, opened_at
            ) VALUES (?, ?, 'cid', 'tok', ?, 10.0, ?, 0.1, ?, ?)
            """,
            (trade_id, question, signal_type, entry_price, status, opened_at),
        )
        conn.commit()


def _row(db_path: Path, trade_id: str) -> dict:
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT * FROM paper_trades WHERE trade_id = ?", (trade_id,)
        )
        row = cur.fetchone()
        return dict(row) if row else {}


@pytest.fixture
def fixture_db(tmp_path: Path) -> Path:
    """Create a tmp DB with 3 rows:
    - fake_pending: MATCHES the purge filter (target)
    - legit_pending: buy_yes, non-0.5 entry, different date -> no match
    - fake_resolved: matches shape but status=resolved -> no match
    """
    db = tmp_path / "test_bot.db"
    _create_schema(db)

    # Row 1 — THE target (must be purged)
    _insert_row(
        db,
        trade_id="trade_fake_pending",
        signal_type="buy_no",
        entry_price=0.5,
        status="pending",
        opened_at="2026-04-11T00:21:44.336431+00:00",
    )
    # Row 2 — legit pending (buy_yes, different entry, different date)
    _insert_row(
        db,
        trade_id="trade_legit_pending",
        signal_type="buy_yes",
        entry_price=0.42,
        status="pending",
        opened_at="2026-04-12T05:00:00+00:00",
    )
    # Row 3 — fake-shape but already resolved (must NOT be touched)
    _insert_row(
        db,
        trade_id="trade_fake_resolved",
        signal_type="buy_no",
        entry_price=0.5,
        status="resolved",
        opened_at="2026-04-11T00:21:44.336431+00:00",
    )
    return db


def test_dry_run_does_not_write(fixture_db: Path) -> None:
    affected = purge(fixture_db, commit=False)
    assert affected == 1  # matched 1, wrote 0

    target = _row(fixture_db, "trade_fake_pending")
    assert target["status"] == "pending"  # unchanged
    assert target["resolved_at"] is None


def test_commit_updates_only_matching_row(fixture_db: Path) -> None:
    affected = purge(fixture_db, commit=True)
    assert affected == 1

    # Target row cancelled
    target = _row(fixture_db, "trade_fake_pending")
    assert target["status"] == CANCELLED_STATUS
    assert target["resolved_at"] is not None
    assert "purge_stub_mid_fakes.py" in target["resolution_source"]
    assert "c42f0c2" in target["resolution_source"]

    # Legit pending unchanged
    legit = _row(fixture_db, "trade_legit_pending")
    assert legit["status"] == "pending"
    assert legit["resolved_at"] is None
    assert legit["resolution_source"] == ""

    # Resolved row unchanged
    resolved = _row(fixture_db, "trade_fake_resolved")
    assert resolved["status"] == "resolved"


def test_purge_is_idempotent(fixture_db: Path) -> None:
    first = purge(fixture_db, commit=True)
    assert first == 1

    # Second run: WHERE no longer matches (status != 'pending')
    second = purge(fixture_db, commit=True)
    assert second == 0

    # Target row still only cancelled once
    target = _row(fixture_db, "trade_fake_pending")
    assert target["status"] == CANCELLED_STATUS


def test_purge_missing_db_returns_error(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist.db"
    result = purge(missing, commit=False)
    assert result == -1
