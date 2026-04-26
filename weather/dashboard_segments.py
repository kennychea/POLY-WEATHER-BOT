"""Segment ranking with Wilson CI + GATE flags — dashboard v3.

Pure compute + Rich render. Read-only on data/bot.db (via existing
dashboard_widgets._fetch_paper_trades). Used by the new --segments flag and
its three focus sub-flags (--by-city / --by-horizon / --by-bucket).

Flagging rule:
    breakeven = weighted mean of entry_prices in the segment
    GATE_OUT  : Wilson_lower_WR <  breakeven                 (red)
    GATE_KEEP : Wilson_lower_WR >  breakeven                 (green)
    WATCH     : breakeven sits inside [Wilson_lower, Wilson_upper]  (yellow)

Segments with n < MIN_N are excluded from the table (noise).
"""

from __future__ import annotations

import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from rich import box
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from weather.dashboard_widgets import (  # noqa: E402
    _fetch_paper_trades, _wilson_ci,
)

MIN_N = 10  # exclude segments below this from the ranking
GateFlag = Literal["GATE_OUT", "GATE_KEEP", "WATCH"]


@dataclass(frozen=True)
class SegmentRow:
    label: str
    n: int
    pnl_total: float
    pnl_per_trade: float
    wr: float
    wilson_lower: float
    wilson_upper: float
    breakeven: float
    flag: GateFlag


# ── pure compute ──────────────────────────────────────────────────────


def _bucket_horizon(h: Any) -> str:
    if h is None:
        return "na"
    try:
        h = float(h)
    except (TypeError, ValueError):
        return "na"
    if h < 24:
        return "0-24h"
    if h < 48:
        return "24-48h"
    if h < 72:
        return "48-72h"
    if h < 120:
        return "72-120h"
    return ">=120h"


def _bucket_forecast(p: Any) -> str:
    if p is None:
        return "na"
    try:
        p = float(p)
    except (TypeError, ValueError):
        return "na"
    if p < 0.05:
        return "<0.05"
    if p < 0.15:
        return "0.05-0.15"
    if p < 0.30:
        return "0.15-0.30"
    if p < 0.50:
        return "0.30-0.50"
    if p < 0.70:
        return "0.50-0.70"
    if p < 0.85:
        return "0.70-0.85"
    if p < 0.95:
        return "0.85-0.95"
    return ">=0.95"


def _extract_city(question: str | None) -> str:
    if not question:
        return "?"
    q_lower = question.lower()
    if " in " not in q_lower or " be " not in q_lower:
        return "?"
    return q_lower.split(" in ", 1)[1].split(" be ", 1)[0].strip().title()


def _classify(wilson_lo: float, wilson_hi: float, breakeven: float) -> GateFlag:
    if breakeven < wilson_lo:
        return "GATE_KEEP"
    if breakeven > wilson_hi:
        return "GATE_OUT"
    return "WATCH"


def _compute_segment(label: str, trades: list[dict]) -> SegmentRow | None:
    n = len(trades)
    if n < MIN_N:
        return None
    pnls = [float(t["pnl"]) for t in trades if t.get("pnl") is not None]
    if not pnls:
        return None
    wins = sum(1 for p in pnls if p > 0)
    pnl_total = sum(pnls)
    wr = wins / n
    wilson_lo, wilson_hi = _wilson_ci(wins, n)
    entries = [float(t["entry_price"]) for t in trades
               if t.get("entry_price") is not None]
    if entries:
        breakeven = sum(entries) / len(entries)
    else:
        breakeven = 0.55  # BUY_NO band midpoint default
    return SegmentRow(
        label=label, n=n,
        pnl_total=pnl_total,
        pnl_per_trade=pnl_total / n,
        wr=wr,
        wilson_lower=wilson_lo, wilson_upper=wilson_hi,
        breakeven=breakeven,
        flag=_classify(wilson_lo, wilson_hi, breakeven),
    )


def _group_resolved_trades(trades: list[dict]) -> list[dict]:
    return [
        t for t in trades
        if t.get("status") in ("resolved", "force_resolved")
        and t.get("pnl") is not None
    ]


def compute_segments_by_city(trades: list[dict]) -> list[SegmentRow]:
    resolved = _group_resolved_trades(trades)
    by_city: dict[str, list[dict]] = defaultdict(list)
    for t in resolved:
        by_city[_extract_city(t.get("market_question"))].append(t)
    rows = [r for r in (_compute_segment(c, ts) for c, ts in by_city.items()) if r]
    rows.sort(key=lambda r: r.pnl_per_trade)  # worst first
    return rows


def compute_segments_by_horizon(trades: list[dict]) -> list[SegmentRow]:
    resolved = _group_resolved_trades(trades)
    by_h: dict[str, list[dict]] = defaultdict(list)
    for t in resolved:
        by_h[_bucket_horizon(t.get("horizon_hours"))].append(t)
    rows = [r for r in (_compute_segment(h, ts) for h, ts in by_h.items()) if r]
    rows.sort(key=lambda r: r.pnl_per_trade)
    return rows


def compute_segments_by_bucket(trades: list[dict]) -> list[SegmentRow]:
    resolved = _group_resolved_trades(trades)
    by_b: dict[str, list[dict]] = defaultdict(list)
    for t in resolved:
        by_b[_bucket_forecast(t.get("forecast_probability"))].append(t)
    rows = [r for r in (_compute_segment(b, ts) for b, ts in by_b.items()) if r]
    rows.sort(key=lambda r: r.pnl_per_trade)
    return rows


# ── render ────────────────────────────────────────────────────────────


_FLAG_STYLE = {
    "GATE_OUT": "red",
    "GATE_KEEP": "green",
    "WATCH": "yellow",
}


def _render_segment_table(title: str, rows: list[SegmentRow]) -> Table:
    t = Table(title=title, box=box.SIMPLE_HEAD, show_edge=False, expand=False)
    t.add_column("segment")
    t.add_column("n", justify="right")
    t.add_column("PnL_total", justify="right")
    t.add_column("PnL/trade", justify="right")
    t.add_column("WR", justify="right")
    t.add_column("Wilson_lo", justify="right")
    t.add_column("breakeven", justify="right")
    t.add_column("flag", justify="center")
    if not rows:
        t.add_row("(no segments — n<10 or no data)", "", "", "", "", "", "", "")
        return t
    for r in rows:
        t.add_row(
            r.label, str(r.n),
            f"${r.pnl_total:+.2f}",
            f"${r.pnl_per_trade:+.3f}",
            f"{r.wr:.1%}",
            f"{r.wilson_lower:.3f}",
            f"{r.breakeven:.3f}",
            Text(r.flag, style=_FLAG_STYLE[r.flag]),
        )
    return t


_FOOTER = (
    "Wilson CI à 95% — segments avec n<10 exclus (bruit). "
    "Décisions gate finales à valider en Pass 2 sur n>=1500."
)


def build_segments_panel(db_path: str) -> Panel:
    trades = _fetch_paper_trades(db_path)
    cities = compute_segments_by_city(trades)
    horizons = compute_segments_by_horizon(trades)
    buckets = compute_segments_by_bucket(trades)

    grid = Table.grid(padding=(1, 0))
    grid.add_row(_render_segment_table("By city (PnL/trade ASC — worst first)", cities))
    grid.add_row(_render_segment_table("By horizon", horizons))
    grid.add_row(_render_segment_table("By forecast bucket", buckets))
    grid.add_row(Text(_FOOTER, style="dim"))
    return Panel(grid, title="Segments — gate-out / gate-keep candidates",
                 border_style="blue")


def build_segments_focused_panel(db_path: str, dim: str) -> Panel:
    """Single-dimension focused view for --by-city / --by-horizon / --by-bucket."""
    trades = _fetch_paper_trades(db_path)
    if dim == "city":
        rows = compute_segments_by_city(trades)
        title = "Segments by city (full screen)"
    elif dim == "horizon":
        rows = compute_segments_by_horizon(trades)
        title = "Segments by horizon (full screen)"
    elif dim == "bucket":
        rows = compute_segments_by_bucket(trades)
        title = "Segments by forecast bucket (full screen)"
    else:
        raise ValueError(f"unknown dim: {dim}")

    grid = Table.grid(padding=(1, 0))
    grid.add_row(_render_segment_table(title, rows))
    grid.add_row(Text(_FOOTER, style="dim"))
    return Panel(grid, title=title, border_style="blue")
