"""Rich Live wrapper for --watch / --watch-interval — dashboard v3.

Manual-refresh Live with a Renderable callable, clean Ctrl+C exit, configurable
interval (clamped to [WATCH_INTERVAL_MIN, WATCH_INTERVAL_MAX]). Tests can pass
max_cycles to bound the loop deterministically.

DB-busy / SQLITE_BUSY: a couple of light retries with backoff, then a graceful
"DB busy, retrying..." frame rather than a traceback.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from rich.console import Console, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

WATCH_INTERVAL_MIN = 5
WATCH_INTERVAL_MAX = 600
WATCH_DEFAULT_INTERVAL = 30
WATCH_DB_RETRY_MAX = 2
WATCH_DB_RETRY_BACKOFF = 0.25  # seconds


def clamp_interval(seconds: int) -> int:
    return max(WATCH_INTERVAL_MIN, min(WATCH_INTERVAL_MAX, int(seconds)))


def _safe_render(renderer: Callable[[], RenderableType]) -> RenderableType:
    """Run renderer with light retry on SQLITE_BUSY, fall back to a graceful frame."""
    last_exc: Exception | None = None
    for attempt in range(WATCH_DB_RETRY_MAX + 1):
        try:
            return renderer()
        except sqlite3.OperationalError as e:
            last_exc = e
            if "locked" in str(e).lower() or "busy" in str(e).lower():
                time.sleep(WATCH_DB_RETRY_BACKOFF * (attempt + 1))
                continue
            return Panel(Text(f"Schema migrating... ({e})", style="yellow"),
                         title="Watch", border_style="yellow")
    return Panel(Text(f"DB busy, retrying... ({last_exc})", style="yellow"),
                 title="Watch", border_style="yellow")


def _wrap_with_header(
    inner: RenderableType, *, interval: int, cycle: int,
) -> RenderableType:
    now = datetime.now(UTC).isoformat(timespec="seconds")
    header = Text()
    header.append(f"Refresh #{cycle} at {now}  ")
    header.append(f"(interval={interval}s)  ", style="dim")
    footer = Text()
    footer.append("Press Ctrl+C to exit  |  ", style="dim")
    footer.append(f"Refreshing every {interval}s  |  ", style="dim")
    footer.append(f"Last refresh {datetime.now(UTC).strftime('%H:%M:%S')}", style="dim")

    grid = Table.grid()
    grid.add_row(header)
    grid.add_row(inner)
    grid.add_row(footer)
    return grid


def run_watch(
    renderer: Callable[[], RenderableType],
    *,
    interval: int = WATCH_DEFAULT_INTERVAL,
    console: Console | None = None,
    max_cycles: int | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> int:
    """Run a Rich Live loop calling renderer() every `interval` seconds.

    Returns the number of cycles rendered. Ctrl+C exits cleanly with the last
    frame preserved.
    """
    interval = clamp_interval(interval)
    console = console or Console()
    cycles = 0
    try:
        with Live(
            _wrap_with_header(_safe_render(renderer), interval=interval, cycle=cycles + 1),
            console=console, auto_refresh=False, screen=False,
            refresh_per_second=4,
        ) as live:
            cycles = 1
            while True:
                if max_cycles is not None and cycles >= max_cycles:
                    break
                sleep_fn(interval)
                cycles += 1
                live.update(
                    _wrap_with_header(_safe_render(renderer),
                                      interval=interval, cycle=cycles),
                    refresh=True,
                )
    except KeyboardInterrupt:
        pass
    return cycles
