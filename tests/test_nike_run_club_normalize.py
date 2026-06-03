from __future__ import annotations

import csv
import importlib.util
import io
import json
import sys
import unittest
from argparse import Namespace
from contextlib import redirect_stderr
from pathlib import Path
from tempfile import TemporaryDirectory


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "skills" / "nike-run-club" / "scripts"


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


normalize_nike_run_club = load_script("normalize_nike_run_club")
pull_nike_run_club = load_script("pull_nike_run_club")


class NikeRunClubNormalizeTests(unittest.TestCase):
    def test_normalize_raw_dir_normalizes_json_activity(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            raw_dir = base / "raw"
            out_dir = base / "staged"
            manifest_path = base / "manifest.json"
            raw_dir.mkdir()
            (raw_dir / "page001.json").write_text(
                json.dumps(
                    {
                        "activities": [
                            {
                                "id": "run-1",
                                "type": "run",
                                "startTime": "2026-05-01T15:00:00Z",
                                "activeDurationMs": 1_800_000,
                                "summaries": [
                                    {"metric": "distance", "summary": "total", "value": 5.0},
                                    {"metric": "heart_rate", "summary": "mean", "value": 145},
                                ],
                                "tags": {"com.nike.name": "Tempo run"},
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            manifest = normalize_nike_run_club.normalize_raw_dir(raw_dir, out_dir, manifest_path=manifest_path)
            with (out_dir / "activities.csv").open(newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            manifest_exists = manifest_path.exists()

        self.assertEqual(manifest["activity_rows"], 1)
        self.assertEqual(rows[0]["activity_id"], "run-1")
        self.assertEqual(rows[0]["source_format"], "json")
        self.assertEqual(rows[0]["duration_minutes"], "30.000000")
        self.assertEqual(rows[0]["distance_miles"], "3.106855")
        self.assertEqual(rows[0]["nike_distance_unit_guess"], "nrc_summary_kilometers")
        self.assertEqual(rows[0]["title"], "Tempo run")
        self.assertTrue(manifest_exists)

    def test_dedupe_rows_uses_activity_identity_not_source_file(self) -> None:
        rows = [
            {
                "date": "2026-05-01",
                "activity_id": "run-1",
                "source_file": "data/raw/nike_run_club_api/api_pages/result001.json",
                "start_time": "2026-05-01T08:00:00-07:00",
                "activity_type": "run",
                "duration_minutes": "30.000000",
                "distance_miles": "3.106855",
            },
            {
                "date": "2026-05-01",
                "activity_id": "run-1",
                "source_file": "data/raw/nike_run_club_api/api_pages/result002.json",
                "start_time": "2026-05-01T08:00:00-07:00",
                "activity_type": "run",
                "duration_minutes": "30.000000",
                "distance_miles": "3.106855",
            },
        ]

        self.assertEqual(len(normalize_nike_run_club.dedupe_rows(rows)), 1)

    def test_filter_rows_with_date_bounds_drops_undated_rows(self) -> None:
        rows = [
            {"date": "", "activity_id": "undated"},
            {"date": "2026-05-01", "activity_id": "inside"},
            {"date": "2026-04-30", "activity_id": "before"},
        ]

        filtered = normalize_nike_run_club.filter_rows(rows, "2026-05-01", "2026-05-02")

        self.assertEqual([row["activity_id"] for row in filtered], ["inside"])

    def test_timezone_override_controls_local_activity_dates(self) -> None:
        row = normalize_nike_run_club.normalize_mapping(
            {"startTime": "2026-05-01T02:00:00Z", "distance_km": "5"},
            Path("manual.csv"),
            "csv",
            local_tz=normalize_nike_run_club.resolve_timezone("Europe/London"),
        )

        assert row is not None
        self.assertEqual(row["date"], "2026-05-01")
        self.assertTrue(row["start_time"].startswith("2026-05-01T03:00:00+01:00"))

    def test_naive_activity_timestamps_use_local_timezone(self) -> None:
        row = normalize_nike_run_club.normalize_mapping(
            {"startTime": "2026-05-01T08:00:00", "distance_km": "5"},
            Path("manual.csv"),
            "csv",
        )

        assert row is not None
        self.assertEqual(row["start_time"], "2026-05-01T08:00:00-07:00")

    def test_normalize_raw_dir_records_timezone(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            raw_dir = base / "raw"
            out_dir = base / "staged"
            manifest_path = base / "manifest.json"
            raw_dir.mkdir()
            (raw_dir / "page001.json").write_text(
                json.dumps({"activities": [{"id": "run-1", "startTime": "2026-05-01T02:00:00Z", "distance": 5000}]}),
                encoding="utf-8",
            )

            manifest = normalize_nike_run_club.normalize_raw_dir(
                raw_dir,
                out_dir,
                manifest_path=manifest_path,
                timezone="Europe/London",
            )

        self.assertEqual(manifest["timezone"], "Europe/London")

    def test_source_files_prefers_api_page_results(self) -> None:
        with TemporaryDirectory() as tmp:
            raw_dir = Path(tmp)
            page_dir = raw_dir / "export=2026-05-23" / "api_pages"
            page_dir.mkdir(parents=True)
            expected = page_dir / "result001.json"
            expected.write_text("{}", encoding="utf-8")
            unrelated = raw_dir / "export=2026-05-23" / "metadata.json"
            unrelated.write_text("{}", encoding="utf-8")

            files = normalize_nike_run_club.source_files(raw_dir)

        self.assertEqual(files, [expected])

    def test_activity_payload_extraction_uses_known_page_shape(self) -> None:
        payload = {
            "metadata": {"id": "not-an-activity", "startTime": "2026-05-01T00:00:00Z"},
            "activities": [
                {"id": "run-1", "start_epoch_ms": 1_777_777_777_000},
                {"id": "bad-metadata-only"},
            ],
        }

        activities = normalize_nike_run_club.activity_mappings_from_payload(payload)

        self.assertEqual([activity["id"] for activity in activities], ["run-1"])

    def test_validate_date_window_rejects_bad_dates(self) -> None:
        with self.assertRaises(ValueError):
            normalize_nike_run_club.validate_date_window("2026/05/01", None)

    def test_validate_date_window_rejects_reversed_dates(self) -> None:
        with self.assertRaises(ValueError):
            normalize_nike_run_club.validate_date_window("2026-05-02", "2026-05-01")

    def test_resolve_timezone_rejects_unknown_name(self) -> None:
        with self.assertRaises(ValueError):
            normalize_nike_run_club.resolve_timezone("Not/AZone")


class NikeRunClubPullTests(unittest.TestCase):
    def test_bearer_token_from_arg_strips_prefix(self) -> None:
        args = Namespace(bearer_token="Bearer abc123")
        self.assertEqual(pull_nike_run_club.bearer_token(args), "abc123")

    def test_bearer_token_extracts_from_curl_paste(self) -> None:
        curl = (
            "curl 'https://api.nike.com/plus/v3/activities/before_id/v3/*?limit=30' \\\n"
            "  -H 'accept: application/json' \\\n"
            "  -H 'authorization: Bearer abc.def-123' \\\n"
            "  -H 'user-agent: Mozilla/5.0'"
        )
        args = Namespace(bearer_token=curl)
        self.assertEqual(pull_nike_run_club.bearer_token(args), "abc.def-123")

    def test_bearer_token_reads_from_stdin_text(self) -> None:
        args = Namespace(bearer_token=None, bearer_token_stdin=True)
        self.assertEqual(pull_nike_run_club.bearer_token(args, stdin_text="Bearer abc123"), "abc123")

    def test_bearer_token_missing_raises(self) -> None:
        with self.assertRaises(SystemExit):
            pull_nike_run_club.bearer_token(Namespace(bearer_token=None))

    def test_extract_bearer_token_bare_value(self) -> None:
        self.assertEqual(pull_nike_run_club.extract_bearer_token("abc123"), "abc123")

    def test_main_prints_error_without_traceback_for_api_failure(self) -> None:
        original_parse_args = pull_nike_run_club.parse_args
        original_bearer_token = pull_nike_run_club.bearer_token
        original_fetch_pages = pull_nike_run_club.fetch_pages

        with TemporaryDirectory() as tmp:
            base = Path(tmp)

            def fake_parse_args() -> Namespace:
                return Namespace(
                    raw_dir=base / "raw",
                    staged_dir=base / "staged",
                    start_date=None,
                    end_date=None,
                    timezone="America/Los_Angeles",
                    normalize_only=False,
                    allow_empty=False,
                )

            def fake_bearer_token(args: Namespace) -> str:
                return "token"

            def fake_fetch_pages(args: Namespace, token: str) -> dict[str, object]:
                raise RuntimeError("Nike API request failed: HTTP 401: expired token")

            try:
                pull_nike_run_club.parse_args = fake_parse_args
                pull_nike_run_club.bearer_token = fake_bearer_token
                pull_nike_run_club.fetch_pages = fake_fetch_pages

                stderr = io.StringIO()
                with redirect_stderr(stderr):
                    result = pull_nike_run_club.main()
            finally:
                pull_nike_run_club.parse_args = original_parse_args
                pull_nike_run_club.bearer_token = original_bearer_token
                pull_nike_run_club.fetch_pages = original_fetch_pages

        self.assertEqual(result, 1)
        self.assertIn("HTTP 401", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
