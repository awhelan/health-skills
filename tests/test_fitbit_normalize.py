from __future__ import annotations

import csv
import importlib.util
import json
import os
import sys
import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "skills" / "fitbit" / "scripts"


def load_script(name: str):
    path = SCRIPT_DIR / f"{name}.py"
    sys.path.insert(0, str(SCRIPT_DIR))
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(SCRIPT_DIR))


normalize_fitbit = load_script("normalize_fitbit")
pull_fitbit = load_script("pull_fitbit")

START = date(2026, 5, 1)
END = date(2026, 5, 2)


def steps_payload() -> dict:
    return {"activities-steps": [
        {"dateTime": "2026-05-01", "value": "1000"},
        {"dateTime": "2026-05-02", "value": "2000"},
    ]}


def distance_payload() -> dict:
    return {"activities-distance": [{"dateTime": "2026-05-01", "value": "0.5"}]}


def azm_payload() -> dict:
    return {"activities-active-zone-minutes": [
        {"dateTime": "2026-05-01", "value": {
            "activeZoneMinutes": 30, "fatBurnActiveZoneMinutes": 20,
            "cardioActiveZoneMinutes": 10, "peakActiveZoneMinutes": 0,
        }},
    ]}


def heart_payload() -> dict:
    return {"activities-heart": [
        {"dateTime": "2026-05-01", "value": {
            "restingHeartRate": 55,
            "heartRateZones": [{"name": "Fat Burn", "minutes": 40}],
        }},
    ]}


def hrv_payload() -> dict:
    return {"hrv": [{"dateTime": "2026-05-01", "value": {"dailyRmssd": 45, "deepRmssd": 50}}]}


def sleep_payload() -> dict:
    return {"2026-05-01": {
        "summary": {"totalMinutesAsleep": 420, "totalTimeInBed": 450, "totalSleepRecords": 1},
        "sleep": [{"isMainSleep": True, "efficiency": 90, "levels": {"summary": {
            "deep": {"minutes": 60, "count": 3},
            "light": {"minutes": 200, "count": 10},
            "rem": {"minutes": 100, "count": 5},
            "wake": {"minutes": 40, "count": 8},
        }}}],
    }}


def activity_logs_payload() -> dict:
    return {"pages": [{"activities": [{
        "logId": 111,
        "activityName": "Run",
        "logType": "tracker",
        "startTime": "2026-05-01T08:00:00.000-07:00",
        "duration": 1_800_000,
        "calories": 300,
        "steps": 4000,
        "distance": 3.0,
        "distanceUnit": "mi",
        "averageHeartRate": 150,
        "heartRateZones": [{"name": "Cardio", "minutes": 20}],
        "activeZoneMinutes": {"totalMinutes": 25},
        "tcxLink": "https://api.fitbit.com/1/user/-/activities/111.tcx",
    }]}]}


def tcx_manifest_payload() -> dict:
    return {"files": [{
        "date": "2026-05-01", "log_id": "111", "activity_name": "Run",
        "start_time": "2026-05-01T08:00:00.000-07:00",
        "tcx_path": "data/raw/fitbit_api/run=2026-05-01_2026-05-02/tcx/x.tcx",
        "source_url": "http://x", "status": "written",
    }], "errors": []}


def full_payloads() -> dict:
    return {
        "steps": steps_payload(),
        "distance": distance_payload(),
        "activity_summary": {},
        "azm": azm_payload(),
        "heart": heart_payload(),
        "hrv": hrv_payload(),
        "sleep": sleep_payload(),
        "health_metrics": {},
        "activity_logs": activity_logs_payload(),
        "tcx_manifest": tcx_manifest_payload(),
    }


class FitbitNormalizeTests(unittest.TestCase):
    def test_normalize_activity_logs_extracts_log_and_daily(self) -> None:
        logs, daily = normalize_fitbit.normalize_activity_logs(activity_logs_payload(), START, END)
        self.assertEqual(len(logs), 1)
        self.assertEqual(str(logs.iloc[0]["log_id"]), "111")
        self.assertAlmostEqual(logs.iloc[0]["duration_minutes"], 30.0)
        self.assertEqual(daily.iloc[0]["exercise_count"], 1)

    def test_normalize_daily_builds_row_per_date_with_metrics(self) -> None:
        _, exercise_daily = normalize_fitbit.normalize_activity_logs(activity_logs_payload(), START, END)
        df = normalize_fitbit.normalize_daily(
            START, END, steps_payload(), distance_payload(), azm_payload(),
            heart_payload(), hrv_payload(), sleep_payload(), exercise_daily,
        )
        by_date = {row["date"]: row for _, row in df.iterrows()}
        self.assertEqual(set(by_date), {"2026-05-01", "2026-05-02"})
        day1 = by_date["2026-05-01"]
        self.assertEqual(day1["daily_steps"], 1000)
        self.assertEqual(day1["resting_heart_rate"], 55)
        self.assertEqual(day1["minutes_asleep"], 420)
        self.assertEqual(day1["daily_hrv_ms"], 45)
        self.assertEqual(day1["active_zone_minutes"], 30)
        self.assertEqual(day1["exercise_count"], 1)
        self.assertEqual(by_date["2026-05-02"]["daily_steps"], 2000)

    def test_normalize_and_stage_writes_tables_and_manifest(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            staged = base / "staged"
            manifest_path = base / "manifest.json"
            manifest = normalize_fitbit.normalize_and_stage(
                staged, START, END, full_payloads(), manifest_path=manifest_path,
            )
            with (staged / "daily_metrics.csv").open(newline="", encoding="utf-8") as handle:
                daily_rows = list(csv.DictReader(handle))
            with (staged / "activity_logs.csv").open(newline="", encoding="utf-8") as handle:
                log_rows = list(csv.DictReader(handle))
            with (staged / "activity_tcx.csv").open(newline="", encoding="utf-8") as handle:
                tcx_rows = list(csv.DictReader(handle))
            manifest_exists = manifest_path.exists()

        self.assertTrue(manifest_exists)
        self.assertEqual(manifest["daily_rows_total"], 2)
        self.assertEqual(len(daily_rows), 2)
        self.assertEqual(str(log_rows[0]["log_id"]), "111")
        self.assertEqual(str(tcx_rows[0]["log_id"]), "111")

    def test_normalize_raw_dir_round_trip(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            run_dir = base / "raw" / "run=2026-05-01_2026-05-02"
            run_dir.mkdir(parents=True)
            files = {
                "steps": steps_payload(),
                "distance": distance_payload(),
                "active_zone_minutes": azm_payload(),
                "heart": heart_payload(),
                "hrv": hrv_payload(),
                "sleep": sleep_payload(),
                "activity_logs": activity_logs_payload(),
                "tcx_manifest": tcx_manifest_payload(),
            }
            for name, payload in files.items():
                (run_dir / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")

            staged = base / "staged"
            manifest = normalize_fitbit.normalize_raw_dir(
                base / "raw", staged, manifest_path=base / "manifest.json",
            )
            with (staged / "daily_metrics.csv").open(newline="", encoding="utf-8") as handle:
                daily_rows = list(csv.DictReader(handle))

        self.assertEqual(manifest["run_dirs"], 1)
        self.assertEqual(len(daily_rows), 2)
        self.assertEqual(manifest["activity_logs_total"], 1)

    def test_merge_csv_dedupes_on_key_keep_last(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "daily_metrics.csv"
            normalize_fitbit.merge_csv(path, pd.DataFrame([{"date": "2026-05-01", "daily_steps": 1}]), "date")
            merged = normalize_fitbit.merge_csv(
                path, pd.DataFrame([{"date": "2026-05-01", "daily_steps": 999}]), "date"
            )
        self.assertEqual(len(merged), 1)
        self.assertEqual(int(merged.iloc[0]["daily_steps"]), 999)

    def test_api_daily_merge_preserves_takeout_fields_and_source(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            staged = base / "staged"
            staged.mkdir()
            pd.DataFrame(
                [
                    {
                        "date": "2026-05-01",
                        "daily_steps": 1111,
                        "stress_score": 40,
                        "source": "takeout",
                    }
                ]
            ).to_csv(staged / "daily_metrics.csv", index=False)

            normalize_fitbit.normalize_and_stage(
                staged,
                START,
                START,
                {
                    "steps": {"activities-steps": [{"dateTime": "2026-05-01", "value": "2000"}]},
                    "distance": {"activities-distance": [{"dateTime": "2026-05-01", "value": "2.5"}]},
                    "activity_summary": {},
                    "azm": {},
                    "heart": {},
                    "hrv": {},
                    "sleep": {},
                    "health_metrics": {},
                    "activity_logs": {"pages": [{"activities": []}]},
                    "tcx_manifest": {"files": [], "errors": []},
                },
                manifest_path=None,
            )
            daily = pd.read_csv(staged / "daily_metrics.csv")

        row = daily.iloc[0]
        self.assertEqual(int(row["daily_steps"]), 1111)
        self.assertEqual(float(row["daily_miles"]), 2.5)
        self.assertEqual(float(row["stress_score"]), 40.0)
        self.assertEqual(row["source"], "api+takeout")

    def test_refetched_activity_window_removes_stale_logs_and_tcx(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            staged = base / "staged"
            normalize_fitbit.normalize_and_stage(staged, START, END, full_payloads(), manifest_path=None)

            no_activity_payloads = {
                **full_payloads(),
                "activity_logs": {"pages": [{"activities": []}]},
                "tcx_manifest": {"files": [], "errors": []},
            }
            manifest = normalize_fitbit.normalize_and_stage(
                staged, START, END, no_activity_payloads, manifest_path=None,
            )
            with (staged / "activity_logs.csv").open(newline="", encoding="utf-8") as handle:
                log_rows = list(csv.DictReader(handle))
            with (staged / "activity_tcx.csv").open(newline="", encoding="utf-8") as handle:
                tcx_rows = list(csv.DictReader(handle))
            daily = pd.read_csv(staged / "daily_metrics.csv")

        self.assertEqual(manifest["activity_logs_total"], 0)
        self.assertEqual(manifest["activity_tcx_total"], 0)
        self.assertEqual(log_rows, [])
        self.assertEqual(tcx_rows, [])
        self.assertEqual(int(daily.loc[daily["date"] == "2026-05-01", "exercise_count"].iloc[0]), 0)

    def test_skipped_tcx_manifest_does_not_remove_existing_tcx_rows(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            staged = base / "staged"
            normalize_fitbit.normalize_and_stage(staged, START, END, full_payloads(), manifest_path=None)

            skipped_tcx_payloads = {
                **full_payloads(),
                "activity_logs": {"pages": [{"activities": []}]},
                "tcx_manifest": {"files": [], "errors": [], "skipped": True},
            }
            manifest = normalize_fitbit.normalize_and_stage(
                staged, START, END, skipped_tcx_payloads, manifest_path=None,
            )
            with (staged / "activity_tcx.csv").open(newline="", encoding="utf-8") as handle:
                tcx_rows = list(csv.DictReader(handle))

        self.assertEqual(manifest["activity_logs_total"], 0)
        self.assertEqual(manifest["activity_tcx_total"], 1)
        self.assertEqual(str(tcx_rows[0]["log_id"]), "111")


class FitbitPullTests(unittest.TestCase):
    def test_chunks_splits_by_max_days(self) -> None:
        self.assertEqual(
            pull_fitbit.chunks(date(2026, 1, 1), date(2026, 1, 3), 2),
            [(date(2026, 1, 1), date(2026, 1, 2)), (date(2026, 1, 3), date(2026, 1, 3))],
        )

    def test_refresh_token_wrapper_is_exposed(self) -> None:
        # The Fitbit/Polar sample calibration imports these names.
        self.assertTrue(callable(pull_fitbit.refresh_token))
        self.assertTrue(callable(pull_fitbit.configured_client_secret))
        self.assertTrue(callable(pull_fitbit.api_get))
        self.assertTrue(callable(pull_fitbit.resolve_client_id))

    def test_resolve_client_id_requires_configured_or_stamped_id(self) -> None:
        existing = os.environ.pop("FITBIT_CLIENT_ID", None)
        try:
            self.assertEqual(pull_fitbit.resolve_client_id("client-1", {}), "client-1")
            self.assertEqual(pull_fitbit.resolve_client_id(None, {"client_id": "client-2"}), "client-2")
            with self.assertRaisesRegex(ValueError, "Fitbit client id is required"):
                pull_fitbit.resolve_client_id(None, {})
        finally:
            if existing is not None:
                os.environ["FITBIT_CLIENT_ID"] = existing

    def test_fetch_activity_tcx_writes_under_raw_run_and_uses_portable_manifest_path(self) -> None:
        with TemporaryDirectory() as tmp:
            raw_run = Path(tmp) / "raw" / "fitbit_api" / "run=2026-05-01_2026-05-02"
            old_api_get_text = pull_fitbit.api_get_text
            old_workspace_path = pull_fitbit.workspace_path
            pull_fitbit.api_get_text = lambda *_args, **_kwargs: "<tcx />"
            pull_fitbit.workspace_path = lambda path: f"data/raw/fitbit_api/{Path(path).name}"
            try:
                manifest = pull_fitbit.fetch_activity_tcx(
                    "access-token",
                    activity_logs_payload(),
                    START,
                    END,
                    raw_run / "tcx",
                )
            finally:
                pull_fitbit.api_get_text = old_api_get_text
                pull_fitbit.workspace_path = old_workspace_path

            files = list((raw_run / "tcx").glob("*.tcx"))

        self.assertEqual(len(files), 1)
        self.assertEqual(len(manifest["files"]), 1)
        self.assertFalse(Path(manifest["files"][0]["tcx_path"]).is_absolute())
        self.assertTrue(manifest["files"][0]["tcx_path"].startswith("data/raw/fitbit_api/"))


if __name__ == "__main__":
    unittest.main()
