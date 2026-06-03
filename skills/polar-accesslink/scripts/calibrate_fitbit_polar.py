#!/usr/bin/env python3
"""Calibrate Fitbit vs Polar exercise heart rate by matching whole runs."""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from zoneinfo import ZoneInfo

from healthdata.config import (
    CALIBRATION_REPORT_DIR,
    DEFAULT_LOCAL_TIMEZONE,
    FITBIT_STAGED_DIR,
    FITBIT_TAKEOUT_DIR,
    POLAR_FITBIT_CALIBRATION_DERIVED_DIR,
    POLAR_STAGED_DIR,
)

LOCAL_TZ = ZoneInfo(DEFAULT_LOCAL_TIMEZONE)
POLAR_DATA_DIR = POLAR_STAGED_DIR
OUT_DIR = POLAR_FITBIT_CALIBRATION_DERIVED_DIR
REPORT_DIR = CALIBRATION_REPORT_DIR


@dataclass(frozen=True)
class MatchRules:
    start_minutes: float = 5.0
    duration_minutes: float = 10.0
    duration_pct: float = 0.25
    distance_miles: float = 0.75
    distance_pct: float = 0.25
    candidate_window_minutes: float = 10.0


def fitbit_takeout_time(value: Any) -> pd.Timestamp:
    # Fitbit Takeout exercise timestamps are UTC clock values with no offset.
    parsed = pd.to_datetime(value, format="%m/%d/%y %H:%M:%S", errors="coerce", utc=True)
    if pd.isna(parsed):
        return pd.NaT
    return parsed.tz_convert(LOCAL_TZ).tz_localize(None)


def fitbit_staged_time(value: Any) -> pd.Timestamp:
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return pd.NaT
    if getattr(parsed, "tzinfo", None) is not None:
        return parsed.tz_convert(LOCAL_TZ).tz_localize(None)
    return parsed


def polar_time(value: Any) -> pd.Timestamp:
    # Polar Beat/Flow session exports have matched local clock time empirically.
    return pd.to_datetime(value, errors="coerce")


def heart_zone_minutes(zones: list[dict[str, Any]] | None) -> dict[str, float | None]:
    result: dict[str, float | None] = {}
    for zone in zones or []:
        key = str(zone.get("name", "")).lower().replace(" ", "_")
        try:
            result[key] = float(zone.get("minutes"))
        except (TypeError, ValueError):
            result[key] = None
    return result


def load_fitbit_runs() -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for path in (FITBIT_TAKEOUT_DIR / "Global Export Data").glob("exercise-*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        for activity in payload:
            if str(activity.get("activityName")).lower() != "run":
                continue
            zones = heart_zone_minutes(activity.get("heartRateZones"))
            rows.append(
                {
                    "fitbit_source": "takeout",
                    "fitbit_id": str(activity.get("logId")),
                    "fitbit_start": fitbit_takeout_time(activity.get("startTime")),
                    "fitbit_duration_min": float(activity.get("duration") or 0) / 60000,
                    "fitbit_distance_mi": activity.get("distance"),
                    "fitbit_avg_hr": activity.get("averageHeartRate"),
                    "fitbit_fat_burn_min": zones.get("fat_burn"),
                    "fitbit_cardio_min": zones.get("cardio"),
                    "fitbit_peak_min": zones.get("peak"),
                }
            )

    staged_path = FITBIT_STAGED_DIR / "activity_logs.csv"
    if staged_path.exists():
        staged = pd.read_csv(staged_path)
        for _, activity in staged.iterrows():
            if str(activity.get("activity_name")).lower() != "run":
                continue
            rows.append(
                {
                    "fitbit_source": "staged",
                    "fitbit_id": str(activity.get("log_id")),
                    "fitbit_start": fitbit_staged_time(activity.get("start_time")),
                    "fitbit_duration_min": activity.get("duration_minutes"),
                    "fitbit_distance_mi": activity.get("distance"),
                    "fitbit_avg_hr": activity.get("average_heart_rate"),
                    "fitbit_fat_burn_min": activity.get("heart_zone_fat_burn_minutes"),
                    "fitbit_cardio_min": activity.get("heart_zone_cardio_minutes"),
                    "fitbit_peak_min": activity.get("heart_zone_peak_minutes"),
                }
            )

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    numeric = [
        "fitbit_duration_min",
        "fitbit_distance_mi",
        "fitbit_avg_hr",
        "fitbit_fat_burn_min",
        "fitbit_cardio_min",
        "fitbit_peak_min",
    ]
    for col in numeric:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return (
        df.dropna(subset=["fitbit_id", "fitbit_start", "fitbit_avg_hr"])
        .sort_values(["fitbit_id", "fitbit_source"])
        .drop_duplicates(subset=["fitbit_id"], keep="last")
        .sort_values("fitbit_start")
        .reset_index(drop=True)
    )


def load_polar_runs() -> pd.DataFrame:
    path = POLAR_DATA_DIR / "training_sessions.csv"
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path)
    if df.empty:
        return df
    runs = df[df["name"].astype(str).str.lower().str.contains("running", na=False)].copy()
    runs["polar_start"] = runs["start_time"].map(polar_time)
    runs["polar_duration_min"] = pd.to_numeric(runs["duration_minutes"], errors="coerce")
    runs["polar_distance_mi"] = pd.to_numeric(runs["distance_miles"], errors="coerce")
    runs["polar_avg_hr"] = pd.to_numeric(runs["hr_avg"], errors="coerce")
    runs["polar_max_hr"] = pd.to_numeric(runs["hr_max"], errors="coerce")
    return (
        runs.dropna(subset=["exercise_id", "polar_start", "polar_duration_min", "polar_distance_mi", "polar_avg_hr"])
        .sort_values("polar_start")
        .reset_index(drop=True)
    )


def match_runs(fitbit: pd.DataFrame, polar: pd.DataFrame, rules: MatchRules) -> tuple[pd.DataFrame, pd.DataFrame]:
    matches: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    for _, polar_row in polar.iterrows():
        lower = polar_row.polar_start - pd.Timedelta(minutes=rules.candidate_window_minutes)
        upper = polar_row.polar_start + pd.Timedelta(minutes=rules.candidate_window_minutes)
        candidates = fitbit[(fitbit["fitbit_start"] >= lower) & (fitbit["fitbit_start"] <= upper)].copy()
        if candidates.empty:
            rejected.append(
                {
                    "polar_start": polar_row.polar_start,
                    "polar_duration_min": polar_row.polar_duration_min,
                    "polar_distance_mi": polar_row.polar_distance_mi,
                    "reason": "no Fitbit run within candidate window",
                }
            )
            continue

        candidates["start_delta_min"] = (
            candidates["fitbit_start"].sub(polar_row.polar_start).abs().dt.total_seconds() / 60
        )
        candidates["duration_delta_min"] = (
            candidates["fitbit_duration_min"].sub(polar_row.polar_duration_min).abs()
        )
        candidates["duration_delta_pct"] = candidates["duration_delta_min"] / polar_row.polar_duration_min
        candidates["distance_delta_mi"] = (
            candidates["fitbit_distance_mi"].sub(polar_row.polar_distance_mi).abs()
        )
        candidates["distance_delta_pct"] = candidates["distance_delta_mi"] / polar_row.polar_distance_mi
        eligible = candidates[
            (candidates["start_delta_min"] <= rules.start_minutes)
            & (candidates["duration_delta_min"] <= rules.duration_minutes)
            & (candidates["duration_delta_pct"] <= rules.duration_pct)
            & (candidates["distance_delta_mi"] <= rules.distance_miles)
            & (candidates["distance_delta_pct"] <= rules.distance_pct)
        ].copy()

        if len(eligible) != 1:
            rejected.append(
                {
                    "polar_start": polar_row.polar_start,
                    "polar_duration_min": polar_row.polar_duration_min,
                    "polar_distance_mi": polar_row.polar_distance_mi,
                    "reason": f"{len(eligible)} eligible Fitbit candidates",
                }
            )
            continue

        fitbit_row = eligible.iloc[0]
        matches.append(
            {
                "polar_start": polar_row.polar_start,
                "fitbit_start": fitbit_row.fitbit_start,
                "fitbit_source": fitbit_row.fitbit_source,
                "polar_exercise_id": str(polar_row.exercise_id),
                "fitbit_id": str(fitbit_row.fitbit_id),
                "start_delta_min": fitbit_row.start_delta_min,
                "duration_delta_min": fitbit_row.duration_delta_min,
                "duration_delta_pct": fitbit_row.duration_delta_pct,
                "distance_delta_mi": fitbit_row.distance_delta_mi,
                "distance_delta_pct": fitbit_row.distance_delta_pct,
                "polar_duration_min": polar_row.polar_duration_min,
                "fitbit_duration_min": fitbit_row.fitbit_duration_min,
                "polar_distance_mi": polar_row.polar_distance_mi,
                "fitbit_distance_mi": fitbit_row.fitbit_distance_mi,
                "polar_avg_hr": polar_row.polar_avg_hr,
                "fitbit_avg_hr": fitbit_row.fitbit_avg_hr,
                "fitbit_minus_polar_hr": fitbit_row.fitbit_avg_hr - polar_row.polar_avg_hr,
                "polar_max_hr": polar_row.polar_max_hr,
                "fitbit_fat_burn_min": fitbit_row.fitbit_fat_burn_min,
                "fitbit_cardio_min": fitbit_row.fitbit_cardio_min,
                "fitbit_peak_min": fitbit_row.fitbit_peak_min,
            }
        )

    matched = pd.DataFrame(matches).sort_values("polar_start").reset_index(drop=True)
    rejected_df = pd.DataFrame(rejected)
    return matched, rejected_df


def mad_outlier_mask(values: pd.Series, threshold: float = 6.0) -> pd.Series:
    median = values.median()
    mad = (values - median).abs().median()
    if mad == 0 or math.isnan(mad):
        return pd.Series([False] * len(values), index=values.index)
    robust_sigma = 1.4826 * mad
    return (values - median).abs() > threshold * robust_sigma


def loo_mae_constant_bias(fitbit_hr: np.ndarray, polar_hr: np.ndarray) -> float:
    errors: list[float] = []
    for idx in range(len(fitbit_hr)):
        train = np.arange(len(fitbit_hr)) != idx
        bias = np.mean(fitbit_hr[train] - polar_hr[train])
        pred = fitbit_hr[idx] - bias
        errors.append(abs(pred - polar_hr[idx]))
    return float(np.mean(errors))


def loo_mae_linear(fitbit_hr: np.ndarray, polar_hr: np.ndarray) -> float:
    errors: list[float] = []
    for idx in range(len(fitbit_hr)):
        train = np.arange(len(fitbit_hr)) != idx
        x_train = fitbit_hr[train]
        y_train = polar_hr[train]
        beta = np.linalg.lstsq(np.c_[np.ones(len(x_train)), x_train], y_train, rcond=None)[0]
        pred = np.array([1.0, fitbit_hr[idx]]) @ beta
        errors.append(abs(pred - polar_hr[idx]))
    return float(np.mean(errors))


def bootstrap_ci(values: np.ndarray, fn, iterations: int = 10000, seed: int = 42) -> tuple[float, float]:
    rng = np.random.default_rng(seed)
    estimates = np.empty(iterations)
    n = len(values)
    for idx in range(iterations):
        sample = values[rng.integers(0, n, n)]
        estimates[idx] = fn(sample)
    lo, hi = np.percentile(estimates, [2.5, 97.5])
    return float(lo), float(hi)


def bootstrap_linear_ci(x: np.ndarray, y: np.ndarray, iterations: int = 10000, seed: int = 42) -> dict[str, tuple[float, float]]:
    rng = np.random.default_rng(seed)
    betas = np.empty((iterations, 2))
    n = len(x)
    for idx in range(iterations):
        sample_idx = rng.integers(0, n, n)
        xs = x[sample_idx]
        ys = y[sample_idx]
        betas[idx] = np.linalg.lstsq(np.c_[np.ones(len(xs)), xs], ys, rcond=None)[0]
    return {
        "intercept": tuple(float(v) for v in np.percentile(betas[:, 0], [2.5, 97.5])),
        "slope": tuple(float(v) for v in np.percentile(betas[:, 1], [2.5, 97.5])),
    }


def summarize_models(matches: pd.DataFrame) -> dict[str, Any]:
    clean = matches[~matches["calibration_outlier"]].copy()
    x = clean["fitbit_avg_hr"].to_numpy(dtype=float)
    y = clean["polar_avg_hr"].to_numpy(dtype=float)
    deltas = x - y
    beta = np.linalg.lstsq(np.c_[np.ones(len(x)), x], y, rcond=None)[0]
    linear_pred = np.c_[np.ones(len(x)), x] @ beta

    raw_mae = float(np.mean(np.abs(x - y)))
    bias = float(np.mean(deltas))
    median_bias = float(np.median(deltas))
    bias_corrected = x - bias

    return {
        "n_matched": int(len(matches)),
        "n_used_for_calibration": int(len(clean)),
        "n_outliers": int(matches["calibration_outlier"].sum()),
        "mean_fitbit_minus_polar_bpm": bias,
        "median_fitbit_minus_polar_bpm": median_bias,
        "bias_95ci": bootstrap_ci(deltas, np.mean),
        "median_bias_95ci": bootstrap_ci(deltas, np.median),
        "raw_fitbit_mae_bpm": raw_mae,
        "constant_bias_mae_in_sample_bpm": float(np.mean(np.abs(bias_corrected - y))),
        "linear_mae_in_sample_bpm": float(np.mean(np.abs(linear_pred - y))),
        "constant_bias_loo_mae_bpm": loo_mae_constant_bias(x, y),
        "linear_loo_mae_bpm": loo_mae_linear(x, y),
        "linear_intercept": float(beta[0]),
        "linear_slope": float(beta[1]),
        "linear_ci": bootstrap_linear_ci(x, y),
        "recommendation": "use_constant_bias_correction",
        "recommended_formula": f"polar_estimated_avg_hr = fitbit_avg_hr - {bias:.1f}",
    }


def write_report(matches: pd.DataFrame, rejected: pd.DataFrame, summary: dict[str, Any]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    matches.to_csv(OUT_DIR / "fitbit_polar_matches.csv", index=False)
    rejected.to_csv(OUT_DIR / "fitbit_polar_rejected_matches.csv", index=False)
    (OUT_DIR / "fitbit_polar_calibration.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )

    outliers = matches[matches["calibration_outlier"]]
    lines = [
        "# Fitbit vs Polar Calibration",
        "",
        f"Matched runs: {summary['n_matched']}",
        f"Calibration runs after robust outlier removal: {summary['n_used_for_calibration']}",
        f"Rejected Polar runs: {len(rejected)}",
        "",
        "## Match Rules",
        "",
        "- Fitbit Takeout exercise timestamps are interpreted as UTC and converted to America/Los_Angeles.",
        "- Polar session timestamps are interpreted as local clock time.",
        "- A match must be unique within +/- 10 minutes.",
        "- The unique candidate must be within 5 minutes start-time delta, 10 minutes and 25% duration delta, and 0.75 miles and 25% distance delta.",
        "",
        "## Result",
        "",
        f"- Mean Fitbit minus Polar HR: {summary['mean_fitbit_minus_polar_bpm']:.1f} bpm.",
        f"- Median Fitbit minus Polar HR: {summary['median_fitbit_minus_polar_bpm']:.1f} bpm.",
        f"- Bootstrap 95% CI for mean bias: {summary['bias_95ci'][0]:.1f} to {summary['bias_95ci'][1]:.1f} bpm.",
        f"- Raw Fitbit average-HR MAE: {summary['raw_fitbit_mae_bpm']:.1f} bpm.",
        f"- Constant-bias leave-one-out MAE: {summary['constant_bias_loo_mae_bpm']:.1f} bpm.",
        f"- Linear-regression leave-one-out MAE: {summary['linear_loo_mae_bpm']:.1f} bpm.",
        "",
        "## Recommendation",
        "",
        f"Use `{summary['recommended_formula']}` for run average-HR estimates in Fitbit-based analysis. Do not use this to rewrite raw data or precise zone minutes.",
        "",
    ]
    if not outliers.empty:
        lines.extend(
            [
                "## Outliers",
                "",
                "The following match was excluded from calibration by a median absolute deviation rule:",
                "",
            ]
        )
        for row in outliers.itertuples(index=False):
            lines.append(
                f"- {row.polar_start}: Polar avg {row.polar_avg_hr:.0f}, Fitbit avg {row.fitbit_avg_hr:.0f}, delta {row.fitbit_minus_polar_hr:.0f} bpm."
            )
        lines.append("")
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / "fitbit_polar_calibration.md").write_text("\n".join(lines), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Calibrate Fitbit vs Polar exercise heart rate by matching whole runs."
    )
    return parser.parse_args()


def main() -> int:
    parse_args()
    rules = MatchRules()
    fitbit = load_fitbit_runs()
    polar = load_polar_runs()
    matches, rejected = match_runs(fitbit, polar, rules)
    if matches.empty:
        raise RuntimeError("No unambiguous Fitbit/Polar run matches found.")
    matches["calibration_outlier"] = mad_outlier_mask(matches["fitbit_minus_polar_hr"])
    summary = summarize_models(matches)
    write_report(matches, rejected, summary)
    print(json.dumps(summary, indent=2, sort_keys=True, default=str))
    print(f"Wrote {OUT_DIR / 'fitbit_polar_matches.csv'}", file=sys.stderr)
    print(f"Wrote {REPORT_DIR / 'fitbit_polar_calibration.md'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
