"""Watch (live auto-refresh) tests — clamp, max_cycles, KeyboardInterrupt, retry."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from weather import dashboard_watch as dw


def test_clamp_interval_lower_bound() -> None:
    assert dw.clamp_interval(0) == dw.WATCH_INTERVAL_MIN
    assert dw.clamp_interval(-50) == dw.WATCH_INTERVAL_MIN


def test_clamp_interval_upper_bound() -> None:
    assert dw.clamp_interval(99999) == dw.WATCH_INTERVAL_MAX


def test_clamp_interval_passes_in_range() -> None:
    assert dw.clamp_interval(45) == 45


def test_run_watch_max_cycles_stops_loop() -> None:
    """max_cycles=3 → renderer called exactly 3 times."""
    counter = {"n": 0}

    def renderer():
        counter["n"] += 1
        return Panel(Text(f"render {counter['n']}"))

    cycles = dw.run_watch(renderer, interval=5, max_cycles=3,
                          sleep_fn=lambda _t: None,
                          console=Console(file=open_devnull(), force_terminal=False))
    assert cycles == 3
    assert counter["n"] == 3


def test_run_watch_keyboard_interrupt_exits_cleanly() -> None:
    """KeyboardInterrupt during sleep → loop exits without raising."""
    def renderer():
        return Panel(Text("ok"))

    def fake_sleep(_t):
        raise KeyboardInterrupt

    cycles = dw.run_watch(renderer, interval=5, max_cycles=10,
                          sleep_fn=fake_sleep,
                          console=Console(file=open_devnull(), force_terminal=False))
    assert cycles >= 1


def test_run_watch_db_busy_returns_graceful_frame(monkeypatch) -> None:
    """SQLITE_BUSY → graceful 'DB busy' frame, not a traceback."""
    def renderer():
        raise sqlite3.OperationalError("database is locked")

    cycles = dw.run_watch(renderer, interval=5, max_cycles=1,
                          sleep_fn=lambda _t: None,
                          console=Console(file=open_devnull(), force_terminal=False))
    assert cycles == 1  # didn't crash


def test_safe_render_returns_renderable_on_unrelated_operationalerror() -> None:
    def bad():
        raise sqlite3.OperationalError("schema mismatch xyz")
    out = dw._safe_render(bad)
    assert out is not None  # graceful schema-migrating frame


# helper


def open_devnull():
    import os
    return open(os.devnull, "w", encoding="utf-8")
