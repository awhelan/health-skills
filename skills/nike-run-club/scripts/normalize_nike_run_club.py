#!/usr/bin/env python3
"""Normalize Nike Run Club raw API pages into a staged wearable CSV.

Nike Run Club's API emits one JSON shape, so this normalizer only reads the
JSON pages written by ``pull_nike_run_club.py``. It tolerates snake_case and
camelCase field names but does not parse other file formats.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from healthdata.config import (  # noqa: E402
    DEFAULT_LOCAL_TIMEZONE,
    NIKE_LAST_NORMALIZE_FILE,
    NIKE_RUN_CLUB_RAW_ROOT,
    NIKE_RUN_CLUB_STAGED_DIR,
    NIKE_RUN_CLUB_TIMEZONE_ENV,
    workspace_path,
)
from healthdata.io import utc_now_iso, write_json_file  # noqa: E402
from healthdata.timeutil import resolve_timezone, validate_date_window  # noqa: E402


ACTIVITY_COLUMNS = [
    "date",
    "activity_id",
    "source_file",
    "source_format",
    "activity_type",
    "start_time",
    "end_time",
    "duration_minutes",
    "distance_miles",
    "nike_distance_value",
    "nike_distance_unit_guess",
    "calories",
    "steps",
    "pace_mean",
    "speed_mean",
    "cadence_mean",
    "heart_rate_mean",
    "heart_rate_max",
    "location",
    "start_location",
    "weather",
    "terrain",
    "title",
    "temperature",
    "notes",
]

DEFAULT_LOCAL_TZ = ZoneInfo(DEFAULT_LOCAL_TIMEZONE)
MILES_PER_KILOMETER = 0.621371

# Nike Run Club activities (see pull_nike_run_club.py output) use epoch-millisecond
# integers and snake_case keys; keep one camelCase fallback per field but do not
# chase field names Nike never sends.
START_FIELDS = ["start_epoch_ms", "startTime"]
END_FIELDS = ["end_epoch_ms", "endTime"]
ACTIVE_DURATION_MS_FIELDS = ["active_duration_ms", "activeDurationMs"]
ACTIVITY_ID_FIELDS = ["id", "activity_id"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Normalize Nike Run Club source files into data/staged/nike_run_club."
    )
    parser.add_argument("--raw-dir", type=Path, default=NIKE_RUN_CLUB_RAW_ROOT)
    parser.add_argument("--staged-dir", type=Path, default=NIKE_RUN_CLUB_STAGED_DIR)
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument(
        "--timezone",
        default=os.environ.get(NIKE_RUN_CLUB_TIMEZONE_ENV, DEFAULT_LOCAL_TIMEZONE),
        help=f"IANA timezone for local activity dates and naive timestamps. Default: {NIKE_RUN_CLUB_TIMEZONE_ENV} or {DEFAULT_LOCAL_TIMEZONE}.",
    )
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        help="Write an empty manifest instead of failing when no NRC files are present.",
    )
    args = parser.parse_args()
    try:
        args.start_date, args.end_date = validate_date_window(args.start_date, args.end_date)
    except ValueError as exc:
        parser.error(str(exc))
    return args


def parse_datetime(value: Any, local_tz: ZoneInfo = DEFAULT_LOCAL_TZ) -> dt.datetime | None:
    if value in {None, ""}:
        return None
    if isinstance(value, (int, float)):
        # Treat large numeric timestamps as milliseconds since epoch.
        seconds = float(value) / 1000 if float(value) > 10_000_000_000 else float(value)
        try:
            return dt.datetime.fromtimestamp(seconds, tz=dt.timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None

    text = str(value).strip()
    if not text:
        return None
    try:
        parsed_date = dt.date.fromisoformat(text)
        return dt.datetime.combine(parsed_date, dt.time(), tzinfo=local_tz)
    except ValueError:
        pass
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=local_tz)
        return parsed
    except ValueError:
        pass
    return None


def parse_float(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def first_value(mapping: dict[str, Any], names: Iterable[str]) -> Any:
    lower = {key.lower(): key for key in mapping}
    for name in names:
        key = lower.get(name.lower())
        if key is not None:
            return mapping[key]
    return None


def summary_value(mapping: dict[str, Any], metric: str, summary: str = "total") -> float | None:
    summaries = mapping.get("summaries")
    if not isinstance(summaries, list):
        return None
    for item in summaries:
        if not isinstance(item, dict):
            continue
        if str(item.get("metric", "")).lower() == metric.lower() and str(item.get("summary", "")).lower() == summary.lower():
            return parse_float(item.get("value"))
    return None


def tag_value(mapping: dict[str, Any], name: str) -> str:
    tags = mapping.get("tags")
    if not isinstance(tags, dict):
        return ""
    value = tags.get(name)
    return "" if value is None else str(value)


def coalesce(*values: float | None) -> float | None:
    """Return the first value that is not None (0.0 is a valid metric value)."""
    for value in values:
        if value is not None:
            return value
    return None


def distance_to_miles(mapping: dict[str, Any]) -> tuple[float | None, float | None, str]:
    """Nike reports the distance total summary in kilometers; convert to miles."""
    km = summary_value(mapping, "distance", "total")
    if km is None:
        return None, None, ""
    return km * MILES_PER_KILOMETER, km, "nrc_summary_kilometers"


def normalize_mapping(
    mapping: dict[str, Any],
    source_file: Path,
    source_format: str,
    local_tz: ZoneInfo = DEFAULT_LOCAL_TZ,
) -> dict[str, str] | None:
    start = parse_datetime(
        first_value(
            mapping,
            START_FIELDS,
        ),
        local_tz,
    )
    end = parse_datetime(
        first_value(mapping, END_FIELDS),
        local_tz,
    )
    active_duration_ms = parse_float(first_value(mapping, ACTIVE_DURATION_MS_FIELDS))
    if active_duration_ms is not None:
        duration_seconds = active_duration_ms / 1000
    elif start and end:
        duration_seconds = max(0.0, (end - start).total_seconds())
    else:
        duration_seconds = None

    distance_miles, nike_distance_value, nike_distance_unit_guess = distance_to_miles(mapping)
    if not start and distance_miles is None and duration_seconds is None:
        return None

    activity_id = first_value(mapping, ACTIVITY_ID_FIELDS) or ""
    activity_type = first_value(mapping, ["type", "activity_type"]) or "Run"
    calories = summary_value(mapping, "calories", "total")
    steps = summary_value(mapping, "steps", "total")
    pace_mean = summary_value(mapping, "pace", "mean")
    speed_mean = summary_value(mapping, "speed", "mean")
    cadence_mean = summary_value(mapping, "cadence", "mean")
    heart_rate_mean = coalesce(summary_value(mapping, "heart_rate", "mean"), summary_value(mapping, "heartrate", "mean"))
    heart_rate_max = coalesce(summary_value(mapping, "heart_rate", "max"), summary_value(mapping, "heartrate", "max"))

    start_local = start.astimezone(local_tz) if start else None
    end_local = end.astimezone(local_tz) if end else None

    date_value = start_local.date().isoformat() if start_local else ""
    return {
        "date": date_value,
        "activity_id": str(activity_id),
        "source_file": str(source_file),
        "source_format": source_format,
        "activity_type": str(activity_type),
        "start_time": start_local.isoformat() if start_local else "",
        "end_time": end_local.isoformat() if end_local else "",
        "duration_minutes": f"{duration_seconds / 60:.6f}" if duration_seconds is not None else "",
        "distance_miles": f"{distance_miles:.6f}" if distance_miles is not None else "",
        "nike_distance_value": f"{nike_distance_value:.6f}" if nike_distance_value is not None else "",
        "nike_distance_unit_guess": nike_distance_unit_guess,
        "calories": f"{calories:.6f}" if calories is not None else "",
        "steps": f"{steps:.6f}" if steps is not None else "",
        "pace_mean": f"{pace_mean:.6f}" if pace_mean is not None else "",
        "speed_mean": f"{speed_mean:.6f}" if speed_mean is not None else "",
        "cadence_mean": f"{cadence_mean:.6f}" if cadence_mean is not None else "",
        "heart_rate_mean": f"{heart_rate_mean:.6f}" if heart_rate_mean is not None else "",
        "heart_rate_max": f"{heart_rate_max:.6f}" if heart_rate_max is not None else "",
        "location": tag_value(mapping, "location"),
        "start_location": tag_value(mapping, "com.nike.running.startlocation"),
        "weather": tag_value(mapping, "com.nike.weather") or tag_value(mapping, "weather"),
        "terrain": tag_value(mapping, "terrain"),
        "title": tag_value(mapping, "com.nike.name"),
        "temperature": tag_value(mapping, "com.nike.temperature") or tag_value(mapping, "temperature"),
        "notes": "",
    }


def looks_like_activity(mapping: dict[str, Any]) -> bool:
    if first_value(mapping, ACTIVITY_ID_FIELDS) is None:
        return False
    return any(
        value is not None
        for value in (
            first_value(mapping, START_FIELDS),
            first_value(mapping, END_FIELDS),
            first_value(mapping, ACTIVE_DURATION_MS_FIELDS),
            summary_value(mapping, "distance", "total"),
        )
    )


def activity_mappings_from_payload(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        activities = value.get("activities")
        if isinstance(activities, list):
            return [item for item in activities if isinstance(item, dict) and looks_like_activity(item)]
        return [value] if looks_like_activity(value) else []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict) and looks_like_activity(item)]
    return []


def normalize_json_file(path: Path, local_tz: ZoneInfo = DEFAULT_LOCAL_TZ) -> list[dict[str, str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows: list[dict[str, str]] = []
    for mapping in activity_mappings_from_payload(payload):
        row = normalize_mapping(mapping, Path(workspace_path(path)), "json", local_tz)
        if row:
            rows.append(row)
    return rows


def source_files(raw_dir: Path) -> list[Path]:
    if not raw_dir.exists():
        return []
    candidates = sorted(raw_dir.glob("result*.json")) if raw_dir.name == "api_pages" else []
    if not candidates:
        candidates = sorted(raw_dir.glob("api_pages/result*.json"))
    if not candidates:
        candidates = sorted(raw_dir.glob("export=*/api_pages/result*.json"))
    if not candidates:
        candidates = sorted(raw_dir.glob("*.json"))
    return [path for path in candidates if path.is_file()]


def filter_rows(rows: list[dict[str, str]], start_date: str | None, end_date: str | None) -> list[dict[str, str]]:
    if not start_date and not end_date:
        return rows
    filtered: list[dict[str, str]] = []
    for row in rows:
        row_date = row.get("date", "")
        if not row_date:
            continue
        if start_date and row_date < start_date:
            continue
        if end_date and row_date > end_date:
            continue
        filtered.append(row)
    return filtered


def dedupe_key(row: dict[str, str]) -> tuple[str, ...]:
    activity_id = row.get("activity_id", "").strip()
    if activity_id:
        return ("activity_id", activity_id)
    return (
        "activity_fingerprint",
        row.get("date", ""),
        row.get("start_time", ""),
        row.get("activity_type", ""),
        row.get("duration_minutes", ""),
        row.get("distance_miles", ""),
    )


def dedupe_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    deduped: dict[tuple[str, ...], dict[str, str]] = {}
    for row in rows:
        key = dedupe_key(row)
        deduped[key] = row
    return sorted(deduped.values(), key=lambda row: (row.get("date", ""), row.get("start_time", ""), row.get("source_file", "")))


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=ACTIVITY_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def normalize_raw_dir(
    raw_dir: Path,
    out_dir: Path,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    allow_empty: bool = False,
    manifest_path: Path = NIKE_LAST_NORMALIZE_FILE,
    timezone: str | ZoneInfo | None = None,
) -> dict[str, Any]:
    start_date, end_date = validate_date_window(start_date, end_date)
    files = source_files(raw_dir)
    if not files and not allow_empty:
        print(
            f"No Nike Run Club export files found under {raw_dir}. "
            "Place NRC export files there, or run with --allow-empty.",
            file=sys.stderr,
        )
        raise FileNotFoundError(f"No Nike Run Club export files found under {raw_dir}")

    local_tz = resolve_timezone(timezone, env_var=NIKE_RUN_CLUB_TIMEZONE_ENV)
    rows: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    for path in files:
        try:
            rows.extend(normalize_json_file(path, local_tz))
        except Exception as exc:  # noqa: BLE001 - importer should continue through bad files.
            errors.append({"file": workspace_path(path), "error": str(exc)})

    rows = dedupe_rows(filter_rows(rows, start_date, end_date))
    write_csv(out_dir / "activities.csv", rows)
    manifest: dict[str, Any] = {
        "normalized_at": utc_now_iso(),
        "raw_dir": workspace_path(raw_dir),
        "timezone": local_tz.key,
        "source_files": len(files),
        "activity_rows": len(rows),
        "errors": errors,
    }
    write_json_file(manifest_path, manifest)
    return manifest


def main() -> int:
    args = parse_args()
    try:
        manifest = normalize_raw_dir(
            args.raw_dir,
            args.staged_dir,
            start_date=args.start_date,
            end_date=args.end_date,
            allow_empty=args.allow_empty,
            timezone=args.timezone,
        )
    except FileNotFoundError:
        return 2
    except ValueError as exc:
        print(f"Normalize failed: {exc}", file=sys.stderr)
        return 1
    print(f"Wrote {args.staged_dir / 'activities.csv'}")
    print(f"Wrote {NIKE_LAST_NORMALIZE_FILE}")
    errors = manifest["errors"]
    if errors:
        print(f"Completed with {len(errors)} file normalization errors.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
