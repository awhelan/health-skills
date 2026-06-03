from __future__ import annotations

import csv
import io
import importlib.util
import json
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "skills" / "polar-accesslink" / "scripts"


def load_script(name: str):
    path = SCRIPT_DIR / f"{name}.py"
    sys.path.insert(0, str(SCRIPT_DIR))
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(SCRIPT_DIR))


normalize_polar = load_script("normalize_polar_accesslink")
pull_polar = load_script("pull_polar_accesslink")


SUMMARY_SESSION = {
    "identifier": "sess-1",
    "name": "Running session",
    "startTime": "2026-05-01T08:00:00.000+00:00",
    "distanceMeters": 1609.344,
    "durationMillis": 600000,
    "calories": 100,
    "hrAvg": 150,
    "hrMax": 170,
}

DETAIL_SESSION = {
    "identifier": "sess-1",
    "name": "Running session",
    "startTime": "2026-05-01T08:00:00+00:00",
    "exercises": [
        {
            "identifier": "ex-1",
            "startTime": "2026-05-01T08:00:00+00:00",
            "distanceMeters": 1609.344,
            "durationMillis": 600000,
            "hrAvg": 150,
            "hrMax": 170,
            "samples": {
                "samples": [
                    {"type": "HEART_RATE", "intervalMillis": 1000, "values": [140, 150, 160]}
                ]
            },
            "zones": [
                {
                    "type": "hr",
                    "zones": [
                        {"lowerLimit": 100, "higherLimit": 120, "inZone": 60000, "distanceMeters": 200}
                    ],
                }
            ],
            "laps": {
                "laps": [{"durationMillis": 300000, "distanceMeters": 804.672}],
                "autoLaps": [],
            },
        }
    ],
}


def summary_payload() -> dict:
    return {"trainingSessions": [SUMMARY_SESSION]}


def detail_payload(
    *,
    hr_values: list[int] | None = None,
    include_zones: bool = True,
    include_laps: bool = True,
) -> dict:
    session = json.loads(json.dumps(DETAIL_SESSION))
    exercise = session["exercises"][0]
    exercise["samples"]["samples"][0]["values"] = hr_values or [140, 150, 160]
    if not include_zones:
        exercise["zones"] = []
    if not include_laps:
        exercise["laps"] = {"laps": [], "autoLaps": []}
    return {"2026-05-01": {"trainingSessions": [session]}}


class PolarNormalizeTests(unittest.TestCase):
    def test_normalize_sessions_merges_summary_and_detail(self) -> None:
        df = normalize_polar.normalize_sessions(summary_payload(), detail_payload())
        self.assertEqual(len(df), 1)
        row = df.iloc[0]
        self.assertEqual(row["session_id"], "sess-1")
        self.assertEqual(row["exercise_id"], "ex-1")
        self.assertEqual(row["name"], "Running session")
        self.assertEqual(row["date"], "2026-05-01")
        self.assertAlmostEqual(row["duration_minutes"], 10.0)
        self.assertAlmostEqual(row["distance_miles"], 1.0, places=3)
        self.assertAlmostEqual(row["pace_min_per_mile"], 10.0, places=2)
        self.assertEqual(row["hr_avg"], 150)

    def test_normalize_hr_samples_expands_per_second(self) -> None:
        df = normalize_polar.normalize_hr_samples(detail_payload())
        self.assertEqual(len(df), 3)
        self.assertEqual(list(df["offset_seconds"]), [0.0, 1.0, 2.0])
        self.assertEqual(list(df["heart_rate"]), [140.0, 150.0, 160.0])
        # naive local clock time (UTC 08:00 -> America/Los_Angeles 01:00)
        self.assertTrue(df.iloc[0]["timestamp"].startswith("2026-05-01T01:00:00"))

    def test_normalize_zones_converts_minutes(self) -> None:
        df = normalize_polar.normalize_zones(detail_payload())
        self.assertEqual(len(df), 1)
        row = df.iloc[0]
        self.assertEqual(row["zone_type"], "hr")
        self.assertEqual(row["zone_index"], 1)
        self.assertAlmostEqual(row["in_zone_minutes"], 1.0)

    def test_normalize_laps_reads_manual_and_auto(self) -> None:
        df = normalize_polar.normalize_laps(detail_payload())
        self.assertEqual(len(df), 1)
        row = df.iloc[0]
        self.assertEqual(row["lap_type"], "laps")
        self.assertEqual(row["lap_index"], 1)
        self.assertAlmostEqual(row["duration_minutes"], 5.0)
        self.assertAlmostEqual(row["distance_miles"], 0.5, places=3)

    def _write_run(
        self,
        raw_dir: Path,
        *,
        run_name: str = "run=2026-05-01_2026-05-02",
        details: dict | None = None,
    ) -> Path:
        run_dir = raw_dir / run_name
        run_dir.mkdir(parents=True)
        (run_dir / "training_summary.json").write_text(json.dumps(summary_payload()), encoding="utf-8")
        (run_dir / "training_details.json").write_text(
            json.dumps(details or detail_payload()),
            encoding="utf-8",
        )
        return run_dir

    def test_normalize_raw_dir_round_trip(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            raw_dir = base / "raw"
            out_dir = base / "staged"
            manifest_path = base / "manifest.json"
            self._write_run(raw_dir)

            manifest = normalize_polar.normalize_raw_dir(raw_dir, out_dir, manifest_path=manifest_path)
            with (out_dir / "training_sessions.csv").open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))

            self.assertTrue(manifest_path.exists())
            self.assertEqual(manifest["sessions_total"], 1)
            self.assertEqual(manifest["hr_samples_total"], 3)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["exercise_id"], "ex-1")

    def test_normalize_raw_dir_is_idempotent(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            raw_dir = base / "raw"
            out_dir = base / "staged"
            manifest_path = base / "manifest.json"
            self._write_run(raw_dir)

            normalize_polar.normalize_raw_dir(raw_dir, out_dir, manifest_path=manifest_path)
            second = normalize_polar.normalize_raw_dir(raw_dir, out_dir, manifest_path=manifest_path)

        self.assertEqual(second["sessions_total"], 1)
        self.assertEqual(second["hr_samples_total"], 3)

    def test_refetched_details_replace_stale_child_rows(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            raw_dir = base / "raw"
            out_dir = base / "staged"
            manifest_path = base / "manifest.json"
            first_run = self._write_run(raw_dir, run_name="run=2026-05-01_2026-05-02")

            normalize_polar.normalize_raw_dir(first_run, out_dir, manifest_path=manifest_path)
            second_run = self._write_run(
                raw_dir,
                run_name="run=2026-05-02_2026-05-03",
                details=detail_payload(hr_values=[141], include_zones=False, include_laps=False),
            )
            manifest = normalize_polar.normalize_raw_dir(second_run, out_dir, manifest_path=manifest_path)

            with (out_dir / "hr_samples.csv").open(newline="", encoding="utf-8") as handle:
                hr_rows = list(csv.DictReader(handle))
            with (out_dir / "zones.csv").open(newline="", encoding="utf-8") as handle:
                zone_rows = list(csv.DictReader(handle))
            with (out_dir / "laps.csv").open(newline="", encoding="utf-8") as handle:
                lap_rows = list(csv.DictReader(handle))

        self.assertEqual(manifest["hr_samples_total"], 1)
        self.assertEqual([row["heart_rate"] for row in hr_rows], ["141.0"])
        self.assertEqual(zone_rows, [])
        self.assertEqual(lap_rows, [])

    def test_run_dirs_accepts_single_run_directory(self) -> None:
        with TemporaryDirectory() as tmp:
            raw_dir = Path(tmp) / "raw"
            self._write_run(raw_dir)
            run_dir = raw_dir / "run=2026-05-01_2026-05-02"

            self.assertEqual(normalize_polar.run_dirs(run_dir), [run_dir])
            self.assertEqual(normalize_polar.run_dirs(raw_dir), [run_dir])


class PolarPullTests(unittest.TestCase):
    def test_chunk_ranges_splits_by_max_days(self) -> None:
        ranges = pull_polar.chunk_ranges(date(2026, 1, 1), date(2026, 1, 5), 2)
        self.assertEqual(
            ranges,
            [
                (date(2026, 1, 1), date(2026, 1, 2)),
                (date(2026, 1, 3), date(2026, 1, 4)),
                (date(2026, 1, 5), date(2026, 1, 5)),
            ],
        )

    def test_token_needs_refresh_when_expired_or_forced(self) -> None:
        self.assertTrue(pull_polar.token_needs_refresh({"expires_at": 0}))
        self.assertTrue(pull_polar.token_needs_refresh({"expires_at": 9_999_999_999}, force=True))
        self.assertFalse(pull_polar.token_needs_refresh({"expires_at": 9_999_999_999}))

    def test_api_datetime_emits_plain_seconds(self) -> None:
        self.assertEqual(pull_polar.api_datetime(date(2026, 5, 1)), "2026-05-01T00:00:00")

    def test_pull_recent_accepts_fresh_token_without_client_secret(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            token_path = base / "tokens.json"
            token_path.write_text(
                json.dumps(
                    {
                        "access_token": "fresh-access-token",
                        "refresh_token": "refresh-token",
                        "client_id": "client-id",
                        "expires_at": int(time.time()) + 3600,
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch.object(pull_polar, "configured_client_secret", return_value=None),
                patch.object(pull_polar, "_client_secret", side_effect=AssertionError("prompted for secret")),
                patch.object(pull_polar, "fetch_training_summary", return_value=summary_payload()),
                patch.object(pull_polar, "fetch_training_details", return_value=detail_payload()),
                patch.object(pull_polar, "POLAR_LAST_PULL_FILE", base / "last-pull.json"),
                patch.object(pull_polar, "POLAR_LAST_NORMALIZE_FILE", base / "last-normalize.json"),
                redirect_stdout(io.StringIO()),
            ):
                summary = pull_polar.pull_recent(
                    token_file=token_path,
                    staged_dir=base / "staged",
                    raw_dir=base / "raw",
                    start_date="2026-05-01",
                    end_date="2026-05-01",
                    allow_prompt=False,
                )

        self.assertEqual(summary["normalize"]["sessions_total"], 1)
        self.assertEqual(summary["normalize"]["hr_samples_total"], 3)


class PolarCliTests(unittest.TestCase):
    def test_script_help_paths_exit_cleanly(self) -> None:
        for script_name in [
            "auth_polar_accesslink.py",
            "pull_polar_accesslink.py",
            "normalize_polar_accesslink.py",
            "calibrate_fitbit_polar.py",
            "calibrate_fitbit_polar_samples.py",
        ]:
            with self.subTest(script_name=script_name):
                result = subprocess.run(
                    [sys.executable, str(SCRIPT_DIR / script_name), "--help"],
                    capture_output=True,
                    check=True,
                    text=True,
                )
                self.assertIn("usage:", result.stdout)


if __name__ == "__main__":
    unittest.main()
