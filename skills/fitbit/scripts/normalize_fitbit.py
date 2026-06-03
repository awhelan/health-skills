#!/usr/bin/env python3
"""Normalize Fitbit raw payloads into staged daily/activity CSV tables.

This is the deterministic transform phase of the fitbit skill. ``pull_fitbit.py``
saves the raw Fitbit Web API payloads under
``data/raw/fitbit_api/run=<start>_<end>/`` and hands the in-memory payloads here;
this module turns them into the staged ``daily_metrics``, ``activity_logs``, and
``activity_tcx`` tables, and can re-read the saved raw to rebuild without calling
Fitbit.

Phase 2 will extend ``normalize_and_stage`` to also ingest Google Takeout into
the same staged schema (Takeout supplies richer fields, e.g. HR PPG confidence),
keyed on the same record identity.
"""

from __future__ import annotations

import argparse
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from healthdata.config import (
    DEFAULT_LOCAL_TIMEZONE,
    FITBIT_API_RAW_DIR,
    FITBIT_LAST_NORMALIZE_FILE,
    FITBIT_STAGED_DIR,
    workspace_path,
)
from healthdata.io import read_json_file, utc_now_iso, write_json_file

LOCAL_TZ = DEFAULT_LOCAL_TIMEZONE

# Saved-raw filenames for the daily summary resources and health metrics, used to
# reconstruct the in-memory payload shape when re-normalizing from disk.
ACTIVITY_SUMMARY_RESOURCES = [
    "calories",
    "floors",
    "elevation",
    "minutesSedentary",
    "minutesLightlyActive",
    "minutesFairlyActive",
    "minutesVeryActive",
]
HEALTH_METRIC_NAMES = ["spo2", "breathing_rate", "temp_skin", "temp_core", "cardio_score"]
TAKEOUT_PREFERRED_DAILY_COLUMNS = {
    "daily_steps",
    "daily_calories",
    "lightly_active_minutes",
    "moderately_active_minutes",
    "very_active_minutes",
    "active_zone_minutes",
    "daily_hrv_ms",
    "minutes_asleep",
    "minutes_in_bed",
    "sleep_score",
    "stress_score",
    "resting_heart_rate",
}
EXERCISE_DAILY_COLUMNS = [
    "exercise_count",
    "exercise_minutes",
    "exercise_calories",
    "exercise_steps",
    "exercise_distance",
    "exercise_active_zone_minutes",
]


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def date_range(start: date, end: date) -> list[date]:
    days = (end - start).days
    if days < 0:
        return []
    return [start + timedelta(days=offset) for offset in range(days + 1)]


def _float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    number = _float(value)
    if number is None:
        return None
    return int(round(number))


def _item_day(item: dict[str, Any]) -> str | None:
    value = item.get("dateTime") or item.get("date")
    if not value:
        return None
    text = str(value)
    if re.match(r"^\d{4}-\d{2}-\d{2}$", text):
        return text
    parsed = pd.to_datetime(text, errors="coerce")
    if pd.isna(parsed):
        return None
    return str(parsed.date())


def _value_dict(item: dict[str, Any]) -> dict[str, Any]:
    value = item.get("value")
    return value if isinstance(value, dict) else {}


def _parse_vo2(value: Any) -> tuple[float | None, float | None, float | None]:
    if value is None:
        return None, None, None
    numbers = [float(part) for part in re.findall(r"\d+(?:\.\d+)?", str(value))]
    if not numbers:
        return None, None, None
    low = min(numbers)
    high = max(numbers)
    midpoint = sum(numbers) / len(numbers)
    return low, high, midpoint


def _activity_date(activity: dict[str, Any]) -> date | None:
    value = activity.get("startTime") or activity.get("originalStartTime")
    if not value:
        return None
    parsed = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.tz_convert(LOCAL_TZ).date()


def _iter_activities(raw: dict[str, Any]) -> list[dict[str, Any]]:
    activities: list[dict[str, Any]] = []
    for page in raw.get("pages", []):
        activities.extend(page.get("activities", []))
    return activities


def _missing(value: Any) -> bool:
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _source_parts(value: Any, default: str) -> set[str]:
    if _missing(value) or str(value) == "":
        return {default} if default else set()
    return {part for part in str(value).split("+") if part}


def _add_activity_summary(records: dict[str, dict[str, Any]], activity_summary: dict[str, Any]) -> None:
    resource_columns = {
        "calories": "daily_calories",
        "floors": "floors",
        "elevation": "elevation",
        "minutesSedentary": "sedentary_minutes",
        "minutesLightlyActive": "lightly_active_minutes",
        "minutesFairlyActive": "moderately_active_minutes",
        "minutesVeryActive": "very_active_minutes",
    }
    for resource, column in resource_columns.items():
        payload = activity_summary.get(resource, {})
        for item in payload.get(f"activities-{resource}", []):
            day = item.get("dateTime")
            if day in records:
                records[day][column] = _float(item.get("value"))


def _add_health_metrics(records: dict[str, dict[str, Any]], health_metrics: dict[str, Any]) -> None:
    for item in health_metrics.get("spo2", {}).get("spo2", []):
        day = _item_day(item)
        if day in records:
            value = _value_dict(item)
            records[day]["spo2_avg_percent"] = _float(value.get("avg"))
            records[day]["spo2_min_percent"] = _float(value.get("min"))
            records[day]["spo2_max_percent"] = _float(value.get("max"))

    for item in health_metrics.get("breathing_rate", {}).get("br", []):
        day = _item_day(item)
        if day in records:
            records[day]["breathing_rate"] = _float(_value_dict(item).get("breathingRate"))

    for item in health_metrics.get("temp_skin", {}).get("tempSkin", []):
        day = _item_day(item)
        if day in records:
            records[day]["skin_temp_nightly_relative_c"] = _float(
                _value_dict(item).get("nightlyRelative")
            )

    core_by_day: dict[str, list[float]] = {}
    for item in health_metrics.get("temp_core", {}).get("tempCore", []):
        day = _item_day(item)
        value = _float(item.get("value"))
        if day and value is not None:
            core_by_day.setdefault(day, []).append(value)
    for day, values in core_by_day.items():
        if day in records:
            records[day]["core_temp_c"] = sum(values) / len(values)

    for item in health_metrics.get("cardio_score", {}).get("cardioScore", []):
        day = _item_day(item)
        if day in records:
            raw_vo2 = _value_dict(item).get("vo2Max")
            low, high, midpoint = _parse_vo2(raw_vo2)
            records[day]["vo2_max_display"] = raw_vo2
            records[day]["vo2_max_low"] = low
            records[day]["vo2_max_high"] = high
            records[day]["vo2_max_midpoint"] = midpoint


def normalize_daily(
    start: date,
    end: date,
    steps: dict[str, Any],
    distance: dict[str, Any],
    azm: dict[str, Any],
    heart: dict[str, Any],
    hrv: dict[str, Any],
    sleep: dict[str, Any],
    exercise_daily: pd.DataFrame,
    activity_summary: dict[str, Any] | None = None,
    health_metrics: dict[str, Any] | None = None,
    activity_logs_present: bool = True,
) -> pd.DataFrame:
    records: dict[str, dict[str, Any]] = {str(day): {"date": str(day)} for day in date_range(start, end)}

    if activity_logs_present:
        for record in records.values():
            record.update({column: 0 for column in EXERCISE_DAILY_COLUMNS})

    for item in steps.get("activities-steps", []):
        day = item.get("dateTime")
        if day in records:
            records[day]["daily_steps"] = _int(item.get("value"))

    for item in distance.get("activities-distance", []):
        day = item.get("dateTime")
        if day in records:
            records[day]["daily_miles"] = _float(item.get("value"))

    if activity_summary:
        _add_activity_summary(records, activity_summary)

    for item in azm.get("activities-active-zone-minutes", []):
        day = item.get("dateTime")
        value = item.get("value", {})
        if day in records:
            records[day]["active_zone_minutes"] = _float(value.get("activeZoneMinutes"))
            records[day]["fat_burn_active_zone_minutes"] = _float(value.get("fatBurnActiveZoneMinutes"))
            records[day]["cardio_active_zone_minutes"] = _float(value.get("cardioActiveZoneMinutes"))
            records[day]["peak_active_zone_minutes"] = _float(value.get("peakActiveZoneMinutes"))

    for item in heart.get("activities-heart", []):
        day = item.get("dateTime")
        value = item.get("value", {})
        if day not in records:
            continue
        records[day]["resting_heart_rate"] = _float(value.get("restingHeartRate"))
        for zone in value.get("heartRateZones", []):
            name = str(zone.get("name", "")).lower().replace(" ", "_")
            if name:
                records[day][f"heart_zone_{name}_minutes"] = _float(zone.get("minutes"))

    for item in hrv.get("hrv", []):
        day = item.get("dateTime")
        value = item.get("value", {})
        if day in records:
            records[day]["daily_hrv_ms"] = _float(value.get("dailyRmssd"))
            records[day]["deep_hrv_ms"] = _float(value.get("deepRmssd"))

    for day, payload in sleep.items():
        if day not in records:
            continue
        summary = payload.get("summary", {})
        records[day]["minutes_asleep"] = _float(summary.get("totalMinutesAsleep"))
        records[day]["minutes_in_bed"] = _float(summary.get("totalTimeInBed"))
        records[day]["sleep_records"] = _int(summary.get("totalSleepRecords"))
        main_sleep = next(
            (entry for entry in payload.get("sleep", []) if entry.get("isMainSleep")),
            None,
        )
        if main_sleep:
            records[day]["sleep_efficiency"] = _float(main_sleep.get("efficiency"))
            levels_summary = (main_sleep.get("levels") or {}).get("summary") or {}
            for stage in ["deep", "light", "rem", "wake"]:
                stage_summary = levels_summary.get(stage) or {}
                records[day][f"sleep_{stage}_minutes"] = _float(stage_summary.get("minutes"))
                records[day][f"sleep_{stage}_count"] = _int(stage_summary.get("count"))

    if health_metrics:
        _add_health_metrics(records, health_metrics)

    if not exercise_daily.empty:
        for row in exercise_daily.itertuples(index=False):
            day = str(row.date)
            if day in records:
                records[day]["exercise_count"] = row.exercise_count
                records[day]["exercise_minutes"] = row.exercise_minutes
                records[day]["exercise_calories"] = row.exercise_calories
                records[day]["exercise_steps"] = row.exercise_steps
                records[day]["exercise_distance"] = row.exercise_distance
                records[day]["exercise_active_zone_minutes"] = row.exercise_active_zone_minutes

    df = pd.DataFrame(records.values())
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date.astype(str)
    return df.sort_values("date")


def normalize_activity_logs(raw: dict[str, Any], start: date, end: date) -> tuple[pd.DataFrame, pd.DataFrame]:
    rows: list[dict[str, Any]] = []
    for activity in _iter_activities(raw):
        day = _activity_date(activity)
        if day is None or day < start or day > end:
            continue
        azm = activity.get("activeZoneMinutes") or {}
        zones = {
            str(zone.get("name", "")).lower().replace(" ", "_"): _float(zone.get("minutes"))
            for zone in activity.get("heartRateZones", [])
        }
        rows.append(
            {
                "date": str(day),
                "log_id": activity.get("logId"),
                "activity_name": activity.get("activityName"),
                "log_type": activity.get("logType"),
                "start_time": activity.get("startTime"),
                "duration_minutes": _float(activity.get("duration")) / 60000
                if _float(activity.get("duration")) is not None
                else None,
                "calories": _float(activity.get("calories")),
                "steps": _float(activity.get("steps")),
                "distance": _float(activity.get("distance")),
                "distance_unit": activity.get("distanceUnit"),
                "average_heart_rate": _float(activity.get("averageHeartRate")),
                "heart_zone_out_of_range_minutes": zones.get("out_of_range"),
                "heart_zone_fat_burn_minutes": zones.get("fat_burn"),
                "heart_zone_cardio_minutes": zones.get("cardio"),
                "heart_zone_peak_minutes": zones.get("peak"),
                "active_zone_minutes": _float(azm.get("totalMinutes")),
            }
        )

    log_columns = [
        "date",
        "log_id",
        "activity_name",
        "log_type",
        "start_time",
        "duration_minutes",
        "calories",
        "steps",
        "distance",
        "distance_unit",
        "average_heart_rate",
        "heart_zone_out_of_range_minutes",
        "heart_zone_fat_burn_minutes",
        "heart_zone_cardio_minutes",
        "heart_zone_peak_minutes",
        "active_zone_minutes",
    ]
    logs = pd.DataFrame(rows, columns=log_columns)
    if logs.empty:
        daily = pd.DataFrame(
            columns=[
                "date",
                "exercise_count",
                "exercise_minutes",
                "exercise_calories",
                "exercise_steps",
                "exercise_distance",
                "exercise_active_zone_minutes",
            ]
        )
        return logs, daily

    numeric = [
        "duration_minutes",
        "calories",
        "steps",
        "distance",
        "average_heart_rate",
        "heart_zone_out_of_range_minutes",
        "heart_zone_fat_burn_minutes",
        "heart_zone_cardio_minutes",
        "heart_zone_peak_minutes",
        "active_zone_minutes",
    ]
    logs[numeric] = logs[numeric].apply(pd.to_numeric, errors="coerce")
    daily = (
        logs.groupby("date", as_index=False)
        .agg(
            exercise_count=("log_id", "count"),
            exercise_minutes=("duration_minutes", "sum"),
            exercise_calories=("calories", "sum"),
            exercise_steps=("steps", "sum"),
            exercise_distance=("distance", "sum"),
            exercise_active_zone_minutes=("active_zone_minutes", "sum"),
        )
        .sort_values("date")
    )
    return logs.sort_values(["date", "start_time"]), daily


def normalize_tcx_manifest(manifest: dict[str, Any]) -> pd.DataFrame:
    columns = [
        "date",
        "log_id",
        "activity_name",
        "start_time",
        "tcx_path",
        "source_url",
        "status",
        "status_code",
        "error",
    ]
    rows = manifest.get("files", []) + manifest.get("errors", [])
    return pd.DataFrame(rows, columns=columns)


def merge_csv(path: Path, incoming: pd.DataFrame, dedupe_key: str) -> pd.DataFrame:
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = []
    if path.exists():
        frames.append(pd.read_csv(path))
    if not incoming.empty:
        frames.append(incoming)
    if not frames:
        incoming.to_csv(path, index=False)
        return incoming

    merged = pd.concat(frames, ignore_index=True)
    if dedupe_key in merged.columns:
        sort_key = f"__{dedupe_key}_sort_key"
        merged[sort_key] = merged[dedupe_key].astype("string")
        merged = merged.drop_duplicates(subset=[sort_key], keep="last")
        merged = merged.sort_values(sort_key).drop(columns=[sort_key])
    else:
        merged = merged.sort_index()
    merged.to_csv(path, index=False)
    return merged


def merge_daily_csv(path: Path, incoming: pd.DataFrame) -> pd.DataFrame:
    """Merge API daily rows without dropping Takeout-only columns or provenance."""
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = pd.read_csv(path) if path.exists() else pd.DataFrame(columns=["date"])
    incoming = incoming.copy()
    if not incoming.empty and "source" not in incoming.columns:
        incoming["source"] = "api"

    if existing.empty and incoming.empty:
        incoming.to_csv(path, index=False)
        return incoming
    if existing.empty:
        merged = incoming.sort_values("date")
        merged.to_csv(path, index=False)
        return merged
    if incoming.empty:
        existing.to_csv(path, index=False)
        return existing

    existing["date"] = existing["date"].astype("string")
    incoming["date"] = incoming["date"].astype("string")
    existing = existing.drop_duplicates(subset=["date"], keep="last").set_index("date")
    incoming = incoming.drop_duplicates(subset=["date"], keep="last").set_index("date")
    dates = sorted(set(existing.index.astype(str)) | set(incoming.index.astype(str)))

    data_columns: list[str] = []
    for frame in [existing, incoming]:
        for column in frame.columns:
            if column != "source" and column not in data_columns:
                data_columns.append(column)

    rows: list[dict[str, Any]] = []
    for day in dates:
        has_existing = day in existing.index
        has_incoming = day in incoming.index
        existing_row = existing.loc[day] if has_existing else pd.Series(dtype=object)
        incoming_row = incoming.loc[day] if has_incoming else pd.Series(dtype=object)
        existing_sources = _source_parts(existing_row.get("source"), "api" if has_existing else "")
        incoming_sources = _source_parts(incoming_row.get("source"), "api" if has_incoming else "")
        sources = existing_sources | incoming_sources

        row: dict[str, Any] = {"date": day}
        for column in data_columns:
            existing_value = existing_row.get(column) if has_existing and column in existing_row.index else None
            incoming_value = incoming_row.get(column) if has_incoming and column in incoming_row.index else None
            keep_takeout_value = (
                "takeout" in existing_sources
                and column in TAKEOUT_PREFERRED_DAILY_COLUMNS
                and not _missing(existing_value)
            )
            if has_incoming and not keep_takeout_value:
                row[column] = incoming_value if not _missing(incoming_value) else existing_value
            else:
                row[column] = existing_value
        row["source"] = "+".join(sorted(sources)) if sources else ""
        rows.append(row)

    columns = ["date", *data_columns, "source"]
    merged = pd.DataFrame(rows, columns=columns).sort_values("date")
    merged.to_csv(path, index=False)
    return merged


def merge_window_csv(
    path: Path,
    incoming: pd.DataFrame,
    dedupe_key: str,
    replace_dates: set[str],
) -> pd.DataFrame:
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = []
    if path.exists():
        existing = pd.read_csv(path)
        if "date" in existing.columns:
            existing = existing[~existing["date"].astype("string").isin(replace_dates)]
        if not existing.empty:
            frames.append(existing)
    if not incoming.empty:
        frames.append(incoming)
    if not frames:
        incoming.to_csv(path, index=False)
        return incoming

    merged = pd.concat(frames, ignore_index=True)
    if dedupe_key in merged.columns:
        sort_key = f"__{dedupe_key}_sort_key"
        merged[sort_key] = merged[dedupe_key].astype("string")
        merged = merged.drop_duplicates(subset=[sort_key], keep="last")
        sort_columns = [
            column for column in ["date", "start_time", sort_key] if column in merged.columns
        ]
        merged = merged.sort_values(sort_columns).drop(columns=[sort_key])
    else:
        merged = merged.sort_index()
    merged.to_csv(path, index=False)
    return merged


def normalize_and_stage(
    staged_dir: str | Path,
    start: date,
    end: date,
    payloads: dict[str, Any],
    *,
    manifest_path: Path | None = FITBIT_LAST_NORMALIZE_FILE,
) -> dict[str, Any]:
    """Normalize one window of Fitbit Web API payloads into the staged tables."""
    out_dir = Path(staged_dir)
    activity_logs_present = "activity_logs" in payloads
    logs, exercise_daily = normalize_activity_logs(payloads.get("activity_logs", {}), start, end)
    daily = normalize_daily(
        start,
        end,
        payloads.get("steps", {}),
        payloads.get("distance", {}),
        payloads.get("azm", {}),
        payloads.get("heart", {}),
        payloads.get("hrv", {}),
        payloads.get("sleep", {}),
        exercise_daily,
        activity_summary=payloads.get("activity_summary"),
        health_metrics=payloads.get("health_metrics"),
        activity_logs_present=activity_logs_present,
    )
    tcx_manifest = payloads.get("tcx_manifest", {})
    tcx_rows = normalize_tcx_manifest(tcx_manifest)
    replace_dates = {str(day) for day in date_range(start, end)}

    merged_daily = merge_daily_csv(out_dir / "daily_metrics.csv", daily)
    merged_logs = (
        merge_window_csv(out_dir / "activity_logs.csv", logs, "log_id", replace_dates)
        if activity_logs_present
        else merge_csv(out_dir / "activity_logs.csv", logs, "log_id")
    )
    merged_tcx = (
        merge_window_csv(out_dir / "activity_tcx.csv", tcx_rows, "log_id", replace_dates)
        if "tcx_manifest" in payloads and not tcx_manifest.get("skipped")
        else merge_csv(out_dir / "activity_tcx.csv", tcx_rows, "log_id")
    )

    manifest = {
        "normalized_at": utc_now_iso(),
        "start_date": str(start),
        "end_date": str(end),
        "daily_rows_written": int(len(daily)),
        "daily_rows_total": int(len(merged_daily)),
        "activity_logs_written": int(len(logs)),
        "activity_logs_total": int(len(merged_logs)),
        "activity_tcx_written": int(len(tcx_rows)),
        "activity_tcx_total": int(len(merged_tcx)),
        "daily_metrics_path": workspace_path(out_dir / "daily_metrics.csv"),
        "activity_logs_path": workspace_path(out_dir / "activity_logs.csv"),
        "activity_tcx_path": workspace_path(out_dir / "activity_tcx.csv"),
    }
    if manifest_path is not None:
        write_json_file(manifest_path, manifest)
    return manifest


def run_dirs(raw_dir: Path) -> list[Path]:
    """Return run directories under ``raw_dir`` (or ``raw_dir`` itself if it is one)."""
    if raw_dir.name.startswith("run=") or (raw_dir / "steps.json").exists():
        return [raw_dir]
    return sorted(raw_dir.glob("run=*"))


def _run_window(run_dir: Path) -> tuple[date, date]:
    name = run_dir.name
    if name.startswith("run="):
        start_text, _, end_text = name[len("run=") :].partition("_")
        return parse_date(start_text), parse_date(end_text)
    raise ValueError(f"Cannot infer date window from raw run directory name: {run_dir.name!r}")


def load_run_payloads(run_dir: Path) -> dict[str, Any]:
    """Reconstruct the in-memory payload shape from one saved raw run directory."""

    def maybe(name: str) -> dict[str, Any]:
        path = run_dir / f"{name}.json"
        return read_json_file(path) if path.exists() else {}

    payloads = {
        "steps": maybe("steps"),
        "distance": maybe("distance"),
        "activity_summary": {
            resource: maybe(resource)
            for resource in ACTIVITY_SUMMARY_RESOURCES
            if (run_dir / f"{resource}.json").exists()
        },
        "azm": maybe("active_zone_minutes"),
        "heart": maybe("heart"),
        "hrv": maybe("hrv"),
        "sleep": maybe("sleep"),
        "health_metrics": {
            name: maybe(name) for name in HEALTH_METRIC_NAMES if (run_dir / f"{name}.json").exists()
        },
    }
    if (run_dir / "activity_logs.json").exists():
        payloads["activity_logs"] = maybe("activity_logs")
    if (run_dir / "tcx_manifest.json").exists():
        payloads["tcx_manifest"] = maybe("tcx_manifest")
    return payloads


def normalize_raw_dir(
    raw_dir: str | Path,
    staged_dir: str | Path,
    *,
    manifest_path: Path | None = FITBIT_LAST_NORMALIZE_FILE,
) -> dict[str, Any]:
    """Re-normalize every saved raw run under ``raw_dir`` into the staged tables."""
    raw_root = Path(raw_dir)
    out_dir = Path(staged_dir)
    dirs = run_dirs(raw_root)
    last: dict[str, Any] = {}
    for run_dir in dirs:
        start, end = _run_window(run_dir)
        last = normalize_and_stage(
            out_dir,
            start,
            end,
            load_run_payloads(run_dir),
            manifest_path=manifest_path,
        )
    last["run_dirs"] = len(dirs)
    if manifest_path is not None and dirs:
        write_json_file(manifest_path, last)
    return last


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Normalize saved Fitbit Web API raw JSON into staged CSV tables."
    )
    parser.add_argument("--raw-dir", type=Path, default=FITBIT_API_RAW_DIR)
    parser.add_argument("--staged-dir", type=Path, default=FITBIT_STAGED_DIR)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    manifest = normalize_raw_dir(args.raw_dir, args.staged_dir)
    print(f"Wrote {args.staged_dir / 'daily_metrics.csv'}")
    print(f"Wrote {args.staged_dir / 'activity_logs.csv'}")
    print(f"Wrote {FITBIT_LAST_NORMALIZE_FILE}")
    print(f"Daily rows: {manifest.get('daily_rows_total', 0)} total.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
