from __future__ import annotations

import datetime as dt
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from healthdata.io import load_env_file, read_json_file, utc_now_iso, write_json_file


class HealthdataIoTest(unittest.TestCase):
    def test_load_env_file_preserves_existing_environment_by_default(self) -> None:
        key = "HEALTHDATA_IO_TEST_VALUE"
        original = os.environ.get(key)
        os.environ[key] = "existing"
        try:
            with TemporaryDirectory() as tmp:
                env_file = Path(tmp) / "provider.env"
                env_file.write_text(
                    "\n".join(
                        [
                            "# comments are ignored",
                            "export NEW_HEALTHDATA_IO_VALUE='fresh'",
                            f"{key}=from-file",
                        ]
                    ),
                    encoding="utf-8",
                )

                load_env_file(env_file)

                self.assertEqual(os.environ[key], "existing")
                self.assertEqual(os.environ["NEW_HEALTHDATA_IO_VALUE"], "fresh")
                os.environ.pop("NEW_HEALTHDATA_IO_VALUE", None)
        finally:
            if original is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = original

    def test_json_helpers_write_trailing_newline_and_read_payload(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "manifest.json"

            write_json_file(path, {"b": 2, "a": 1})

            self.assertEqual(read_json_file(path), {"a": 1, "b": 2})
            self.assertTrue(path.read_text(encoding="utf-8").endswith("\n"))

    def test_utc_now_iso_is_timezone_aware(self) -> None:
        parsed = dt.datetime.fromisoformat(utc_now_iso())

        self.assertEqual(parsed.tzinfo, dt.timezone.utc)


if __name__ == "__main__":
    unittest.main()
