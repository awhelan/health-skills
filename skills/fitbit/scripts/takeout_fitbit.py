#!/usr/bin/env python3
"""Ingest the Google Takeout Fitbit export into the staged tables.

Takeout is the deep, more-complete historical source (years of daily metrics,
full exercise history, 1-second HR with PPG confidence). The Web API pull only
covers a recent window. This module promotes the Takeout parsing that used to
live in `fitbit_takeout_analysis.py` into a real normalizer that **field-unions
Takeout into the same `data/staged/fitbit/` tables** the API writes.

Phase-2 status: daily_metrics backfill is implemented here. Activities (fuzzy
Takeout↔API match) and activity-window `hr_samples` are the next slices.

Merge model: keyed on `date`, Takeout-preferred — Takeout values win on overlap,
the API fills recent dates Takeout lacks, and Takeout-only columns (stress,
sleep score, …) come in as new fields. A stable `source` column records origin.
"""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
from typing import Iterable

import pandas as pd

from healthdata.config import (
    DEFAULT_LOCAL_TIMEZONE,
    FITBIT_STAGED_DIR,
    FITBIT_TAKEOUT_DIR,
    workspace_path,
)
from healthdata.io import utc_now_iso, write_json_file

LOCAL_TZ = DEFAULT_LOCAL_TIMEZONE
TAKEOUT_LAST_NORMALIZE_FILE = (
    Path(__file__).resolve().parents[3]
    / "data" / "manifests" / "ingestions" / "fitbit-takeout-last-normalize.json"
)


def _to_date_str(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, errors="coerce").dt.date.astype("string")


def _read_time_series_sum(root: Path, pattern: str, value_columns: list[str]) -> pd.DataFrame:
    """Sum a GoogleData minute/aggregate CSV series to local-date totals."""
    frames: list[pd.DataFrame] = []
    for path in sorted(root.glob(pattern)):
        df = pd.read_csv(path, usecols=["timestamp", *value_columns])
        if df.empty:
            continue
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
        df = df.dropna(subset=["timestamp"])
        df["date"] = df["timestamp"].dt.tz_convert(LOCAL_TZ).dt.date
        numeric = df[value_columns].apply(pd.to_numeric, errors="coerce").fillna(0)
        frames.append(pd.concat([df[["date"]], numeric], axis=1).groupby("date", as_index=False).sum())
    if not frames:
        return pd.DataFrame(columns=["date", *value_columns])
    merged = pd.concat(frames, ignore_index=True).groupby("date", as_index=False).sum()
    merged["date"] = merged["date"].astype("string")
    return merged


def load_steps(root: Path) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for path in sorted(root.glob("Global Export Data/steps-*.json")):
        try:
            data = pd.read_json(path)
        except ValueError:
            continue
        if data.empty:
            continue
        data["dateTime"] = pd.to_datetime(data["dateTime"], format="%m/%d/%y %H:%M:%S", errors="coerce")
        data["daily_steps"] = pd.to_numeric(data["value"], errors="coerce")
        data = data.dropna(subset=["dateTime", "daily_steps"])
        data["date"] = data["dateTime"].dt.date
        frames.append(data.groupby("date", as_index=False)["daily_steps"].sum())
    if not frames:
        return pd.DataFrame(columns=["date", "daily_steps"])
    merged = pd.concat(frames, ignore_index=True).groupby("date", as_index=False)["daily_steps"].sum()
    merged["date"] = merged["date"].astype("string")
    return merged


def load_calories(root: Path) -> pd.DataFrame:
    return _read_time_series_sum(root, "Physical Activity_GoogleData/calories_[0-9]*.csv", ["calories"]).rename(
        columns={"calories": "daily_calories"}
    )


def load_active_minutes(root: Path) -> pd.DataFrame:
    df = _read_time_series_sum(
        root, "Physical Activity_GoogleData/active_minutes_*.csv", ["light", "moderate", "very"]
    )
    return df.rename(
        columns={
            "light": "lightly_active_minutes",
            "moderate": "moderately_active_minutes",
            "very": "very_active_minutes",
        }
    )


def load_active_zone_minutes(root: Path) -> pd.DataFrame:
    return _read_time_series_sum(
        root, "Physical Activity_GoogleData/active_zone_minutes_*.csv", ["total minutes"]
    ).rename(columns={"total minutes": "active_zone_minutes"})


def load_hrv(root: Path) -> pd.DataFrame:
    col = "root mean square of successive differences milliseconds"
    frames: list[pd.DataFrame] = []
    for path in sorted(root.glob("Physical Activity_GoogleData/heart_rate_variability_*.csv")):
        df = pd.read_csv(path, usecols=["timestamp", col])
        if df.empty:
            continue
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
        df = df.dropna(subset=["timestamp"])
        df["date"] = df["timestamp"].dt.tz_convert(LOCAL_TZ).dt.date
        df["daily_hrv_ms"] = pd.to_numeric(df[col], errors="coerce")
        frames.append(df.groupby("date", as_index=False)["daily_hrv_ms"].median())
    if not frames:
        return pd.DataFrame(columns=["date", "daily_hrv_ms"])
    merged = pd.concat(frames, ignore_index=True).groupby("date", as_index=False)["daily_hrv_ms"].median()
    merged["date"] = merged["date"].astype("string")
    return merged


def load_stress(root: Path) -> pd.DataFrame:
    path = root / "Stress Score" / "Stress Score.csv"
    if not path.exists():
        return pd.DataFrame(columns=["date", "stress_score"])
    df = pd.read_csv(path)
    df = df[df["CALCULATION_FAILED"].astype(str).str.lower() != "true"].copy()
    df["date"] = _to_date_str(df["DATE"])
    df["stress_score"] = pd.to_numeric(df["STRESS_SCORE"], errors="coerce")
    df = df[["date", "stress_score"]].dropna()
    return df.groupby("date", as_index=False).mean(numeric_only=True)


def _latest(root: Path, pattern: str) -> Path | None:
    matches = sorted(root.glob(pattern))
    return matches[-1] if matches else None


def load_sleep(root: Path) -> pd.DataFrame:
    """Daily sleep from Takeout: minutes from sessions, score + resting HR from scores."""
    out: pd.DataFrame | None = None

    sessions_path = _latest(root, "Health Fitness Data_GoogleData/UserSleeps_*.csv")
    if sessions_path is not None:
        df = pd.read_csv(sessions_path)
        df = df[df["sleep_type"].isin(["STAGES", "CLASSIC"])].copy()
        df["date"] = _to_date_str(pd.to_datetime(df["sleep_end"], utc=True, errors="coerce").dt.tz_convert(LOCAL_TZ))
        for src, dst in [("minutes_asleep", "minutes_asleep"), ("minutes_in_sleep_period", "minutes_in_bed")]:
            df[dst] = pd.to_numeric(df[src], errors="coerce")
        out = df.dropna(subset=["date"]).groupby("date", as_index=False)[["minutes_asleep", "minutes_in_bed"]].mean()

    scores_path = _latest(root, "Health Fitness Data_GoogleData/UserSleepScores_*.csv")
    if scores_path is not None:
        df = pd.read_csv(scores_path)
        df["date"] = _to_date_str(pd.to_datetime(df["score_time"], utc=True, errors="coerce").dt.tz_convert(LOCAL_TZ))
        df["sleep_score"] = pd.to_numeric(df["overall_score"], errors="coerce")
        df["resting_heart_rate"] = pd.to_numeric(df["resting_heart_rate"], errors="coerce")
        scores = df.dropna(subset=["date"]).groupby("date", as_index=False)[["sleep_score", "resting_heart_rate"]].mean()
        out = scores if out is None else out.merge(scores, on="date", how="outer")

    return out if out is not None else pd.DataFrame(columns=["date"])


def build_takeout_daily(root: Path) -> pd.DataFrame:
    """Outer-join every Takeout daily source into one date-keyed frame."""
    frames: Iterable[pd.DataFrame] = [
        load_steps(root),
        load_calories(root),
        load_active_minutes(root),
        load_active_zone_minutes(root),
        load_hrv(root),
        load_stress(root),
        load_sleep(root),
    ]
    merged: pd.DataFrame | None = None
    for frame in frames:
        if frame.empty or "date" not in frame.columns:
            continue
        frame = frame.copy()
        frame["date"] = frame["date"].astype("string")
        merged = frame if merged is None else merged.merge(frame, on="date", how="outer")
    if merged is None:
        return pd.DataFrame(columns=["date"])
    return merged.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)


def _source_sets(df: pd.DataFrame, default: str) -> dict[str, set[str]]:
    sources: dict[str, set[str]] = {}
    has_col = "source" in df.columns
    for _, row in df.iterrows():
        key = str(row["date"])
        raw = str(row["source"]) if has_col and pd.notna(row.get("source")) else default
        sources[key] = {part for part in raw.split("+") if part}
    return sources


def union_daily(staged: pd.DataFrame, takeout: pd.DataFrame) -> pd.DataFrame:
    """Field-union two date-keyed daily frames, Takeout-preferred, with provenance.

    Idempotent: re-running over an already-unioned staged frame reproduces the
    same values and `source` labels.
    """
    if takeout.empty:
        return staged
    api_src = _source_sets(staged, "api")
    tk_src = _source_sets(takeout, "takeout")

    api = staged.drop(columns=["source"], errors="ignore").set_index("date")
    tk = takeout.drop(columns=["source"], errors="ignore").set_index("date")
    combined = tk.combine_first(api)  # Takeout wins on overlap, API fills the rest

    sources: list[str] = []
    for key in combined.index.astype(str):
        merged = set(api_src.get(key, set())) | set(tk_src.get(key, set()))
        sources.append("+".join(sorted(merged)) if merged else "")
    combined["source"] = sources
    return combined.reset_index().rename(columns={"index": "date"}).sort_values("date").reset_index(drop=True)


def stage_takeout_daily(
    staged_dir: str | Path,
    takeout_root: str | Path = FITBIT_TAKEOUT_DIR,
    *,
    manifest_path: Path | None = TAKEOUT_LAST_NORMALIZE_FILE,
) -> dict[str, object]:
    """Backfill the Takeout daily history into `daily_metrics.csv`."""
    out_dir = Path(staged_dir)
    table = out_dir / "daily_metrics.csv"
    staged = pd.read_csv(table) if table.exists() else pd.DataFrame(columns=["date"])
    takeout = build_takeout_daily(Path(takeout_root))

    combined = union_daily(staged, takeout)
    out_dir.mkdir(parents=True, exist_ok=True)
    combined.to_csv(table, index=False)

    dates = combined["date"].dropna().astype(str)
    manifest = {
        "normalized_at": utc_now_iso(),
        "takeout_root": workspace_path(Path(takeout_root)),
        "takeout_days": int(len(takeout)),
        "daily_rows_total": int(len(combined)),
        "date_min": dates.min() if not dates.empty else "",
        "date_max": dates.max() if not dates.empty else "",
        "source_breakdown": combined.get("source", pd.Series(dtype=str)).value_counts().to_dict(),
        "daily_metrics_path": workspace_path(table),
    }
    if manifest_path is not None:
        write_json_file(manifest_path, manifest)
    return manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Field-union the Google Takeout Fitbit daily history into data/staged/fitbit."
    )
    parser.add_argument("--takeout-root", type=Path, default=FITBIT_TAKEOUT_DIR)
    parser.add_argument("--staged-dir", type=Path, default=FITBIT_STAGED_DIR)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    manifest = stage_takeout_daily(args.staged_dir, args.takeout_root)
    print(f"Wrote {args.staged_dir / 'daily_metrics.csv'}")
    print(
        f"Daily rows: {manifest['daily_rows_total']} total "
        f"({manifest['date_min']}..{manifest['date_max']}); by source: {manifest['source_breakdown']}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
