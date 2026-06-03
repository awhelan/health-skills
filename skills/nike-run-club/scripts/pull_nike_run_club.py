#!/usr/bin/env python3
"""One-time Nike Run Club historical backfill.

Nike does not provide a stable public export API. This script uses the
authenticated NRC/Nike API endpoint documented in the local notes and stores the
raw JSON responses before normalizing them. Treat this as a backfill tool, not a
scheduled sync.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

try:
    import requests
except ImportError as exc:  # pragma: no cover - exercised by missing local dep
    raise SystemExit("This script requires `requests`: python3 -m pip install requests") from exc

from healthdata.config import (  # noqa: E402
    DEFAULT_LOCAL_TIMEZONE,
    NIKE_LAST_NORMALIZE_FILE,
    NIKE_LAST_PULL_FILE,
    NIKE_RUN_CLUB_EXPORT_DATE_ENV,
    NIKE_RUN_CLUB_RAW_ROOT,
    NIKE_RUN_CLUB_STAGED_DIR,
    NIKE_RUN_CLUB_TIMEZONE_ENV,
    workspace_path,
)
from healthdata.io import utc_now_iso, write_json_file  # noqa: E402
from healthdata.timeutil import validate_date_window  # noqa: E402
from normalize_nike_run_club import normalize_raw_dir


DEFAULT_ACTIVITIES_URL = "https://api.nike.com/plus/v3/activities/before_id/v3/{before_id}"
DEFAULT_TYPES = "run,jogging"


def default_raw_export_dir() -> Path:
    export_date = os.environ.get(NIKE_RUN_CLUB_EXPORT_DATE_ENV, "").strip() or dt.date.today().isoformat()
    return NIKE_RUN_CLUB_RAW_ROOT / f"export={export_date}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill historical Nike Run Club activities into the wearable data tree."
    )
    parser.add_argument(
        "--bearer-token",
        help="Nike bearer token, or a pasted 'Authorization: Bearer ...' header / copied cURL blob to extract it from. Prefer --bearer-token-stdin.",
    )
    parser.add_argument(
        "--bearer-token-stdin",
        action="store_true",
        help="Read the Nike bearer token/header/cURL blob from stdin so it is not exposed in process argv.",
    )
    parser.add_argument("--raw-dir", type=Path)
    parser.add_argument("--staged-dir", type=Path, default=NIKE_RUN_CLUB_STAGED_DIR)
    parser.add_argument("--api-url-template", default=DEFAULT_ACTIVITIES_URL)
    parser.add_argument("--types", default=DEFAULT_TYPES)
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--include-deleted", action="store_true")
    parser.add_argument("--max-pages", type=int, default=1000)
    parser.add_argument("--pause-seconds", type=float, default=0.2)
    parser.add_argument(
        "--normalize-only",
        action="store_true",
        help="Normalize existing raw pages without calling Nike.",
    )
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        help="Write an empty staged table and manifest when no NRC files are present.",
    )
    parser.add_argument("--force", action="store_true", help="Replace existing raw API page files.")
    parser.add_argument("--start-date", help="Optional normalized output lower date bound, YYYY-MM-DD.")
    parser.add_argument("--end-date", help="Optional normalized output upper date bound, YYYY-MM-DD.")
    parser.add_argument(
        "--timezone",
        default=os.environ.get(NIKE_RUN_CLUB_TIMEZONE_ENV, DEFAULT_LOCAL_TIMEZONE),
        help=f"IANA timezone for local activity dates and naive timestamps. Default: {NIKE_RUN_CLUB_TIMEZONE_ENV} or {DEFAULT_LOCAL_TIMEZONE}.",
    )
    args = parser.parse_args()
    if args.bearer_token and args.bearer_token_stdin:
        parser.error("use --bearer-token or --bearer-token-stdin, not both")
    try:
        args.start_date, args.end_date = validate_date_window(args.start_date, args.end_date)
    except ValueError as exc:
        parser.error(str(exc))
    if args.raw_dir is None:
        args.raw_dir = NIKE_RUN_CLUB_RAW_ROOT if args.normalize_only else default_raw_export_dir()
    return args


# RFC 6750 bearer-token charset, used to fish a token out of a header/cURL paste.
_AUTH_HEADER_RE = re.compile(r"authorization\s*:\s*bearer\s+([A-Za-z0-9\-._~+/]+=*)", re.IGNORECASE)


def extract_bearer_token(raw: str) -> str:
    """Pull a token out of a bare token, an Authorization header, or a copied cURL blob."""
    text = (raw or "").strip()
    if not text:
        return ""
    match = _AUTH_HEADER_RE.search(text)
    if match:
        return match.group(1)
    if text.lower().startswith("bearer "):
        return text.split(" ", 1)[1].strip()
    return text.strip("'\"")


def bearer_token(args: argparse.Namespace, stdin_text: str | None = None) -> str:
    # Nike tokens are short-lived, so we never read a stored token from the
    # environment or a file; the caller passes a fresh one per run.
    if getattr(args, "bearer_token_stdin", False):
        raw_token = sys.stdin.read() if stdin_text is None else stdin_text
    else:
        raw_token = getattr(args, "bearer_token", None)
    if not raw_token:
        raise SystemExit(
            "Missing Nike bearer token. Prefer --bearer-token-stdin with a fresh token, "
            "pasted 'Authorization: Bearer ...' header, or copied cURL blob."
        )
    token = extract_bearer_token(raw_token)
    if not token:
        raise SystemExit("Nike bearer token is empty.")
    return token


def api_get_json(url: str, token: str) -> dict[str, Any]:
    response = requests.get(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "health-workspace-nrc-backfill/1.0",
        },
        timeout=60,
    )
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        raise RuntimeError(
            f"Nike API request failed: HTTP {response.status_code}: {url}: {response.text}"
        ) from exc
    return response.json()


def activities_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    activities = payload.get("activities")
    if isinstance(activities, list):
        return [item for item in activities if isinstance(item, dict)]
    return []


def next_before_id(payload: dict[str, Any], activities: list[dict[str, Any]]) -> str:
    paging = payload.get("paging")
    if isinstance(paging, dict):
        for key in ("before_id", "beforeId", "before"):
            value = paging.get(key)
            if value:
                return str(value)
    if activities:
        activity_id = activities[-1].get("id")
        if activity_id:
            return str(activity_id)
    return ""


def page_url(template: str, before_id: str, args: argparse.Namespace) -> str:
    safe_before_id = urllib.parse.quote(before_id, safe="*")
    base = template.format(before_id=safe_before_id)
    query = urllib.parse.urlencode(
        {
            "limit": str(args.limit),
            "types": args.types,
            "include_deleted": str(args.include_deleted).lower(),
        }
    )
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}{query}"


def prepare_page_dir(raw_dir: Path, force: bool) -> Path:
    page_dir = raw_dir / "api_pages"
    if force and page_dir.exists():
        shutil.rmtree(page_dir)
    page_dir.mkdir(parents=True, exist_ok=True)
    existing = list(page_dir.glob("result*.json"))
    if existing and not force:
        raise SystemExit(
            f"{page_dir} already contains API pages. "
            "Use --force to replace them, or --normalize-only to reuse them."
        )
    return page_dir


def write_page(page_dir: Path, page_number: int, payload: dict[str, Any], url: str) -> Path:
    output = page_dir / f"result{page_number:03d}.json"
    wrapped = {
        "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source_url": url,
        **payload,
    }
    output.write_text(json.dumps(wrapped, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return output


def fetch_pages(args: argparse.Namespace, token: str) -> dict[str, Any]:
    page_dir = prepare_page_dir(args.raw_dir, args.force)
    before_id = "*"
    seen_before_ids: set[str] = set()
    seen_activity_ids: set[str] = set()
    pages = 0
    activities = 0

    for page_number in range(1, args.max_pages + 1):
        if before_id in seen_before_ids:
            break
        seen_before_ids.add(before_id)

        url = page_url(args.api_url_template, before_id, args)
        payload = api_get_json(url, token)
        page_activities = activities_from_payload(payload)
        write_page(page_dir, page_number, payload, url)

        new_ids = [str(item.get("id")) for item in page_activities if item.get("id")]
        new_count = sum(1 for activity_id in new_ids if activity_id not in seen_activity_ids)
        seen_activity_ids.update(new_ids)
        pages = page_number
        activities += len(page_activities)

        if not page_activities:
            break
        next_id = next_before_id(payload, page_activities)
        if not next_id or next_id == before_id:
            break
        if new_count == 0 and next_id in seen_before_ids:
            break
        before_id = next_id
        if args.pause_seconds > 0:
            time.sleep(args.pause_seconds)

    return {
        "page_dir": workspace_path(page_dir),
        "pages_fetched": pages,
        "activity_references_seen": activities,
        "unique_activity_ids_seen": len(seen_activity_ids),
    }


def summarize_existing_pages(raw_dir: Path) -> dict[str, Any]:
    page_dir = raw_dir / "api_pages"
    pages = sorted(page_dir.glob("result*.json")) if page_dir.exists() else []
    activities = 0
    seen_activity_ids: set[str] = set()
    for path in pages:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        page_activities = activities_from_payload(payload)
        activities += len(page_activities)
        for activity in page_activities:
            activity_id = activity.get("id")
            if activity_id:
                seen_activity_ids.add(str(activity_id))
    return {
        "normalize_only": True,
        "page_dir": workspace_path(page_dir),
        "pages_available": len(pages),
        "activity_references_available": activities,
        "unique_activity_ids_available": len(seen_activity_ids),
    }


def main() -> int:
    args = parse_args()
    args.raw_dir.mkdir(parents=True, exist_ok=True)
    args.staged_dir.mkdir(parents=True, exist_ok=True)

    try:
        fetch_summary = (
            summarize_existing_pages(args.raw_dir)
            if args.normalize_only
            else fetch_pages(args, bearer_token(args))
        )

        normalize_summary = normalize_raw_dir(
            args.raw_dir,
            args.staged_dir,
            start_date=args.start_date,
            end_date=args.end_date,
            allow_empty=args.allow_empty,
            timezone=args.timezone,
        )
    except FileNotFoundError:
        # normalize_raw_dir already printed a helpful "no raw pages" message.
        return 2
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except requests.RequestException as exc:
        print(f"network error: {exc}", file=sys.stderr)
        return 1

    summary = {
        "pulled_at": utc_now_iso(),
        "raw_dir": workspace_path(args.raw_dir),
        "staged_dir": workspace_path(args.staged_dir),
        "fetch": fetch_summary,
        "normalize": normalize_summary,
    }
    write_json_file(NIKE_LAST_PULL_FILE, summary)
    print(f"Wrote {args.staged_dir / 'activities.csv'}")
    print(f"Wrote {NIKE_LAST_NORMALIZE_FILE}")
    print(f"Wrote {NIKE_LAST_PULL_FILE}")
    return 1 if normalize_summary.get("errors") else 0


if __name__ == "__main__":
    raise SystemExit(main())
