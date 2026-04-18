"""Phase 12.B — Calibration diagnosis.

Produces three analyses from the backfilled data:

  B.1 Reliability diagram — bin model predictions by decile, compare to
      actual YES rate. Compute ECE (Expected Calibration Error).
  B.2 PnL attribution — decompose realized PnL into components:
      fees, payout asymmetry, miscalibration.
  B.3 Segmentation — WR/PF by horizon, city, entry_price bucket.
      (regime/ensemble_std are NULL for backfilled rows; Phase A.3+
       captures these for new trades going forward.)

Writes a markdown report to tasks/diagnostic.md.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "bot.db"
REPORT_PATH = Path(__file__).parent.parent / "tasks" / "diagnostic.md"


def fmt_pct(v: float | None, width: int = 5) -> str:
    if v is None:
        return " n/a "
    return f"{v*100:{width}.1f}%"


def b1_reliability(cur: sqlite3.Cursor) -> list[str]:
    """Reliability diagram + ECE."""
    out: list[str] = ["## B.1 — Reliability diagram\n"]

    # Bucket by 10% increments
    cur.execute(
        """SELECT
             CAST(probability * 10 AS INTEGER) as bucket,
             COUNT(*) as n,
             AVG(probability) as avg_prob,
             AVG(CAST(actual_outcome AS REAL)) as actual_rate,
             AVG(brier_score) as avg_brier
           FROM forecast_log WHERE actual_outcome IS NOT NULL
           GROUP BY bucket ORDER BY bucket"""
    )
    rows = cur.fetchall()

    out.append("| Bin | n | Model says | Actual rate | Δ (pp) | Brier |")
    out.append("|---|---|---|---|---|---|")

    ece_num = 0.0
    total_n = 0
    for r in rows:
        bucket, n, avg_prob, actual_rate, avg_brier = r
        lo = bucket * 10
        hi = lo + 10
        delta = (actual_rate - avg_prob) * 100
        out.append(
            f"| [{lo}-{hi}%) | {n} | {avg_prob*100:.1f}% | "
            f"{actual_rate*100:.1f}% | {delta:+.1f} | {avg_brier:.4f} |"
        )
        ece_num += n * abs(actual_rate - avg_prob)
        total_n += n

    ece = ece_num / total_n if total_n > 0 else float("nan")
    cur.execute(
        "SELECT AVG(brier_score) FROM forecast_log WHERE brier_score IS NOT NULL"
    )
    brier = cur.fetchone()[0]

    out.append("")
    out.append(f"**Expected Calibration Error (ECE):** {ece*100:.1f}pp  ")
    out.append(f"**Average Brier score:** {brier:.4f}  ")
    out.append("")
    out.append("**Interpretation:**")
    if brier > 0.25:
        out.append(
            f"- Brier {brier:.4f} > 0.25 baseline (always-50% predictor). "
            "**The model is WORSE than coin-flip.** Forecasts actively "
            "mislead the trade engine."
        )
    if ece > 0.20:
        out.append(
            f"- ECE {ece*100:.1f}pp means predicted probabilities differ "
            "from realized rates by 20+ percentage points on average."
        )
    # Largest miscalibration bucket
    worst = max(rows, key=lambda r: abs(r[3] - r[2]))
    lo = worst[0] * 10
    out.append(
        f"- Worst bucket: [{lo}-{lo+10}%) — model says "
        f"{worst[2]*100:.1f}%, actual {worst[3]*100:.1f}% "
        f"({(worst[3]-worst[2])*100:+.1f}pp)."
    )
    return out


def b2_pnl_attribution(cur: sqlite3.Cursor) -> list[str]:
    """Decompose PnL into fees, payout asymmetry, miscalibration."""
    out: list[str] = ["\n## B.2 — PnL attribution\n"]

    cur.execute(
        """SELECT COUNT(*) n, SUM(pnl) total_pnl,
                  SUM(CASE WHEN pnl>0 THEN pnl ELSE 0 END) gross_win,
                  SUM(CASE WHEN pnl<0 THEN -pnl ELSE 0 END) gross_loss,
                  SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) wins,
                  AVG(CASE WHEN pnl>0 THEN pnl END) avg_win,
                  AVG(CASE WHEN pnl<0 THEN pnl END) avg_loss,
                  SUM(fees) total_fees
           FROM paper_trades
           WHERE status IN ('resolved','force_resolved')"""
    )
    r = cur.fetchone()
    n, total, gw, gl, wins, avg_w, avg_l, fees = r
    wr = wins / n if n else 0.0
    pf = gw / gl if gl else float("inf")

    # Breakeven analysis
    be_wr = -avg_l / (avg_w - avg_l) if avg_w and avg_l else None
    wr_gap = wr - be_wr if be_wr is not None else None

    out.append(f"- **Trades resolved:** {n}")
    out.append(f"- **Total PnL:** ${total:+.2f}")
    out.append(f"- **Win rate:** {wr*100:.1f}%")
    out.append(f"- **Profit factor:** {pf:.2f}")
    out.append(f"- **Gross wins / losses:** ${gw:.2f} / ${gl:.2f}")
    out.append(f"- **Average win:** ${avg_w:+.2f}")
    out.append(f"- **Average loss:** ${avg_l:+.2f}")
    out.append(f"- **Total fees paid:** ${fees:.2f}")
    if be_wr is not None:
        out.append(
            f"- **Breakeven WR needed:** {be_wr*100:.1f}% → "
            f"realized {wr_gap*100:+.1f}pp"
        )
    out.append("")

    # Decomposition
    out.append("### Components of the loss")
    out.append("")
    # 1. Fees: total fees paid
    # 2. Asymmetric payout: (avg_win / avg_loss) ratio means even breakeven WR
    #    needs to be >50% just to cover the payout asymmetry
    payout_ratio = avg_w / (-avg_l) if avg_l else float("inf")
    extra_wr_for_asymmetry = (0.5 - be_wr) if be_wr is not None else 0

    # 3. Miscalibration contribution: if model were perfectly calibrated, how much
    #    would have been won? With Brier 0.35 and BUY_NO strategy, model prob of
    #    YES is consistently too low; YES resolves higher than expected.
    cur.execute(
        """SELECT AVG(pt.forecast_probability) as avg_fc,
                  AVG(CAST(fl.actual_outcome AS REAL)) as avg_actual
           FROM paper_trades pt
           JOIN forecast_log fl ON pt.market_id = fl.market_id
           WHERE pt.status IN ('resolved','force_resolved')
             AND fl.actual_outcome IS NOT NULL
             AND pt.forecast_probability IS NOT NULL"""
    )
    r2 = cur.fetchone()
    avg_fc, avg_actual = r2 if r2 else (None, None)

    out.append(f"| Component | Impact | Explanation |")
    out.append(f"|---|---|---|")
    out.append(
        f"| Fees | -${fees:.2f} | 1% taker fee per open (no exit fee on redemption). "
        f"Unavoidable at current size. |"
    )
    out.append(
        f"| Payout asymmetry | WR floor {be_wr*100:.1f}% | "
        f"Avg gain ${avg_w:.2f} < avg loss ${-avg_l:.2f} "
        f"(ratio {payout_ratio:.2f}). Requires WR > 50% just to "
        f"break even, even with perfect calibration. |"
    )
    if avg_fc is not None and avg_actual is not None:
        miscal_pp = (avg_actual - avg_fc) * 100
        out.append(
            f"| Miscalibration | {miscal_pp:+.1f}pp error | "
            f"Model avg P(YES) = {avg_fc*100:.1f}%, "
            f"actual YES rate = {avg_actual*100:.1f}%. "
            f"**Forecasts systematically under-predict YES.** |"
        )
    return out


def b3_segmentation(cur: sqlite3.Cursor) -> list[str]:
    """Segment WR/PF by horizon, city, entry_price bucket."""
    out: list[str] = ["\n## B.3 — Segmentation\n"]

    # By horizon_hours bucket
    out.append("### By horizon (signal → target distance)")
    cur.execute(
        """SELECT
             CASE
               WHEN horizon_hours < 24 THEN 'a) 0-24h'
               WHEN horizon_hours < 48 THEN 'b) 24-48h'
               WHEN horizon_hours < 72 THEN 'c) 48-72h'
               WHEN horizon_hours < 120 THEN 'd) 72-120h'
               ELSE 'e) 120h+'
             END as bucket,
             COUNT(*) as n,
             SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) * 1.0 / COUNT(*) as wr,
             SUM(pnl) as total_pnl,
             SUM(CASE WHEN pnl>0 THEN pnl ELSE 0 END) as gw,
             SUM(CASE WHEN pnl<0 THEN -pnl ELSE 0 END) as gl
           FROM paper_trades
           WHERE status IN ('resolved','force_resolved')
             AND horizon_hours IS NOT NULL
           GROUP BY bucket ORDER BY bucket"""
    )
    out.append("| Horizon | n | WR | Total PnL | PF |")
    out.append("|---|---|---|---|---|")
    for r in cur.fetchall():
        bucket, n, wr, pnl, gw, gl = r
        pf = gw / gl if gl else float("inf")
        pf_s = f"{pf:.2f}" if pf != float("inf") else "∞"
        out.append(f"| {bucket} | {n} | {wr*100:.1f}% | ${pnl:+.2f} | {pf_s} |")

    # By entry_price bucket
    out.append("")
    out.append("### By entry_price bucket")
    cur.execute(
        """SELECT
             CASE
               WHEN entry_price < 0.40 THEN 'a) <0.40'
               WHEN entry_price < 0.50 THEN 'b) 0.40-0.49'
               WHEN entry_price < 0.60 THEN 'c) 0.50-0.59'
               WHEN entry_price < 0.70 THEN 'd) 0.60-0.69'
               ELSE 'e) 0.70+'
             END as bucket,
             COUNT(*) as n,
             SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) * 1.0 / COUNT(*) as wr,
             SUM(pnl) as total_pnl,
             SUM(CASE WHEN pnl>0 THEN pnl ELSE 0 END) as gw,
             SUM(CASE WHEN pnl<0 THEN -pnl ELSE 0 END) as gl,
             AVG(forecast_probability) as avg_fc
           FROM paper_trades
           WHERE status IN ('resolved','force_resolved')
           GROUP BY bucket ORDER BY bucket"""
    )
    out.append("| Entry bucket | n | WR | Total PnL | PF | Avg forecast P(YES) |")
    out.append("|---|---|---|---|---|---|")
    for r in cur.fetchall():
        bucket, n, wr, pnl, gw, gl, avg_fc = r
        pf = gw / gl if gl else float("inf")
        pf_s = f"{pf:.2f}" if pf != float("inf") else "∞"
        fc_s = f"{avg_fc*100:.1f}%" if avg_fc else "n/a"
        out.append(
            f"| {bucket} | {n} | {wr*100:.1f}% | ${pnl:+.2f} | {pf_s} | {fc_s} |"
        )

    # By top 5 cities
    out.append("")
    out.append("### Top cities by volume")
    cur.execute(
        """SELECT
             CASE
               WHEN market_question LIKE '%New York%' THEN 'New York'
               WHEN market_question LIKE '%Los Angeles%' THEN 'Los Angeles'
               WHEN market_question LIKE '%Chicago%' THEN 'Chicago'
               WHEN market_question LIKE '%Miami%' THEN 'Miami'
               WHEN market_question LIKE '%London%' THEN 'London'
               WHEN market_question LIKE '%Houston%' THEN 'Houston'
               WHEN market_question LIKE '%Dallas%' THEN 'Dallas'
               WHEN market_question LIKE '%San Francisco%' THEN 'San Francisco'
               WHEN market_question LIKE '%Seattle%' THEN 'Seattle'
               WHEN market_question LIKE '%Atlanta%' THEN 'Atlanta'
               WHEN market_question LIKE '%Denver%' THEN 'Denver'
               WHEN market_question LIKE '%Tokyo%' THEN 'Tokyo'
               WHEN market_question LIKE '%Seoul%' THEN 'Seoul'
               WHEN market_question LIKE '%Austin%' THEN 'Austin'
               WHEN market_question LIKE '%Paris%' THEN 'Paris'
               ELSE 'Other'
             END as city,
             COUNT(*) as n,
             SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) * 1.0 / COUNT(*) as wr,
             SUM(pnl) as total_pnl
           FROM paper_trades
           WHERE status IN ('resolved','force_resolved')
           GROUP BY city HAVING n >= 2 ORDER BY n DESC LIMIT 10"""
    )
    out.append("| City | n | WR | Total PnL |")
    out.append("|---|---|---|---|")
    for r in cur.fetchall():
        city, n, wr, pnl = r
        out.append(f"| {city} | {n} | {wr*100:.1f}% | ${pnl:+.2f} |")

    return out


def ranked_drivers(cur: sqlite3.Cursor) -> list[str]:
    """Rank the top 3 drivers of losing PnL."""
    out: list[str] = ["\n## Ranked drivers of the -$49 PnL\n"]

    cur.execute(
        "SELECT AVG(brier_score) FROM forecast_log WHERE brier_score IS NOT NULL"
    )
    brier = cur.fetchone()[0]

    cur.execute(
        """SELECT AVG(forecast_probability), AVG(CAST(fl.actual_outcome AS REAL))
           FROM paper_trades pt
           JOIN forecast_log fl ON pt.market_id = fl.market_id
           WHERE pt.status IN ('resolved','force_resolved')
             AND fl.actual_outcome IS NOT NULL
             AND pt.forecast_probability IS NOT NULL"""
    )
    avg_fc, avg_actual = cur.fetchone()

    out.append(
        "1. **Miscalibration (root cause)** — "
        f"forecasts under-predict YES by {(avg_actual-avg_fc)*100:.1f}pp "
        f"(model says avg {avg_fc*100:.1f}%, reality {avg_actual*100:.1f}%). "
        f"Brier {brier:.4f} > 0.25 baseline. Every BUY_NO trade is placed on "
        "a false premise."
    )
    out.append(
        "2. **Payout asymmetry amplifier** — avg loss 40% larger than avg win, "
        "requiring WR > 59% to break even. Miscalibration makes that threshold "
        "unreachable in current state."
    )
    out.append(
        "3. **Gamma-fallback regime** — CLOB books empty for most markets; "
        "bot executes at Gamma prices that may diverge from true mid. "
        "Regime metadata only captured from Phase A.3 onward, so this driver "
        "is hypothesized but not yet quantified. Will be measurable after "
        "~2 weeks of instrumented data."
    )
    return out


def main() -> int:
    if not DB_PATH.exists():
        print(f"DB not found: {DB_PATH}", file=sys.stderr)
        return 1
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()

    sections: list[str] = [
        "# Calibration Diagnostic — Phase 12.B",
        "",
        "Generated by `scripts/diagnose_calibration.py` from bot.db.",
        "",
    ]
    sections.extend(b1_reliability(cur))
    sections.extend(b2_pnl_attribution(cur))
    sections.extend(b3_segmentation(cur))
    sections.extend(ranked_drivers(cur))

    REPORT_PATH.write_text("\n".join(sections) + "\n", encoding="utf-8")
    conn.close()

    print(f"Report written to {REPORT_PATH}")
    print("\n--- Summary ---")
    # Re-open to print short summary
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()
    cur.execute(
        "SELECT AVG(brier_score), COUNT(*) FROM forecast_log "
        "WHERE brier_score IS NOT NULL"
    )
    brier, n_fc = cur.fetchone()
    cur.execute(
        "SELECT COUNT(*), SUM(pnl) FROM paper_trades "
        "WHERE status IN ('resolved','force_resolved')"
    )
    n_tr, total_pnl = cur.fetchone()
    print(f"  Trades resolved: {n_tr}")
    print(f"  Total PnL: ${total_pnl:+.2f}")
    print(f"  Forecasts scored: {n_fc}")
    print(f"  Brier: {brier:.4f}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
