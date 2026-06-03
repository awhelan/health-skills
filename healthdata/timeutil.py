"""Timezone and date-window helpers shared by provider normalizers."""

from __future__ import annotations

import datetime as dt
import os
from zoneinfo import ZoneInfo

from healthdata.config import DEFAULT_LOCAL_TIMEZONE


def resolve_timezone(
    name: str | ZoneInfo | None,
    *,
    env_var: str | None = None,
    default: str = DEFAULT_LOCAL_TIMEZONE,
) -> ZoneInfo:
    """Resolve an IANA name (else ``env_var``, else ``default``) to a ZoneInfo."""
    if isinstance(name, ZoneInfo):
        return name
    value = (name or (os.environ.get(env_var) if env_var else None) or default).strip()
    try:
        return ZoneInfo(value)
    except Exception as exc:
        raise ValueError(f"Unknown timezone {value!r}; use an IANA name like 'America/Los_Angeles'.") from exc


def validate_date_arg(value: str | None, name: str) -> str | None:
    """Validate a single YYYY-MM-DD argument, returning the normalized ISO date."""
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError(f"{name} must be YYYY-MM-DD, got {value!r}") from exc


def validate_date_window(start_date: str | None, end_date: str | None) -> tuple[str | None, str | None]:
    """Validate a --start-date/--end-date window, enforcing start <= end."""
    start = validate_date_arg(start_date, "--start-date")
    end = validate_date_arg(end_date, "--end-date")
    if start and end and start > end:
        raise ValueError(f"--start-date must be on or before --end-date, got {start!r} > {end!r}")
    return start, end
