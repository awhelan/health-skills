from __future__ import annotations

import csv
import importlib.util
import json
import sys
import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory


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


def detail_payload() -> dict:
    return {"2026-05-01": {"trainingSessions": [DETAIL_SESSION]}}


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

    def _write_run(self, raw_dir: Path) -> None:
        run_dir = raw_dir / "run=2026-05-01_2026-05-02"
        run_dir.mkdir(parents=True)
        (run_dir / "training_summary.json").write_text(json.dumps(summary_payload()), encoding="utf-8")
        (run_dir / "training_details.json").write_text(json.dumps(detail_payload()), encoding="utf-8")

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


if __name__ == "__main__":
    unittest.main()
