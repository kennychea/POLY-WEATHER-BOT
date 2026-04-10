"""Manual resolution of 20 stuck April 8 paper trades.

These trades are stuck because Gamma migrated their condition_ids.
We use the actual Polymarket resolution data (winning bins) to resolve them.

Run: python scripts/resolve_april8.py
Then restart the bot for clean in-memory state.
"""

import sqlite3
from datetime import datetime, timezone

DB_PATH = "data/bot.db"
NOW = datetime.now(timezone.utc).isoformat()

# Actual winning bins from Polymarket Gamma API (fetched 2026-04-09):
# NYC: 46-47°F, Miami: 80-81°F, LA: 70-71°F, Houston: 78-79°F,
# Dallas: 78-79°F, SF: 70-71°F, Seattle: 60-61°F, Atlanta: 68-69°F

# For each trade: (trade_id, resolution_yes_price)
# resolution_yes_price = 1.0 if the market question IS the winning bin, else 0.0
RESOLUTIONS = {
    # NYC actual: 46-47°F
    "paper_4fc0b4413e02": 0.0,   # BUY_YES "44-45°F" → not winning bin
    "paper_8b74d66f15b2": 0.0,   # BUY_NO  "48-49°F" → not winning bin
    "paper_e4b4b6568ef8": 0.0,   # BUY_NO  "50-51°F" → not winning bin
    # Miami actual: 80-81°F
    "paper_2f83b722f345": 0.0,   # BUY_NO  "76-77°F" → not winning bin
    "paper_b09ac2e09e28": 0.0,   # BUY_NO  "78-79°F" → not winning bin
    # LA actual: 70-71°F
    "paper_b49bc5c48e6e": 1.0,   # BUY_NO  "70-71°F" → IS the winning bin
    "paper_c75bac2b8603": 0.0,   # BUY_NO  "72-73°F" → not winning bin
    "paper_d6c2e01f799e": 0.0,   # BUY_YES "76°F+"   → not winning bin
    # Houston actual: 78-79°F
    "paper_82f6165dac5f": 0.0,   # BUY_YES "76-77°F" → not winning bin
    "paper_bcc7f9429e71": 0.0,   # BUY_NO  "80-81°F" → not winning bin
    # Dallas actual: 78-79°F
    "paper_ab952c129620": 0.0,   # BUY_YES "74-75°F" → not winning bin
    "paper_4063de28232b": 0.0,   # BUY_YES "76-77°F" → not winning bin
    "paper_26dc9888fe99": 1.0,   # BUY_NO  "78-79°F" → IS the winning bin
    "paper_268232c79fb7": 0.0,   # BUY_NO  "80-81°F" → not winning bin
    # SF actual: 70-71°F
    "paper_72c363ba870c": 0.0,   # BUY_NO  "66-67°F" → not winning bin
    "paper_43ecd3f237bd": 0.0,   # BUY_NO  "68-69°F" → not winning bin
    # Seattle actual: 60-61°F
    "paper_4536362f1e86": 0.0,   # BUY_NO  "58-59°F" → not winning bin
    "paper_b0f3147ca4f3": 1.0,   # BUY_NO  "60-61°F" → IS the winning bin
    "paper_d4b059e9b0b3": 0.0,   # BUY_YES "64°F+"   → not winning bin
    # Atlanta actual: 68-69°F
    "paper_4cc79246997c": 0.0,   # BUY_YES "64-65°F" → not winning bin
}


def compute_pnl(
    signal_type: str,
    entry_price: float,
    resolution_yes_price: float,
    size_usdc: float,
    fees: float,
) -> tuple[float, float]:
    """Compute (exit_price, pnl) matching paper_trader._compute_pnl logic."""
    if signal_type == "buy_no":
        exit_price = 1.0 - resolution_yes_price
    else:
        exit_price = resolution_yes_price

    if entry_price <= 0:
        return exit_price, -fees

    shares = size_usdc / entry_price
    raw_pnl = (exit_price - entry_price) * shares
    return exit_price, raw_pnl - fees


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # Read pending trades
    rows = conn.execute(
        "SELECT * FROM paper_trades WHERE status = 'pending'"
        " AND trade_id NOT IN"
        " (SELECT trade_id FROM paper_trades WHERE status IN ('resolved', 'force_resolved'))"
    ).fetchall()

    if not rows:
        print("No pending trades to resolve.")
        return

    resolved_count = 0
    total_pnl = 0.0
    wins = 0
    losses = 0

    for row in rows:
        trade_id = row["trade_id"]
        if trade_id not in RESOLUTIONS:
            print(f"  SKIP: {trade_id} not in resolution map")
            continue

        res_yes_price = RESOLUTIONS[trade_id]
        exit_price, pnl = compute_pnl(
            signal_type=row["signal_type"],
            entry_price=row["entry_price"],
            resolution_yes_price=res_yes_price,
            size_usdc=row["size_usdc"],
            fees=row["fees"],
        )

        win = pnl > 0
        if win:
            wins += 1
        else:
            losses += 1
        total_pnl += pnl

        # Insert resolved row (same pattern as paper_trader._resolve_trade)
        conn.execute(
            "INSERT INTO paper_trades"
            " (trade_id, market_question, market_id, token_id,"
            "  signal_type, size_usdc, entry_price, exit_price,"
            "  fees, pnl, status, opened_at, resolved_at, resolution_source)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                trade_id,
                row["market_question"],
                row["market_id"],
                row["token_id"],
                row["signal_type"],
                row["size_usdc"],
                row["entry_price"],
                exit_price,
                row["fees"],
                pnl,
                "resolved",
                row["opened_at"],
                NOW,
                "manual_polymarket_data",
            ),
        )

        # Mark traded_position as resolved
        conn.execute(
            "UPDATE traded_positions SET status = 'resolved'"
            " WHERE market_id = ? AND signal_type = ? AND status = 'pending'",
            (row["market_id"], row["signal_type"]),
        )

        outcome = "WIN" if win else "LOSS"
        print(
            f"  {outcome:4s} {trade_id} | {row['signal_type']:7s} | "
            f"entry={row['entry_price']:.4f} exit={exit_price:.4f} "
            f"PnL=${pnl:+.2f} | {row['market_question'][:55]}"
        )
        resolved_count += 1

    conn.commit()
    conn.close()

    print(f"\n{'='*60}")
    print(f"Resolved: {resolved_count} trades")
    print(f"Wins: {wins}, Losses: {losses}, Win rate: {wins/(wins+losses)*100:.0f}%")
    print(f"Total PnL: ${total_pnl:+.2f}")
    print(f"\nRestart the bot to sync in-memory state.")


if __name__ == "__main__":
    main()
