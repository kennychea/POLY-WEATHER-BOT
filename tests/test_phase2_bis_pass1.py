"""Tests for scripts/phase2_bis_pass1_audit.py — pure helpers only.

Phase 2.1-bis Pass 1. The audit script itself runs against the prod DB and
produces files; the testable surface is the dedup + baseline-brier logic.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from scripts import phase2_bis_pass1_audit as p1  # type: ignore[import-not-found]


# ── dedupe_latest_forecast ────────────────────────────────────────────


def test_dedupe_keeps_max_timestamp_per_market() -> None:
    rows = [
        {"market_question": "Q1", "timestamp": "2026-04-20T10:00:00Z", "forecast_probability": 0.10},
        {"market_question": "Q1", "timestamp": "2026-04-25T10:00:00Z", "forecast_probability": 0.20},  # latest
        {"market_question": "Q1", "timestamp": "2026-04-22T10:00:00Z", "forecast_probability": 0.15},
        {"market_question": "Q2", "timestamp": "2026-04-21T10:00:00Z", "forecast_probability": 0.50},
        {"market_question": "Q2", "timestamp": "2026-04-24T10:00:00Z", "forecast_probability": 0.55},  # latest
    ]
    out = p1.dedupe_latest_forecast(rows)
    by_q = {r["market_question"]: r for r in out}
    assert len(out) == 2
    assert by_q["Q1"]["forecast_probability"] == 0.20
    assert by_q["Q2"]["forecast_probability"] == 0.55


def test_dedupe_handles_single_row_per_market() -> None:
    rows = [
        {"market_question": "Q1", "timestamp": "2026-04-20T10:00:00Z", "forecast_probability": 0.10},
        {"market_question": "Q2", "timestamp": "2026-04-21T10:00:00Z", "forecast_probability": 0.50},
    ]
    out = p1.dedupe_latest_forecast(rows)
    assert len(out) == 2


def test_dedupe_handles_empty() -> None:
    assert p1.dedupe_latest_forecast([]) == []


def test_dedupe_handles_missing_timestamp_gracefully() -> None:
    """Rows missing timestamp should be considered stalest (lose the dedup)."""
    rows = [
        {"market_question": "Q1", "timestamp": None, "forecast_probability": 0.10},
        {"market_question": "Q1", "timestamp": "2026-04-25T10:00:00Z", "forecast_probability": 0.20},
    ]
    out = p1.dedupe_latest_forecast(rows)
    assert len(out) == 1
    assert out[0]["forecast_probability"] == 0.20


# ── naive_baseline_brier ──────────────────────────────────────────────


def test_naive_baseline_brier_constant_yes_predictor() -> None:
    """Brier of a constant P(YES)=0.19 predictor on outcomes [1,0,0,0,0] (1/5 YES rate)."""
    actuals = [1, 0, 0, 0, 0]
    p_yes = 0.19
    expected = ((0.19 - 1) ** 2 + 4 * (0.19 - 0) ** 2) / 5
    assert p1.naive_baseline_brier(actuals, p_yes) == pytest.approx(expected)


def test_naive_baseline_brier_perfect_baseline() -> None:
    """Constant 0.5 predictor on perfectly balanced outcomes → 0.25."""
    actuals = [1, 0]
    assert p1.naive_baseline_brier(actuals, 0.5) == pytest.approx(0.25)


def test_naive_baseline_brier_empty() -> None:
    assert p1.naive_baseline_brier([], 0.19) == 0.0
