#!/usr/bin/env python3
"""Pull Fitbit Web API metrics, save raw JSON, and normalize staged tables.

This is the fetch phase of the fitbit skill. It refreshes the stored OAuth token
(see ``auth_fitbit.py``), downloads daily metrics, activity logs, and TCX files,
saves the raw payloads under ``data/raw/fitbit_api/run=<start>_<end>/``, then
hands them to ``normalize_fitbit.normalize_and_stage`` to write the staged
``daily_metrics``/``activity_logs``/``activity_tcx`` tables.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from getpass import getpass
from pathlib import Path
from typing import Any

import pandas as pd

from healthdata.auth import refresh_oauth_token, token_needs_refresh
from healthdata.config import (
    DEFAULT_LOCAL_TIMEZONE,
    FITBIT_API_RAW_DIR,
    FITBIT_ENV_FILE,
    FITBIT_LAST_NORMALIZE_FILE,
    FITBIT_LAST_PULL_FILE,
    FITBIT_STAGED_DIR,
    FITBIT_TAKEOUT_DIR,
    FITBIT_TOKEN_FILE,
    workspace_path,
)
from healthdata.io import load_env_file as _load_env_file, read_json_file, utc_now_iso, write_json_file
from normalize_fitbit import (
    _activity_date,
    _iter_activities,
    date_range,
    normalize_and_stage,
    parse_date,
)


API_ROOT = "https://api.fitbit.com"
TOKEN_URL = "https://api.fitbit.com/oauth2/token"
DEFAULT_TOKEN_FILE = FITBIT_TOKEN_FILE
DEFAULT_STAGED_DIR = FITBIT_STAGED_DIR
DEFAULT_RAW_DIR = FITBIT_API_RAW_DIR
DEFAULT_TAKEOUT_ROOT = FITBIT_TAKEOUT_DIR
LOCAL_TZ = DEFAULT_LOCAL_TIMEZONE
LOCAL_ENV_FILE = FITBIT_ENV_FILE


class FitbitApiError(RuntimeError):
    def __init__(self, status_code: int, url: str, detail: str) -> None:
        self.status_code = status_code
        self.url = url
        self.detail = detail
        super().__init__(f"Fitbit API request failed: HTTP {status_code}: {url}: {detail}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "status_code": self.status_code,
            "url": self.url,
            "detail": self.detail,
        }


def load_local_env(path: str | Path = LOCAL_ENV_FILE) -> None:
    _load_env_file(path)


def configured_client_secret() -> str | None:
    load_local_env()
    return os.environ.get("FITBIT_CLIENT_SECRET")


def _read_json(path: Path) -> dict[str, Any]:
    return read_json_file(path)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    write_json_file(path, payload, private=True)


def _client_secret(client_type: str) -> str | None:
    if client_type != "server":
        return None
    return configured_client_secret() or getpass("Fitbit client secret: ")


def resolve_client_id(client_id: str | None, token: dict[str, Any] | None = None) -> str:
    resolved = client_id or (token or {}).get("client_id") or os.environ.get("FITBIT_CLIENT_ID")
    if resolved:
        return str(resolved)
    raise ValueError(
        "Fitbit client id is required. Set FITBIT_CLIENT_ID in "
        ".local/secrets/fitbit.env, pass --client-id, or re-run auth to stamp "
        "the token with client_id."
    )


def refresh_token(
    token_path: Path,
    client_id: str,
    client_secret: str | None,
    client_type: str,
    force: bool = False,
) -> dict[str, Any]:
    """Refresh the stored Fitbit token via the shared OAuth helper.

    Kept as a thin wrapper so callers (the pull and the Fitbit/Polar sample
    calibration) keep a stable Fitbit-shaped signature.
    """
    return refresh_oauth_token(
        token_path,
        token_url=TOKEN_URL,
        client_id=client_id,
        client_secret=client_secret,
        use_basic_auth=(client_type == "server"),
        force=force,
        label="Fitbit",
    )


def api_read(
    access_token: str,
    path_or_url: str,
    *,
    accept: str = "application/json",
) -> bytes:
    url = path_or_url if path_or_url.startswith("https://") else f"{API_ROOT}{path_or_url}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": accept,
            "Accept-Language": "en_US",
            "Authorization": f"Bearer {access_token}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise FitbitApiError(exc.code, url, detail) from exc


def api_get(access_token: str, path_or_url: str) -> dict[str, Any]:
    return json.loads(api_read(access_token, path_or_url).decode("utf-8"))


def api_get_text(access_token: str, path_or_url: str, *, accept: str = "*/*") -> str:
    return api_read(access_token, path_or_url, accept=accept).decode("utf-8")


def chunks(start: date, end: date, max_days: int) -> list[tuple[date, date]]:
    ranges: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=max_days - 1), end)
        ranges.append((cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)
    return ranges


def latest_takeout_step_date(root: Path) -> date | None:
    dates: list[date] = []
    for path in (root / "Global Export Data").glob("steps-*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if not data:
            continue
        parsed = pd.to_datetime(
            [item.get("dateTime") for item in data],
            format="%m/%d/%y %H:%M:%S",
            errors="coerce",
        )
        parsed = parsed.dropna()
        if not parsed.empty:
            dates.append(parsed.max().date())
    return max(dates) if dates else None


def latest_staged_date(out_dir: Path) -> date | None:
    path = out_dir / "daily_metrics.csv"
    if not path.exists():
        return None
    df = pd.read_csv(path, usecols=["date"])
    if df.empty:
        return None
    parsed = pd.to_datetime(df["date"], errors="coerce").dropna()
    if parsed.empty:
        return None
    return parsed.max().date()


def default_start_date(root: Path, out_dir: Path, end: date) -> date:
    takeout_latest = latest_takeout_step_date(root)
    staged_latest = latest_staged_date(out_dir)
    if staged_latest:
        # Re-pull recent days because today's and yesterday's Fitbit summaries often settle late.
        return min(max(staged_latest - timedelta(days=2), date.min), end)
    if takeout_latest:
        return min(takeout_latest + timedelta(days=1), end)
    return end - timedelta(days=29)


def save_raw(raw_dir: Path, name: str, payload: Any) -> None:
    write_json_file(raw_dir / f"{name}.json", payload)


def _optional_error_payload(label: str, exc: FitbitApiError) -> dict[str, Any]:
    print(f"Skipping optional Fitbit {label}: HTTP {exc.status_code}.")
    return {"error": exc.to_dict()}


def fetch_time_series(access_token: str, resource: str, start: date, end: date) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    key = f"activities-{resource.split('/')[-1]}"
    values: list[dict[str, Any]] = []
    for chunk_start, chunk_end in chunks(start, end, 365):
        payload = api_get(
            access_token,
            f"/1/user/-/activities/{resource}/date/{chunk_start}/{chunk_end}.json",
        )
        values.extend(payload.get(key, []))
    merged[key] = values
    return merged


def fetch_time_series_optional(
    access_token: str,
    label: str,
    resource: str,
    start: date,
    end: date,
) -> dict[str, Any]:
    try:
        return fetch_time_series(access_token, resource, start, end)
    except FitbitApiError as exc:
        return _optional_error_payload(label, exc)


def fetch_interval_series(
    access_token: str,
    endpoint: str,
    response_key: str,
    start: date,
    end: date,
    *,
    max_days: int = 30,
) -> dict[str, Any]:
    values: list[dict[str, Any]] = []
    for chunk_start, chunk_end in chunks(start, end, max_days):
        payload = api_get(access_token, f"/1/user/-/{endpoint}/date/{chunk_start}/{chunk_end}.json")
        if isinstance(payload, list):
            values.extend(payload)
            continue
        response_value = payload.get(response_key, [])
        if isinstance(response_value, list):
            values.extend(response_value)
    return {response_key: values}


def fetch_interval_series_optional(
    access_token: str,
    label: str,
    endpoint: str,
    response_key: str,
    start: date,
    end: date,
) -> dict[str, Any]:
    try:
        return fetch_interval_series(access_token, endpoint, response_key, start, end)
    except FitbitApiError as exc:
        return _optional_error_payload(label, exc)


def fetch_activity_summary(access_token: str, start: date, end: date) -> dict[str, Any]:
    resources = {
        "calories": "calories",
        "floors": "floors",
        "elevation": "elevation",
        "minutesSedentary": "sedentary minutes",
        "minutesLightlyActive": "lightly active minutes",
        "minutesFairlyActive": "moderately active minutes",
        "minutesVeryActive": "very active minutes",
    }
    return {
        resource: fetch_time_series_optional(access_token, label, resource, start, end)
        for resource, label in resources.items()
    }


def fetch_health_metrics(access_token: str, start: date, end: date) -> dict[str, Any]:
    endpoints = {
        "spo2": ("SpO2", "spo2", "spo2"),
        "breathing_rate": ("breathing rate", "br", "br"),
        "temp_skin": ("skin temperature", "temp/skin", "tempSkin"),
        "temp_core": ("core temperature", "temp/core", "tempCore"),
        "cardio_score": ("cardio fitness score", "cardioscore", "cardioScore"),
    }
    return {
        name: fetch_interval_series_optional(access_token, label, endpoint, response_key, start, end)
        for name, (label, endpoint, response_key) in endpoints.items()
    }


def fetch_azm(access_token: str, start: date, end: date) -> dict[str, Any]:
    values: list[dict[str, Any]] = []
    for chunk_start, chunk_end in chunks(start, end, 365):
        payload = api_get(
            access_token,
            f"/1/user/-/activities/active-zone-minutes/date/{chunk_start}/{chunk_end}.json",
        )
        values.extend(payload.get("activities-active-zone-minutes", []))
    return {"activities-active-zone-minutes": values}


def fetch_heart(access_token: str, start: date, end: date) -> dict[str, Any]:
    values: list[dict[str, Any]] = []
    for chunk_start, chunk_end in chunks(start, end, 365):
        payload = api_get(
            access_token,
            f"/1/user/-/activities/heart/date/{chunk_start}/{chunk_end}.json",
        )
        values.extend(payload.get("activities-heart", []))
    return {"activities-heart": values}


def fetch_hrv(access_token: str, start: date, end: date) -> dict[str, Any]:
    values: list[dict[str, Any]] = []
    for chunk_start, chunk_end in chunks(start, end, 30):
        payload = api_get(access_token, f"/1/user/-/hrv/date/{chunk_start}/{chunk_end}.json")
        values.extend(payload.get("hrv", []))
    return {"hrv": values}


def fetch_sleep(access_token: str, start: date, end: date) -> dict[str, Any]:
    payloads: dict[str, Any] = {}
    for day in date_range(start, end):
        payloads[str(day)] = api_get(access_token, f"/1.2/user/-/sleep/date/{day}.json")
    return payloads


def fetch_activity_logs(access_token: str, start: date, end: date) -> dict[str, Any]:
    pages: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    url = (
        "/1/user/-/activities/list.json?"
        + urllib.parse.urlencode({"afterDate": str(start), "sort": "asc", "offset": 0, "limit": 100})
    )

    for _ in range(50):
        absolute_url = url if url.startswith("https://") else f"{API_ROOT}{url}"
        if absolute_url in seen_urls:
            break
        seen_urls.add(absolute_url)
        payload = api_get(access_token, url)
        pages.append(payload)
        activities = payload.get("activities", [])
        if not activities:
            break
        last_start = _activity_date(activities[-1])
        if last_start and last_start > end:
            break
        next_url = payload.get("pagination", {}).get("next")
        if not next_url:
            break
        url = next_url
    return {"pages": pages}


def _safe_filename_part(value: Any) -> str:
    clean = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "").strip()).strip("_")
    return clean or "activity"


def fetch_activity_tcx(
    access_token: str,
    raw: dict[str, Any],
    start: date,
    end: date,
    tcx_dir: Path,
) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    tcx_dir.mkdir(parents=True, exist_ok=True)

    for activity in _iter_activities(raw):
        day = _activity_date(activity)
        if day is None or day < start or day > end:
            continue
        tcx_url = activity.get("tcxLink")
        log_id = activity.get("logId")
        if not tcx_url or log_id is None:
            continue

        activity_name = activity.get("activityName")
        filename = f"{day}_{log_id}_{_safe_filename_part(activity_name)}.tcx"
        path = tcx_dir / filename
        row = {
            "date": str(day),
            "log_id": str(log_id),
            "activity_name": activity_name,
            "start_time": activity.get("startTime") or activity.get("originalStartTime"),
            "tcx_path": workspace_path(path),
            "source_url": tcx_url,
        }
        if path.exists() and path.stat().st_size > 0:
            files.append({**row, "status": "existing"})
            continue

        try:
            tcx = api_get_text(
                access_token,
                str(tcx_url),
                accept="application/vnd.garmin.tcx+xml, application/xml, text/xml, */*",
            )
        except FitbitApiError as exc:
            errors.append({**row, "status": "error", "error": exc.detail, "status_code": exc.status_code})
            continue

        path.write_text(tcx, encoding="utf-8")
        files.append({**row, "status": "written"})

    return {"files": files, "errors": errors}


def pull_recent(
    *,
    token_file: str | Path = DEFAULT_TOKEN_FILE,
    client_id: str | None = None,
    client_type: str = "server",
    client_secret: str | None = None,
    staged_dir: str | Path = DEFAULT_STAGED_DIR,
    raw_dir: str | Path = DEFAULT_RAW_DIR,
    takeout_root: str | Path | None = None,
    start_date: str | date | None = None,
    end_date: str | date | None = None,
    days: int | None = None,
    force_refresh: bool = False,
    allow_prompt: bool = True,
    include_health_metrics: bool = True,
    include_tcx: bool = True,
) -> dict[str, Any]:
    load_local_env()
    root = Path(
        takeout_root or os.environ.get("FITBIT_TAKEOUT_ROOT", str(DEFAULT_TAKEOUT_ROOT))
    ).expanduser()
    token_path = Path(token_file)
    output_dir = Path(staged_dir)
    raw_root = Path(raw_dir)
    end = parse_date(end_date) if isinstance(end_date, str) else end_date or date.today()

    if days:
        start = end - timedelta(days=days - 1)
    elif isinstance(start_date, str):
        start = parse_date(start_date)
    elif start_date:
        start = start_date
    else:
        start = default_start_date(root, output_dir, end)
    if start > end:
        start = end

    token = _read_json(token_path)
    effective_client_id = resolve_client_id(client_id, token)
    secret = client_secret or configured_client_secret()
    if client_type == "server" and token_needs_refresh(token, force_refresh) and not secret:
        if not allow_prompt:
            raise ValueError("Fitbit client secret is required to refresh the access token.")
        secret = _client_secret(client_type)

    token = refresh_token(token_path, str(effective_client_id), secret, client_type, force_refresh)
    access_token = str(token["access_token"])

    raw_output_dir = raw_root / f"run={start}_{end}"
    print(f"Pulling Fitbit daily data from {start} through {end}.")
    steps = fetch_time_series(access_token, "steps", start, end)
    distance = fetch_time_series(access_token, "distance", start, end)
    activity_summary = fetch_activity_summary(access_token, start, end)
    azm = fetch_azm(access_token, start, end)
    heart = fetch_heart(access_token, start, end)
    hrv = fetch_hrv(access_token, start, end)
    sleep = fetch_sleep(access_token, start, end)
    health_metrics = fetch_health_metrics(access_token, start, end) if include_health_metrics else {}
    activity_logs_raw = fetch_activity_logs(access_token, start, end)
    tcx_manifest = (
        fetch_activity_tcx(access_token, activity_logs_raw, start, end, raw_output_dir / "tcx")
        if include_tcx
        else {"files": [], "errors": [], "skipped": True}
    )

    save_raw(raw_output_dir, "steps", steps)
    save_raw(raw_output_dir, "distance", distance)
    for name, payload in activity_summary.items():
        save_raw(raw_output_dir, name, payload)
    save_raw(raw_output_dir, "active_zone_minutes", azm)
    save_raw(raw_output_dir, "heart", heart)
    save_raw(raw_output_dir, "hrv", hrv)
    save_raw(raw_output_dir, "sleep", sleep)
    for name, payload in health_metrics.items():
        save_raw(raw_output_dir, name, payload)
    save_raw(raw_output_dir, "activity_logs", activity_logs_raw)
    save_raw(raw_output_dir, "tcx_manifest", tcx_manifest)

    payloads = {
        "steps": steps,
        "distance": distance,
        "activity_summary": activity_summary,
        "azm": azm,
        "heart": heart,
        "hrv": hrv,
        "sleep": sleep,
        "health_metrics": health_metrics,
        "activity_logs": activity_logs_raw,
        "tcx_manifest": tcx_manifest,
    }
    normalize_summary = normalize_and_stage(
        output_dir, start, end, payloads, manifest_path=FITBIT_LAST_NORMALIZE_FILE
    )

    optional_errors = {
        name: payload["error"]
        for group in [activity_summary, health_metrics]
        for name, payload in group.items()
        if isinstance(payload, dict) and "error" in payload
    }
    summary = {
        "start_date": str(start),
        "end_date": str(end),
        "daily_rows_written": normalize_summary["daily_rows_written"],
        "daily_rows_total": normalize_summary["daily_rows_total"],
        "tcx_files_written_or_existing": int(len(tcx_manifest.get("files", []))),
        "tcx_errors": int(len(tcx_manifest.get("errors", []))),
        "optional_errors": optional_errors,
        "pulled_at": utc_now_iso(),
        "raw_dir": workspace_path(raw_output_dir),
        "daily_metrics_path": normalize_summary["daily_metrics_path"],
        "activity_logs_path": normalize_summary["activity_logs_path"],
        "activity_tcx_path": normalize_summary["activity_tcx_path"],
    }
    _write_json(FITBIT_LAST_PULL_FILE, summary)
    print(f"Wrote {output_dir / 'daily_metrics.csv'}")
    print(f"Wrote {output_dir / 'activity_logs.csv'}")
    print(f"Wrote {output_dir / 'activity_tcx.csv'}")
    return summary


def parse_args() -> argparse.Namespace:
    load_local_env()
    parser = argparse.ArgumentParser(description="Pull recent Fitbit Web API data and normalize it into staged CSV files.")
    parser.add_argument("--token-file", default=DEFAULT_TOKEN_FILE)
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
    parser.add_argument("--client-id", default=os.environ.get("FITBIT_CLIENT_ID"))
    parser.add_argument(
        "--client-type",
        choices=["server", "client"],
        default=os.environ.get("FITBIT_CLIENT_TYPE", "server"),
    )
    parser.add_argument("--staged-dir", default=DEFAULT_STAGED_DIR)
    parser.add_argument(
        "--takeout-root",
        default=os.environ.get("FITBIT_TAKEOUT_ROOT", str(DEFAULT_TAKEOUT_ROOT)),
        help="Raw Fitbit Takeout root used only to infer the default incremental start date.",
    )
    parser.add_argument("--start-date")
    parser.add_argument("--end-date", default=str(date.today()))
    parser.add_argument("--days", type=int, help="Override --start-date with a trailing day count.")
    parser.add_argument("--force-refresh", action="store_true")
    parser.add_argument("--no-prompt", action="store_true", help="Fail instead of prompting for secrets.")
    parser.add_argument("--skip-health-metrics", action="store_true")
    parser.add_argument("--skip-tcx", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        pull_recent(
            token_file=args.token_file,
            client_id=args.client_id,
            client_type=args.client_type,
            staged_dir=args.staged_dir,
            raw_dir=args.raw_dir,
            takeout_root=args.takeout_root,
            start_date=args.start_date,
            end_date=args.end_date,
            days=args.days,
            force_refresh=args.force_refresh,
            allow_prompt=not args.no_prompt,
            include_health_metrics=not args.skip_health_metrics,
            include_tcx=not args.skip_tcx,
        )
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
