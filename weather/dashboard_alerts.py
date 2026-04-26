"""Anomaly alert engine for the dashboard — dashboard v3.

8 rules across 3 severities. Snapshots written to data/dashboard_history.json
(rolling 7 days) so brier_diverging can compare against prior snapshots.

Rules:
  CRITICAL
    data_only_mode_off
    new_paper_trades_in_window
    ingestion_dropped
  WARNING
    outcome_skew_today
    brier_diverging
    pending_resolution_lag
  INFO
    extreme_price_share_high
    new_market_outage
"""

from __future__ import annotations

import json
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from rich.panel import Panel
from rich.table import Table
from rich.text import Text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

Severity = Literal["CRITICAL", "WARNING", "INFO"]


@dataclass(frozen=True)
class Alert:
    severity: Severity
    rule: str
    message: str


SEVERITY_ORDER = {"CRITICAL": 0, "WARNING": 1, "INFO": 2}
SEVERITY_STYLE = {"CRITICAL": "red", "WARNING": "yellow", "INFO": "cyan"}

INGESTION_DROPPED_THRESHOLD = 100  # rows/h
OUTCOME_SKEW_DELTA = 0.30
BRIER_DIVERGE_PCT = 0.20
PENDING_TRADES_THRESHOLD = 100
PENDING_AGE_DAYS_THRESHOLD = 5
EXTREME_PRICE_SHARE_THRESHOLD = 0.30
HISTORY_DAYS = 7


# ── persistence: rolling snapshot history ─────────────────────────────


def _read_history(history_path: Path) -> list[dict]:
    if not history_path.exists():
        return []
    try:
        data = json.loads(history_path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _write_history(history_path: Path, snapshots: list[dict]) -> None:
    try:
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_path.write_text(json.dumps(snapshots, indent=2), encoding="utf-8")
    except OSError:
        pass  # silent failure — alerts shouldn't break the dashboard


def append_snapshot(history_path: Path, snapshot: dict) -> list[dict]:
    """Append a snapshot, prune older than HISTORY_DAYS, return final list."""
    snapshots = _read_history(history_path)
    snapshots.append(snapshot)
    cutoff = datetime.now(UTC) - timedelta(days=HISTORY_DAYS)
    fresh = []
    for s in snapshots:
        ts = s.get("timestamp")
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if dt >= cutoff:
                fresh.append(s)
        except ValueError:
            continue
    _write_history(history_path, fresh)
    return fresh


# ── individual rules ──────────────────────────────────────────────────


def _is_data_only_mode_safe() -> bool | None:
    try:
        from infra.config import is_data_only_mode  # noqa: WPS433
        return bool(is_data_only_mode())
    except Exception:
        return None


def _query_one(db_path: str, sql: str, params: tuple = ()) -> Any:
    if not Path(db_path).exists():
        return None
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.execute(sql, params)
        row = cur.fetchone()
        conn.close()
        return row[0] if row else None
    except sqlite3.OperationalError:
        return None


def alert_data_only_mode_off() -> Alert | None:
    mode = _is_data_only_mode_safe()
    if mode is False:
        return Alert("CRITICAL", "data_only_mode_off",
                     "DATA_ONLY_MODE=False during collection phase")
    return None


def alert_new_paper_trades_in_window(db_path: str) -> Alert | None:
    """If DATA_ONLY=on and new paper_trades opened in the last 24h → critical."""
    if _is_data_only_mode_safe() is not True:
        return None
    cutoff = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
    n = _query_one(
        db_path,
        "SELECT COUNT(*) FROM paper_trades WHERE opened_at >= ?",
        (cutoff,),
    )
    if n is not None and n > 0:
        return Alert("CRITICAL", "new_paper_trades_in_window",
                     f"{n} paper_trade(s) opened in last 24h while DATA_ONLY=on")
    return None


def alert_ingestion_dropped(db_path: str) -> Alert | None:
    cutoff = (datetime.now(UTC) - timedelta(hours=6)).isoformat()
    n = _query_one(
        db_path,
        "SELECT COUNT(*) FROM weather_signals WHERE timestamp >= ?",
        (cutoff,),
    )
    if n is None:
        return None
    rate_per_h = n / 6.0
    if rate_per_h < INGESTION_DROPPED_THRESHOLD:
        return Alert("CRITICAL", "ingestion_dropped",
                     f"ingestion rate {rate_per_h:.0f} rows/h < threshold "
                     f"{INGESTION_DROPPED_THRESHOLD} (6h moving average)")
    return None


def alert_outcome_skew_today(db_path: str) -> Alert | None:
    """If today's resolved markets have a YES rate > 30 pts off the long-run base rate."""
    today = datetime.now(UTC).date().isoformat()
    if not Path(db_path).exists():
        return None
    try:
        conn = sqlite3.connect(db_path)
        # Long-run base rate (excluding today)
        cur = conn.execute(
            "SELECT AVG(actual_outcome), COUNT(*) FROM weather_signals "
            "WHERE actual_outcome IS NOT NULL AND DATE(timestamp) < ?",
            (today,),
        )
        base_avg, base_n = cur.fetchone()
        # Today's mean
        cur = conn.execute(
            "SELECT AVG(actual_outcome), COUNT(*) FROM weather_signals "
            "WHERE actual_outcome IS NOT NULL AND DATE(timestamp) = ?",
            (today,),
        )
        today_avg, today_n = cur.fetchone()
        conn.close()
    except sqlite3.OperationalError:
        return None
    if base_avg is None or today_avg is None or today_n < 5 or base_n < 50:
        return None
    if abs(today_avg - base_avg) > OUTCOME_SKEW_DELTA:
        return Alert("WARNING", "outcome_skew_today",
                     f"today YES rate {today_avg:.2f} vs long-run {base_avg:.2f} "
                     f"(d={abs(today_avg - base_avg):.2f}) on n={today_n} resolved today")
    return None


def alert_brier_diverging(history: list[dict]) -> Alert | None:
    """Compare current brier_delta vs the snapshot ~3 days ago."""
    if len(history) < 2:
        return Alert("WARNING", "brier_diverging",
                     "no baseline yet - need >=2 history snapshots")
    current = history[-1]
    cur_delta = current.get("brier_delta")
    if cur_delta is None:
        return None
    cutoff = datetime.now(UTC) - timedelta(days=3, hours=12)
    candidates = []
    for s in history[:-1]:
        ts = s.get("timestamp")
        if not ts or s.get("brier_delta") is None:
            continue
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt <= cutoff:
            candidates.append(s)
    if not candidates:
        return None
    base = candidates[-1]
    base_delta = base["brier_delta"]
    if base_delta == 0:
        return None
    pct_change = (cur_delta - base_delta) / abs(base_delta)
    if pct_change > BRIER_DIVERGE_PCT:
        return Alert("WARNING", "brier_diverging",
                     f"brier_delta worsened {pct_change*100:.0f}% vs 3-day-old baseline "
                     f"({base_delta:+.4f} → {cur_delta:+.4f})")
    return None


def alert_pending_resolution_lag(db_path: str) -> Alert | None:
    n_pending = _query_one(db_path, "SELECT COUNT(*) FROM paper_trades WHERE status='pending'")
    if n_pending is None:
        return None
    if n_pending > PENDING_TRADES_THRESHOLD:
        return Alert("WARNING", "pending_resolution_lag",
                     f"{n_pending} pending paper_trades > threshold {PENDING_TRADES_THRESHOLD}")
    cutoff = (datetime.now(UTC) - timedelta(days=PENDING_AGE_DAYS_THRESHOLD)).isoformat()
    n_old = _query_one(
        db_path,
        "SELECT COUNT(*) FROM paper_trades WHERE status='pending' AND opened_at < ?",
        (cutoff,),
    )
    if n_old is not None and n_old > 0:
        return Alert("WARNING", "pending_resolution_lag",
                     f"{n_old} pending paper_trades older than {PENDING_AGE_DAYS_THRESHOLD}d")
    return None


def alert_extreme_price_share_high(db_path: str) -> Alert | None:
    today = datetime.now(UTC).date().isoformat()
    if not Path(db_path).exists():
        return None
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.execute(
            "SELECT outcome_source, COUNT(DISTINCT market_id) "
            "FROM weather_signals "
            "WHERE outcome_source IS NOT NULL AND DATE(timestamp) = ? "
            "GROUP BY outcome_source",
            (today,),
        )
        rows = cur.fetchall()
        conn.close()
    except sqlite3.OperationalError:
        return None
    counts = {src: n for src, n in rows}
    total = sum(counts.values())
    if total == 0:
        return None
    ep = counts.get("polymarket_extreme_price", 0)
    share = ep / total
    if share > EXTREME_PRICE_SHARE_THRESHOLD:
        return Alert(
            "INFO", "extreme_price_share_high",
            f"polymarket_extreme_price share {share*100:.0f}% of today's resolutions",
        )
    return None


def alert_new_market_outage(db_path: str) -> Alert | None:
    cutoff = (datetime.now(UTC) - timedelta(hours=12)).isoformat()
    n = _query_one(
        db_path,
        "SELECT COUNT(DISTINCT market_id) FROM weather_signals WHERE timestamp >= ?",
        (cutoff,),
    )
    if n is None:
        return None
    if n == 0:
        return Alert("INFO", "new_market_outage",
                     "0 new market_ids indexed in the last 12h")
    return None


# ── orchestration ─────────────────────────────────────────────────────


def compute_all_alerts(
    db_path: str,
    *,
    history_path: Path | None = None,
) -> list[Alert]:
    history = _read_history(history_path) if history_path else []
    candidates: list[Alert | None] = [
        alert_data_only_mode_off(),
        alert_new_paper_trades_in_window(db_path),
        alert_ingestion_dropped(db_path),
        alert_outcome_skew_today(db_path),
        alert_brier_diverging(history),
        alert_pending_resolution_lag(db_path),
        alert_extreme_price_share_high(db_path),
        alert_new_market_outage(db_path),
    ]
    alerts = [a for a in candidates if a is not None]
    alerts.sort(key=lambda a: SEVERITY_ORDER[a.severity])
    return alerts


# ── render ────────────────────────────────────────────────────────────


def render_alerts_block(alerts: list[Alert], top_n: int | None = None) -> Panel:
    if not alerts:
        return Panel(Text("no anomalies detected", style="dim"),
                     title="Alerts", border_style="dim", padding=(0, 1))
    show = alerts[:top_n] if top_n else alerts
    t = Table.grid(padding=(0, 2))
    t.add_column(); t.add_column()
    for a in show:
        sev_t = Text(f"[{a.severity}]", style=SEVERITY_STYLE[a.severity])
        t.add_row(sev_t, f"{a.rule}: {a.message}")
    border = "red" if any(a.severity == "CRITICAL" for a in alerts) else (
        "yellow" if any(a.severity == "WARNING" for a in alerts) else "cyan"
    )
    return Panel(t, title=f"Alerts ({len(alerts)})", border_style=border, padding=(0, 1))


def render_alerts_footer(alerts: list[Alert]) -> Text:
    crit = sum(1 for a in alerts if a.severity == "CRITICAL")
    warn = sum(1 for a in alerts if a.severity == "WARNING")
    info = sum(1 for a in alerts if a.severity == "INFO")
    txt = Text("[")
    txt.append(f"CRIT {crit}", style="red" if crit else "dim")
    txt.append(" / ")
    txt.append(f"WARN {warn}", style="yellow" if warn else "dim")
    txt.append(" / ")
    txt.append(f"INFO {info}", style="cyan" if info else "dim")
    txt.append("]")
    return txt
