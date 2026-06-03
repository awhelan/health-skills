#!/usr/bin/env python3
"""Pull Polar AccessLink training data into data/raw, then normalize to CSV.

This is the fetch phase of the polar-accesslink skill. It refreshes the stored
OAuth token (see ``auth_polar_accesslink.py``), downloads training sessions plus
feature-rich HR/zone/lap detail, saves the raw JSON under
``data/raw/polar_accesslink/run=<start>_<end>/``, then hands those payloads to
``normalize_polar_accesslink.normalize_raw_dir`` to merge the staged CSV tables.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from getpass import getpass
from pathlib import Path
from typing import Any

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from healthdata.config import (
    DEFAULT_LOCAL_TIMEZONE,
    POLAR_API_RAW_DIR,
    POLAR_ENV_FILE,
    POLAR_LAST_NORMALIZE_FILE,
    POLAR_LAST_PULL_FILE,
    POLAR_STAGED_DIR,
    POLAR_TOKEN_FILE,
    workspace_path,
)
from healthdata.auth import refresh_oauth_token, token_needs_refresh
from healthdata.io import load_env_file as _load_env_file, read_json_file, utc_now_iso, write_json_file
from normalize_polar_accesslink import normalize_raw_dir


API_ROOT = "https://www.polaraccesslink.com/v4/data"
TOKEN_URL = "https://auth.polar.com/oauth/token"
DEFAULT_TOKEN_FILE = POLAR_TOKEN_FILE
DEFAULT_STAGED_DIR = POLAR_STAGED_DIR
DEFAULT_RAW_DIR = POLAR_API_RAW_DIR
DEFAULT_START_DATE = date(2019, 1, 1)
LOCAL_ENV_FILE = POLAR_ENV_FILE
LOCAL_TZ = DEFAULT_LOCAL_TIMEZONE
FEATURES = ("samples", "statistics", "zones", "laps", "routes")


def load_local_env(path: str | Path = LOCAL_ENV_FILE) -> None:
    _load_env_file(path)


def configured_client_secret() -> str | None:
    load_local_env()
    return os.environ.get("POLAR_CLIENT_SECRET")


def _read_json(path: Path) -> dict[str, Any]:
    return read_json_file(path)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    write_json_file(path, payload, private=True)


def _client_secret() -> str:
    return configured_client_secret() or getpass("Polar client secret: ")


def api_get(access_token: str, path: str, query: dict[str, Any] | None = None) -> dict[str, Any]:
    suffix = path if path.startswith("/") else f"/{path}"
    url = f"{API_ROOT}{suffix}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query, doseq=True)}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {access_token}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Polar API request failed: HTTP {exc.code}: {url}: {detail}") from exc


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def latest_staged_date(out_dir: Path) -> date | None:
    path = out_dir / "training_sessions.csv"
    if not path.exists():
        return None
    df = pd.read_csv(path, usecols=["date"])
    if df.empty:
        return None
    parsed = pd.to_datetime(df["date"], errors="coerce").dropna()
    if parsed.empty:
        return None
    return parsed.max().date()


def default_start_date(out_dir: Path, end: date) -> date:
    staged_latest = latest_staged_date(out_dir)
    if staged_latest:
        return min(max(staged_latest - timedelta(days=7), DEFAULT_START_DATE), end)
    return DEFAULT_START_DATE


def chunk_ranges(start: date, end: date, max_days: int) -> list[tuple[date, date]]:
    ranges: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=max_days - 1), end)
        ranges.append((cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)
    return ranges


def api_datetime(day: date) -> str:
    # Polar's v4 docs say ISO 8601, but the query parser rejects date-only,
    # UTC suffix, millis, and explicit offsets. Plain seconds are accepted.
    return f"{day.isoformat()}T00:00:00"


def save_raw(raw_dir: Path, name: str, payload: Any) -> None:
    write_json_file(raw_dir / f"{name}.json", payload)


def fetch_training_summary(access_token: str, start: date, end: date) -> dict[str, Any]:
    sessions: list[dict[str, Any]] = []
    for chunk_start, chunk_end in chunk_ranges(start, end, 90):
        payload = api_get(
            access_token,
            "/training-sessions/list",
            {"from": api_datetime(chunk_start), "to": api_datetime(chunk_end + timedelta(days=1))},
        )
        sessions.extend(payload.get("trainingSessions", []))
    return {"trainingSessions": sessions}


def _session_date(session: dict[str, Any]) -> date | None:
    start_time = session.get("startTime")
    parsed = pd.to_datetime(start_time, errors="coerce")
    if pd.isna(parsed):
        return None
    if getattr(parsed, "tzinfo", None) is not None:
        parsed = parsed.tz_convert(LOCAL_TZ).tz_localize(None)
    return parsed.date()


def fetch_training_details(access_token: str, days: list[date]) -> dict[str, Any]:
    by_date: dict[str, Any] = {}
    for day in sorted(set(days)):
        by_date[str(day)] = api_get(
            access_token,
            "/training-sessions/list",
            {
                "from": api_datetime(day),
                "to": api_datetime(day + timedelta(days=1)),
                "features": list(FEATURES),
            },
        )
    return by_date


def normalize_only(staged_dir: str | Path, raw_dir: str | Path) -> dict[str, Any]:
    """Re-normalize every saved raw run without calling Polar."""
    return normalize_raw_dir(Path(raw_dir), Path(staged_dir), manifest_path=POLAR_LAST_NORMALIZE_FILE)


def pull_recent(
    *,
    token_file: str | Path = DEFAULT_TOKEN_FILE,
    client_id: str | None = None,
    client_secret: str | None = None,
    staged_dir: str | Path = DEFAULT_STAGED_DIR,
    raw_dir: str | Path = DEFAULT_RAW_DIR,
    start_date: str | date | None = None,
    end_date: str | date | None = None,
    days: int | None = None,
    detail_days: int = 45,
    force_refresh: bool = False,
    allow_prompt: bool = True,
) -> dict[str, Any]:
    load_local_env()
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
        start = default_start_date(output_dir, end)
    if start > end:
        start = end

    token = _read_json(token_path)
    effective_client_id = client_id or os.environ.get("POLAR_CLIENT_ID") or token.get("client_id")
    if not effective_client_id:
        raise ValueError("POLAR_CLIENT_ID is required.")
    secret = client_secret or configured_client_secret()
    needs_refresh = token_needs_refresh(token, force_refresh)
    if needs_refresh and not secret:
        if not allow_prompt:
            raise ValueError("Polar client secret is required to refresh the access token.")
        secret = _client_secret()

    token = refresh_oauth_token(
        token_path,
        token_url=TOKEN_URL,
        client_id=str(effective_client_id),
        client_secret=secret,
        use_basic_auth=True,
        force=force_refresh,
        label="Polar",
    )
    access_token = str(token["access_token"])

    detail_start = max(start, end - timedelta(days=detail_days - 1))
    raw_output_dir = raw_root / f"run={start}_{end}"
    print(f"Pulling Polar sessions from {start} through {end}.")
    summary_raw = fetch_training_summary(access_token, start, end)
    detail_dates = [
        day
        for day in (_session_date(session) for session in summary_raw.get("trainingSessions", []))
        if day is not None and detail_start <= day <= end
    ]
    detail_raw = fetch_training_details(access_token, detail_dates)
    save_raw(raw_output_dir, "training_summary", summary_raw)
    save_raw(raw_output_dir, "training_details", detail_raw)

    normalize_summary = normalize_raw_dir(raw_output_dir, output_dir, manifest_path=POLAR_LAST_NORMALIZE_FILE)

    summary = {
        "start_date": str(start),
        "end_date": str(end),
        "detail_start_date": str(detail_start),
        "pulled_at": utc_now_iso(),
        "raw_dir": workspace_path(raw_output_dir),
        "normalize": normalize_summary,
    }
    _write_json(POLAR_LAST_PULL_FILE, summary)
    print(f"Wrote {output_dir / 'training_sessions.csv'}")
    print(f"Wrote {output_dir / 'hr_samples.csv'}")
    print(
        f"Sessions: {normalize_summary['sessions_total']} total "
        f"({normalize_summary['sessions_written']} from this pull); "
        f"HR samples: {normalize_summary['hr_samples_total']} total."
    )
    return summary


def parse_args() -> argparse.Namespace:
    load_local_env()
    parser = argparse.ArgumentParser(
        description="Pull Polar AccessLink data and normalize it into staged CSV files."
    )
    parser.add_argument("--token-file", default=DEFAULT_TOKEN_FILE)
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
    parser.add_argument("--client-id", default=os.environ.get("POLAR_CLIENT_ID"))
    parser.add_argument("--staged-dir", default=DEFAULT_STAGED_DIR)
    parser.add_argument("--start-date")
    parser.add_argument("--end-date", default=str(date.today()))
    parser.add_argument("--days", type=int, help="Override --start-date with a trailing day count.")
    parser.add_argument(
        "--detail-days",
        type=int,
        default=45,
        help="Pull feature-rich HR/zones/laps for this trailing day count. Summary pulls cover the whole date range.",
    )
    parser.add_argument("--force-refresh", action="store_true")
    parser.add_argument("--no-prompt", action="store_true", help="Fail instead of prompting for secrets.")
    parser.add_argument(
        "--normalize-only",
        action="store_true",
        help="Re-normalize every saved raw run under --raw-dir without calling Polar.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.normalize_only:
            manifest = normalize_only(args.staged_dir, args.raw_dir)
            print(f"Wrote {Path(args.staged_dir) / 'training_sessions.csv'}")
            print(
                f"Sessions: {manifest['sessions_total']} total; "
                f"HR samples: {manifest['hr_samples_total']} total."
            )
            return 0
        pull_recent(
            token_file=args.token_file,
            client_id=args.client_id,
            staged_dir=args.staged_dir,
            raw_dir=args.raw_dir,
            start_date=args.start_date,
            end_date=args.end_date,
            days=args.days,
            detail_days=args.detail_days,
            force_refresh=args.force_refresh,
            allow_prompt=not args.no_prompt,
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
