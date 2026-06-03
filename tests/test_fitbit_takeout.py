from __future__ import annotations

import csv
import importlib.util
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "skills" / "fitbit" / "scripts"


def load_script(name: str):
    # healthdata is an editable install, so no sys.path bootstrap is needed.
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


takeout_fitbit = load_script("takeout_fitbit")


class UnionDailyTests(unittest.TestCase):
    def staged(self) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {"date": "2026-05-20", "daily_steps": 1000, "source": "api"},
                {"date": "2026-05-21", "daily_steps": 2000, "source": "api"},
            ]
        )

    def takeout(self) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {"date": "2026-05-19", "daily_steps": 500, "stress_score": 30, "source": "takeout"},
                {"date": "2026-05-20", "daily_steps": 1111, "stress_score": 40, "source": "takeout"},
            ]
        )

    def test_union_is_takeout_preferred_with_provenance(self) -> None:
        combined = takeout_fitbit.union_daily(self.staged(), self.takeout())
        by_date = {row["date"]: row for _, row in combined.iterrows()}

        self.assertEqual(set(by_date), {"2026-05-19", "2026-05-20", "2026-05-21"})
        # Takeout-only historical date
        self.assertEqual(by_date["2026-05-19"]["source"], "takeout")
        self.assertEqual(float(by_date["2026-05-19"]["daily_steps"]), 500.0)
        # Overlap: Takeout wins the shared column, API contributes provenance
        self.assertEqual(by_date["2026-05-20"]["source"], "api+takeout")
        self.assertEqual(float(by_date["2026-05-20"]["daily_steps"]), 1111.0)
        self.assertEqual(float(by_date["2026-05-20"]["stress_score"]), 40.0)
        # API-only recent date keeps its value and a takeout-only column stays empty
        self.assertEqual(by_date["2026-05-21"]["source"], "api")
        self.assertEqual(float(by_date["2026-05-21"]["daily_steps"]), 2000.0)
        self.assertTrue(pd.isna(by_date["2026-05-21"]["stress_score"]))

    def test_union_is_idempotent(self) -> None:
        once = takeout_fitbit.union_daily(self.staged(), self.takeout())
        twice = takeout_fitbit.union_daily(once, self.takeout())
        sources_once = dict(zip(once["date"], once["source"]))
        sources_twice = dict(zip(twice["date"], twice["source"]))
        self.assertEqual(sources_once, sources_twice)
        self.assertEqual(len(once), len(twice))

    def test_union_empty_takeout_returns_staged(self) -> None:
        staged = self.staged()
        self.assertIs(takeout_fitbit.union_daily(staged, pd.DataFrame(columns=["date"])), staged)


class TakeoutDailyParserTests(unittest.TestCase):
    def _write_steps(self, root: Path) -> None:
        ged = root / "Global Export Data"
        ged.mkdir(parents=True)
        (ged / "steps-2026-05.json").write_text(
            json.dumps([
                {"dateTime": "05/19/26 00:00:00", "value": "100"},
                {"dateTime": "05/19/26 00:01:00", "value": "50"},
                {"dateTime": "05/20/26 09:00:00", "value": "200"},
            ]),
            encoding="utf-8",
        )

    def test_build_takeout_daily_sums_steps_per_local_date(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_steps(root)
            daily = takeout_fitbit.build_takeout_daily(root)
        by_date = {row["date"]: row for _, row in daily.iterrows()}
        self.assertEqual(float(by_date["2026-05-19"]["daily_steps"]), 150.0)
        self.assertEqual(float(by_date["2026-05-20"]["daily_steps"]), 200.0)

    def test_stage_takeout_daily_backfills_and_reports(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "takeout"
            self._write_steps(root)
            staged = base / "staged"
            staged.mkdir()
            # Pre-existing API row for a recent date Takeout lacks.
            (staged / "daily_metrics.csv").write_text(
                "date,daily_steps\n2026-05-25,9999\n", encoding="utf-8"
            )

            manifest = takeout_fitbit.stage_takeout_daily(
                staged, root, manifest_path=base / "manifest.json"
            )
            with (staged / "daily_metrics.csv").open(newline="", encoding="utf-8") as handle:
                rows = {r["date"]: r for r in csv.DictReader(handle)}

        self.assertEqual(manifest["daily_rows_total"], 3)
        self.assertEqual(set(rows), {"2026-05-19", "2026-05-20", "2026-05-25"})
        self.assertEqual(rows["2026-05-19"]["source"], "takeout")
        self.assertEqual(rows["2026-05-25"]["source"], "api")


if __name__ == "__main__":
    unittest.main()
