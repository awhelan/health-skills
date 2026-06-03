#!/usr/bin/env python3
"""Normalize Dexcom/Stelo Clarity CSV exports into one staged CGM table.

Clarity exports overlap heavily, so keeping every export wastes space and
forces consumers to dedup on read. This module extracts EGV readings from one
or more exports, maps them into the staged CGM schema, and deduplicates by
reading timestamp; re-normalizing the same export is a no-op.
"""

from __future__ import annotations

import argparse
from collections import Counter
import csv
import datetime as dt
import json
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(WORKSPACE_ROOT))

from healthdata.timeutil import resolve_timezone  # noqa: E402

TABLE_COLUMNS = ["timestamp", "glucose_mg_dl", "source_device", "transmitter_id"]
DEFAULT_TABLE = WORKSPACE_ROOT / "data/staged/dexcom_clarity/cgm_readings.csv"
DEFAULT_NORMALIZE_MANIFEST = WORKSPACE_ROOT / "data/manifests/ingestions/dexcom-clarity-last-normalize.json"

# Clarity exports timestamps as the account's local wall-clock with no offset.
# Tag them with this timezone so the staged table emits tz-aware ISO 8601 and
# joins cleanly against nike-run-club, which follows the same convention.
DEXCOM_TIMEZONE_ENV = "DEXCOM_CLARITY_TIMEZONE"
DEFAULT_TIMEZONE = "America/Los_Angeles"
DEFAULT_LOCAL_TZ = ZoneInfo(DEFAULT_TIMEZONE)
MG_DL_PER_MMOL_L = 18.0182
# Clarity writes "High"/"Low" markers for EGVs off the sensor's measurable scale.
GLUCOSE_HIGH_MG_DL = 400
GLUCOSE_LOW_MG_DL = 40
TIMESTAMP_FORMATS = ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M")


def _find_column(fieldnames: list[str], *needles: str) -> str | None:
    """Return the first header containing all needles, case-insensitively."""
    for name in fieldnames:
        lower = name.lower()
        if all(needle in lower for needle in needles):
            return name
    return None


def _find_header_index(lines: list[str]) -> int:
    for index, line in enumerate(lines):
        try:
            fields = next(csv.reader([line]))
        except csv.Error:
            continue
        if _find_column(fields, "timestamp") and _find_column(fields, "glucose value"):
            return index
    return 0


def _is_aware(value: dt.datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def parse_timestamp(timestamp: str) -> dt.datetime:
    """Parse a Clarity timestamp, preserving whether it already has an offset."""
    text = timestamp.strip()
    for fmt in TIMESTAMP_FORMATS:
        try:
            return dt.datetime.strptime(text, fmt)
        except ValueError:
            continue

    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError as exc:
        formats = "YYYY-MM-DD HH:MM[:SS], YYYY-MM-DDTHH:MM[:SS], or an ISO timestamp with offset"
        raise ValueError(f"Unrecognized Clarity timestamp {timestamp!r}; expected {formats}.") from exc
    if not _is_aware(parsed):
        raise ValueError(f"Unrecognized Clarity timestamp {timestamp!r}; expected a time component.")
    return parsed


def _is_ambiguous_wall_time(value: dt.datetime, local_tz: ZoneInfo) -> bool:
    first = value.replace(tzinfo=local_tz, fold=0)
    second = value.replace(tzinfo=local_tz, fold=1)
    return first.utcoffset() != second.utcoffset()


def _folds_for_timestamps(timestamps: list[dt.datetime], local_tz: ZoneInfo) -> list[int]:
    """Tag the second pass through a DST fall-back hour with fold=1.

    Clarity exports naive wall-clock times, so the repeated 01:00-01:59 hour at
    the fall-back transition would otherwise collide on the table key and drop
    the second pass. Giving the second occurrence fold=1 emits a distinct -08:00
    offset so both readings survive. We deliberately don't infer from the
    export's sort order which physical reading is PDT vs PST: either labeling
    preserves both rows, which is all the dedup key needs.
    """
    counts = Counter(value for value in timestamps if not _is_aware(value))
    seen: Counter[dt.datetime] = Counter()
    folds: list[int] = []
    for value in timestamps:
        if _is_aware(value) or counts[value] < 2 or not _is_ambiguous_wall_time(value, local_tz):
            folds.append(0)
            continue
        folds.append(min(seen[value], 1))
        seen[value] += 1
    return folds


def _timestamp_iso(value: dt.datetime, local_tz: ZoneInfo, fold: int = 0) -> str:
    if _is_aware(value):
        return value.isoformat()
    return value.replace(tzinfo=local_tz, fold=fold).isoformat()


def glucose_to_mg_dl(value: str, header: str) -> str:
    """Return the reading in mg/dL, clamping out-of-range markers and converting mmol/L.

    Clarity writes the literal ``High``/``Low`` markers (in either unit) for
    readings off the sensor's measurable scale; clamp them to the 40-400 mg/dL
    bounds so the staged ``glucose_mg_dl`` column is always numeric. mg/dL is the
    default, but a mmol/L locale export is detected from the header and converted.
    """
    marker = value.strip().lower()
    if marker == "high":
        return str(GLUCOSE_HIGH_MG_DL)
    if marker == "low":
        return str(GLUCOSE_LOW_MG_DL)
    if "mmol" not in header.lower():
        return value
    try:
        return str(round(float(value) * MG_DL_PER_MMOL_L))
    except ValueError as exc:
        raise ValueError(f"Cannot convert non-numeric mmol/L glucose value {value!r} to mg/dL.") from exc


def parse_clarity_readings(data: bytes | str, local_tz: ZoneInfo = DEFAULT_LOCAL_TZ) -> list[dict[str, str]]:
    """Extract normalized EGV readings from one Clarity CSV export."""
    text = data.decode("utf-8-sig", errors="replace") if isinstance(data, bytes) else data
    text = text.lstrip("\ufeff")
    lines = text.splitlines()
    reader = csv.DictReader(lines[_find_header_index(lines) :])
    fields = reader.fieldnames or []
    ts_col = _find_column(fields, "timestamp")
    glucose_col = _find_column(fields, "glucose value")
    event_col = _find_column(fields, "event", "type")
    device_col = _find_column(fields, "source device")
    transmitter_col = _find_column(fields, "transmitter", "id")
    if not ts_col or not glucose_col:
        missing = []
        if not ts_col:
            missing.append("Timestamp")
        if not glucose_col:
            missing.append("Glucose Value")
        raise ValueError(f"Clarity CSV is missing required column(s): {', '.join(missing)}")

    parsed_rows: list[tuple[dt.datetime, dict[str, str]]] = []
    for row in reader:
        if event_col and (row.get(event_col) or "").strip().upper() != "EGV":
            continue
        timestamp = (row.get(ts_col) or "").strip()
        glucose = (row.get(glucose_col) or "").strip()
        if not timestamp or not glucose:
            continue
        parsed_rows.append(
            (
                parse_timestamp(timestamp),
                {
                    "glucose_mg_dl": glucose_to_mg_dl(glucose, glucose_col),
                    "source_device": (row.get(device_col) or "").strip() if device_col else "",
                    "transmitter_id": (row.get(transmitter_col) or "").strip() if transmitter_col else "",
                },
            )
        )

    timestamps = [timestamp for timestamp, _ in parsed_rows]
    folds = _folds_for_timestamps(timestamps, local_tz)
    readings: list[dict[str, str]] = []
    for (timestamp, reading), fold in zip(parsed_rows, folds):
        readings.append(
            {
                "timestamp": _timestamp_iso(timestamp, local_tz, fold),
                **reading,
            }
        )
    return readings


def read_table(path: Path) -> dict[str, dict[str, str]]:
    """Load the staged CGM table keyed by timestamp."""
    if not path.exists():
        return {}
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        if "timestamp" not in (reader.fieldnames or []):
            raise ValueError(f"{path} is missing required 'timestamp' column")
        return {row["timestamp"]: row for row in reader if row.get("timestamp")}


def write_table(path: Path, rows_by_ts: dict[str, dict[str, str]]) -> None:
    """Write the table sorted by timestamp using an atomic replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TABLE_COLUMNS)
        writer.writeheader()
        for ts in sorted(rows_by_ts):
            writer.writerow({col: rows_by_ts[ts].get(col, "") for col in TABLE_COLUMNS})
    tmp.replace(path)


def normalize_readings(table_path: Path, readings: list[dict[str, str]]) -> tuple[int, int]:
    """Upsert readings into the table. Returns (added, total)."""
    table = read_table(table_path)
    added = 0
    for reading in readings:
        if reading["timestamp"] not in table:
            added += 1
        table[reading["timestamp"]] = reading
    write_table(table_path, table)
    return added, len(table)


def normalize_export(table_path: Path, data: bytes | str, local_tz: ZoneInfo = DEFAULT_LOCAL_TZ) -> tuple[int, int]:
    """Normalize one Clarity export into the staged CGM table."""
    return normalize_readings(table_path, parse_clarity_readings(data, local_tz))


def workspace_path(path: Path) -> str:
    """Render a path relative to the workspace root for manifests."""
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(WORKSPACE_ROOT))
    except ValueError:
        return str(resolved)


def write_json_file(path: Path, payload: object) -> Path:
    """Write pretty JSON with a trailing newline using an atomic replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def write_normalize_manifest(
    manifest_path: Path,
    table_path: Path,
    *,
    exports: int,
    added: int,
    total: int,
    timezone: str = DEFAULT_TIMEZONE,
) -> Path:
    """Write a last-normalize provenance manifest under data/manifests/ingestions/."""
    manifest = {
        "normalized_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "table": workspace_path(table_path),
        "timezone": timezone,
        "exports": exports,
        "readings_added": added,
        "readings_total": total,
    }
    return write_json_file(manifest_path, manifest)


def _is_export_file(path: Path, table_path: Path) -> bool:
    return path.suffix == ".csv" and path.resolve() != table_path.resolve()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Normalize Dexcom Clarity exports into one deduplicated CGM table.")
    parser.add_argument("exports", nargs="*", type=Path, help="Export CSVs to normalize; default: all *.csv beside the table.")
    parser.add_argument("--table", type=Path, default=DEFAULT_TABLE, help="Normalized CGM table path.")
    parser.add_argument("--prune", action="store_true", help="Delete source exports after successful normalization.")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_NORMALIZE_MANIFEST, help="Last-normalize manifest path.")
    parser.add_argument(
        "--timezone",
        default=os.environ.get(DEXCOM_TIMEZONE_ENV, DEFAULT_TIMEZONE),
        help=f"IANA timezone for tagging naive Clarity timestamps. Default: {DEXCOM_TIMEZONE_ENV} or {DEFAULT_TIMEZONE}.",
    )
    args = parser.parse_args(argv)

    try:
        local_tz = resolve_timezone(args.timezone, env_var=DEXCOM_TIMEZONE_ENV)
    except ValueError as exc:
        print(f"Normalize failed: {exc}", file=sys.stderr)
        return 1

    table_path = args.table
    exports = args.exports or [p for p in sorted(table_path.parent.glob("*.csv")) if _is_export_file(p, table_path)]
    if not exports:
        print(f"No export CSVs to normalize into {table_path}")
        return 0

    total = 0
    added_total = 0
    try:
        parsed_exports: list[tuple[Path, list[dict[str, str]]]] = []
        for export in exports:
            if not export.exists():
                print(f"Export not found: {export}", file=sys.stderr)
                return 2
            readings = parse_clarity_readings(export.read_bytes(), local_tz)
            if args.prune and not readings:
                print(f"Refusing to prune {export}: no parseable EGV readings found.", file=sys.stderr)
                return 1
            parsed_exports.append((export, readings))

        for export, readings in parsed_exports:
            added, total = normalize_readings(table_path, readings)
            added_total += added
            print(f"Normalized {export.name}: {len(readings)} parsed, +{added} new ({total} total)")

        if args.prune:
            for export in exports:
                export.unlink()
            print(f"Pruned {len(exports)} source export(s).")

        write_normalize_manifest(
            args.manifest,
            table_path,
            exports=len(exports),
            added=added_total,
            total=total,
            timezone=local_tz.key,
        )
    except OSError as exc:
        print(f"Normalize failed: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"Normalize failed: {exc}", file=sys.stderr)
        return 1

    print(f"{table_path} now holds {total} unique readings.")
    print(f"Wrote {args.manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
