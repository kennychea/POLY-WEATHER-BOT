"""Phase 2.1-bis re-run — Pass 1 (signal-only, n=464).

Audit reliability + resolution on the post-2.1-quater backfilled population.
DOES NOT make a KILL/diag/regate decision — Pass 1 is signal-only. Decision
is deferred to Pass 2 (~5–8 days, n cumulé ≥1500).

Population:
    weather_signals WHERE actual_outcome IS NOT NULL,
    deduplicated by market_question (latest forecast row per market).

Outputs:
    tasks/phase2_bis/pass1/global_reliability.png
    tasks/phase2_bis/pass1/global_histogram.png
    tasks/phase2_bis/pass1/reliability_by_forecast_bucket.png
    tasks/phase2_bis/pass1/reliability_by_city.png
    tasks/phase2_bis/pass1/segment_ranking.csv
    tasks/phase2_bis_rerun_pass1.md
"""

from __future__ import annotations

import csv
import logging
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from weather.calibration import (  # noqa: E402
    brier_decomposition,
    expected_calibration_error,
    maximum_calibration_error,
    monotonicity_score,
    reliability_diagram,
)

# Reuse helpers from the existing audit module
from scripts.phase2_full_population_audit import (  # noqa: E402
    analyze_segments,
    bucket_ensemble_std,
    bucket_forecast,
    bucket_horizon,
    compute_segment,
    plot_global,
    plot_histogram,
    plot_segment,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("phase2_bis_pass1")

OUT_DIR = ROOT / "tasks" / "phase2_bis" / "pass1"
REPORT_PATH = ROOT / "tasks" / "phase2_bis_rerun_pass1.md"

# ── pure helpers (unit-tested) ─────────────────────────────────────────


def dedupe_latest_forecast(rows: list[dict]) -> list[dict]:
    """For each market_question, keep the row with the max(timestamp).

    Rows missing timestamp are deprioritized (treated as stalest).
    Heuristic justification: Polymarket markets settle ~J+2 after forecast_date,
    so the row with the latest timestamp is the freshest pre-settlement forecast
    — least informative leakage from later observations, most representative of
    what the bot would have actually used to trade.
    """
    by_q: dict[str, dict] = {}
    for r in rows:
        q = r.get("market_question") or ""
        ts = r.get("timestamp")
        if q not in by_q:
            by_q[q] = r
            continue
        prev_ts = by_q[q].get("timestamp")
        # Both None → keep first; new None → keep prev; prev None → take new; else compare
        if ts is None and prev_ts is None:
            continue
        if ts is None:
            continue
        if prev_ts is None:
            by_q[q] = r
            continue
        if str(ts) > str(prev_ts):  # ISO-8601 lexicographic == chronological
            by_q[q] = r
    return list(by_q.values())


def naive_baseline_brier(actuals: list[int], p_yes: float) -> float:
    """Brier score of a constant predictor that always says P(YES)=p_yes."""
    if not actuals:
        return 0.0
    return sum((p_yes - y) ** 2 for y in actuals) / len(actuals)


# ── data loading ──────────────────────────────────────────────────────


def load_population(db_path: Path) -> list[dict]:
    """Load every weather_signals row with actual_outcome NOT NULL."""
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute(
        """
        SELECT market_id, market_question, forecast_probability, market_price,
               location, forecast_date, weather_metric, threshold_value,
               timestamp, confidence, signal_type, ensemble_std, horizon_hours,
               actual_outcome, outcome_source
        FROM weather_signals
        WHERE actual_outcome IS NOT NULL
        """
    )
    rows: list[dict] = []
    for r in cur.fetchall():
        rows.append({
            "market_id": r[0], "market_question": r[1],
            "forecast_probability": float(r[2]),
            "market_price": r[3],
            "location": r[4], "forecast_date": r[5],
            "metric": r[6], "threshold_value": r[7],
            "timestamp": r[8],
            "confidence": r[9] or "unknown",
            "signal_type": r[10],
            "ensemble_std": r[11], "horizon_hours": r[12],
            "actual_yes": int(r[13]),
            "outcome_source": r[14],
        })
    conn.close()
    return rows


# ── plotting ──────────────────────────────────────────────────────────


def plot_reliability_by_city_grid(
    city_to_rows: dict[str, list[dict]],
    out_path: Path,
    top_n: int = 8,
) -> None:
    """3×3 grid (1 spare cell empty): reliability scatter for top-N cities."""
    cities = sorted(city_to_rows.items(), key=lambda kv: -len(kv[1]))[:top_n]
    fig, axes = plt.subplots(3, 3, figsize=(12, 11), sharex=True, sharey=True)
    axes = axes.flatten()
    for i, (city, crows) in enumerate(cities):
        ax = axes[i]
        f = [r["forecast_probability"] for r in crows]
        o = [r["actual_yes"] for r in crows]
        if len(crows) >= 5:
            rd = reliability_diagram(f, o, bins=5)
            mf = [v for v in rd["bin_mean_forecast"] if v is not None]
            of = [v for v in rd["bin_obs_freq"] if v is not None]
            counts = [c for c in rd["bin_count"] if c > 0]
            ax.plot([0, 1], [0, 1], "--", color="gray")
            if mf:
                sizes = [max(20, 200 * c / max(counts)) for c in counts]
                ax.scatter(mf, of, s=sizes, color="tab:blue", alpha=0.7)
        ax.set_title(f"{city} (n={len(crows)})")
        ax.set_xlim(0, 1); ax.set_ylim(0, 1)
    for j in range(len(cities), 9):
        axes[j].axis("off")
    fig.suptitle("Reliability per city (top 8)")
    fig.tight_layout(); fig.savefig(out_path, dpi=100); plt.close(fig)


# ── markdown report ───────────────────────────────────────────────────


def _fmt(x: Any, digits: int = 4) -> str:
    if x is None:
        return "—"
    if isinstance(x, float):
        return f"{x:.{digits}f}"
    return str(x)


def _format_segment_table(segments: list[dict], min_n: int = 30) -> str:
    """Markdown table; segments with n<min_n marked 'wait Pass 2'."""
    headers = ["segment", "bucket", "n", "mean_fc", "obs_freq", "Wilson_lo", "Wilson_hi", "ECE", "rel", "res", "rank", "verdict"]
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    ranked = sorted(
        [s for s in segments if not s.get("skip")],
        key=lambda s: -s.get("rank_score", 0),
    )
    for s in ranked:
        verdict = "wait Pass 2" if s["n"] < min_n else "candidate"
        lo, hi = s["obs_freq_ci"]
        lines.append("| " + " | ".join([
            s["segment"], str(s["bucket"]), str(s["n"]),
            _fmt(s["mean_forecast"], 3), _fmt(s["obs_freq"], 3),
            _fmt(lo, 3), _fmt(hi, 3),
            _fmt(s["ece"], 4), _fmt(s["rel"], 4), _fmt(s["res"], 4),
            _fmt(s["rank_score"], 4), verdict,
        ]) + " |")
    return "\n".join(lines)


def _write_report(
    *,
    n_total_rows: int,
    n_after_dedup: int,
    base_rate_yes: float,
    base_rate_no: float,
    brier_model: float,
    brier_baseline: float,
    ece: float,
    mce: float,
    rel_g: float,
    res_g: float,
    unc_g: float,
    mono_g: float,
    mono_gq: float,
    forecast_dist: list[dict],
    city_segments: list[dict],
    horizon_segments: list[dict],
) -> None:
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    lines.append("# Phase 2.1-bis — Re-run Pass 1 (signal-only, n=464)\n")
    lines.append(
        "_Pass 1 verdict on the post-2.1-quater backfilled population. "
        "**No KILL / 2.3 / 2.4 decision is taken on this pass** — Pass 2 "
        "(~5–8 days, n cumulé ≥1500) tranche._\n"
    )

    # ── Verdict global ────────────────────────────────────────────────
    delta = brier_model - brier_baseline
    if delta < -0.005:
        verdict = "**model BEATS naive baseline** (Δ Brier > 0.005 in favour of model)"
    elif delta > 0.005:
        verdict = "**model LOSES to naive baseline** (Δ Brier > 0.005 against model — RED FLAG)"
    else:
        verdict = "**model TIES naive baseline** (Δ Brier within ±0.005 — no measurable signal)"

    lines.append("## Verdict global\n")
    lines.append(f"| Metric | Value |")
    lines.append(f"| --- | --- |")
    lines.append(f"| Markets after dedup | {n_after_dedup} |")
    lines.append(f"| Total rows pre-dedup | {n_total_rows} |")
    lines.append(f"| Base rate YES (observed) | {base_rate_yes:.4f} ({base_rate_yes*100:.1f}%) |")
    lines.append(f"| Base rate NO (observed) | {base_rate_no:.4f} ({base_rate_no*100:.1f}%) |")
    lines.append(f"| **Brier — model** | {brier_model:.4f} |")
    lines.append(f"| **Brier — baseline P(YES)={base_rate_yes:.3f}** | {brier_baseline:.4f} |")
    lines.append(f"| **Δ (model − baseline)** | {delta:+.4f} |")
    lines.append(f"| ECE (10 equal bins) | {ece:.4f} |")
    lines.append(f"| MCE (10 equal bins) | {mce:.4f} |")
    lines.append(f"| Reliability (lower = better) | {rel_g:.4f} |")
    lines.append(f"| Resolution (higher = better) | {res_g:.4f} |")
    lines.append(f"| Uncertainty (base-rate term) | {unc_g:.4f} |")
    lines.append(f"| Monotonicity — equal bins | {mono_g:.4f} |")
    lines.append(f"| Monotonicity — quantile bins | {mono_gq:.4f} |")
    lines.append("")
    lines.append(f"**Conclusion (signal-only):** {verdict}.")
    if rel_g > res_g:
        lines.append(
            f"Brier decomposition shows reliability ({rel_g:.4f}) > resolution ({res_g:.4f}) — "
            "the model's calibration error dominates over its discriminating power. Re-calibration "
            "(isotonic / Platt) is a lever; gating on a discriminating sub-segment is harder."
        )
    else:
        lines.append(
            f"Brier decomposition shows resolution ({res_g:.4f}) ≥ reliability ({rel_g:.4f}) — "
            "the model has measurable discriminating power that calibration error has not yet eaten. "
            "Sub-segments are worth investigating in Pass 2."
        )
    lines.append("")

    # ── Distribution par bucket ───────────────────────────────────────
    lines.append("## Distribution par forecast_bucket\n")
    lines.append("| bucket | n | mean_forecast (P(YES)) | obs_freq (YES) | obs_freq (NO) | rel | res |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for s in forecast_dist:
        if s.get("skip"):
            lines.append(f"| {s['bucket']} | {s['n']} | — | — | — | — | — |")
            continue
        lines.append(
            f"| {s['bucket']} | {s['n']} | {s['mean_forecast']:.3f} | "
            f"{s['obs_freq']:.3f} | {1 - s['obs_freq']:.3f} | "
            f"{s['rel']:.4f} | {s['res']:.4f} |"
        )
    lines.append("")
    lines.append(
        "_G1 reference pattern from Phase D: forecast 3 % → outcome ~42 % YES on n=115. "
        "Compare the `<0.05` bucket above to that anchor; if `obs_freq (YES)` is still well "
        "above 0.05, the original miscalibration finding holds._"
    )
    lines.append("")

    # ── Segments (signal-only) ────────────────────────────────────────
    lines.append("## Segments (signal-only — do NOT decide on these numbers)\n")
    lines.append("**Pass 2 (n cumulé ≥1500) is when these segments become actionable.** "
                 "n<30 segments are flagged 'wait Pass 2'; numbers shown for orientation only.\n")

    lines.append("### City\n")
    lines.append(_format_segment_table(city_segments, min_n=30))
    lines.append("")

    lines.append("### Horizon\n")
    lines.append(_format_segment_table(horizon_segments, min_n=30))
    lines.append("")

    lines.append("### Forecast bucket\n")
    lines.append(_format_segment_table(forecast_dist, min_n=30))
    lines.append("")

    # ── Décision Pass 1 ───────────────────────────────────────────────
    lines.append("## Décision Pass 1\n")
    lines.append(
        "Verdict signal-only ; **décision finale Pass 2 à J+5–8 sur n cumulé ≥1500**.\n"
    )
    lines.append("- Aucune action KILL / 2.3 / 2.4 prise sur cette base.")
    lines.append("- La population continue de croître via le bot data-only ; "
                 "`scripts/shadow_monitor.py` est l'oracle pour déclencher Pass 2.")
    lines.append("- Le `outcome_resolution_age` gate et le `coverage_by_outcome_source` "
                 "restent les sentinelles d'intégrité du backfill.\n")

    # ── Files generated ───────────────────────────────────────────────
    lines.append("## Generated artifacts\n")
    for p in sorted(OUT_DIR.iterdir()):
        lines.append(f"- `tasks/phase2_bis/pass1/{p.name}`")
    lines.append(f"- `{REPORT_PATH.relative_to(ROOT).as_posix()}` (this file)")
    lines.append("")

    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")


# ── main ──────────────────────────────────────────────────────────────


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    db_path = ROOT / "data" / "bot.db"
    raw_rows = load_population(db_path)
    n_total = len(raw_rows)
    rows = dedupe_latest_forecast(raw_rows)
    n_dedup = len(rows)
    logger.info("loaded %d raw rows → %d unique markets after dedup", n_total, n_dedup)

    # Filter rows with valid actual_outcome (drop the few HK 'failed' which are NULL anyway)
    rows = [r for r in rows if r.get("actual_yes") in (0, 1)]

    f_all = [r["forecast_probability"] for r in rows]
    o_all = [r["actual_yes"] for r in rows]
    n = len(rows)
    base_rate_yes = sum(o_all) / n
    base_rate_no = 1.0 - base_rate_yes

    brier_model = sum((p - y) ** 2 for p, y in zip(f_all, o_all)) / n
    brier_baseline = naive_baseline_brier(o_all, base_rate_yes)

    ece = expected_calibration_error(f_all, o_all, bins=10)
    mce = maximum_calibration_error(f_all, o_all, bins=10)
    rel, res, unc = brier_decomposition(f_all, o_all, bins=10)
    rd = reliability_diagram(f_all, o_all, bins=10)
    rdq = reliability_diagram(f_all, o_all, bins=10, bin_strategy="quantile")
    mono_eq = monotonicity_score(rd["bin_obs_freq"], rd["bin_mean_forecast"])
    mono_q = monotonicity_score(rdq["bin_obs_freq"], rdq["bin_mean_forecast"])

    # Plots
    plot_global(rd, OUT_DIR / "global_reliability.png")
    plot_histogram(f_all, OUT_DIR / "global_histogram.png")

    forecast_dist = analyze_segments(
        rows, lambda t: bucket_forecast(t["forecast_probability"]), "forecast_bucket"
    )
    plot_segment(forecast_dist, "forecast_bucket", OUT_DIR / "reliability_by_forecast_bucket.png")

    city_to_rows: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        city_to_rows[r.get("location") or "?"].append(r)
    plot_reliability_by_city_grid(city_to_rows, OUT_DIR / "reliability_by_city.png", top_n=8)

    city_cnt = {c: len(rs) for c, rs in city_to_rows.items()}
    top15 = set(sorted(city_cnt.keys(), key=lambda c: -city_cnt[c])[:15])
    city_segments = analyze_segments(
        rows,
        lambda t: (t.get("location") or "?") if (t.get("location") or "?") in top15 else "other",
        "city_top15",
    )

    horizon_segments = analyze_segments(
        rows, lambda t: bucket_horizon(t.get("horizon_hours")), "horizon"
    )

    # Segment ranking CSV
    csv_path = OUT_DIR / "segment_ranking.csv"
    fields = ["segment", "bucket", "n", "mean_forecast", "obs_freq",
              "obs_freq_ci_lo", "obs_freq_ci_hi", "ece", "rel", "res",
              "monotonicity", "rank_score"]
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for s in forecast_dist + city_segments + horizon_segments:
            if s.get("skip"):
                w.writerow({"segment": s["segment"], "bucket": s["bucket"], "n": s["n"]})
                continue
            lo, hi = s["obs_freq_ci"]
            w.writerow({
                "segment": s["segment"], "bucket": s["bucket"], "n": s["n"],
                "mean_forecast": round(s["mean_forecast"], 4),
                "obs_freq": round(s["obs_freq"], 4),
                "obs_freq_ci_lo": round(lo, 4), "obs_freq_ci_hi": round(hi, 4),
                "ece": round(s["ece"], 4), "rel": round(s["rel"], 4),
                "res": round(s["res"], 4),
                "monotonicity": round(s["monotonicity"], 4),
                "rank_score": round(s["rank_score"], 4),
            })

    _write_report(
        n_total_rows=n_total,
        n_after_dedup=n_dedup,
        base_rate_yes=base_rate_yes,
        base_rate_no=base_rate_no,
        brier_model=brier_model,
        brier_baseline=brier_baseline,
        ece=ece, mce=mce,
        rel_g=rel, res_g=res, unc_g=unc,
        mono_g=mono_eq, mono_gq=mono_q,
        forecast_dist=forecast_dist,
        city_segments=city_segments,
        horizon_segments=horizon_segments,
    )

    logger.info(
        "Pass 1 done — Brier model=%.4f baseline=%.4f Δ=%+.4f ECE=%.4f mono(eq)=%.3f mono(q)=%.3f",
        brier_model, brier_baseline, brier_model - brier_baseline, ece, mono_eq, mono_q,
    )
    logger.info("report: %s", REPORT_PATH)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
