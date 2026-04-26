"""Rich terminal dashboard for the weather arbitrage bot.

Launch: python -m weather.dashboard
       python -m weather.dashboard --refresh 60
Refreshes via Rich Live (default 30s).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone, UTC
from pathlib import Path

import aiohttp
from rich import box
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

# ── project imports ───────────────────────────────────────────────

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

from core.reconciler import Reconciler
from core.risk import RiskManager
from infra.db import DbWriter
from weather.backtest import BacktestResult, run_backtest
from weather.ensemble import get_cache_stats

logger = logging.getLogger(__name__)

DEFAULT_REFRESH = 30  # seconds
BACKTEST_CACHE_TTL = 300  # 5 minutes
SCANNER_CACHE_TTL = 120  # 2 minutes
DB_PATH = os.environ.get("DB_PATH", "data/bot.db")

GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"

# Cities to scan (slug format → display name)
_WEATHER_CITY_SLUGS: dict[str, str] = {
    "nyc": "NYC",
    "chicago": "Chicago",
    "miami": "Miami",
    "los-angeles": "Los Angeles",
    "houston": "Houston",
    "dallas": "Dallas",
    "london": "London",
    "paris": "Paris",
    "seoul": "Seoul",
    "shanghai": "Shanghai",
    "hong-kong": "Hong Kong",
    "tokyo": "Tokyo",
    "beijing": "Beijing",
    "madrid": "Madrid",
    "san-francisco": "San Francisco",
    "denver": "Denver",
    "seattle": "Seattle",
    "atlanta": "Atlanta",
    "austin": "Austin",
    "toronto": "Toronto",
    "buenos-aires": "Buenos Aires",
    "wellington": "Wellington",
    "moscow": "Moscow",
    "mexico-city": "Mexico City",
    "singapore": "Singapore",
    "jakarta": "Jakarta",
    "kuala-lumpur": "Kuala Lumpur",
    "sao-paulo": "Sao Paulo",
    "amsterdam": "Amsterdam",
    "taipei": "Taipei",
    "istanbul": "Istanbul",
    # Phase 10.C additions
    "tel-aviv": "Tel Aviv",
    "panama-city": "Panama City",
    "warsaw": "Warsaw",
    "munich": "Munich",
    "helsinki": "Helsinki",
    "lucknow": "Lucknow",
    "ankara": "Ankara",
    "milan": "Milan",
    "shenzhen": "Shenzhen",
    "chongqing": "Chongqing",
    "wuhan": "Wuhan",
    "busan": "Busan",
    "chengdu": "Chengdu",
}


# ── helpers ───────────────────────────────────────────────────────


def _pnl_color(value: float) -> str:
    if value > 0:
        return "green"
    if value < 0:
        return "red"
    return "dim"


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _usd(value: float) -> str:
    return f"${value:,.2f}"


def _vol(value: float) -> str:
    """Format volume as compact dollar amount."""
    if value >= 1_000_000:
        return f"${value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"${value / 1_000:.1f}K"
    return f"${value:.0f}"


def _extract_bucket(question: str) -> str:
    """Extract temperature bucket from market question for compact display."""
    import re

    # "between 80-81°F" → "80-81°F"
    m = re.search(r"between\s+(\d+-\d+)\s*[°]?\s*([FfCc])", question)
    if m:
        return f"{m.group(1)}°{m.group(2).upper()}"
    # "76°F or higher" → "≥76°F"
    m = re.search(r"(\d+)\s*[°]?\s*([FfCc])\s+or\s+higher", question)
    if m:
        return f"≥{m.group(1)}°{m.group(2).upper()}"
    # "37°F or below" → "≤37°F"
    m = re.search(r"(\d+)\s*[°]?\s*([FfCc])\s+or\s+below", question)
    if m:
        return f"≤{m.group(1)}°{m.group(2).upper()}"
    # "be 29°C on" (exact temperature) → "29°C"
    m = re.search(r"be\s+(\d+)\s*[°]?\s*([FfCc])\s+on", question)
    if m:
        return f"{m.group(1)}°{m.group(2).upper()}"
    return question[:20]


# ── scanner data fetch ───────────────────────────────────────────


async def fetch_scanner_data(
    forecast_days: int = 3,
) -> list[dict]:
    """Fetch live weather market data from Gamma, grouped by city.

    Returns a list of dicts, one per city, sorted by 24h volume desc.
    Each dict: {city, slug, date, volume_24h, bucket, bucket_question,
                mkt_price, mkt_vol, condition_id}
    """
    today = datetime.now(timezone.utc)
    slugs: list[tuple[str, str, str]] = []  # (slug, city_display, date)
    for day_offset in range(forecast_days):
        d = today + timedelta(days=day_offset)
        month_name = d.strftime("%B").lower()
        day, year = d.day, d.year
        date_str = d.strftime("%Y-%m-%d")
        for city_slug, city_name in _WEATHER_CITY_SLUGS.items():
            s = f"highest-temperature-in-{city_slug}-on-{month_name}-{day}-{year}"
            slugs.append((s, city_name, date_str))

    sem = asyncio.Semaphore(8)
    timeout = aiohttp.ClientTimeout(total=10)
    headers = {"User-Agent": "Mozilla/5.0"}

    async def _fetch_event(
        slug: str, city: str, date: str,
    ) -> dict | None:
        async with sem:
            try:
                async with aiohttp.ClientSession(
                    timeout=timeout, headers=headers,
                ) as session, session.get(
                    GAMMA_EVENTS_URL, params={"slug": slug},
                ) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
                    if not data or not isinstance(data, list):
                        return None
                    event = data[0]
                    markets = event.get("markets", [])
                    if not markets:
                        return None

                    # Find the market-favorite bucket (highest YES price)
                    best_market = None
                    best_yes = -1.0
                    for m in markets:
                        if m.get("closed"):
                            continue
                        prices_raw = m.get("outcomePrices")
                        if not prices_raw:
                            continue
                        try:
                            prices = (
                                json.loads(prices_raw)
                                if isinstance(prices_raw, str)
                                else prices_raw
                            )
                            yes_price = float(prices[0])
                        except (json.JSONDecodeError, ValueError, IndexError):
                            continue
                        if yes_price > best_yes:
                            best_yes = yes_price
                            best_market = m

                    if best_market is None or best_yes <= 0:
                        return None

                    vol_24h = float(event.get("volume24hr") or 0)
                    mkt_vol = float(best_market.get("volume") or 0)
                    question = best_market.get("question", "")

                    return {
                        "city": city,
                        "date": date,
                        "volume_24h": vol_24h,
                        "bucket": _extract_bucket(question),
                        "bucket_question": question,
                        "mkt_price": best_yes,
                        "mkt_vol": mkt_vol,
                        "condition_id": best_market.get("conditionId", ""),
                    }
            except Exception:
                return None

    tasks = [_fetch_event(s, c, d) for s, c, d in slugs]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    rows = []
    for r in results:
        if isinstance(r, dict):
            rows.append(r)

    # Sort by 24h volume descending
    rows.sort(key=lambda x: x["volume_24h"], reverse=True)
    return rows


async def enrich_with_forecasts(
    scanner_rows: list[dict], db: DbWriter,
) -> None:
    """Enrich scanner rows with latest forecast probability from DB."""
    if not scanner_rows:
        return

    # Build a map of condition_id → latest forecast probability
    try:
        async with db._get_conn() as conn:
            cursor = await conn.execute(
                "SELECT market_id, probability FROM forecast_log"
                " WHERE logged_at > datetime('now', '-24 hours')"
                " ORDER BY logged_at DESC",
            )
            rows = await cursor.fetchall()
    except Exception:
        return

    forecast_map: dict[str, float] = {}
    for row in rows:
        mid = row[0]
        if mid not in forecast_map:  # keep latest only
            forecast_map[mid] = row[1]

    for sr in scanner_rows:
        cid = sr.get("condition_id", "")
        if cid in forecast_map:
            sr["forecast"] = forecast_map[cid]


# ── panel builders ────────────────────────────────────────────────


def build_scanner_panel(scanner_rows: list[dict], min_edge: float = 0.05) -> Panel:
    """Moon Dev-style market scanner table."""
    tbl = Table(expand=True, padding=(0, 1), show_edge=False)
    tbl.add_column("#", style="dim", width=3, justify="right")
    tbl.add_column("City", width=14, style="bold")
    tbl.add_column("Date", width=7, style="dim")
    tbl.add_column("24h Vol", justify="right", width=9)
    tbl.add_column("Bucket", width=12)
    tbl.add_column("Mkt Vol", justify="right", width=9)
    tbl.add_column("Forecast", justify="right", width=8)
    tbl.add_column("Mkt Fav", justify="right", width=8)
    tbl.add_column("Edge", justify="right", width=10)

    # Count edges for title
    edge_count = 0
    for row in scanner_rows[:20]:
        f = row.get("forecast")
        if f is not None and (f - row["mkt_price"]) > min_edge:
            edge_count += 1

    for i, row in enumerate(scanner_rows[:20], 1):
        forecast = row.get("forecast")
        mkt_price = row["mkt_price"]

        # Compact date display
        raw_date = row.get("date", "")
        if raw_date:
            try:
                dt = datetime.strptime(raw_date, "%Y-%m-%d")
                date_display = dt.strftime("%b-%d")
            except ValueError:
                date_display = raw_date[:6]
        else:
            date_display = "-"

        # Forecast display + edge
        edge = 0.0
        if forecast is not None:
            forecast_txt = Text(f"{forecast:.1%}", style="cyan")
            edge = forecast - mkt_price
            if edge > min_edge:
                edge_txt = Text(f"{edge:+.1%} EDGE", style="bold green")
            elif edge > 0:
                edge_txt = Text(f"{edge:+.1%}", style="yellow")
            else:
                edge_txt = Text(f"{edge:+.1%}", style="dim")
        else:
            forecast_txt = Text("-", style="dim")
            edge_txt = Text("-", style="dim")

        # Mkt Fav color
        if mkt_price > 0.5:
            fav_style = "bold white"
        elif mkt_price > 0.2:
            fav_style = "white"
        else:
            fav_style = "dim"

        # Row highlighting for edges
        row_style = ""
        if forecast is not None:
            if edge > min_edge:
                row_style = "on dark_green"
            elif edge > 0:
                row_style = "on grey11"

        tbl.add_row(
            str(i),
            row["city"],
            date_display,
            _vol(row["volume_24h"]),
            row["bucket"],
            _vol(row["mkt_vol"]),
            forecast_txt,
            Text(f"{mkt_price:.1%}", style=fav_style),
            edge_txt,
            style=row_style,
        )

    if edge_count > 0:
        edge_label = f"EDGE{'S' if edge_count != 1 else ''}"
        title = f"ALL CITIES — [bold green]{edge_count} {edge_label}[/bold green] found ({len(scanner_rows)} markets)"
    else:
        title = f"ALL CITIES — 0 edges ({len(scanner_rows)} markets)"
    return Panel(tbl, title=title, border_style="bright_green", title_align="left", box=box.ROUNDED)


def build_portfolio_panel(stats: dict) -> Panel:
    """Section: Portfolio stats from reconciler."""
    tbl = Table(show_header=False, expand=True, padding=(0, 1))
    tbl.add_column("metric", style="bold")
    tbl.add_column("value", justify="right")

    total = stats.get("total_trades", 0)
    wins = stats.get("wins", 0)
    losses = stats.get("losses", 0)
    wr = stats.get("win_rate", 0.0)
    pnl = stats.get("total_pnl", 0.0)
    pf = stats.get("profit_factor", 0.0)

    wr_style = "green" if wr >= 0.6 else ("yellow" if wr >= 0.5 else "red")

    tbl.add_row("Trades", f"{total}  ({wins}W / {losses}L)")
    tbl.add_row("Win Rate", Text(_pct(wr), style=wr_style))
    tbl.add_row("PnL", Text(_usd(pnl), style=_pnl_color(pnl)))
    tbl.add_row("Profit Factor", f"{pf:.2f}")

    return Panel(tbl, title="Portfolio", border_style="cyan", box=box.ROUNDED)


def build_risk_panel(risk: RiskManager) -> Panel:
    """Section: Risk metrics."""
    tbl = Table(show_header=False, expand=True, padding=(0, 1))
    tbl.add_column("metric", style="bold")
    tbl.add_column("value", justify="right")

    exposure = risk.current_exposure
    max_exp = risk._max_exposure
    pct_used = (exposure / max_exp * 100) if max_exp > 0 else 0.0
    exp_style = "green" if pct_used < 50 else ("yellow" if pct_used < 80 else "red")

    tbl.add_row("Bankroll", Text(_usd(risk.bankroll), style="bold green"))
    tbl.add_row("Exposure", Text(f"{_usd(exposure)} / {_usd(max_exp)}  ({pct_used:.0f}%)", style=exp_style))
    tbl.add_row("Max Bet", _usd(risk._max_bet))
    tbl.add_row("Kelly Frac", f"{risk._kelly_fraction:.0%}")
    exp_bar = ProgressBar(
        total=100,
        completed=min(pct_used, 100),
        width=None,
        complete_style=exp_style,
        finished_style="bold red",
        style="grey37",
    )
    tbl.add_row("", exp_bar)

    return Panel(tbl, title="Risk", border_style="yellow", box=box.ROUNDED)


def build_pending_panel(trades: list[dict]) -> Panel:
    """Section: Pending trades with full market names."""
    tbl = Table(expand=True, padding=(0, 1))
    tbl.add_column("#", style="dim", width=3)
    tbl.add_column("Market", no_wrap=False, ratio=3)
    tbl.add_column("Side", width=8)
    tbl.add_column("Entry", justify="right", width=6)
    tbl.add_column("Size", justify="right", width=8)
    tbl.add_column("Age", justify="right", width=6)

    now = datetime.now(UTC)
    for i, t in enumerate(trades[:20], 1):
        side = t.get("signal_type", "?")
        side_style = "green" if "yes" in side.lower() else "red"
        question = t.get("market_question", t.get("market_id", "?"))

        # Calculate age
        opened_str = t.get("opened_at", "")
        if opened_str:
            try:
                opened = datetime.fromisoformat(opened_str)
                if opened.tzinfo is None:
                    opened = opened.replace(tzinfo=UTC)
                delta = now - opened
                hours = delta.total_seconds() / 3600
                if hours < 1:
                    age = f"{int(delta.total_seconds() / 60)}m"
                elif hours < 24:
                    age = f"{hours:.0f}h"
                else:
                    age = f"{delta.days}d{int(hours % 24)}h"
            except (ValueError, TypeError):
                age = "-"
        else:
            age = "-"

        tbl.add_row(
            str(i),
            question,
            Text(side, style=side_style),
            f"{t.get('entry_price', 0):.3f}",
            _usd(t.get("size_usdc", 0)),
            age,
        )

    count = len(trades)
    total_exposure = sum(t.get("size_usdc", 0) for t in trades)
    title = f"Pending Trades ({count})"
    if total_exposure > 0:
        title += f" -- [bold yellow]{_usd(total_exposure)} exposed[/bold yellow]"
    if count > 20:
        title += f" [dim](showing 20/{count})[/dim]"

    return Panel(tbl, title=title, border_style="magenta", box=box.ROUNDED)


def build_resolved_panel(trades: list[dict]) -> Panel:
    """Section: Recently resolved trades."""
    tbl = Table(expand=True, padding=(0, 1))
    tbl.add_column("#", style="dim", width=3)
    tbl.add_column("Market", no_wrap=False, ratio=3)
    tbl.add_column("Side", width=8)
    tbl.add_column("Entry", justify="right", width=6)
    tbl.add_column("Exit", justify="right", width=6)
    tbl.add_column("PnL", justify="right", width=9)

    for i, t in enumerate(trades[:10], 1):
        side = t.get("signal_type", "?")
        side_style = "green" if "yes" in side.lower() else "red"
        question = t.get("market_question", "?")
        pnl = t.get("pnl") or 0.0
        pnl_style = "green" if pnl > 0 else ("red" if pnl < 0 else "dim")

        tbl.add_row(
            str(i),
            question,
            Text(side, style=side_style),
            f"{t.get('entry_price', 0):.3f}",
            f"{t.get('exit_price', 0):.3f}" if t.get("exit_price") is not None else "-",
            Text(f"${pnl:+.2f}", style=pnl_style),
        )

    total_pnl = sum(t.get("pnl") or 0.0 for t in trades)
    pnl_style_t = "green" if total_pnl > 0 else ("red" if total_pnl < 0 else "dim")
    title = f"Recent Trades ({len(trades)}) -- [{pnl_style_t}]PnL: ${total_pnl:+.2f}[/{pnl_style_t}]"
    return Panel(tbl, title=title, border_style="blue", box=box.ROUNDED)


def build_calibration_panel(bt: BacktestResult | None) -> Panel:
    """Section: Calibration / backtest metrics."""
    tbl = Table(show_header=False, expand=True, padding=(0, 1))
    tbl.add_column("metric", style="bold")
    tbl.add_column("value", justify="right")

    if bt is None or bt.total_trades == 0:
        tbl.add_row("Status", Text("No calibration data", style="dim"))
        return Panel(tbl, title="Calibration", border_style="green", box=box.ROUNDED)

    brier_style = "green" if bt.brier_score < 0.2 else ("yellow" if bt.brier_score < 0.25 else "red")
    corr_style = "green" if bt.edge_correlation > 0.3 else ("yellow" if bt.edge_correlation > 0 else "red")

    tbl.add_row("Brier Score", Text(f"{bt.brier_score:.4f}", style=brier_style))
    tbl.add_row("Edge Corr", Text(f"{bt.edge_correlation:+.3f}", style=corr_style))
    tbl.add_row("Trades", str(bt.total_trades))
    tbl.add_row(
        "High Conf",
        f"{bt.high_conf_trades}t / {_pct(bt.high_conf_win_rate)} WR",
    )
    tbl.add_row(
        "Med Conf",
        f"{bt.medium_conf_trades}t / {_pct(bt.medium_conf_win_rate)} WR",
    )

    return Panel(tbl, title="Calibration", border_style="green", box=box.ROUNDED)


def build_cache_panel() -> Panel:
    """Section: Ensemble cache hit rates."""
    stats = get_cache_stats()
    tbl = Table(show_header=False, expand=True, padding=(0, 1))
    tbl.add_column("metric", style="bold")
    tbl.add_column("value", justify="right")

    l1 = stats.get("l1_hits", 0)
    l2 = stats.get("l2_hits", 0)
    misses = stats.get("misses", 0)
    total = l1 + l2 + misses

    if total > 0:
        hit_rate = (l1 + l2) / total
        hr_style = "green" if hit_rate > 0.5 else "yellow"
        tbl.add_row("Hit Rate", Text(f"{hit_rate:.0%}", style=hr_style))
    else:
        tbl.add_row("Hit Rate", Text("n/a", style="dim"))

    tbl.add_row("L1 (memory)", str(l1))
    tbl.add_row("L2 (file)", str(l2))
    tbl.add_row("Misses", str(misses))

    return Panel(tbl, title="Cache", border_style="white", box=box.ROUNDED)


def build_signals_panel(signals: list[dict]) -> Panel:
    """Section: Recent bot signals (last 5 decisions)."""
    tbl = Table(expand=True, padding=(0, 1), show_edge=False)
    tbl.add_column("#", style="dim", width=3, justify="right")
    tbl.add_column("Location", width=14, style="bold")
    tbl.add_column("Date", width=7, style="dim")
    tbl.add_column("Side", width=10)
    tbl.add_column("Forecast", justify="right", width=8)
    tbl.add_column("Market", justify="right", width=8)
    tbl.add_column("Edge", justify="right", width=10)
    tbl.add_column("Conf", justify="right", width=6)

    for i, sig in enumerate(signals[:5], 1):
        side = sig.get("signal_type", "?")
        side_style = "green" if "yes" in side.lower() else "red"

        forecast_p = sig.get("forecast_probability", 0)
        mkt_p = sig.get("market_price", 0)
        edge = sig.get("net_edge") or (forecast_p - mkt_p)

        conf = sig.get("confidence", "?")
        conf_style = {"high": "bold green", "medium": "yellow", "low": "dim"}.get(
            conf, "dim",
        )

        raw_date = sig.get("forecast_date", "")
        if raw_date:
            try:
                dt = datetime.strptime(raw_date, "%Y-%m-%d")
                date_display = dt.strftime("%b-%d")
            except ValueError:
                date_display = raw_date[:6]
        else:
            date_display = "-"

        if edge > 0.05:
            edge_txt = Text(f"{edge:+.1%}", style="bold green")
        elif edge > 0:
            edge_txt = Text(f"{edge:+.1%}", style="yellow")
        else:
            edge_txt = Text(f"{edge:+.1%}", style="dim")

        tbl.add_row(
            str(i),
            sig.get("location", "?"),
            date_display,
            Text(side, style=side_style),
            Text(f"{forecast_p:.1%}", style="cyan"),
            Text(f"{mkt_p:.1%}", style="white"),
            edge_txt,
            Text(conf, style=conf_style),
        )

    count = len(signals)
    members = signals[0].get("ensemble_member_count", 0) if signals else 0
    title = f"Recent Signals ({count})"
    if members:
        title += f" -- {members} members"
    return Panel(tbl, title=title, border_style="bright_yellow", box=box.ROUNDED)


def build_footer(
    *,
    trading_mode: str = "PAPER",
    ensemble_model: str = "gfs_seamless",
    start_time: float | None = None,
    db_path: str = "",
) -> Panel:
    """Footer bar with mode, model, uptime, and debug info."""
    grid = Table.grid(expand=True)
    grid.add_column(ratio=1)
    grid.add_column(ratio=1, justify="center")
    grid.add_column(ratio=1, justify="center")
    grid.add_column(ratio=1, justify="right")

    mode_upper = trading_mode.upper()
    mode_style = "bold red" if mode_upper == "LIVE" else "bold yellow"

    if start_time is not None:
        elapsed = time.monotonic() - start_time
        hours = int(elapsed // 3600)
        minutes = int((elapsed % 3600) // 60)
        secs = int(elapsed % 60)
        uptime_str = f"Up: {hours}h{minutes:02d}m{secs:02d}s"
    else:
        uptime_str = "Up: --"

    grid.add_row(
        Text(f"Mode: {mode_upper}", style=mode_style),
        Text(f"Model: {ensemble_model}", style="cyan"),
        Text(uptime_str, style="dim"),
        Text(f"DB: {db_path}", style="dim"),
    )

    return Panel(grid, style="white on grey7", box=box.ROUNDED)


def build_header(
    refresh_interval: int,
    last_refresh: str | None = None,
    *,
    elapsed: int = 0,
    status: str = "LIVE",
) -> Panel:
    """Header with bot name, live clock, status indicator, and countdown bar."""
    now = datetime.now(UTC).strftime("%H:%M:%S UTC")

    # Status indicator
    status_map = {
        "LIVE": ("bold bright_green", "[LIVE]"),
        "FETCHING": ("bold yellow", "[FETCHING]"),
        "ERROR": ("bold red", "[ERROR]"),
    }
    st_style, st_label = status_map.get(status, ("dim", f"[{status}]"))

    # Row 1: Bot name | Status | Clock
    row1 = Table.grid(expand=True)
    row1.add_column(ratio=1)
    row1.add_column(ratio=1, justify="center")
    row1.add_column(ratio=1, justify="right")
    row1.add_row(
        Text.from_markup("[bold bright_green]Polymarket Weather Bot[/bold bright_green]"),
        Text(st_label, style=st_style),
        Text(now, style="bold white"),
    )

    # Row 2: Countdown bar
    remaining = max(0, refresh_interval - elapsed)
    bar = ProgressBar(
        total=max(refresh_interval, 1),
        completed=min(elapsed, refresh_interval),
        width=None,
        complete_style="bright_cyan",
        finished_style="bright_green",
        style="grey37",
    )
    row2 = Table.grid(expand=True)
    row2.add_column(width=16)
    row2.add_column(ratio=1)
    row2.add_column(width=18, justify="right")
    last_str = f"  Last: {last_refresh}" if last_refresh else ""
    row2.add_row(
        Text("Next refresh", style="dim"),
        bar,
        Text(f"{remaining}s{last_str}", style="bold cyan"),
    )

    return Panel(
        Group(row1, row2),
        style="white on dark_blue",
        box=box.ROUNDED,
    )


# ── layout assembly ───────────────────────────────────────────────


def build_layout() -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=4),
        Layout(name="scanner", ratio=3),
        Layout(name="signals", size=5),
        Layout(name="trades_row", ratio=2),
        Layout(name="bottom", size=9),
        Layout(name="footer", size=3),
    )
    layout["trades_row"].split_row(
        Layout(name="pending", ratio=1),
        Layout(name="resolved", ratio=1),
    )
    layout["bottom"].split_row(
        Layout(name="portfolio"),
        Layout(name="risk"),
        Layout(name="calibration"),
        Layout(name="cache"),
    )
    return layout


async def fetch_data(db: DbWriter) -> dict:
    """Fetch all dashboard data from DB (async)."""
    pending, signals, resolved = await asyncio.gather(
        db.read_pending_trades(),
        db.read_recent_signals(5),
        db.read_resolved_trades(),
    )
    # Sort resolved by resolved_at desc, take last 10
    resolved.sort(key=lambda r: r.get("resolved_at") or "", reverse=True)
    return {
        "pending": pending,
        "signals": signals,
        "resolved": resolved[:10],
    }


async def fetch_backtest_cached(
    cache: dict[str, BacktestResult | float | None],
) -> BacktestResult | None:
    """Run backtest with 5-minute TTL cache."""
    now = time.monotonic()
    if now - (cache.get("_ts") or 0) < BACKTEST_CACHE_TTL and "result" in cache:
        return cache["result"]

    db_path = Path(DB_PATH)
    if not db_path.exists():
        return None
    try:
        result = await run_backtest(db_path)
        cache["result"] = result
        cache["_ts"] = now
        return result
    except Exception:
        logger.exception("Backtest failed")
        return cache.get("result")


async def fetch_scanner_cached(cache: dict) -> list[dict]:
    """Fetch scanner data with TTL cache."""
    now = time.monotonic()
    if now - (cache.get("_ts") or 0) < SCANNER_CACHE_TTL and "rows" in cache:
        return cache["rows"]

    try:
        rows = await fetch_scanner_data(forecast_days=3)
        cache["rows"] = rows
        cache["_ts"] = now
        return rows
    except Exception:
        logger.exception("Scanner fetch failed")
        return cache.get("rows", [])


async def run_dashboard(refresh_interval: int = DEFAULT_REFRESH) -> None:
    """Main dashboard loop with Rich Live."""
    console = Console()

    # Initialize components
    db = DbWriter(DB_PATH)
    await db.init_db()

    reconciler = Reconciler()
    await reconciler.hydrate(db)

    risk = RiskManager(
        bankroll=float(os.environ.get("BANKROLL_USDC", "100")),
        max_bet=float(os.environ.get("MAX_BET_USDC", "10")),
        max_exposure=float(os.environ.get("MAX_EXPOSURE_USDC", "50")),
        kelly_fraction=float(os.environ.get("KELLY_FRACTION", "0.25")),
        max_edge=float(os.environ.get("MAX_EDGE", "0.15")),
    )

    # Reconstruct exposure from pending trades
    pending_init = await db.read_pending_trades()
    for t in pending_init:
        risk.add_exposure(t.get("size_usdc", 0))
    # Adjust bankroll for resolved PnL
    stats = reconciler.stats()
    risk.update_bankroll(stats.get("total_pnl", 0.0))

    layout = build_layout()
    bt_cache: dict = {}
    scanner_cache: dict = {}

    tick = refresh_interval  # trigger immediate fetch on first iteration
    status = "LIVE"
    last_refresh_str: str | None = None
    start_time = time.monotonic()
    trading_mode = os.environ.get("TRADING_MODE", "paper")
    ensemble_model = os.environ.get("ENSEMBLE_MODEL", "gfs_seamless")

    with Live(layout, console=console, refresh_per_second=1, screen=True) as live:
        while True:
            # ── Heavy refresh: fetch data every refresh_interval seconds ──
            if tick >= refresh_interval:
                try:
                    status = "FETCHING"
                    layout["header"].update(
                        build_header(refresh_interval, last_refresh_str,
                                     elapsed=0, status=status),
                    )
                    live.refresh()

                    # Fetch all data concurrently
                    data_task = fetch_data(db)
                    bt_task = fetch_backtest_cached(bt_cache)
                    scanner_task = fetch_scanner_cached(scanner_cache)
                    data, bt, scanner_rows = await asyncio.gather(
                        data_task, bt_task, scanner_task,
                    )

                    # Enrich scanner with forecast data from DB
                    await enrich_with_forecasts(scanner_rows, db)

                    stats = reconciler.stats()
                    last_refresh_str = datetime.now(UTC).strftime("%H:%M:%S")

                    layout["scanner"].update(build_scanner_panel(scanner_rows))
                    layout["signals"].update(build_signals_panel(data["signals"]))
                    layout["pending"].update(build_pending_panel(data["pending"]))
                    layout["resolved"].update(
                        build_resolved_panel(data["resolved"]),
                    )
                    layout["portfolio"].update(build_portfolio_panel(stats))
                    layout["risk"].update(build_risk_panel(risk))
                    layout["calibration"].update(build_calibration_panel(bt))
                    layout["cache"].update(build_cache_panel())

                    status = "LIVE"
                    tick = 0
                except Exception as exc:
                    logger.exception("Dashboard refresh failed")
                    status = "ERROR"
                    tick = 0

            # ── Light refresh: header + footer (every second) ────────
            layout["header"].update(
                build_header(refresh_interval, last_refresh_str,
                             elapsed=tick, status=status),
            )
            layout["footer"].update(
                build_footer(
                    trading_mode=trading_mode,
                    ensemble_model=ensemble_model,
                    start_time=start_time,
                    db_path=DB_PATH,
                ),
            )
            live.refresh()

            await asyncio.sleep(1)
            tick += 1


async def show_trades(mode: str) -> None:
    """Print trades to stdout (scrollable, no Live mode).

    mode: "pending", "resolved", or "all"
    """
    console = Console()
    db = DbWriter(DB_PATH)
    await db.init_db()

    now = datetime.now(UTC)

    if mode in ("pending", "all"):
        trades = await db.read_pending_trades()
        tbl = Table(title=f"Pending Trades ({len(trades)})", expand=True)
        tbl.add_column("#", style="dim", width=3)
        tbl.add_column("Market", no_wrap=False, ratio=3)
        tbl.add_column("Side", width=8)
        tbl.add_column("Entry", justify="right", width=7)
        tbl.add_column("Size", justify="right", width=8)
        tbl.add_column("Age", justify="right", width=7)

        for i, t in enumerate(trades, 1):
            side = t.get("signal_type", "?")
            side_style = "green" if "yes" in side.lower() else "red"
            question = t.get("market_question", t.get("market_id", "?"))
            opened_str = t.get("opened_at", "")
            age = "-"
            if opened_str:
                try:
                    opened = datetime.fromisoformat(opened_str)
                    if opened.tzinfo is None:
                        opened = opened.replace(tzinfo=UTC)
                    hours = (now - opened).total_seconds() / 3600
                    if hours < 1:
                        age = f"{int((now - opened).total_seconds() / 60)}m"
                    elif hours < 24:
                        age = f"{hours:.0f}h"
                    else:
                        age = f"{(now - opened).days}d{int(hours % 24)}h"
                except (ValueError, TypeError):
                    pass

            tbl.add_row(
                str(i), question, Text(side, style=side_style),
                f"{t.get('entry_price', 0):.3f}",
                _usd(t.get("size_usdc", 0)), age,
            )
        console.print(tbl)
        console.print()

    if mode in ("resolved", "all"):
        resolved = await db.read_resolved_trades()
        resolved.sort(key=lambda r: r.get("resolved_at") or "", reverse=True)

        total_pnl = sum(r.get("pnl") or 0 for r in resolved)
        wins = sum(1 for r in resolved if (r.get("pnl") or 0) > 0)
        losses = len(resolved) - wins

        tbl = Table(
            title=f"Resolved Trades ({len(resolved)}) — {wins}W/{losses}L — PnL: ${total_pnl:+.2f}",
            expand=True,
        )
        tbl.add_column("#", style="dim", width=3)
        tbl.add_column("Market", no_wrap=False, ratio=3)
        tbl.add_column("Side", width=8)
        tbl.add_column("Entry", justify="right", width=7)
        tbl.add_column("Exit", justify="right", width=7)
        tbl.add_column("PnL", justify="right", width=9)
        tbl.add_column("Source", width=10)

        for i, t in enumerate(resolved, 1):
            side = t.get("signal_type", "?")
            side_style = "green" if "yes" in side.lower() else "red"
            question = t.get("market_question", "?")
            pnl = t.get("pnl") or 0.0
            pnl_style = "green" if pnl > 0 else ("red" if pnl < 0 else "dim")
            exit_p = t.get("exit_price")

            tbl.add_row(
                str(i), question, Text(side, style=side_style),
                f"{t.get('entry_price', 0):.3f}",
                f"{exit_p:.3f}" if exit_p is not None else "-",
                Text(f"${pnl:+.2f}", style=pnl_style),
                t.get("resolution_source", "-")[:10],
            )
        console.print(tbl)


def main() -> None:
    """Entry point for python -m weather.dashboard.

    Usage:
        python -m weather.dashboard                # Condensed default — 4 panels summary
        python -m weather.dashboard --phase2       # Phase 2 collection status (full)
        python -m weather.dashboard --calibration  # Brier / ECE / reliability (full)
        python -m weather.dashboard --bot          # Bot operational status (full)
        python -m weather.dashboard --trades       # Trade history (full)
        python -m weather.dashboard --pending      # Trades panel filtered to pending (compat)
        python -m weather.dashboard --resolved     # Trades panel filtered to resolved (compat)
        python -m weather.dashboard --scanner      # Legacy live scanner (Moon Dev style)
    """
    from weather import dashboard_widgets as dw

    parser = argparse.ArgumentParser(description="Weather Bot Dashboard")
    parser.add_argument("--db", default="data/bot.db", help="SQLite path (default: data/bot.db)")
    parser.add_argument("--log", default=None, help="Bot log path for the bot operational panel")
    parser.add_argument(
        "--refresh", type=int, default=DEFAULT_REFRESH,
        help=f"Refresh interval for --scanner (default: {DEFAULT_REFRESH}s)",
    )
    sel = parser.add_mutually_exclusive_group()
    sel.add_argument("--phase2", action="store_true",
                     help="Phase 2 collection status (full panel)")
    sel.add_argument("--calibration", action="store_true",
                     help="Calibration panel (full)")
    sel.add_argument("--bot", action="store_true",
                     help="Bot operational status (full)")
    sel.add_argument("--trades", action="store_true",
                     help="Trade history panel (full)")
    sel.add_argument("--scanner", action="store_true",
                     help="Legacy live scanner (Moon Dev style, refreshing)")
    parser.add_argument("--pending", action="store_true",
                        help="Trades panel filtered to pending (backward-compat)")
    parser.add_argument("--resolved", action="store_true",
                        help="Trades panel filtered to resolved (backward-compat)")
    args = parser.parse_args()

    console = Console()

    bare_panel_picked = args.phase2 or args.calibration or args.bot or args.trades or args.scanner
    if args.pending and not bare_panel_picked:
        data = dw.compute_trades_data(args.db)
        console.print(dw.build_trades_panel(data, mode="pending"))
        return
    if args.resolved and not bare_panel_picked:
        data = dw.compute_trades_data(args.db)
        console.print(dw.build_trades_panel(data, mode="resolved"))
        return

    if args.scanner:
        asyncio.run(run_dashboard(args.refresh))
        return
    if args.phase2:
        console.print(dw.build_phase2_panel(dw.compute_phase2_data(args.db)))
        return
    if args.calibration:
        console.print(dw.build_calibration_panel(dw.compute_calibration_data(args.db)))
        return
    if args.bot:
        console.print(dw.build_bot_panel(dw.compute_bot_data(args.db, log_path=args.log)))
        return
    if args.trades:
        mode = "pending" if args.pending else ("resolved" if args.resolved else "all")
        console.print(dw.build_trades_panel(dw.compute_trades_data(args.db), mode=mode))
        return

    # Default: condensed 4-panel view
    dw.render_condensed_default(console, args.db, log_path=args.log)


if __name__ == "__main__":
    main()
