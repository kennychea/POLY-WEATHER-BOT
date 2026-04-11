"""Purge fake BUY_NO pending trades opened from the pre-c42f0c2 stub-book bug.

Context
-------
Before commit c42f0c2 ("reject wide-spread stub books in get_mid_price"), the
bot would seed trades from stub order books using a synthetic mid of 0.5. This
produced BUY_NO paper trades with entry_price=0.5 that never reflected real
market conditions. Commit c42f0c2 fixed the source but left 16 such pendings
in data/bot.db from 2026-04-11. Letting them resolve naturally would pollute
win-rate calibration.

This script marks those 16 rows as status='cancelled_fake_mid' (no DELETE —
audit trail preserved).

Idempotency
-----------
The WHERE clause matches ONLY:
    signal_type='buy_no'
    AND ABS(entry_price - 0.5) < 0.001
    AND status='pending'
    AND opened_at LIKE '2026-04-11%'
After the first --commit run, those rows have status='cancelled_fake_mid' and
no longer match the clause — a second run is a safe no-op.

Usage
-----
    python scripts/purge_stub_mid_fakes.py            # dry-run (default)
    python scripts/purge_stub_mid_fakes.py --dry-run  # explicit dry-run
    python scripts/purge_stub_mid_fakes.py --commit   # actually write
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

# Repo-root relative path — works regardless of cwd.
DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "bot.db"

PURGE_WHERE = (
    "signal_type = 'buy_no' "
    "AND ABS(entry_price - 0.5) < 0.001 "
    "AND status = 'pending' "
    "AND opened_at LIKE '2026-04-11%'"
)

RESOLUTION_SOURCE = "purge_stub_mid_fakes.py / c42f0c2"
CANCELLED_STATUS = "cancelled_fake_mid"


def count_status(conn: sqlite3.Connection, status: str) -> int:
    """Return count of paper_trades rows with the given status."""
    cur = conn.execute(
        "SELECT COUNT(*) FROM paper_trades WHERE status = ?", (status,)
    )
    return int(cur.fetchone()[0])


def select_matches(conn: sqlite3.Connection) -> list[tuple]:
    """Return rows that the purge WHERE clause matches."""
    cur = conn.execute(
        f"SELECT trade_id, entry_price, opened_at, market_question "
        f"FROM paper_trades WHERE {PURGE_WHERE} "
        f"ORDER BY opened_at"
    )
    return cur.fetchall()


def purge(db_path: Path, *, commit: bool) -> int:
    """Mark matching rows cancelled_fake_mid. Return count of rows affected.

    Uses DEFERRED isolation so the write transaction is explicit; safe under
    WAL-mode concurrent access (the live bot is reading/writing the same DB).
    """
    if not db_path.exists():
        print(f"ERROR: db not found at {db_path}", file=sys.stderr)
        return -1

    with sqlite3.connect(str(db_path), isolation_level="DEFERRED") as conn:
        # Defensive: ensure WAL mode (bot sets it too, but this is idempotent).
        conn.execute("PRAGMA journal_mode=WAL")

        before_pending = count_status(conn, "pending")
        before_cancelled = count_status(conn, CANCELLED_STATUS)
        before_resolved = count_status(conn, "resolved")

        matches = select_matches(conn)
        print(f"[{'COMMIT' if commit else 'DRY-RUN'}] matched rows: {len(matches)}")
        for row in matches:
            trade_id, entry_price, opened_at, question = row
            q_short = (question or "")[:70]
            print(f"  {trade_id}  entry={entry_price}  opened={opened_at}  q={q_short}")

        print(
            f"  before: pending={before_pending} "
            f"cancelled_fake_mid={before_cancelled} resolved={before_resolved}"
        )

        if not commit:
            print("[DRY-RUN] no writes performed. Re-run with --commit to apply.")
            return len(matches)

        cur = conn.execute(
            f"UPDATE paper_trades "
            f"SET status = ?, "
            f"    resolved_at = CURRENT_TIMESTAMP, "
            f"    resolution_source = ? "
            f"WHERE {PURGE_WHERE}",
            (CANCELLED_STATUS, RESOLUTION_SOURCE),
        )
        affected = cur.rowcount
        conn.commit()

        after_pending = count_status(conn, "pending")
        after_cancelled = count_status(conn, CANCELLED_STATUS)
        after_resolved = count_status(conn, "resolved")

        print(f"[COMMIT] rows affected: {affected}")
        print(
            f"  after:  pending={after_pending} "
            f"cancelled_fake_mid={after_cancelled} resolved={after_resolved}"
        )
        return affected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--dry-run",
        action="store_true",
        help="Print matched rows without writing (default)",
    )
    group.add_argument(
        "--commit",
        action="store_true",
        help="Actually perform the UPDATE (required to write)",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"Path to bot.db (default: {DEFAULT_DB_PATH})",
    )
    args = parser.parse_args(argv)

    # Default = dry-run unless --commit explicitly passed.
    commit = bool(args.commit)
    result = purge(args.db, commit=commit)
    return 0 if result >= 0 else 1


if __name__ == "__main__":
    sys.exit(main())
