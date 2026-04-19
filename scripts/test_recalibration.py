"""Phase 12.D.H1 — test isotonic/binned recalibration via leave-one-out CV.

For each of the 72 resolved trades:
1. Fit a binned calibrator on the OTHER 71 trades:
     calibrated(prob) = mean(actual_outcome | prob in same decile bin)
2. Apply calibrator to this trade's forecast_probability.
3. Recompute the edge: for BUY_NO, edge = entry_price - calibrated_prob.
4. Decide: would the bot have opened this trade if it had the calibrated
   forecast? (apply min_edge_threshold = 0.05 and net of 1% fee)
5. If would_open: pnl = actual pnl. If not: pnl = 0 (trade skipped).

Compare: actual PnL vs simulated PnL with recalibration.

Also reports: counterfactual WR, PF, how many trades are filtered out.

Writes summary to tasks/recalibration_test.md.
"""

from __future__ import annotations

import sqlite3
import statistics
import sys
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "bot.db"
REPORT_PATH = Path(__file__).parent.parent / "tasks" / "recalibration_test.md"

MIN_EDGE_THRESHOLD = 0.05  # same as .env
TAKER_FEE_PCT = 0.01


def pav_isotonic(x: list[float], y: list[float]) -> list[float]:
    """Pool Adjacent Violators — fit isotonic (monotone non-decreasing) regression.

    Returns calibrated y values aligned with the sorted-by-x order.
    """
    # Sort by x; keep indices to unsort
    n = len(x)
    order = sorted(range(n), key=lambda i: x[i])
    y_sorted = [y[i] for i in order]
    weights = [1.0] * n
    # PAV algorithm
    i = 0
    while i < len(y_sorted) - 1:
        if y_sorted[i] > y_sorted[i + 1]:
            # Merge blocks
            total_w = weights[i] + weights[i + 1]
            merged = (y_sorted[i] * weights[i] + y_sorted[i + 1] * weights[i + 1]) / total_w
            y_sorted[i:i + 2] = [merged]
            weights[i:i + 2] = [total_w]
            if i > 0:
                i -= 1
        else:
            i += 1
    # Expand pools back out
    result_sorted = []
    for yv, w in zip(y_sorted, weights):
        result_sorted.extend([yv] * int(w))
    # Unsort back to original positions
    result = [0.0] * n
    for orig_idx, sorted_idx in enumerate(order):
        result[sorted_idx] = result_sorted[orig_idx]
    return result


def apply_isotonic(x_train: list[float], y_calibrated: list[float],
                   x_query: float) -> float:
    """Look up calibrated y for a new x via nearest neighbors in x_train."""
    # Find position via binary search
    pairs = sorted(zip(x_train, y_calibrated))
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    if x_query <= xs[0]:
        return ys[0]
    if x_query >= xs[-1]:
        return ys[-1]
    # Linear interpolation between bracketing points
    for i in range(len(xs) - 1):
        if xs[i] <= x_query <= xs[i + 1]:
            if xs[i + 1] == xs[i]:
                return ys[i]
            t = (x_query - xs[i]) / (xs[i + 1] - xs[i])
            return ys[i] + t * (ys[i + 1] - ys[i])
    return ys[-1]


def recalibrate_and_simulate(conn: sqlite3.Connection) -> tuple[list[dict], dict]:
    """Leave-one-out CV recalibration. Returns (per-trade decisions, summary)."""
    cur = conn.cursor()
    cur.execute(
        """SELECT pt.trade_id, pt.signal_type, pt.entry_price, pt.pnl,
                  pt.size_usdc, pt.forecast_probability,
                  fl.actual_outcome
           FROM paper_trades pt
           JOIN forecast_log fl ON pt.market_id = fl.market_id
           WHERE pt.status IN ('resolved','force_resolved')
             AND pt.forecast_probability IS NOT NULL
             AND fl.actual_outcome IS NOT NULL
           GROUP BY pt.trade_id
           ORDER BY pt.opened_at"""
    )
    rows = [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]

    if not rows:
        return [], {}

    decisions = []
    for i, trade in enumerate(rows):
        # LOOCV: fit on all except current
        train = [r for j, r in enumerate(rows) if j != i]
        x_train = [float(r["forecast_probability"]) for r in train]
        y_train = [float(r["actual_outcome"]) for r in train]
        y_cal_train = pav_isotonic(x_train, y_train)

        # Calibrated P(YES) for this trade
        cal_prob = apply_isotonic(x_train, y_cal_train,
                                  float(trade["forecast_probability"]))

        # Recompute edge for BUY_NO (same logic as core/edge_calculator)
        # BUY_NO wins if YES resolves false. Cost is entry_price_NO = 1 - entry_price_YES.
        # Edge (BUY_NO) = entry_price_YES - calibrated_P(YES), net of 1% fee.
        # Actually re-derive from the same formula the bot uses:
        # For BUY_NO: raw_edge = yes_price - prob_yes
        # net_edge = raw_edge - fee
        if trade["signal_type"] == "buy_no":
            raw_edge = trade["entry_price"] - cal_prob
        else:  # buy_yes
            raw_edge = cal_prob - trade["entry_price"]
        net_edge = raw_edge - TAKER_FEE_PCT

        would_open = net_edge >= MIN_EDGE_THRESHOLD
        simulated_pnl = trade["pnl"] if would_open else 0.0

        decisions.append({
            "trade_id": trade["trade_id"],
            "signal_type": trade["signal_type"],
            "entry_price": trade["entry_price"],
            "forecast_prob": trade["forecast_probability"],
            "calibrated_prob": cal_prob,
            "net_edge_new": net_edge,
            "would_open": would_open,
            "actual_pnl": trade["pnl"],
            "simulated_pnl": simulated_pnl,
            "actual_outcome": trade["actual_outcome"],
        })

    # Summary
    actual_total = sum(d["actual_pnl"] for d in decisions)
    sim_total = sum(d["simulated_pnl"] for d in decisions)
    opened = [d for d in decisions if d["would_open"]]
    opened_wins = sum(1 for d in opened if d["simulated_pnl"] > 0)
    opened_losses = sum(1 for d in opened if d["simulated_pnl"] < 0)
    gw = sum(d["simulated_pnl"] for d in opened if d["simulated_pnl"] > 0)
    gl = sum(-d["simulated_pnl"] for d in opened if d["simulated_pnl"] < 0)

    summary = {
        "n_total": len(decisions),
        "actual_total_pnl": actual_total,
        "sim_total_pnl": sim_total,
        "actual_trades_opened": len(decisions),
        "sim_trades_opened": len(opened),
        "sim_filter_rate": 1 - len(opened) / len(decisions) if decisions else 0,
        "sim_wins": opened_wins,
        "sim_losses": opened_losses,
        "sim_wr": opened_wins / len(opened) if opened else None,
        "sim_pf": gw / gl if gl > 0 else (float("inf") if gw > 0 else None),
    }
    return decisions, summary


def write_report(decisions: list[dict], summary: dict) -> None:
    out = [
        "# Recalibration Counterfactual — Phase 12.D.H1",
        "",
        "Leave-one-out isotonic (PAV) recalibration of forecast probability.",
        "Replays the 72 resolved trades asking: *would a calibrated model have opened each trade, and what would PnL have been?*",
        "",
        "**Assumptions:**",
        "- Same market price (entry_price) used — we don't simulate different entry timing.",
        "- Same `min_edge_threshold = 0.05` and 1% taker fee.",
        "- Skipped trades contribute PnL = 0.",
        "- LOOCV prevents in-sample leakage (each trade calibrated on the other 71).",
        "",
        "## Summary",
        "",
        f"| Metric | Actual | With recalibration | Δ |",
        f"|---|---|---|---|",
        f"| Trades opened | {summary['n_total']} | {summary['sim_trades_opened']} | "
        f"−{summary['n_total'] - summary['sim_trades_opened']} ({summary['sim_filter_rate']*100:.0f}% filtered) |",
        f"| Total PnL | ${summary['actual_total_pnl']:+.2f} | "
        f"${summary['sim_total_pnl']:+.2f} | "
        f"${summary['sim_total_pnl'] - summary['actual_total_pnl']:+.2f} |",
    ]
    if summary["sim_wr"] is not None:
        out.append(
            f"| Win rate (of trades opened) | 55.6% | "
            f"{summary['sim_wr']*100:.1f}% | "
            f"{(summary['sim_wr'] - 0.556)*100:+.1f}pp |"
        )
    if summary["sim_pf"] is not None:
        out.append(
            f"| Profit factor | 0.86 | {summary['sim_pf']:.2f} | "
            f"{summary['sim_pf'] - 0.86:+.2f} |"
        )
    out.append("")

    # Calibration delta distribution
    deltas = [d["calibrated_prob"] - d["forecast_prob"] for d in decisions]
    out.append("### Calibration shift distribution")
    out.append("")
    out.append(
        f"- Mean shift: `calibrated - forecast = {statistics.mean(deltas)*100:+.1f}pp`"
    )
    out.append(
        f"- Median shift: `{statistics.median(deltas)*100:+.1f}pp`"
    )
    out.append(
        f"- Range: [{min(deltas)*100:+.1f}pp, {max(deltas)*100:+.1f}pp]"
    )
    out.append("")

    # Sample of filtered vs opened trades
    out.append("### First 10 trade decisions (sample)")
    out.append("")
    out.append("| trade_id | side | entry | model | calibrated | new_edge | would_open | outcome | pnl |")
    out.append("|---|---|---|---|---|---|---|---|---|")
    for d in decisions[:10]:
        status = "OPEN" if d["would_open"] else "SKIP"
        out.append(
            f"| {d['trade_id'][:12]} | {d['signal_type']} | "
            f"{d['entry_price']:.3f} | {d['forecast_prob']*100:.1f}% | "
            f"{d['calibrated_prob']*100:.1f}% | {d['net_edge_new']:+.3f} | "
            f"{status} | YES={'T' if d['actual_outcome'] else 'F'} | "
            f"${d['actual_pnl']:+.2f} |"
        )
    out.append("")

    # Verdict
    out.append("## Verdict")
    out.append("")
    improvement = summary["sim_total_pnl"] - summary["actual_total_pnl"]
    if summary["sim_total_pnl"] > 0:
        out.append(
            f"- Recalibration turns a ${summary['actual_total_pnl']:.2f} loss "
            f"into a ${summary['sim_total_pnl']:+.2f} profit (Δ = ${improvement:+.2f}). "
            "H1 is a strong candidate — but n=72 and LOOCV may overfit the "
            "historical regime. Validate in paper over Phase E."
        )
    elif improvement > 0:
        out.append(
            f"- Recalibration reduces loss from ${summary['actual_total_pnl']:.2f} "
            f"to ${summary['sim_total_pnl']:.2f} (Δ = ${improvement:+.2f}). "
            "Directional improvement but not yet profitable. "
            "Consider combining with stricter edge threshold or regime gating."
        )
    else:
        out.append(
            f"- Recalibration makes PnL worse (${improvement:+.2f}). "
            "H1 alone is insufficient. Need different approach "
            "(Platt scaling, or reject current model entirely)."
        )
    out.append("")
    out.append(
        f"- **Filter rate: {summary['sim_filter_rate']*100:.0f}%** of historical "
        "trades would have been skipped. This is a strong argument for adding "
        "stricter entry gates."
    )

    REPORT_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")


def main() -> int:
    if not DB_PATH.exists():
        print(f"DB not found: {DB_PATH}", file=sys.stderr)
        return 1
    conn = sqlite3.connect(str(DB_PATH))
    decisions, summary = recalibrate_and_simulate(conn)
    conn.close()
    if not decisions:
        print("No resolved trades with forecast probability to analyze.")
        return 1

    write_report(decisions, summary)
    print(f"Report written to {REPORT_PATH}")
    print("\n--- Summary ---")
    print(f"  Actual PnL:       ${summary['actual_total_pnl']:+.2f} "
          f"({summary['n_total']} trades)")
    print(f"  Simulated PnL:    ${summary['sim_total_pnl']:+.2f} "
          f"({summary['sim_trades_opened']} trades, "
          f"{summary['sim_filter_rate']*100:.0f}% filtered)")
    if summary["sim_wr"] is not None:
        print(f"  Simulated WR:     {summary['sim_wr']*100:.1f}%")
    if summary["sim_pf"] is not None:
        print(f"  Simulated PF:     {summary['sim_pf']:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
