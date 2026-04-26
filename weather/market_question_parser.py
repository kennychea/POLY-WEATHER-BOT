"""Pure parser for Polymarket weather market questions.

Phase 2.1-quater. Used by scripts/backfill_weather_signals_outcomes.py to
recover (city, target_date, direction, threshold_low, threshold_high) from
the question text — these were never stored as columns on weather_signals.

Four observed patterns (covering 99% of past-due markets):
  exact : "be 18°C on April 28"
  bucket: "be between 88-89°F on April 28"
  above : "be 19°C or higher on April 28"
  below : "be 19°C or below on April 28"

Both metrics ("highest"/"lowest") and both units (°C/°F) are supported.
The corrupted replacement char (\\ufffd) is also accepted in place of °.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

# ── data class ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ParsedQuestion:
    metric: Literal["highest", "lowest"]
    city: str
    month: int  # 1-12
    day: int
    direction: Literal["exact", "bucket", "above", "below"]
    threshold_low: float
    threshold_high: float | None  # None for above/below; equal to low for exact
    unit: Literal["F", "C"]


# ── month → number ────────────────────────────────────────────────────

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}

# Cities where " City" is part of the canonical name (do NOT strip).
# Aligned with weather/resolution_stations.py POLYMARKET_STATIONS keys
# and CITY_ALIASES in weather/ensemble.py / market_scanner.py.
_CITY_CITY_KEEP: set[str] = {
    "Mexico City",
    "Panama City",
    "Kansas City",
    "Kuwait City",
    "Ho Chi Minh City",
}


# ── core parser ───────────────────────────────────────────────────────


_DEG = "[\u00b0\ufffd]"  # ° or replacement char (DB encoding mishaps)
_NUM = r"-?\d+(?:\.\d+)?"


# Pattern: "Will the {metric} temperature in {city} be {threshold_clause} on {Month} {day}?"
# threshold_clause variants:
#   exact : "<num>°<F|C>"
#   bucket: "between <num>-<num>°<F|C>"
#   above : "<num>°<F|C> or higher"
#   below : "<num>°<F|C> or below"
_QUESTION_RE = re.compile(
    r"^Will the (?P<metric>highest|lowest) temperature in (?P<city>.+?) be (?P<thr>.+?)"
    r" on (?P<month>[A-Za-z]+) (?P<day>\d{1,2})\??$",
    re.IGNORECASE,
)


def parse_market_question(question: str) -> ParsedQuestion | None:
    """Parse a weather market question into structured fields.

    Returns None on any parse failure (missing fields, unknown month, etc.).
    """
    if not question:
        return None
    q = question.strip()

    m = _QUESTION_RE.match(q)
    if m is None:
        return None

    metric_raw = m.group("metric").lower()
    if metric_raw not in ("highest", "lowest"):
        return None
    metric: Literal["highest", "lowest"] = metric_raw  # type: ignore[assignment]

    month_name = m.group("month").lower()
    month = _MONTHS.get(month_name)
    if month is None:
        return None
    day = int(m.group("day"))
    if not (1 <= day <= 31):
        return None

    city = _normalize_city(m.group("city"))

    parsed_thr = _parse_threshold_clause(m.group("thr"))
    if parsed_thr is None:
        return None
    direction, low, high, unit = parsed_thr

    return ParsedQuestion(
        metric=metric,
        city=city,
        month=month,
        day=day,
        direction=direction,
        threshold_low=low,
        threshold_high=high,
        unit=unit,
    )


def _normalize_city(raw: str) -> str:
    """Strip a trailing ' City' unless the city's canonical name keeps it."""
    s = raw.strip()
    if s in _CITY_CITY_KEEP:
        return s
    if s.endswith(" City") and s not in _CITY_CITY_KEEP:
        # Strip the suffix unless the prefix without " City" would be unrecognizable
        return s[: -len(" City")].strip()
    return s


_BUCKET_RE = re.compile(rf"^between\s+({_NUM})\s*-\s*({_NUM}){_DEG}([FC])$", re.IGNORECASE)
_ABOVE_RE = re.compile(rf"^({_NUM}){_DEG}([FC])\s+or\s+higher$", re.IGNORECASE)
_BELOW_RE = re.compile(rf"^({_NUM}){_DEG}([FC])\s+or\s+below$", re.IGNORECASE)
_EXACT_RE = re.compile(rf"^({_NUM}){_DEG}([FC])$", re.IGNORECASE)


def _parse_threshold_clause(
    clause: str,
) -> tuple[Literal["exact", "bucket", "above", "below"], float, float | None, Literal["F", "C"]] | None:
    """Return (direction, low, high, unit) from the threshold sub-clause."""
    s = clause.strip()

    if mb := _BUCKET_RE.match(s):
        low = float(mb.group(1))
        high = float(mb.group(2))
        unit = mb.group(3).upper()
        if unit not in ("F", "C"):
            return None
        return ("bucket", low, high, unit)  # type: ignore[return-value]

    if ma := _ABOVE_RE.match(s):
        low = float(ma.group(1))
        unit = ma.group(2).upper()
        if unit not in ("F", "C"):
            return None
        return ("above", low, None, unit)  # type: ignore[return-value]

    if mb_ := _BELOW_RE.match(s):
        low = float(mb_.group(1))
        unit = mb_.group(2).upper()
        if unit not in ("F", "C"):
            return None
        return ("below", low, None, unit)  # type: ignore[return-value]

    if me := _EXACT_RE.match(s):
        v = float(me.group(1))
        unit = me.group(2).upper()
        if unit not in ("F", "C"):
            return None
        return ("exact", v, v, unit)  # type: ignore[return-value]

    return None


# ── METAR comparison helper ───────────────────────────────────────────


def _f_to_c(f: float) -> float:
    return (f - 32.0) * 5.0 / 9.0


def check_outcome(
    parsed: ParsedQuestion,
    *,
    observed_high_f: float,
    observed_low_f: float,
) -> int:
    """Compare a parsed market against METAR observed daily extremes.

    Returns 1 (YES) or 0 (NO).

    Polymarket weather markets resolve on the daily extreme (high or low),
    typically rounded to whole degrees in the question's stated unit.
    """
    obs_f = observed_high_f if parsed.metric == "highest" else observed_low_f
    obs = obs_f if parsed.unit == "F" else _f_to_c(obs_f)

    if parsed.direction == "exact":
        return 1 if round(obs) == round(parsed.threshold_low) else 0

    if parsed.direction == "bucket":
        assert parsed.threshold_high is not None
        return 1 if parsed.threshold_low <= obs <= parsed.threshold_high else 0

    if parsed.direction == "above":
        return 1 if obs >= parsed.threshold_low else 0

    if parsed.direction == "below":
        return 1 if obs <= parsed.threshold_low else 0

    return 0  # unreachable
