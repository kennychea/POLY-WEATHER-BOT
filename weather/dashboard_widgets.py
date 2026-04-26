"""Rich panel builders for the refactored dashboard.

Phase 2.X. Read-only on data/bot.db. No hot-path coupling.

Public surface:
    compute_phase2_data(db_path) -> dict
    build_phase2_panel(data) -> Panel
    build_phase2_summary(data) -> Renderable

    compute_calibration_data(db_path) -> dict   # TTL-cached
    build_calibration_panel(data) -> Panel
    build_calibration_summary(data) -> Renderable

    compute_bot_data(db_path, log_path) -> dict
    build_bot_panel(data) -> Panel
    build_bot_summary(data) -> Renderable

    compute_trades_data(db_path) -> dict
    build_trades_panel(data, mode='all') -> Panel
    build_trades_summary(data) -> Renderable

    render_condensed_default(console, db_path, log_path=None) -> None
"""

from __future__ import annotations

import math
import os
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from rich import box
from rich.columns import Columns
from rich.console import Console, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# Project imports — late, since this module sits inside the weather/ package
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.shadow_monitor import (  # noqa: E402
    GATE_BUCKETS_MIN, GATE_BUCKET_N, GATE_CITIES_MIN, GATE_CITY_N,
    GATE_FILL_RATIO, GATE_HORIZONS_MIN, GATE_HORIZON_N, GATE_MULTI_VAR_RATIO,
    GATE_N_TOTAL, GATE_OUTCOME_AGE_MAX_DAYS,
    compute_snapshot,
)

PASS2_N_TARGET = 1500


# ── shared helpers ────────────────────────────────────────────────────


def _gate_color(passed: bool, marginal: bool = False) -> str:
    if passed:
        return "green"
    return "yellow" if marginal else "red"


def _pct(x: float, digits: int = 1) -> str:
    return f"{100 * x:.{digits}f}%"


def _safe_iso_to_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _wilson_ci(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = wins / n
    denom = 1 + (z**2) / n
    centre = (p + (z**2) / (2 * n)) / denom
    margin = (z * math.sqrt(p * (1 - p) / n + (z**2) / (4 * n**2))) / denom
    return (max(0.0, centre - margin), min(1.0, centre + margin))


# ── PANEL 1 — Phase 2 status ──────────────────────────────────────────


def _read_weather_signals_full(db_path: str) -> list[dict[str, Any]]:
    if not Path(db_path).exists():
        return []
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT timestamp, forecast_probability, actual_outcome, location, "
            "horizon_hours, humidity_mean, forecast_date, outcome_source, market_question "
            "FROM weather_signals"
        )
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows
    except sqlite3.OperationalError:
        return []


def compute_phase2_data(db_path: str) -> dict[str, Any]:
    """Snapshot + a few extra dashboard-only derivations."""
    rows = _read_weather_signals_full(db_path)
    snap = compute_snapshot(rows)

    # n_unique_markets_resolved (dedup by market_question on resolved rows)
    unique_resolved = len({
        r.get("market_question")
        for r in rows
        if r.get("actual_outcome") is not None and r.get("market_question")
    })

    # ETA Pass 2: linear projection from recent ingestion rate
    eta_pass2: Any = "bootstrapping"
    if (
        snap.uptime_hours >= 48
        and snap.ingestion_rate_last_3d
        and snap.ingestion_rate_last_3d > 0
        and unique_resolved < PASS2_N_TARGET
    ):
        # Crude proxy: assume unique markets resolved scales with ingestion
        # Use unique-markets-per-day heuristic from the last 3 days
        cutoff = datetime.now(UTC) - timedelta(days=3)
        recent_questions = {
            r.get("market_question")
            for r in rows
            if r.get("actual_outcome") is not None
            and (_safe_iso_to_dt(r.get("timestamp")) or datetime.min.replace(tzinfo=UTC)) >= cutoff
        }
        if recent_questions:
            per_day = len(recent_questions) / 3.0
            if per_day > 0:
                eta_pass2 = round(max(0.0, (PASS2_N_TARGET - unique_resolved) / per_day), 1)

    # Top 10 cities by unique resolved markets
    city_unique: dict[str, set[str]] = defaultdict(set)
    for r in rows:
        if r.get("actual_outcome") is None:
            continue
        c = r.get("location") or "?"
        q = r.get("market_question")
        if q:
            city_unique[c].add(q)
    top10_cities = sorted(
        ((c, len(qs)) for c, qs in city_unique.items()), key=lambda kv: -kv[1]
    )[:10]

    # Outcome source breakdown — % of resolved-with-source
    src_counts = dict(snap.coverage_by_outcome_source)
    src_total = sum(src_counts.values()) or 1

    return {
        "snapshot": snap,
        "n_unique_markets_resolved": unique_resolved,
        "eta_pass2_days": eta_pass2,
        "top10_cities": top10_cities,
        "outcome_source_pct": {k: v / src_total for k, v in src_counts.items()},
        "outcome_source_counts": src_counts,
    }


def build_phase2_panel(data: dict[str, Any]) -> Panel:
    snap = data["snapshot"]
    body = Table.grid(padding=(0, 2))
    body.add_column(justify="left")
    body.add_column(justify="right")
    body.add_row("n_total weather_signals", str(snap.n_total))
    body.add_row("n_with_outcome", str(snap.n_with_outcome))
    body.add_row("n_unique_markets_resolved", str(data["n_unique_markets_resolved"]))
    body.add_row("fill_ratio", _pct(snap.fill_ratio, 2))
    body.add_row("uptime_hours", f"{snap.uptime_hours:.1f}")
    body.add_row(
        "rate (3d avg)",
        f"{snap.ingestion_rate_last_3d:.1f} rows/day"
        if snap.ingestion_rate_last_3d else "—",
    )
    body.add_row("ETA Pass 2 (n=1500)", f"{data['eta_pass2_days']}")
    if snap.outcome_resolution_age_days is not None:
        body.add_row("outcome_age (median)", f"{snap.outcome_resolution_age_days:.1f}d")

    # Exit gates table
    gates_t = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False, expand=False)
    gates_t.add_column("gate", justify="left")
    gates_t.add_column("status", justify="center")
    for name, passed in snap.exit_gates.items():
        color = _gate_color(passed)
        status = Text("PASS" if passed else "WAIT", style=color)
        gates_t.add_row(name, status)

    # Outcome source distribution
    src_t = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False)
    src_t.add_column("source")
    src_t.add_column("count", justify="right")
    src_t.add_column("%", justify="right")
    for name, count in sorted(data["outcome_source_counts"].items(), key=lambda kv: -kv[1]):
        src_t.add_row(name, str(count), _pct(data["outcome_source_pct"].get(name, 0)))

    # Top 10 cities
    cities_t = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False)
    cities_t.add_column("city")
    cities_t.add_column("n_resolved", justify="right")
    for c, n in data["top10_cities"]:
        cities_t.add_row(str(c), str(n))

    side_grid = Table.grid(padding=(0, 2), expand=False)
    side_grid.add_column(); side_grid.add_column(); side_grid.add_column()
    side_grid.add_row(gates_t, src_t, cities_t)

    body_grid = Table.grid(padding=(1, 0))
    body_grid.add_row(body)
    body_grid.add_row(side_grid)

    title = "Phase 2 — Data-only collection"
    return Panel.fit(
        body_grid,
        title=title, border_style="cyan",
        subtitle=f"OVERALL_READY: {'YES' if snap.overall_ready else 'no'}",
    )


def build_phase2_summary(data: dict[str, Any]) -> Panel:
    """4-line condensed Phase 2 panel for the default view."""
    snap = data["snapshot"]
    n_pass = sum(1 for v in snap.exit_gates.values() if v)
    n_total_gates = len(snap.exit_gates) or 1
    fill_color = _gate_color(snap.fill_ratio >= GATE_FILL_RATIO,
                             marginal=snap.fill_ratio >= 0.30)
    txt = Text()
    txt.append(f"fill_ratio   {_pct(snap.fill_ratio, 2):>8}", style=fill_color)
    txt.append(f"   n_unique  {data['n_unique_markets_resolved']:>5}\n")
    txt.append(f"gates        {n_pass}/{n_total_gates} PASS\n")
    txt.append(f"ETA Pass 2   {data['eta_pass2_days']} days")
    return Panel(txt, title="Phase 2", border_style="cyan", padding=(0, 1))


# ── PANEL 2 — Calibration ─────────────────────────────────────────────


_calibration_cache: dict[str, tuple[float, dict[str, Any]]] = {}
CALIBRATION_TTL_SECONDS = 60.0


def _dedupe_latest_per_market(rows: list[dict]) -> list[dict]:
    by_q: dict[str, dict] = {}
    for r in rows:
        q = r.get("market_question") or ""
        if q not in by_q:
            by_q[q] = r
            continue
        prev_ts = by_q[q].get("timestamp")
        ts = r.get("timestamp")
        if ts is None:
            continue
        if prev_ts is None or str(ts) > str(prev_ts):
            by_q[q] = r
    return list(by_q.values())


def compute_calibration_data(
    db_path: str,
    *,
    ttl_seconds: float = CALIBRATION_TTL_SECONDS,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Compute Brier / ECE / decomposition / per-bucket distribution.

    Cached per db_path with a TTL — the dedup + Brier pass scales linearly with
    rows but is fast (<<1 s on 472 markets); cache exists for hygiene against
    the dashboard refresh cadence.
    """
    cache_key = db_path
    now_t = time.monotonic()
    if use_cache:
        entry = _calibration_cache.get(cache_key)
        if entry is not None and (now_t - entry[0]) < ttl_seconds:
            return entry[1]

    raw = _read_weather_signals_full(db_path)
    raw_resolved = [r for r in raw if r.get("actual_outcome") is not None]
    deduped = _dedupe_latest_per_market(raw_resolved)
    deduped = [r for r in deduped if r.get("forecast_probability") is not None]

    n = len(deduped)
    if n == 0:
        result = {"empty": True, "n": 0}
        _calibration_cache[cache_key] = (now_t, result)
        return result

    forecasts = [float(r["forecast_probability"]) for r in deduped]
    actuals = [int(r["actual_outcome"]) for r in deduped]
    base_rate_yes = sum(actuals) / n
    base_rate_no = 1.0 - base_rate_yes

    brier_model = sum((p - y) ** 2 for p, y in zip(forecasts, actuals)) / n
    brier_baseline = sum((base_rate_yes - y) ** 2 for y in actuals) / n

    # Lazy import — only when actually called
    from weather.calibration import (
        brier_decomposition, expected_calibration_error,
        maximum_calibration_error, monotonicity_score, reliability_diagram,
    )
    ece = expected_calibration_error(forecasts, actuals, bins=10)
    mce = maximum_calibration_error(forecasts, actuals, bins=10)
    rel, res, unc = brier_decomposition(forecasts, actuals, bins=10)
    rd = reliability_diagram(forecasts, actuals, bins=10)
    rdq = reliability_diagram(forecasts, actuals, bins=10, bin_strategy="quantile")
    mono_eq = monotonicity_score(rd["bin_obs_freq"], rd["bin_mean_forecast"])
    mono_q = monotonicity_score(rdq["bin_obs_freq"], rdq["bin_mean_forecast"])

    # Per-bucket distribution
    bucket_edges = [0.0, 0.05, 0.15, 0.30, 0.50, 0.70, 0.85, 0.95, 1.01]
    bucket_labels = [
        "<0.05", "0.05-0.15", "0.15-0.30", "0.30-0.50",
        "0.50-0.70", "0.70-0.85", "0.85-0.95", ">=0.95",
    ]
    buckets: list[dict[str, Any]] = []
    for i in range(len(bucket_labels)):
        lo, hi = bucket_edges[i], bucket_edges[i + 1]
        in_bucket = [(p, y) for p, y in zip(forecasts, actuals) if lo <= p < hi]
        if not in_bucket:
            buckets.append({"label": bucket_labels[i], "n": 0})
            continue
        b_n = len(in_bucket)
        b_mean = sum(p for p, _ in in_bucket) / b_n
        b_obs_yes = sum(y for _, y in in_bucket) / b_n
        # local reliability = (mean_forecast - obs_freq)^2
        b_local_rel = (b_mean - b_obs_yes) ** 2
        buckets.append({
            "label": bucket_labels[i],
            "n": b_n,
            "mean_forecast": b_mean,
            "obs_freq_yes": b_obs_yes,
            "obs_freq_no": 1.0 - b_obs_yes,
            "ecart": abs(b_mean - b_obs_yes),
            "rel_local": b_local_rel,
        })

    result: dict[str, Any] = {
        "empty": False,
        "n": n,
        "base_rate_yes": base_rate_yes,
        "base_rate_no": base_rate_no,
        "brier_model": brier_model,
        "brier_baseline": brier_baseline,
        "ece": ece,
        "mce": mce,
        "rel": rel, "res": res, "unc": unc,
        "mono_equal": mono_eq,
        "mono_quantile": mono_q,
        "buckets": buckets,
        "reliability_diagram": rd,  # for ASCII renderer
    }
    _calibration_cache[cache_key] = (now_t, result)
    return result


def _ascii_reliability_diagram(rd: dict[str, Any], width: int = 20) -> Text:
    """A 10-row ASCII reliability scatter: row per bin, '#' bar = obs_freq, '*' = mean_forecast."""
    txt = Text()
    txt.append("bin    obs_freq                                mean_fc\n",
               style="bold")
    for i, (mf, of, n) in enumerate(zip(
        rd["bin_mean_forecast"], rd["bin_obs_freq"], rd["bin_count"],
    )):
        if n == 0:
            txt.append(f" {i:2d}    (empty)\n", style="dim")
            continue
        of_pos = int((of or 0) * width)
        mf_pos = int((mf or 0) * width)
        bar = []
        for k in range(width + 1):
            if k == mf_pos:
                bar.append("*")
            elif k <= of_pos:
                bar.append("#")
            else:
                bar.append(" ")
        txt.append(f" {i:2d}  ")
        txt.append("".join(bar), style="cyan")
        txt.append(f"  obs={of:.2f} mf={mf:.2f} n={n}\n")
    return txt


def build_calibration_panel(data: dict[str, Any]) -> Panel:
    if data.get("empty"):
        return Panel(Text("no resolved markets in DB", style="dim"),
                     title="Calibration", border_style="magenta")

    delta = data["brier_model"] - data["brier_baseline"]
    delta_color = "red" if delta > 0.005 else ("green" if delta < -0.005 else "yellow")

    head = Table.grid(padding=(0, 2))
    head.add_column(justify="left"); head.add_column(justify="right")
    head.add_row("n unique markets (dedup)", str(data["n"]))
    head.add_row("base rate NO", _pct(data["base_rate_no"], 2))
    head.add_row("Brier — model", f"{data['brier_model']:.4f}")
    head.add_row(f"Brier — baseline P(NO)={data['base_rate_no']:.3f}", f"{data['brier_baseline']:.4f}")
    delta_t = Text(f"{delta:+.4f}", style=delta_color)
    head.add_row("delta (model - baseline)", delta_t)
    head.add_row("ECE / MCE", f"{data['ece']:.4f} / {data['mce']:.4f}")
    head.add_row("Decomp (rel/res/unc)",
                 f"{data['rel']:.4f} / {data['res']:.4f} / {data['unc']:.4f}")
    head.add_row("Mono (equal / quantile)",
                 f"{data['mono_equal']:.3f} / {data['mono_quantile']:.3f}")

    # Bucket table
    bt = Table(box=box.SIMPLE_HEAD, show_edge=False)
    bt.add_column("bucket"); bt.add_column("n", justify="right")
    bt.add_column("mean_fc", justify="right")
    bt.add_column("obs_NO", justify="right")
    bt.add_column("ecart", justify="right")
    bt.add_column("rel_local", justify="right")
    for b in data["buckets"]:
        if b["n"] == 0:
            bt.add_row(b["label"], "0", "—", "—", "—", "—")
            continue
        bt.add_row(
            b["label"], str(b["n"]),
            f"{b['mean_forecast']:.3f}",
            f"{b['obs_freq_no']:.3f}",
            f"{b['ecart']:.3f}",
            f"{b['rel_local']:.4f}",
        )

    body = Table.grid(padding=(0, 2))
    body.add_row(head)
    body.add_row(Text("Per-bucket distribution (NO outcome):", style="bold"))
    body.add_row(bt)
    body.add_row(Text("Reliability diagram (10 equal bins):", style="bold"))
    body.add_row(_ascii_reliability_diagram(data["reliability_diagram"]))

    return Panel(body, title="Calibration", border_style="magenta")


def build_calibration_summary(data: dict[str, Any]) -> Panel:
    if data.get("empty"):
        return Panel(Text("no resolved markets", style="dim"),
                     title="Calibration", border_style="magenta", padding=(0, 1))
    delta = data["brier_model"] - data["brier_baseline"]
    delta_color = "red" if delta > 0.005 else ("green" if delta < -0.005 else "yellow")
    # Top miscalibrated bucket by ecart (n>=10 only)
    candidates = [b for b in data["buckets"] if b["n"] >= 10]
    if candidates:
        worst = max(candidates, key=lambda b: b["ecart"])
        worst_str = f"{worst['label']} ecart={worst['ecart']:.3f} n={worst['n']}"
    else:
        worst_str = "—"

    txt = Text()
    txt.append(f"Brier model {data['brier_model']:.4f}  base {data['brier_baseline']:.4f}  d=", style="default")
    txt.append(f"{delta:+.4f}\n", style=delta_color)
    txt.append(f"ECE          {data['ece']:.4f}\n")
    txt.append(f"mono(q)      {data['mono_quantile']:.3f}\n")
    txt.append(f"worst bucket {worst_str}")
    return Panel(txt, title="Calibration", border_style="magenta", padding=(0, 1))


# ── PANEL 3 — Bot operational ─────────────────────────────────────────


def _is_data_only_mode_safe() -> bool | None:
    try:
        from infra.config import is_data_only_mode  # noqa: WPS433
        return bool(is_data_only_mode())
    except Exception:
        return None


def _tail(path: Path, n_lines: int = 200) -> list[str]:
    try:
        data = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return data.splitlines()[-n_lines:]


def _resolve_log_path(explicit: str | None) -> Path | None:
    if explicit:
        p = Path(explicit)
        return p if p.exists() else None
    env = os.environ.get("BOT_LOG_PATH")
    if env:
        p = Path(env)
        if p.exists():
            return p
    for cand in ("bot.log", "logs/bot.log", "data/bot.log"):
        p = Path(cand)
        if p.exists():
            return p
    return None


def _parse_recent_cycles(log_lines: list[str], limit: int = 5) -> list[dict[str, Any]]:
    """Parse 'SCAN complete:' lines, most recent first."""
    cycles: list[dict[str, Any]] = []
    for line in reversed(log_lines):
        if "SCAN complete:" not in line:
            continue
        # Format roughly: "SCAN complete: %d markets, %d trades, %.1fs [%s] | cache ... | %s"
        cycles.append({"raw": line.strip()})
        if len(cycles) >= limit:
            break
    return cycles


def compute_bot_data(db_path: str, log_path: str | None = None) -> dict[str, Any]:
    rows = _read_weather_signals_full(db_path)

    # Ingestion rate over the last 6 hours (rows/h)
    cutoff = datetime.now(UTC) - timedelta(hours=6)
    recent = [r for r in rows
              if (_safe_iso_to_dt(r.get("timestamp")) or datetime.min.replace(tzinfo=UTC)) >= cutoff]
    rate_per_h = len(recent) / 6.0 if recent else 0.0

    last_ts = max((_safe_iso_to_dt(r.get("timestamp")) for r in rows
                   if _safe_iso_to_dt(r.get("timestamp")) is not None),
                  default=None)

    log_p = _resolve_log_path(log_path)
    log_tail = _tail(log_p, n_lines=300) if log_p else []
    cycles = _parse_recent_cycles(log_tail) if log_tail else []

    alerts: list[str] = []
    data_only = _is_data_only_mode_safe()
    if data_only is True and rate_per_h < 1.0:
        # data-only ON but ingestion is dead — flag it
        alerts.append("low ingestion rate while DATA_ONLY=on")

    return {
        "data_only_mode": data_only,
        "log_path": str(log_p) if log_p else None,
        "last_signal_ts": last_ts.isoformat() if last_ts else None,
        "ingestion_rate_per_h": rate_per_h,
        "n_signals_last_6h": len(recent),
        "recent_cycles": cycles,
        "alerts": alerts,
    }


def build_bot_panel(data: dict[str, Any]) -> Panel:
    body = Table.grid(padding=(0, 2))
    body.add_column(); body.add_column()
    do_mode = data["data_only_mode"]
    do_str = "ON" if do_mode is True else ("OFF" if do_mode is False else "?")
    body.add_row("DATA_ONLY_MODE", Text(do_str, style="green" if do_mode else "yellow"))
    body.add_row("last weather_signal", data["last_signal_ts"] or "—")
    body.add_row("ingestion rate (6h)", f"{data['ingestion_rate_per_h']:.1f} rows/h")
    body.add_row("rows last 6h", str(data["n_signals_last_6h"]))
    body.add_row("log path", data["log_path"] or "(no log file found)")

    cycles_block: RenderableType
    if not data["recent_cycles"]:
        cycles_block = Text("no recent activity", style="dim")
    else:
        ct = Table(box=box.SIMPLE_HEAD, show_edge=False)
        ct.add_column("cycle (most recent first)")
        for c in data["recent_cycles"]:
            ct.add_row(c["raw"])
        cycles_block = ct

    alerts_block: RenderableType
    if data["alerts"]:
        at = Table.grid()
        for a in data["alerts"]:
            at.add_row(Text(f"⚠ {a}", style="red"))
        alerts_block = at
    else:
        alerts_block = Text("no active alerts", style="dim")

    grid = Table.grid(padding=(1, 0))
    grid.add_row(body)
    grid.add_row(Text("Recent SCAN cycles:", style="bold"))
    grid.add_row(cycles_block)
    grid.add_row(Text("Alerts:", style="bold"))
    grid.add_row(alerts_block)

    return Panel(grid, title="Bot operational", border_style="green")


def build_bot_summary(data: dict[str, Any]) -> Panel:
    do_mode = data["data_only_mode"]
    do_str = "ON" if do_mode is True else ("OFF" if do_mode is False else "?")
    last_ts = data["last_signal_ts"] or "—"
    rate = data["ingestion_rate_per_h"]
    rate_color = "green" if rate >= 5 else ("yellow" if rate >= 1 else "red")
    txt = Text()
    txt.append(f"DATA_ONLY    {do_str}\n", style="green" if do_mode else "yellow")
    txt.append(f"last signal  {last_ts[-19:] if last_ts != '—' else '—'}\n")
    txt.append(f"rate (6h)    {rate:.1f} rows/h\n", style=rate_color)
    if data["alerts"]:
        txt.append(f"alerts       {data['alerts'][0]}", style="red")
    else:
        txt.append("alerts       none", style="dim")
    return Panel(txt, title="Bot", border_style="green", padding=(0, 1))


# ── PANEL 4 — Trade history ───────────────────────────────────────────


def _fetch_paper_trades(db_path: str) -> list[dict[str, Any]]:
    if not Path(db_path).exists():
        return []
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT trade_id, market_question, signal_type, size_usdc, entry_price, "
            "exit_price, fees, pnl, status, opened_at, resolved_at, "
            "forecast_probability, ensemble_std, horizon_hours "
            "FROM paper_trades"
        )
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows
    except sqlite3.OperationalError:
        return []


def _profit_factor(wins_pnl: list[float], losses_pnl: list[float]) -> float:
    gross_w = sum(p for p in wins_pnl if p > 0)
    gross_l = sum(-p for p in losses_pnl if p < 0)
    if gross_l == 0:
        return float("inf") if gross_w > 0 else 0.0
    return gross_w / gross_l


def compute_trades_data(db_path: str) -> dict[str, Any]:
    rows = _fetch_paper_trades(db_path)
    resolved = [r for r in rows if r.get("status") in ("resolved", "force_resolved")
                and r.get("pnl") is not None]
    pending = [r for r in rows if r.get("status") == "pending"]

    pnl_total = sum(float(r["pnl"]) for r in resolved) if resolved else 0.0
    n = len(resolved)
    wins = [r for r in resolved if float(r["pnl"]) > 0]
    losses = [r for r in resolved if float(r["pnl"]) <= 0]
    wr = (len(wins) / n) if n else 0.0
    pf = _profit_factor([float(r["pnl"]) for r in wins], [float(r["pnl"]) for r in losses])

    # Anti-lottery PnL: exclude top-PnL trade
    top_pnl = max((float(r["pnl"]) for r in resolved), default=0.0) if resolved else 0.0
    pnl_ex_top = pnl_total - top_pnl if resolved else 0.0

    # PF per signal_type
    by_st: dict[str, list[dict]] = defaultdict(list)
    for r in resolved:
        by_st[r.get("signal_type") or "?"].append(r)
    pf_by_signal = {
        st: _profit_factor(
            [float(rr["pnl"]) for rr in lst if float(rr["pnl"]) > 0],
            [float(rr["pnl"]) for rr in lst if float(rr["pnl"]) <= 0],
        )
        for st, lst in by_st.items()
    }
    n_by_signal = {st: len(lst) for st, lst in by_st.items()}

    # Top 5 cities by n_trades + PnL (parsed from market_question — heuristic)
    city_stats: dict[str, dict[str, float]] = defaultdict(lambda: {"n": 0, "pnl": 0.0})
    for r in resolved:
        q = (r.get("market_question") or "")
        # Coarse city extraction: word(s) between "in " and " be"
        city = "?"
        if " in " in q.lower() and " be " in q.lower():
            chunk = q.lower().split(" in ", 1)[1].split(" be ", 1)[0].strip()
            city = chunk.title()
        city_stats[city]["n"] += 1
        city_stats[city]["pnl"] += float(r["pnl"])
    top_cities = sorted(
        ((c, s["n"], s["pnl"]) for c, s in city_stats.items()),
        key=lambda kv: -kv[1],
    )[:5]

    # Wilson CI on win rate
    wlo, whi = _wilson_ci(len(wins), n)

    return {
        "n_resolved": n, "n_pending": len(pending),
        "pnl_total": pnl_total, "pnl_ex_top": pnl_ex_top,
        "top_pnl_trade": top_pnl,
        "wr": wr, "wr_ci": (wlo, whi),
        "pf": pf,
        "pf_by_signal": pf_by_signal,
        "n_by_signal": n_by_signal,
        "top_cities": top_cities,
        "pending_recent": pending[-5:],
        "resolved": resolved,
    }


def build_trades_panel(data: dict[str, Any], mode: str = "all") -> Panel:
    body = Table.grid(padding=(0, 2))
    body.add_column(); body.add_column()
    body.add_row("n_resolved", str(data["n_resolved"]))
    body.add_row("n_pending", str(data["n_pending"]))
    pnl_color = "green" if data["pnl_total"] > 0 else "red" if data["pnl_total"] < 0 else "yellow"
    body.add_row("PnL total", Text(f"${data['pnl_total']:+.2f}", style=pnl_color))
    body.add_row("PnL ex-top trade", f"${data['pnl_ex_top']:+.2f}  (top=${data['top_pnl_trade']:+.2f})")
    wr_color = "green" if data["wr"] >= 0.60 else ("yellow" if data["wr"] >= 0.55 else "red")
    body.add_row("WR (95% Wilson)",
                 Text(f"{_pct(data['wr'], 1)} [{_pct(data['wr_ci'][0])} – {_pct(data['wr_ci'][1])}]",
                      style=wr_color))
    pf_color = "green" if data["pf"] >= 1.3 else ("yellow" if data["pf"] >= 1.0 else "red")
    body.add_row("Profit Factor", Text(f"{data['pf']:.2f}", style=pf_color))

    # PF by signal_type
    pf_t = Table(box=box.SIMPLE_HEAD, show_edge=False)
    pf_t.add_column("signal_type"); pf_t.add_column("n", justify="right"); pf_t.add_column("PF", justify="right")
    for st, pf in sorted(data["pf_by_signal"].items()):
        pf_t.add_row(st, str(data["n_by_signal"].get(st, 0)), f"{pf:.2f}")

    cities_t = Table(box=box.SIMPLE_HEAD, show_edge=False)
    cities_t.add_column("city"); cities_t.add_column("n", justify="right"); cities_t.add_column("PnL", justify="right")
    for city, n_, pnl_ in data["top_cities"]:
        cities_t.add_row(str(city), str(n_), f"${pnl_:+.2f}")

    # Pending tail
    if mode in ("all", "pending"):
        pt = Table(box=box.SIMPLE_HEAD, show_edge=False)
        pt.add_column("opened_at"); pt.add_column("entry", justify="right"); pt.add_column("market_question")
        for r in data["pending_recent"]:
            pt.add_row(
                (r.get("opened_at") or "")[-19:],
                f"{float(r.get('entry_price') or 0):.3f}",
                (r.get("market_question") or "")[:60],
            )
    else:
        pt = Text("(--resolved-only mode, pending hidden)", style="dim")

    grid = Table.grid(padding=(1, 0))
    grid.add_row(body)
    grid.add_row(Text("PF by signal_type:", style="bold"))
    grid.add_row(pf_t)
    grid.add_row(Text("Top 5 cities:", style="bold"))
    grid.add_row(cities_t)
    grid.add_row(Text("Pending (last 5):", style="bold"))
    grid.add_row(pt)

    title = f"Trade history — mode={mode}"
    return Panel(grid, title=title, border_style="yellow")


def build_trades_summary(data: dict[str, Any]) -> Panel:
    pnl_color = "green" if data["pnl_total"] > 0 else "red" if data["pnl_total"] < 0 else "yellow"
    pf_color = "green" if data["pf"] >= 1.3 else "yellow" if data["pf"] >= 1.0 else "red"
    txt = Text()
    txt.append(f"n_resolved   {data['n_resolved']}\n")
    txt.append(f"PnL total    ${data['pnl_total']:+.2f}\n", style=pnl_color)
    txt.append(f"WR           {_pct(data['wr'], 1)}\n")
    txt.append(f"PF           {data['pf']:.2f}", style=pf_color)
    return Panel(txt, title="Trades", border_style="yellow", padding=(0, 1))


# ── default condensed view ────────────────────────────────────────────


def render_condensed_default(
    console: Console,
    db_path: str,
    *,
    log_path: str | None = None,
) -> None:
    # Alerts: print top 3 (CRITICAL > WARNING > INFO ordered) above the panels
    try:
        from weather.dashboard_alerts import (
            compute_all_alerts, render_alerts_block,
        )
        alerts = compute_all_alerts(db_path, history_path=Path("data/dashboard_history.json"))
        console.print(render_alerts_block(alerts, top_n=3))
    except Exception:
        alerts = []  # alerts must never break the dashboard

    p2 = compute_phase2_data(db_path)
    cal = compute_calibration_data(db_path)
    bot = compute_bot_data(db_path, log_path=log_path)
    tr = compute_trades_data(db_path)

    cols = Columns(
        [
            build_phase2_summary(p2),
            build_calibration_summary(cal),
            build_bot_summary(bot),
            build_trades_summary(tr),
        ],
        equal=True, expand=True, padding=(0, 1),
    )
    console.print(cols)
    eta = p2.get("eta_pass2_days")
    footer = Text()
    footer.append("Pass 2 trigger ETA: ", style="dim")
    footer.append(f"{eta} ", style="bold")
    footer.append("days  |  ", style="dim")
    footer.append("--phase2/--calibration/--bot/--trades/--segments  |  --watch for live",
                  style="dim")
    console.print(footer)
