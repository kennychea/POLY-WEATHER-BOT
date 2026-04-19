"""Phase 12 followup — cleanup stuck/stale pending trades.

Two jobs:

  1. PURGE STALE — paper_trades uses append-only INSERT, so resolved trades
     keep their original "pending" row. This script deletes those orphan
     pending rows (where trade_id has a matching resolved/force_resolved
     row). Cosmetic — the running bot already ignores them.

  2. RESOLVE PAST-DUE — for real pending trades (no resolved twin) whose
     target_date is in the past, run the same slug-based resolution path
     the bot uses (market_data.indexer._resolve_via_slug). Updates DB
     directly and calls db.resolve_forecast for forecast_log linkage.

Usage:
    python scripts/cleanup_pending.py --dry-run   # show what would happen
    python scripts/cleanup_pending.py             # commit changes

Backup the DB first:
    cp data/bot.db data/bot.db.pre_cleanup_$(date +%Y%m%d)
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

# Allow script-relative import of project modules
sys.path.insert(0, str(Path(__file__).parent.parent))

from infra.types import SignalType  # noqa: E402
from market_data.indexer import MarketIndexer  # noqa: E402

DB_PATH = Path(__file__).parent.parent / "data" / "bot.db"


def purge_stale_pending(conn: sqlite3.Connection, dry_run: bool) -> int:
    """Delete pending rows whose trade_id already has a resolved/force_resolved twin."""
    cur = conn.cursor()
    cur.execute(
        """SELECT id, trade_id FROM paper_trades
           WHERE status = 'pending'
             AND trade_id IN (
                 SELECT trade_id FROM paper_trades
                 WHERE status IN ('resolved', 'force_resolved')
             )"""
    )
    victims = cur.fetchall()
    print(f"[PURGE] {len(victims)} stale pending rows (already resolved elsewhere)")
    if not dry_run and victims:
        ids = [v[0] for v in victims]
        placeholders = ",".join("?" for _ in ids)
        cur.execute(f"DELETE FROM paper_trades WHERE id IN ({placeholders})", ids)
        conn.commit()
        print(f"[PURGE] deleted {cur.rowcount} rows")
    return len(victims)


def get_past_due_pending(conn: sqlite3.Connection) -> list[dict]:
    """Return real pending trades (no resolved twin) with target_date in the past."""
    cur = conn.cursor()
    today = datetime.now(UTC).date().isoformat()
    cur.execute(
        """SELECT pt.trade_id, pt.market_question, pt.market_id, pt.token_id,
                  pt.signal_type, pt.size_usdc, pt.entry_price, pt.fees,
                  pt.opened_at, fl.target_date, pt.forecast_probability
           FROM paper_trades pt
           LEFT JOIN forecast_log fl ON pt.market_id = fl.market_id
           WHERE pt.status = 'pending'
             AND pt.trade_id NOT IN (
                 SELECT trade_id FROM paper_trades
                 WHERE status IN ('resolved', 'force_resolved')
             )
             AND fl.target_date IS NOT NULL
             AND fl.target_date < ?
           GROUP BY pt.trade_id""",
        (today,),
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def compute_pnl(signal_type: str, entry_price: float, exit_price_yes: float,
                size_usdc: float, fees: float) -> tuple[float, float]:
    """Compute PnL identically to paper_trader._compute_pnl.

    Returns (exit_price_for_side, pnl).
    For BUY_NO: exit_price is inverted; for BUY_YES: direct.
    """
    if signal_type == "buy_no":
        side_exit = 1.0 - exit_price_yes
    else:
        side_exit = exit_price_yes
    side_exit = max(0.0, min(1.0, side_exit))

    if entry_price <= 0:
        return side_exit, -fees
    shares = size_usdc / entry_price
    gross = shares * side_exit - size_usdc
    return side_exit, gross - fees


async def resolve_one(trade: dict, indexer: MarketIndexer) -> dict | None:
    """Try slug-based resolution; return resolution dict or None.

    Falls back to "extreme price" detection for past-due markets where
    Polymarket hasn't formally closed the market but prices are already
    >= 0.99 or <= 0.01 (outcome determined, official close pending).
    """
    # Primary: official close detection via slug
    try:
        resolution = await indexer._resolve_via_slug(trade["market_question"])
    except Exception as e:  # noqa: BLE001
        print(f"  ERROR resolving {trade['trade_id'][:12]}: {e}")
        return None
    if resolution is not None:
        return {
            "resolution_price": resolution.resolution_price,
            "source": resolution.source,
        }

    # Fallback: past-due + extreme price on Polymarket
    import aiohttp
    import json as _json

    slug = indexer._build_slug_from_question(trade["market_question"])
    if slug is None:
        return None
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                "https://gamma-api.polymarket.com/events", params={"slug": slug},
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                if not data or not isinstance(data, list):
                    return None
                event = data[0]
                q_norm = trade["market_question"].strip().lower()
                for m in event.get("markets", []):
                    if (m.get("question") or "").strip().lower() != q_norm:
                        continue
                    prices = _json.loads(m.get("outcomePrices") or "[]")
                    if not prices:
                        return None
                    yes_price = float(prices[0])
                    if yes_price >= 0.99 or yes_price <= 0.01:
                        # Outcome effectively determined even if not officially closed
                        return {
                            "resolution_price": 1.0 if yes_price >= 0.99 else 0.0,
                            "source": "extreme_price_past_due",
                        }
    except Exception as e:  # noqa: BLE001
        print(f"  extreme-price fallback failed for {trade['trade_id'][:12]}: {e}")
    return None


async def resolve_past_due(conn: sqlite3.Connection, dry_run: bool) -> None:
    past_due = get_past_due_pending(conn)
    print(f"\n[RESOLVE] {len(past_due)} real past-due pending trades")
    if not past_due:
        return

    indexer = MarketIndexer(refresh_interval=60)
    now_iso = datetime.now(UTC).isoformat()

    resolved_count = 0
    not_found = 0
    for trade in past_due:
        resolution = await resolve_one(trade, indexer)
        tid_short = trade["trade_id"][:14]
        if resolution is None:
            print(f"  SKIP  {tid_short} (slug not found or still open)")
            not_found += 1
            continue

        exit_side, pnl = compute_pnl(
            trade["signal_type"],
            trade["entry_price"],
            resolution["resolution_price"],
            trade["size_usdc"],
            trade["fees"],
        )
        actual_yes = 1 if resolution["resolution_price"] >= 0.5 else 0

        print(
            f"  RESOLVE {tid_short} exit={exit_side:.3f} pnl=${pnl:+.2f} "
            f"YES={actual_yes} src={resolution['source']}"
        )

        if not dry_run:
            cur = conn.cursor()
            # Insert resolved row (append-only pattern, matches bot's behavior)
            cur.execute(
                """INSERT INTO paper_trades
                   (trade_id, market_question, market_id, token_id, signal_type,
                    size_usdc, entry_price, exit_price, fees, pnl, status,
                    opened_at, resolved_at, resolution_source,
                    forecast_probability, ensemble_std, regime, horizon_hours)
                   SELECT trade_id, market_question, market_id, token_id, signal_type,
                          size_usdc, entry_price, ?, fees, ?, 'resolved',
                          opened_at, ?, ?, forecast_probability, ensemble_std,
                          regime, horizon_hours
                   FROM paper_trades WHERE trade_id = ? AND status = 'pending'
                   LIMIT 1""",
                (exit_side, pnl, now_iso, resolution["source"], trade["trade_id"]),
            )
            # Update forecast_log with actual_outcome + brier
            cur.execute(
                "SELECT probability FROM forecast_log WHERE market_id = ? "
                "AND actual_outcome IS NULL LIMIT 1",
                (trade["market_id"],),
            )
            row = cur.fetchone()
            if row is not None:
                brier = (row[0] - actual_yes) ** 2
                cur.execute(
                    "UPDATE forecast_log SET actual_outcome = ?, brier_score = ? "
                    "WHERE market_id = ? AND actual_outcome IS NULL",
                    (actual_yes, brier, trade["market_id"]),
                )
            conn.commit()
        resolved_count += 1

    print(f"\n[RESOLVE] resolved {resolved_count}, not-found {not_found}")


async def main_async(args) -> int:
    if not DB_PATH.exists():
        print(f"DB not found: {DB_PATH}", file=sys.stderr)
        return 1
    conn = sqlite3.connect(str(DB_PATH))
    try:
        purge_stale_pending(conn, dry_run=args.dry_run)
        await resolve_past_due(conn, dry_run=args.dry_run)

        # Final state check
        cur = conn.cursor()
        cur.execute(
            """SELECT COUNT(*) FROM paper_trades
               WHERE status = 'pending'
                 AND trade_id NOT IN (
                     SELECT trade_id FROM paper_trades
                     WHERE status IN ('resolved','force_resolved')
                 )"""
        )
        real_pending = cur.fetchone()[0]
        cur.execute(
            "SELECT COUNT(*) FROM paper_trades WHERE status = 'pending'"
        )
        total_pending_rows = cur.fetchone()[0]
        print(f"\n[STATE] real pending: {real_pending}")
        print(f"[STATE] total pending rows (includes orphans): {total_pending_rows}")
    finally:
        conn.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
