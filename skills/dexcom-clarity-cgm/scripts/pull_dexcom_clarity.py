#!/usr/bin/env python3
"""Download Dexcom Clarity CSV exports for local Stelo/CGM readings.

Authentication (the UAM/Keycloak login flow, the cookie jar, the session cache
under `.local/state/dexcom_clarity/`, and JWT freshness) lives in
`auth_dexcom_clarity.py`. This script resolves a session through that module,
exports the Clarity CSV for the requested window, and normalizes it into the
deduplicated CGM table.

State files (owned by the auth module):

- `.local/secrets/dexcom_clarity.env` for credentials and stable account metadata.
- `.local/state/dexcom_clarity/` for Dexcom cookies and the current subject token.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import urllib.parse
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

try:
    import requests
except ImportError as exc:  # pragma: no cover - exercised by missing local dep
    raise SystemExit("This script requires `requests`: python3 -m pip install requests") from exc

from auth_dexcom_clarity import (
    DEFAULT_CACHE_DIR,
    DEFAULT_ENV_FILE,
    WORKSPACE_ROOT,
    Settings,
    load_settings,
    make_http_session,
    require_settings,
    resolve_session,
    save_cookies,
)
from normalize_dexcom_clarity import (
    DEFAULT_NORMALIZE_MANIFEST,
    DEFAULT_TIMEZONE,
    DEXCOM_TIMEZONE_ENV,
    normalize_readings,
    parse_clarity_readings,
    resolve_timezone,
    workspace_path,
    write_normalize_manifest,
)
from healthdata.io import utc_now_iso, write_json_file


DEFAULT_STAGED_DIR = WORKSPACE_ROOT / "data/staged/dexcom_clarity"
DEFAULT_PULL_MANIFEST = WORKSPACE_ROOT / "data/manifests/ingestions/dexcom-clarity-last-pull.json"
CGM_TABLE_NAME = "cgm_readings.csv"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Pull Dexcom Clarity CGM data and normalize it into data/staged/dexcom_clarity."
    )
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument(
        "--staged-dir",
        type=Path,
        default=DEFAULT_STAGED_DIR,
    )
    parser.add_argument("--output", type=Path, help="Exact CSV output path.")
    parser.add_argument("--start-date", help="Start date as YYYY-MM-DD.")
    parser.add_argument("--end-date", help="End date as YYYY-MM-DD. Default: tomorrow.")
    parser.add_argument(
        "--days",
        type=int,
        default=15,
        help="Number of calendar dates to request when --start-date is omitted.",
    )
    parser.add_argument(
        "--timezone",
        default=None,
        help=f"IANA timezone for tagging naive Clarity timestamps. Default: {DEXCOM_TIMEZONE_ENV} or {DEFAULT_TIMEZONE}.",
    )
    parser.add_argument("--force-login", action="store_true", help="Ignore cached subject token.")
    parser.add_argument(
        "--no-login",
        action="store_true",
        help="Use only cached/env token; fail instead of posting the password.",
    )
    parser.add_argument("--login-only", action="store_true", help="Refresh session cache only.")
    parser.add_argument("--no-summary", action="store_true", help="Skip printing the CSV row summary.")
    parser.add_argument(
        "--no-normalize",
        action="store_true",
        help="Write a dated raw export instead of normalizing into the deduplicated CGM table.",
    )
    return parser.parse_args()


def parse_date(value: str, name: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise SystemExit(f"{name} must be YYYY-MM-DD, got {value!r}") from exc


def requested_interval(args: argparse.Namespace) -> tuple[dt.date, dt.date]:
    if args.days < 1:
        raise SystemExit("--days must be at least 1")

    end_date = (
        parse_date(args.end_date, "--end-date")
        if args.end_date
        else dt.date.today() + dt.timedelta(days=1)
    )
    start_date = (
        parse_date(args.start_date, "--start-date")
        if args.start_date
        else end_date - dt.timedelta(days=args.days - 1)
    )
    if start_date > end_date:
        raise SystemExit("--start-date must be on or before --end-date")
    return start_date, end_date


def fetch_subject_profile(
    settings: Settings,
    http: requests.Session,
    subject_id: str,
    subject_token: str,
) -> dict[str, str]:
    out = {
        "first_name": settings.first_name,
        "last_name": settings.last_name,
        "date_of_birth": settings.date_of_birth,
    }
    if all(out.values()):
        return out

    url = f"{settings.api_base}/v1/subject/{urllib.parse.quote(subject_id)}"
    response = http.get(
        url,
        headers={"Access-Token": subject_token, "Accept": "application/json"},
        timeout=45,
    )
    if response.ok:
        payload = response.json()
        out["first_name"] = out["first_name"] or str(payload.get("firstName") or "")
        out["last_name"] = out["last_name"] or str(payload.get("lastName") or "")
        out["date_of_birth"] = out["date_of_birth"] or str(payload.get("dateOfBirth") or "")

    missing = [key for key, value in out.items() if not value]
    if missing:
        env_names = {
            "first_name": "DEXCOM_CLARITY_FIRST_NAME",
            "last_name": "DEXCOM_CLARITY_LAST_NAME",
            "date_of_birth": "DEXCOM_CLARITY_DATE_OF_BIRTH",
        }
        raise RuntimeError(
            "Could not resolve required export fields: "
            + ", ".join(env_names[key] for key in missing)
        )
    return out


def export_csv(
    settings: Settings,
    http: requests.Session,
    session_data: dict[str, Any],
    start_date: dt.date,
    end_date: dt.date,
) -> bytes:
    subject_token = str(session_data.get("access_token") or "")
    subject_id = str(session_data.get("subject_id") or settings.subject_id or "")
    if not subject_token:
        raise RuntimeError("No Clarity subject token available.")
    if not subject_id:
        raise RuntimeError("No Clarity subject id available.")

    profile = fetch_subject_profile(settings, http, subject_id, subject_token)
    url = f"{settings.api_base}/subject/{urllib.parse.quote(subject_id)}/export"
    form = {
        "locale": settings.locale,
        "units": settings.units,
        "dateInterval": f"{start_date.isoformat()}/{end_date.isoformat()}",
        "accessToken": subject_token,
        "firstName": profile["first_name"],
        "lastName": profile["last_name"],
        "dateOfBirth": profile["date_of_birth"],
        "submitExport": "Export",
    }
    response = http.post(
        url,
        data=form,
        headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://clarity.dexcom.com",
            "Referer": "https://clarity.dexcom.com/i/",
        },
        allow_redirects=True,
        timeout=90,
    )
    response.raise_for_status()
    body = response.content
    head = body[:300].decode("utf-8", errors="ignore").lower()
    if not body.strip():
        raise RuntimeError("Dexcom export returned an empty response.")
    if "<html" in head and "timestamp" not in head:
        raise RuntimeError("Dexcom export returned HTML instead of CSV; cached auth may be stale.")
    return body


def output_path(
    args: argparse.Namespace,
    output_dir: Path,
    start_date: dt.date,
    end_date: dt.date,
) -> Path:
    if args.output:
        return args.output.expanduser()
    filename = f"dexcom-stelo-clarity-export_{start_date.isoformat()}_{end_date.isoformat()}.csv"
    return output_dir / filename


def summarize_readings(readings: list[dict[str, str]]) -> dict[str, Any]:
    """Summarize already-parsed EGV readings for the manifest and console.

    Timestamps are reported in the same tz-aware ISO 8601 form the staged table
    uses.
    """
    if not readings:
        return {"glucose_rows": 0, "first_timestamp": "", "last_timestamp": ""}

    def timestamp_key(reading: dict[str, str]) -> dt.datetime:
        return dt.datetime.fromisoformat(reading["timestamp"])

    first = min(readings, key=timestamp_key)
    last = max(readings, key=timestamp_key)
    return {
        "glucose_rows": len(readings),
        "first_timestamp": first["timestamp"],
        "last_timestamp": last["timestamp"],
    }


def summarize_csv(content: bytes, local_tz: ZoneInfo) -> dict[str, Any]:
    """Parse and summarize a raw export, tolerating an unparseable one.

    Used by the raw (--no-normalize) path, where a summary must never abort an
    otherwise successful pull; falls back to an empty summary.
    """
    try:
        readings = parse_clarity_readings(content, local_tz)
    except ValueError:
        return {"glucose_rows": 0, "first_timestamp": "", "last_timestamp": ""}
    return summarize_readings(readings)


def main() -> int:
    args = parse_args()
    args.staged_dir = args.staged_dir.expanduser()
    settings = load_settings(args.env_file, args.cache_dir)
    require_settings(settings, args.no_login)
    try:
        local_tz = resolve_timezone(args.timezone)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    start_date, end_date = requested_interval(args)
    http = make_http_session(settings)
    try:
        try:
            session_data = resolve_session(settings, http, args.force_login, args.no_login)
            if args.login_only:
                print(
                    "Cached Dexcom Clarity session for subject "
                    f"{session_data.get('subject_id', '<unknown>')}."
                )
                return 0

            csv_bytes = export_csv(settings, http, session_data, start_date, end_date)
            manifest: dict[str, Any] = {
                "pulled_at": utc_now_iso(),
                "staged_dir": workspace_path(args.staged_dir),
                "interval": f"{start_date.isoformat()}/{end_date.isoformat()}",
                "timezone": local_tz.key,
                "subject_id": str(session_data.get("subject_id") or ""),
            }

            if args.no_normalize or args.output:
                summary = summarize_csv(csv_bytes, local_tz)
                out_path = output_path(args, args.staged_dir, start_date, end_date)
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_bytes(csv_bytes)
                manifest["mode"] = "raw"
                manifest["output"] = workspace_path(out_path)
                print(f"Wrote {out_path}")
            else:
                readings = parse_clarity_readings(csv_bytes, local_tz)
                summary = summarize_readings(readings)
                table_path = args.staged_dir / CGM_TABLE_NAME
                added, total = normalize_readings(table_path, readings)
                write_normalize_manifest(
                    DEFAULT_NORMALIZE_MANIFEST,
                    table_path,
                    exports=1,
                    added=added,
                    total=total,
                    timezone=local_tz.key,
                )
                manifest["mode"] = "normalized"
                manifest["table"] = workspace_path(table_path)
                manifest["normalize"] = {"readings_added": added, "readings_total": total}
                print(f"Normalized into {table_path}: +{added} new ({total} total readings)")

            manifest["summary"] = summary
            write_json_file(DEFAULT_PULL_MANIFEST, manifest)
            print(f"Wrote {DEFAULT_PULL_MANIFEST}")

            if not args.no_summary:
                print(
                    "Glucose rows: {glucose_rows}; first: {first_timestamp}; last: {last_timestamp}".format(
                        **summary
                    )
                )
            return 0
        finally:
            save_cookies(http, settings)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except requests.RequestException as exc:
        print(f"network error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
