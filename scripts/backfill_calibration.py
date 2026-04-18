"""Phase 12.A.5 — backfill calibration data for pre-Phase-12 resolved trades.

For each resolved paper_trade (opened before Phase 12.A.3 was deployed),
this script:

1. Matches it to the closest-earlier forecast_log row by market_id.
2. Copies forecast_log.probability into paper_trades.forecast_probability.
3. Computes horizon_hours from opened_at vs forecast_log.target_date.
4. Infers actual_outcome from the trade's exit_price + signal_type and calls
   db.resolve_forecast(market_id, actual_outcome) to populate
   forecast_log.actual_outcome and brier_score.

Fields that cannot be reconstructed from history:
- ensemble_std: requires the 31 raw ensemble members (not stored per-trade)
- regime: requires runtime CLOB state (not stored)

These remain NULL for backfilled trades. New trades (Phase 12.A.3+) will
have all 4 fields populated.

Usage:
    python scripts/backfill_calibration.py [--dry-run]

Backup the DB first:
    cp data/bot.db data/bot.db.pre_backfill_$(date +%Y%m%d)
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "bot.db"


def infer_yes_outcome(signal_type: str, exit_price: float | None) -> int | None:
    """Infer whether YES resolved true from the trade's exit_price + side.

    Returns 1 if YES was true, 0 if YES was false, None if ambiguous
    (e.g., force_resolved trades where exit_price == entry_price).
    """
    if exit_price is None:
        return None
    # BUY_YES: exit_price mirrors YES resolution directly
    # BUY_NO: exit_price = 1.0 - YES_resolution (so invert)
    if signal_type == "buy_yes":
        if exit_price >= 0.99:
            return 1
        if exit_price <= 0.01:
            return 0
    elif signal_type == "buy_no":
        if exit_price >= 0.99:
            return 0  # BUY_NO wins when YES is false
        if exit_price <= 0.01:
            return 1  # BUY_NO loses when YES is true
    return None  # Ambiguous (force_resolved etc.)


def backfill(db_path: Path, dry_run: bool) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # Resolved paper_trades without forecast linkage yet
    cur.execute(
        """SELECT trade_id, market_id, signal_type, entry_price, exit_price,
                  opened_at, status
           FROM paper_trades
           WHERE status IN ('resolved', 'force_resolved')
             AND forecast_probability IS NULL"""
    )
    trades = cur.fetchall()
    print(f"Found {len(trades)} resolved trades needing backfill.")

    linked = 0
    outcome_updates = 0
    for trade in trades:
        # Match to closest-earlier forecast_log row by market_id
        cur.execute(
            """SELECT id, probability, target_date, logged_at
               FROM forecast_log
               WHERE market_id = ? AND logged_at <= ?
               ORDER BY logged_at DESC LIMIT 1""",
            (trade["market_id"], trade["opened_at"]),
        )
        fc = cur.fetchone()
        if fc is None:
            # Try most-recent forecast for same market regardless of time
            cur.execute(
                """SELECT id, probability, target_date, logged_at
                   FROM forecast_log WHERE market_id = ?
                   ORDER BY logged_at DESC LIMIT 1""",
                (trade["market_id"],),
            )
            fc = cur.fetchone()
        if fc is None:
            continue  # no forecast exists for this market

        # Compute horizon_hours
        horizon_hours: int | None = None
        try:
            opened = datetime.fromisoformat(trade["opened_at"].replace("Z", "+00:00"))
            target = datetime.fromisoformat(fc["target_date"] + "T12:00:00+00:00")
            horizon_hours = int((target - opened).total_seconds() / 3600)
        except (ValueError, TypeError, AttributeError):
            pass

        if not dry_run:
            cur.execute(
                """UPDATE paper_trades
                   SET forecast_probability = ?, horizon_hours = ?
                   WHERE trade_id = ?""",
                (fc["probability"], horizon_hours, trade["trade_id"]),
            )
        linked += 1

        # Infer YES outcome and populate forecast_log.actual_outcome
        actual = infer_yes_outcome(trade["signal_type"], trade["exit_price"])
        if actual is not None and not dry_run:
            # Use the DB method logic inline (avoid async context here)
            brier = (fc["probability"] - actual) ** 2
            cur.execute(
                """UPDATE forecast_log
                   SET actual_outcome = ?, brier_score = ?
                   WHERE market_id = ? AND actual_outcome IS NULL""",
                (actual, brier, trade["market_id"]),
            )
            outcome_updates += cur.rowcount

    if not dry_run:
        conn.commit()

    cur.execute(
        """SELECT COUNT(*) FROM paper_trades
           WHERE status IN ('resolved','force_resolved')
             AND forecast_probability IS NOT NULL"""
    )
    trades_linked_total = cur.fetchone()[0]
    cur.execute(
        "SELECT COUNT(*) FROM forecast_log WHERE actual_outcome IS NOT NULL"
    )
    forecasts_resolved = cur.fetchone()[0]
    cur.execute(
        "SELECT AVG(brier_score) FROM forecast_log WHERE brier_score IS NOT NULL"
    )
    avg_brier = cur.fetchone()[0]

    conn.close()

    print(f"\n{'DRY RUN' if dry_run else 'COMMITTED'}:")
    print(f"  paper_trades linked this run:   {linked}")
    print(f"  forecast_log outcomes this run: {outcome_updates}")
    print(f"  Total trades linked (cumulative): {trades_linked_total}")
    print(f"  Total forecasts with outcome:     {forecasts_resolved}")
    if avg_brier is not None:
        print(f"  Avg Brier score:                  {avg_brier:.4f}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    args = parser.parse_args()
    if not args.db.exists():
        print(f"DB not found: {args.db}", file=sys.stderr)
        return 1
    backfill(args.db, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
