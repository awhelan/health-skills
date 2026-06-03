#!/usr/bin/env python3
"""Calibrate Fitbit vs Polar heart rate at the 1-second sample level."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from zoneinfo import ZoneInfo

_REPO_ROOT = Path(__file__).resolve().parents[3]
# Cross-provider calibration uses the Fitbit puller's token refresh + 1sec HR
# fetch; import it from the fitbit skill.
for import_path in (_REPO_ROOT, _REPO_ROOT / "skills" / "fitbit" / "scripts"):
    if str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))

import pull_fitbit as fitbit_pull

from healthdata.config import (  # noqa: E402
    CALIBRATION_REPORT_DIR,
    DEFAULT_LOCAL_TIMEZONE,
    FITBIT_STAGED_DIR,
    FITBIT_TAKEOUT_DIR,
    FITBIT_TOKEN_FILE,
    POLAR_FITBIT_CALIBRATION_DERIVED_DIR,
    POLAR_STAGED_DIR,
)

POLAR_DATA_DIR = POLAR_STAGED_DIR
OUT_DIR = POLAR_FITBIT_CALIBRATION_DERIVED_DIR
CACHE_DIR = FITBIT_STAGED_DIR / "intraday_hr_1sec"
REPORT_DIR = CALIBRATION_REPORT_DIR
LOCAL_TZ = ZoneInfo(DEFAULT_LOCAL_TIMEZONE)
MI_PER_METER = 0.000621371


def local_timestamp(value: Any) -> pd.Timestamp:
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return pd.NaT
    if getattr(parsed, "tzinfo", None) is not None:
        return parsed.tz_convert(LOCAL_TZ).tz_localize(None)
    return parsed


def fitbit_takeout_timestamp(value: Any) -> pd.Timestamp:
    parsed = pd.to_datetime(value, format="%m/%d/%y %H:%M:%S", errors="coerce", utc=True)
    if pd.isna(parsed):
        return pd.NaT
    return parsed.tz_convert(LOCAL_TZ).tz_localize(None)


def load_polar_sessions() -> pd.DataFrame:
    sessions = pd.read_csv(POLAR_DATA_DIR / "training_sessions.csv")
    sessions = sessions[sessions["name"].astype(str).str.lower().str.contains("running", na=False)].copy()
    sessions["exercise_id"] = sessions["exercise_id"].astype(str)
    sessions["polar_start"] = sessions["start_time"].map(local_timestamp)
    sessions["duration_minutes"] = pd.to_numeric(sessions["duration_minutes"], errors="coerce")
    sessions["polar_end"] = sessions["polar_start"] + pd.to_timedelta(sessions["duration_minutes"], unit="m")
    sessions["polar_avg_hr"] = pd.to_numeric(sessions["hr_avg"], errors="coerce")
    return sessions.dropna(subset=["exercise_id", "polar_start", "polar_end", "polar_avg_hr"])


def load_polar_hr_samples() -> pd.DataFrame:
    hr = pd.read_csv(POLAR_DATA_DIR / "hr_samples.csv")
    hr["exercise_id"] = hr["exercise_id"].astype(str)
    hr["polar_ts"] = hr["timestamp"].map(local_timestamp)
    hr["polar_hr"] = pd.to_numeric(hr["heart_rate"], errors="coerce")
    hr = hr.dropna(subset=["exercise_id", "polar_ts", "polar_hr"])
    hr = hr[(hr["polar_hr"] >= 40) & (hr["polar_hr"] <= 220)]
    return hr.sort_values("polar_ts")


def load_fitbit_takeout_day(day: pd.Timestamp) -> pd.DataFrame:
    path = FITBIT_TAKEOUT_DIR / "Global Export Data" / f"heart_rate-{day.date().isoformat()}.json"
    if not path.exists():
        return pd.DataFrame(columns=["fitbit_ts", "fitbit_hr", "fitbit_confidence", "source"])
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = []
    for item in payload:
        value = item.get("value") or {}
        rows.append(
            {
                "fitbit_ts": fitbit_takeout_timestamp(item.get("dateTime")),
                "fitbit_hr": value.get("bpm"),
                "fitbit_confidence": value.get("confidence"),
                "source": "takeout",
            }
        )
    df = pd.DataFrame(rows)
    df["fitbit_hr"] = pd.to_numeric(df["fitbit_hr"], errors="coerce")
    df["fitbit_confidence"] = pd.to_numeric(df["fitbit_confidence"], errors="coerce")
    df = df.dropna(subset=["fitbit_ts", "fitbit_hr"])
    return df[(df["fitbit_hr"] >= 40) & (df["fitbit_hr"] <= 220)]


def fitbit_api_token() -> str | None:
    token_path = FITBIT_TOKEN_FILE
    if not token_path.exists():
        return None
    token = json.loads(token_path.read_text(encoding="utf-8"))
    try:
        secret = fitbit_pull.configured_client_secret()
        refreshed = fitbit_pull.refresh_token(
            token_path,
            fitbit_pull.resolve_client_id(None, token),
            secret,
            "server",
        )
        return str(refreshed["access_token"])
    except Exception:
        if int(token.get("expires_at", 0)) > int(time.time()) + 300:
            return str(token.get("access_token"))
    return None


def fetch_fitbit_api_window(day: pd.Timestamp, start: pd.Timestamp, end: pd.Timestamp, access_token: str) -> pd.DataFrame:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_name = f"{day.date().isoformat()}_{start.strftime('%H%M%S')}_{end.strftime('%H%M%S')}.json"
    cache_path = CACHE_DIR / cache_name
    if cache_path.exists():
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    else:
        query_date = day.date().isoformat()
        path = (
            f"/1/user/-/activities/heart/date/{query_date}/1d/1sec/time/"
            f"{start.strftime('%H:%M')}/{end.strftime('%H:%M')}.json"
        )
        payload = fitbit_pull.api_get(access_token, path)
        cache_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    dataset = (payload.get("activities-heart-intraday") or {}).get("dataset") or []
    rows = []
    for item in dataset:
        rows.append(
            {
                "fitbit_ts": pd.Timestamp(f"{day.date().isoformat()} {item.get('time')}"),
                "fitbit_hr": item.get("value"),
                "fitbit_confidence": np.nan,
                "source": "api_1sec",
            }
        )
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=["fitbit_ts", "fitbit_hr", "fitbit_confidence", "source"])
    df["fitbit_hr"] = pd.to_numeric(df["fitbit_hr"], errors="coerce")
    df = df.dropna(subset=["fitbit_ts", "fitbit_hr"])
    return df[(df["fitbit_hr"] >= 40) & (df["fitbit_hr"] <= 220)]


def load_fitbit_window(start: pd.Timestamp, end: pd.Timestamp, access_token: str | None) -> pd.DataFrame:
    days = pd.date_range(start.date() - pd.Timedelta(days=1), end.date() + pd.Timedelta(days=1), freq="D")
    frames = [frame for day in days if not (frame := load_fitbit_takeout_day(day)).empty]
    fitbit = (
        pd.concat(frames, ignore_index=True)
        if frames
        else pd.DataFrame(columns=["fitbit_ts", "fitbit_hr", "fitbit_confidence", "source"])
    )
    window = fitbit[(fitbit["fitbit_ts"] >= start) & (fitbit["fitbit_ts"] <= end)].copy()

    # If Takeout does not cover this window, use Fitbit's smallest API HR interval.
    if len(window) < max(20, (end - start).total_seconds() / 120) and access_token:
        api_frames = []
        for day in pd.date_range(start.date(), end.date(), freq="D"):
            day_start = max(start, pd.Timestamp(day.date()))
            day_end = min(end, pd.Timestamp(day.date()) + pd.Timedelta(hours=23, minutes=59))
            try:
                api_frames.append(fetch_fitbit_api_window(day, day_start, day_end, access_token))
            except Exception as exc:
                print(f"Warning: Fitbit 1sec API fetch failed for {day.date()}: {exc}", file=sys.stderr)
        if api_frames:
            api = pd.concat(api_frames, ignore_index=True)
            api_window = api[(api["fitbit_ts"] >= start) & (api["fitbit_ts"] <= end)].copy()
            if len(api_window) > len(window):
                window = api_window

    if window.empty:
        return window
    return window.sort_values("fitbit_ts").drop_duplicates(subset=["fitbit_ts"], keep="last")


def pair_with_lag(polar: pd.DataFrame, fitbit: pd.DataFrame, lag_seconds: int, tolerance_seconds: float = 3.0) -> pd.DataFrame:
    p = polar[["polar_ts", "polar_hr"]].copy()
    p["align_ts"] = p["polar_ts"] + pd.to_timedelta(lag_seconds, unit="s")
    p = p.sort_values("align_ts")
    f = fitbit[["fitbit_ts", "fitbit_hr", "fitbit_confidence", "source"]].sort_values("fitbit_ts")
    paired = pd.merge_asof(
        p,
        f,
        left_on="align_ts",
        right_on="fitbit_ts",
        direction="nearest",
        tolerance=pd.Timedelta(seconds=tolerance_seconds),
    ).dropna(subset=["fitbit_hr"])
    if paired.empty:
        return paired
    paired["lag_seconds"] = lag_seconds
    paired["hr_delta"] = paired["fitbit_hr"] - paired["polar_hr"]
    return paired


def best_lag_alignment(polar: pd.DataFrame, fitbit: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    best: pd.DataFrame | None = None
    best_score: tuple[float, float, float] | None = None
    best_meta: dict[str, Any] = {}
    duration_seconds = max(1.0, (polar["polar_ts"].max() - polar["polar_ts"].min()).total_seconds())

    for lag in range(-300, 301, 5):
        paired = pair_with_lag(polar, fitbit, lag)
        if len(paired) < 60:
            continue
        coverage = len(paired) / duration_seconds
        if paired["polar_hr"].std() < 3 or paired["fitbit_hr"].std() < 3:
            corr = 0.0
        else:
            corr = paired["polar_hr"].corr(paired["fitbit_hr"])
            if pd.isna(corr):
                corr = 0.0
        mae = (paired["fitbit_hr"] - paired["polar_hr"]).abs().mean()
        # Prefer stronger curve agreement, then more coverage, then lower error.
        score = (float(corr), float(coverage), -float(mae))
        if best_score is None or score > best_score:
            best = paired
            best_score = score
            best_meta = {"lag_seconds": lag, "correlation": float(corr), "coverage_per_second": float(coverage), "mae": float(mae)}

    if best is None:
        return pd.DataFrame(), {"reason": "no lag produced enough paired samples"}
    return best, best_meta


def grouped_model_scores(samples: pd.DataFrame) -> dict[str, Any]:
    usable = samples.copy()
    exercise_ids = usable["exercise_id"].unique()
    results: dict[str, list[float]] = {
        "raw": [],
        "constant_bias": [],
        "linear_fitbit_hr": [],
        "piecewise_fitbit_hr": [],
        "confidence_bias": [],
    }
    run_results: dict[str, list[float]] = {key: [] for key in results}

    for exercise_id in exercise_ids:
        train = usable[usable["exercise_id"] != exercise_id]
        test = usable[usable["exercise_id"] == exercise_id]
        if train.empty or test.empty:
            continue
        train_run_bias = train.groupby("exercise_id")["hr_delta"].mean()
        bias = float(train_run_bias.mean())

        raw_pred = test["fitbit_hr"].to_numpy(float)
        const_pred = raw_pred - bias

        x_train = train["fitbit_hr"].to_numpy(float)
        y_train = train["polar_hr"].to_numpy(float)
        beta = np.linalg.lstsq(np.c_[np.ones(len(x_train)), x_train], y_train, rcond=None)[0]
        linear_pred = beta[0] + raw_pred * beta[1]

        bins = pd.cut(train["fitbit_hr"], bins=[0, 125, 140, 155, 999], labels=False, include_lowest=True)
        bin_bias = train.assign(bin=bins).groupby("bin")["hr_delta"].mean().to_dict()
        test_bins = pd.cut(test["fitbit_hr"], bins=[0, 125, 140, 155, 999], labels=False, include_lowest=True)
        piecewise_bias = np.array([bin_bias.get(int(b), bias) if not pd.isna(b) else bias for b in test_bins])
        piecewise_pred = raw_pred - piecewise_bias

        # Confidence-stratified bias: learn a mean bias per Fitbit PPG confidence
        # level on train, then correct each test sample by its own confidence.
        # Samples with no confidence (Fitbit API source) fall back to global bias.
        conf_bias_map = (
            train.dropna(subset=["fitbit_confidence"]).groupby("fitbit_confidence")["hr_delta"].mean().to_dict()
        )
        test_conf = test["fitbit_confidence"].to_numpy()
        confidence_bias = np.array(
            [conf_bias_map.get(c, bias) if not pd.isna(c) else bias for c in test_conf]
        )
        confidence_pred = raw_pred - confidence_bias

        preds = {
            "raw": raw_pred,
            "constant_bias": const_pred,
            "linear_fitbit_hr": linear_pred,
            "piecewise_fitbit_hr": piecewise_pred,
            "confidence_bias": confidence_pred,
        }
        y_test = test["polar_hr"].to_numpy(float)
        for key, pred in preds.items():
            abs_err = np.abs(pred - y_test)
            results[key].extend(abs_err.tolist())
            run_results[key].append(float(abs_err.mean()))

    return {
        "sample_mae": {key: float(np.mean(values)) for key, values in results.items()},
        "run_weighted_sample_mae": {key: float(np.mean(values)) for key, values in run_results.items()},
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Calibrate Fitbit vs Polar heart rate at the 1-second sample level."
    )
    return parser.parse_args()


def main() -> int:
    parse_args()
    sessions = load_polar_sessions()
    polar_hr = load_polar_hr_samples()
    access_token = fitbit_api_token()

    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    sample_frames: list[pd.DataFrame] = []

    for session in sessions.sort_values("polar_start").itertuples(index=False):
        exercise_id = str(session.exercise_id)
        polar = polar_hr[polar_hr["exercise_id"] == exercise_id].copy()
        if polar.empty:
            rejected.append({"exercise_id": exercise_id, "polar_start": session.polar_start, "reason": "no Polar HR samples"})
            continue

        window_start = session.polar_start - pd.Timedelta(minutes=8)
        window_end = session.polar_end + pd.Timedelta(minutes=8)
        fitbit = load_fitbit_window(window_start, window_end, access_token)
        if fitbit.empty:
            rejected.append({"exercise_id": exercise_id, "polar_start": session.polar_start, "reason": "no Fitbit HR samples"})
            continue

        paired, meta = best_lag_alignment(polar, fitbit)
        if paired.empty:
            rejected.append({"exercise_id": exercise_id, "polar_start": session.polar_start, **meta})
            continue
        duration_seconds = max(1.0, (polar["polar_ts"].max() - polar["polar_ts"].min()).total_seconds())
        coverage_ratio = len(paired) / duration_seconds
        if abs(float(meta.get("lag_seconds", 0))) >= 295:
            rejected.append(
                {
                    "exercise_id": exercise_id,
                    "polar_start": session.polar_start,
                    "reason": "best lag hit search boundary",
                    **meta,
                    "paired_samples": len(paired),
                    "coverage_ratio": coverage_ratio,
                }
            )
            continue
        if meta.get("correlation", 0) < 0.55 or coverage_ratio < 0.12:
            rejected.append(
                {
                    "exercise_id": exercise_id,
                    "polar_start": session.polar_start,
                    "reason": "low-confidence sample alignment",
                    **meta,
                    "paired_samples": len(paired),
                    "coverage_ratio": coverage_ratio,
                }
            )
            continue

        paired["exercise_id"] = exercise_id
        paired["polar_start"] = session.polar_start
        paired["polar_avg_hr"] = session.polar_avg_hr
        sample_frames.append(paired)
        accepted.append(
            {
                "exercise_id": exercise_id,
                "polar_start": session.polar_start,
                "polar_duration_min": session.duration_minutes,
                "polar_avg_hr": session.polar_avg_hr,
                "paired_samples": len(paired),
                "coverage_ratio": coverage_ratio,
                "fitbit_source": ",".join(sorted(set(paired["source"].astype(str)))),
                "mean_fitbit_hr": paired["fitbit_hr"].mean(),
                "mean_polar_hr": paired["polar_hr"].mean(),
                "mean_delta": paired["hr_delta"].mean(),
                "median_delta": paired["hr_delta"].median(),
                **meta,
            }
        )

    if not sample_frames:
        raise RuntimeError("No accepted sample-level alignments.")

    samples = pd.concat(sample_frames, ignore_index=True)
    accepted_df = pd.DataFrame(accepted).sort_values("polar_start")
    rejected_df = pd.DataFrame(rejected)
    scores = grouped_model_scores(samples)

    run_bias = accepted_df["mean_delta"].to_numpy(float)
    sample_bias = samples["hr_delta"].to_numpy(float)
    conf_diag = (
        samples.dropna(subset=["fitbit_confidence"])
        .assign(abs_delta=lambda d: d["hr_delta"].abs())
        .groupby("fitbit_confidence")
        .agg(
            n_samples=("hr_delta", "size"),
            mean_signed_delta=("hr_delta", "mean"),
            mean_abs_error=("abs_delta", "mean"),
        )
        .reset_index()
        .to_dict(orient="records")
    )
    n_conf = int(samples["fitbit_confidence"].notna().sum())
    summary = {
        "accepted_runs": int(len(accepted_df)),
        "rejected_runs": int(len(rejected_df)),
        "paired_samples": int(len(samples)),
        "mean_run_bias_bpm": float(np.mean(run_bias)),
        "median_run_bias_bpm": float(np.median(run_bias)),
        "mean_sample_bias_bpm": float(np.mean(sample_bias)),
        "median_sample_bias_bpm": float(np.median(sample_bias)),
        "sample_delta_correlation_with_polar_hr": float(samples["hr_delta"].corr(samples["polar_hr"])),
        "sample_delta_correlation_with_fitbit_hr": float(samples["hr_delta"].corr(samples["fitbit_hr"])),
        "samples_with_confidence": n_conf,
        "bias_by_confidence": conf_diag,
        "model_scores": scores,
        "recommendation": "use_sample_level_constant_bias_for_now",
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    samples.to_csv(OUT_DIR / "fitbit_polar_sample_pairs.csv", index=False)
    accepted_df.to_csv(OUT_DIR / "fitbit_polar_sample_alignment_runs.csv", index=False)
    rejected_df.to_csv(OUT_DIR / "fitbit_polar_sample_alignment_rejected.csv", index=False)
    (OUT_DIR / "fitbit_polar_sample_calibration.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )

    lines = [
        "# Fitbit vs Polar Sample-Level Calibration",
        "",
        f"Accepted sample-aligned runs: {summary['accepted_runs']}",
        f"Rejected runs: {summary['rejected_runs']}",
        f"Paired HR samples: {summary['paired_samples']}",
        "",
        "## Method",
        "",
        "- Uses Polar per-sample HR from AccessLink.",
        "- Uses Fitbit Takeout HR samples when available, parsed as UTC and converted to local time.",
        "- Uses Fitbit API `1sec` HR samples when Takeout does not cover the run window.",
        "- Takeout samples carry Fitbit's PPG confidence (0-3); API samples have none.",
        "- Estimates each run's time lag by grid search from -300 to +300 seconds in 5-second steps.",
        "- Rejects alignments where the best lag hits the +/-300 second search boundary.",
        "- Accepts a run only if sample alignment has correlation >= 0.55 and coverage >= 12% of session seconds.",
        "- Model comparison uses leave-one-run-out validation, not random sample splits.",
        "",
        "## Result",
        "",
        f"- Run-weighted mean Fitbit minus Polar sample bias: {summary['mean_run_bias_bpm']:.1f} bpm.",
        f"- Run-weighted median bias: {summary['median_run_bias_bpm']:.1f} bpm.",
        f"- Sample-weighted mean bias: {summary['mean_sample_bias_bpm']:.1f} bpm.",
        f"- Delta correlation with Polar HR: {summary['sample_delta_correlation_with_polar_hr']:.2f}.",
        f"- Delta correlation with Fitbit HR: {summary['sample_delta_correlation_with_fitbit_hr']:.2f}.",
        "",
        "## Leave-One-Run-Out MAE",
        "",
    ]
    for key, value in scores["run_weighted_sample_mae"].items():
        lines.append(f"- {key}: {value:.2f} bpm")
    if conf_diag:
        lines.extend(
            [
                "",
                "## Error By Fitbit PPG Confidence",
                "",
                f"From {n_conf} Takeout samples with a confidence value (API samples have none):",
                "",
                "| confidence | samples | signed bias bpm | mean abs error bpm |",
                "| --- | --- | --- | --- |",
            ]
        )
        for row in conf_diag:
            lines.append(
                f"| {int(row['fitbit_confidence'])} | {int(row['n_samples'])} "
                f"| {row['mean_signed_delta']:.1f} | {row['mean_abs_error']:.1f} |"
            )
    lines.extend(
        [
            "",
            "## Recommendation",
            "",
            "Use the sample-level correction results for future HR calibration decisions. Whole-run average matching is retained only as a fallback check.",
            "",
        ]
    )
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / "fitbit_polar_sample_calibration.md").write_text("\n".join(lines), encoding="utf-8")

    print(json.dumps(summary, indent=2, sort_keys=True))
    print(f"Wrote {REPORT_DIR / 'fitbit_polar_sample_calibration.md'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
