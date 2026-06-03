#!/usr/bin/env python3
"""Normalize saved Polar AccessLink raw JSON into staged CSV tables.

This is the deterministic second phase of the polar-accesslink skill.
``pull_polar_accesslink.py`` saves raw API payloads under
``data/raw/polar_accesslink/run=<start>_<end>/`` as ``training_summary.json``
and ``training_details.json``; this script reads those payloads and merges them
into the staged ``training_sessions``, ``hr_samples``, ``zones``, and ``laps``
tables without calling Polar.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pandas as pd

from healthdata.config import (
    DEFAULT_LOCAL_TIMEZONE,
    POLAR_API_RAW_DIR,
    POLAR_LAST_NORMALIZE_FILE,
    POLAR_STAGED_DIR,
    workspace_path,
)
from healthdata.io import read_json_file, utc_now_iso, write_json_file

LOCAL_TZ = DEFAULT_LOCAL_TIMEZONE
MI_PER_METER = 0.000621371

SESSION_COLUMNS = [
    "date",
    "session_id",
    "exercise_id",
    "name",
    "start_time",
    "stop_time",
    "duration_minutes",
    "distance_miles",
    "pace_min_per_mile",
    "calories",
    "hr_avg",
    "hr_max",
    "training_load",
    "recovery_minutes",
    "running_index",
    "ascent_meters",
    "descent_meters",
    "walking_minutes",
    "walking_miles",
    "sport_id",
    "device_id",
    "product",
    "application",
    "modified",
]
HR_SAMPLE_COLUMNS = ["session_id", "exercise_id", "timestamp", "offset_seconds", "heart_rate"]
ZONE_COLUMNS = [
    "session_id",
    "exercise_id",
    "zone_type",
    "zone_index",
    "lower_limit",
    "higher_limit",
    "in_zone_millis",
    "in_zone_minutes",
    "distance_miles",
]
LAP_COLUMNS = [
    "session_id",
    "exercise_id",
    "lap_type",
    "lap_index",
    "duration_minutes",
    "distance_miles",
    "pace_min_per_mile",
    "ascent_meters",
    "descent_meters",
]


def _float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _millis_to_minutes(value: Any) -> float | None:
    number = _float(value)
    if number is None:
        return None
    return number / 60000


def _meters_to_miles(value: Any) -> float | None:
    number = _float(value)
    if number is None:
        return None
    return number * MI_PER_METER


def _local_date(start_time: Any) -> str | None:
    parsed = pd.to_datetime(start_time, errors="coerce")
    if pd.isna(parsed):
        return None
    if getattr(parsed, "tzinfo", None) is not None:
        parsed = parsed.tz_convert(LOCAL_TZ).tz_localize(None)
    return parsed.date().isoformat()


def _id(value: Any) -> str | None:
    if isinstance(value, dict):
        nested = value.get("id")
        return str(nested) if nested is not None else None
    if value is None:
        return None
    return str(value)


def iter_sessions(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if "trainingSessions" in payload:
        return list(payload.get("trainingSessions", []))
    sessions: list[dict[str, Any]] = []
    for daily_payload in payload.values():
        if isinstance(daily_payload, dict):
            sessions.extend(daily_payload.get("trainingSessions", []))
    return sessions


def detail_exercise_ids(detail_payload: dict[str, Any]) -> set[str]:
    """Exercise IDs included in a feature-rich detail payload."""
    exercise_ids: set[str] = set()
    for session in iter_sessions(detail_payload):
        session_id = _id(session.get("identifier"))
        for exercise in session.get("exercises") or []:
            exercise_id = _id(exercise.get("identifier")) or session_id
            if exercise_id:
                exercise_ids.add(exercise_id)
    return exercise_ids


def normalize_sessions(summary_payload: dict[str, Any], detail_payload: dict[str, Any]) -> pd.DataFrame:
    by_id: dict[str, dict[str, Any]] = {}
    for source in [summary_payload, detail_payload]:
        for session in iter_sessions(source):
            session_id = _id(session.get("identifier"))
            if not session_id:
                continue
            by_id[session_id] = {**by_id.get(session_id, {}), **session}

    rows: list[dict[str, Any]] = []
    for session_id, session in by_id.items():
        exercises = session.get("exercises") or [{}]
        for exercise in exercises:
            exercise_id = _id(exercise.get("identifier")) or session_id
            start_time = exercise.get("startTime") or session.get("startTime")
            distance_m = _float(exercise.get("distanceMeters")) or _float(session.get("distanceMeters"))
            duration_min = _millis_to_minutes(exercise.get("durationMillis")) or _millis_to_minutes(
                session.get("durationMillis")
            )
            distance_mi = _meters_to_miles(distance_m)
            rows.append(
                {
                    "date": _local_date(start_time),
                    "session_id": session_id,
                    "exercise_id": exercise_id,
                    "name": session.get("name"),
                    "start_time": start_time,
                    "stop_time": exercise.get("stopTime") or session.get("stopTime"),
                    "duration_minutes": duration_min,
                    "distance_miles": distance_mi,
                    "pace_min_per_mile": duration_min / distance_mi if duration_min and distance_mi else None,
                    "calories": _float(exercise.get("calories")) or _float(session.get("calories")),
                    "hr_avg": _float(exercise.get("hrAvg")) or _float(session.get("hrAvg")),
                    "hr_max": _float(exercise.get("hrMax")) or _float(session.get("hrMax")),
                    "training_load": _float(exercise.get("trainingLoad")) or _float(session.get("trainingLoad")),
                    "recovery_minutes": _millis_to_minutes(exercise.get("recoveryTimeMillis"))
                    or _millis_to_minutes(session.get("recoveryTimeMillis")),
                    "running_index": _float(exercise.get("runningIndex")),
                    "ascent_meters": _float(exercise.get("ascentMeters")),
                    "descent_meters": _float(exercise.get("descentMeters")),
                    "walking_minutes": _millis_to_minutes(exercise.get("walkingDurationMillis")),
                    "walking_miles": _meters_to_miles(exercise.get("walkingDistanceMeters")),
                    "sport_id": _id(exercise.get("sport")) or _id(session.get("sport")),
                    "device_id": session.get("deviceId"),
                    "product": (session.get("product") or {}).get("modelName"),
                    "application": (session.get("application") or {}).get("name"),
                    "modified": exercise.get("modified") or session.get("modified"),
                }
            )

    df = pd.DataFrame(rows, columns=SESSION_COLUMNS)
    if df.empty:
        return df
    return df.dropna(subset=["date"]).sort_values(["date", "start_time"])


def normalize_hr_samples(detail_payload: dict[str, Any]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for session in iter_sessions(detail_payload):
        session_id = _id(session.get("identifier"))
        for exercise in session.get("exercises") or []:
            exercise_id = _id(exercise.get("identifier")) or session_id
            start_time = exercise.get("startTime") or session.get("startTime")
            base = pd.to_datetime(start_time, errors="coerce")
            if pd.isna(base):
                continue
            if getattr(base, "tzinfo", None) is not None:
                base = base.tz_convert(LOCAL_TZ).tz_localize(None)
            for sample in (exercise.get("samples") or {}).get("samples") or []:
                if str(sample.get("type")).upper() != "HEART_RATE":
                    continue
                interval = int(_float(sample.get("intervalMillis")) or 1000)
                for index, value in enumerate(sample.get("values") or []):
                    offset_ms = index * interval
                    rows.append(
                        {
                            "session_id": session_id,
                            "exercise_id": exercise_id,
                            "timestamp": (base + pd.Timedelta(milliseconds=offset_ms)).isoformat(),
                            "offset_seconds": offset_ms / 1000,
                            "heart_rate": _float(value),
                        }
                    )

    df = pd.DataFrame(rows, columns=HR_SAMPLE_COLUMNS)
    if df.empty:
        return df
    return df.sort_values(["exercise_id", "offset_seconds"])


def normalize_zones(detail_payload: dict[str, Any]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for session in iter_sessions(detail_payload):
        session_id = _id(session.get("identifier"))
        for exercise in session.get("exercises") or []:
            exercise_id = _id(exercise.get("identifier")) or session_id
            for zone_block in exercise.get("zones") or []:
                zone_type = zone_block.get("type")
                for index, zone in enumerate(zone_block.get("zones") or [], start=1):
                    in_zone_millis = _float(zone.get("inZone"))
                    rows.append(
                        {
                            "session_id": session_id,
                            "exercise_id": exercise_id,
                            "zone_type": zone_type,
                            "zone_index": index,
                            "lower_limit": _float(zone.get("lowerLimit")),
                            "higher_limit": _float(zone.get("higherLimit")),
                            "in_zone_millis": in_zone_millis,
                            "in_zone_minutes": (in_zone_millis or 0) / 60000,
                            "distance_miles": _meters_to_miles(zone.get("distanceMeters")),
                        }
                    )

    df = pd.DataFrame(rows, columns=ZONE_COLUMNS)
    if df.empty:
        return df
    return df.sort_values(["exercise_id", "zone_type", "zone_index"])


def normalize_laps(detail_payload: dict[str, Any]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for session in iter_sessions(detail_payload):
        session_id = _id(session.get("identifier"))
        for exercise in session.get("exercises") or []:
            exercise_id = _id(exercise.get("identifier")) or session_id
            laps = exercise.get("laps") or {}
            for kind in ["laps", "autoLaps"]:
                for index, lap in enumerate(laps.get(kind) or [], start=1):
                    duration_min = _millis_to_minutes(lap.get("durationMillis"))
                    distance_mi = _meters_to_miles(lap.get("distanceMeters"))
                    rows.append(
                        {
                            "session_id": session_id,
                            "exercise_id": exercise_id,
                            "lap_type": kind,
                            "lap_index": index,
                            "duration_minutes": duration_min,
                            "distance_miles": distance_mi,
                            "pace_min_per_mile": duration_min / distance_mi
                            if duration_min and distance_mi
                            else None,
                            "ascent_meters": _float(lap.get("ascentMeters")),
                            "descent_meters": _float(lap.get("descentMeters")),
                        }
                    )

    df = pd.DataFrame(rows, columns=LAP_COLUMNS)
    if df.empty:
        return df
    return df.sort_values(["exercise_id", "lap_type", "lap_index"])


def merge_csv(
    path: Path,
    incoming: pd.DataFrame,
    dedupe_keys: list[str],
    *,
    replace_exercise_ids: set[str] | None = None,
) -> pd.DataFrame:
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = []
    if path.exists():
        existing = pd.read_csv(path)
        if replace_exercise_ids and "exercise_id" in existing.columns:
            ids = {str(exercise_id) for exercise_id in replace_exercise_ids}
            existing = existing[~existing["exercise_id"].astype("string").isin(ids)]
        if not existing.empty:
            frames.append(existing)
    if not incoming.empty:
        frames.append(incoming)
    if not frames:
        incoming.to_csv(path, index=False)
        return incoming

    merged = pd.concat(frames, ignore_index=True)
    keys = [key for key in dedupe_keys if key in merged.columns]
    if keys:
        for key in keys:
            merged[key] = merged[key].astype("string")
        merged = merged.drop_duplicates(subset=keys, keep="last")
    sort_keys = [
        key
        for key in ["date", "start_time", "exercise_id", "offset_seconds", "zone_index", "lap_index"]
        if key in merged.columns
    ]
    if sort_keys:
        merged = merged.sort_values(sort_keys)
    merged.to_csv(path, index=False)
    return merged


def run_dirs(raw_dir: Path) -> list[Path]:
    """Return the run directories holding raw payloads under ``raw_dir``.

    Accepts either a single ``run=<start>_<end>`` directory (used by the pull
    phase to normalize just-fetched data) or the raw root, in which case every
    ``run=*`` subdirectory is read for a full rebuild.
    """
    if (raw_dir / "training_summary.json").exists() or (raw_dir / "training_details.json").exists():
        return [raw_dir]
    return sorted(raw_dir.glob("run=*"))


def load_raw_payloads(raw_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Combine saved raw payloads into one summary and one detail payload."""
    summary_sessions: list[dict[str, Any]] = []
    detail: dict[str, Any] = {}
    for run_dir in run_dirs(raw_dir):
        summary_path = run_dir / "training_summary.json"
        detail_path = run_dir / "training_details.json"
        if summary_path.exists():
            summary_sessions.extend(read_json_file(summary_path).get("trainingSessions", []))
        if detail_path.exists():
            for key, value in read_json_file(detail_path).items():
                detail[f"{run_dir.name}:{key}"] = value
    return {"trainingSessions": summary_sessions}, detail


def normalize_raw_dir(
    raw_dir: Path,
    out_dir: Path,
    *,
    manifest_path: Path = POLAR_LAST_NORMALIZE_FILE,
) -> dict[str, Any]:
    """Read saved raw payloads under ``raw_dir`` and merge into staged CSVs."""
    out_dir = Path(out_dir)
    summary_payload, detail_payload = load_raw_payloads(Path(raw_dir))

    sessions = normalize_sessions(summary_payload, detail_payload)
    hr_samples = normalize_hr_samples(detail_payload)
    zones = normalize_zones(detail_payload)
    laps = normalize_laps(detail_payload)
    detailed_exercises = detail_exercise_ids(detail_payload)

    merged_sessions = merge_csv(out_dir / "training_sessions.csv", sessions, ["exercise_id"])
    merged_hr = merge_csv(
        out_dir / "hr_samples.csv",
        hr_samples,
        ["exercise_id", "offset_seconds"],
        replace_exercise_ids=detailed_exercises,
    )
    merged_zones = merge_csv(
        out_dir / "zones.csv",
        zones,
        ["exercise_id", "zone_type", "zone_index"],
        replace_exercise_ids=detailed_exercises,
    )
    merged_laps = merge_csv(
        out_dir / "laps.csv",
        laps,
        ["exercise_id", "lap_type", "lap_index"],
        replace_exercise_ids=detailed_exercises,
    )

    manifest = {
        "normalized_at": utc_now_iso(),
        "raw_dir": workspace_path(Path(raw_dir)),
        "run_dirs": len(run_dirs(Path(raw_dir))),
        "sessions_written": int(len(sessions)),
        "sessions_total": int(len(merged_sessions)),
        "hr_samples_written": int(len(hr_samples)),
        "hr_samples_total": int(len(merged_hr)),
        "zones_written": int(len(zones)),
        "zones_total": int(len(merged_zones)),
        "laps_written": int(len(laps)),
        "laps_total": int(len(merged_laps)),
        "training_sessions_path": workspace_path(out_dir / "training_sessions.csv"),
        "hr_samples_path": workspace_path(out_dir / "hr_samples.csv"),
        "zones_path": workspace_path(out_dir / "zones.csv"),
        "laps_path": workspace_path(out_dir / "laps.csv"),
    }
    if manifest_path is not None:
        write_json_file(manifest_path, manifest)
    return manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Normalize saved Polar AccessLink raw JSON into staged CSV tables."
    )
    parser.add_argument("--raw-dir", type=Path, default=POLAR_API_RAW_DIR)
    parser.add_argument("--staged-dir", type=Path, default=POLAR_STAGED_DIR)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    manifest = normalize_raw_dir(args.raw_dir, args.staged_dir)
    print(f"Wrote {args.staged_dir / 'training_sessions.csv'}")
    print(f"Wrote {args.staged_dir / 'hr_samples.csv'}")
    print(f"Wrote {POLAR_LAST_NORMALIZE_FILE}")
    print(
        f"Sessions: {manifest['sessions_total']} total, "
        f"HR samples: {manifest['hr_samples_total']} total."
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
