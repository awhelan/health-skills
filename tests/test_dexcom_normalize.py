from __future__ import annotations

import base64
import csv
import datetime as dt
import importlib.util
import io
import json
from contextlib import redirect_stderr, redirect_stdout
import sys
import time
import unittest
import urllib.parse
from pathlib import Path
from tempfile import TemporaryDirectory


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "skills" / "dexcom-clarity-cgm" / "scripts"
MODULE_PATH = SCRIPT_DIR / "normalize_dexcom_clarity.py"
SPEC = importlib.util.spec_from_file_location("normalize_dexcom_clarity", MODULE_PATH)
assert SPEC and SPEC.loader
normalize_dexcom_clarity = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(normalize_dexcom_clarity)


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


auth_dexcom_clarity = load_script("auth_dexcom_clarity")
pull_dexcom_clarity = load_script("pull_dexcom_clarity")


def unsigned_jwt(payload: dict[str, object]) -> str:
    def encode(data: dict[str, object]) -> str:
        raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{encode({'alg': 'none', 'typ': 'JWT'})}.{encode(payload)}."


def clarity_csv(rows: list[dict[str, str]]) -> str:
    fields = [
        "Timestamp (YYYY-MM-DDThh:mm:ss)",
        "Glucose Value (mg/dL)",
        "Event Type",
        "Source Device",
        "Transmitter ID",
    ]
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


class DexcomNormalizeTests(unittest.TestCase):
    def test_parse_clarity_readings_filters_to_egv(self) -> None:
        export = "\ufeff" + clarity_csv(
            [
                {
                    "Timestamp (YYYY-MM-DDThh:mm:ss)": "2026-05-01 08:00:00",
                    "Glucose Value (mg/dL)": "101",
                    "Event Type": "egv",
                    "Source Device": "Stelo",
                    "Transmitter ID": "abc",
                },
                {
                    "Timestamp (YYYY-MM-DDThh:mm:ss)": "2026-05-01 08:05:00",
                    "Glucose Value (mg/dL)": "104",
                    "Event Type": "Calibration",
                    "Source Device": "Stelo",
                    "Transmitter ID": "abc",
                },
            ]
        )

        self.assertEqual(
            normalize_dexcom_clarity.parse_clarity_readings(export.encode("utf-8")),
            [
                {
                    "timestamp": "2026-05-01T08:00:00-07:00",
                    "glucose_mg_dl": "101",
                    "source_device": "Stelo",
                    "transmitter_id": "abc",
                }
            ],
        )

    def test_parse_clarity_readings_requires_timestamp_and_glucose(self) -> None:
        with self.assertRaises(ValueError):
            normalize_dexcom_clarity.parse_clarity_readings("Event Type\nEGV\n")

    def test_parse_clarity_readings_skips_export_preamble(self) -> None:
        export = (
            "Dexcom Clarity Export\n"
            "Generated,2026-06-01\n"
            "\n"
            + clarity_csv(
                [
                    {
                        "Timestamp (YYYY-MM-DDThh:mm:ss)": "2026-05-01 08:00:00",
                        "Glucose Value (mg/dL)": "101",
                        "Event Type": "EGV",
                        "Source Device": "Stelo",
                        "Transmitter ID": "abc",
                    }
                ]
            )
        )

        rows = normalize_dexcom_clarity.parse_clarity_readings(export)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["timestamp"], "2026-05-01T08:00:00-07:00")
        self.assertEqual(rows[0]["glucose_mg_dl"], "101")

    def test_parse_clarity_readings_preserves_dst_fall_back_duplicates(self) -> None:
        export = clarity_csv(
            [
                {
                    "Timestamp (YYYY-MM-DDThh:mm:ss)": "2026-11-01 01:00:00",
                    "Glucose Value (mg/dL)": "101",
                    "Event Type": "EGV",
                },
                {
                    "Timestamp (YYYY-MM-DDThh:mm:ss)": "2026-11-01 01:00:00",
                    "Glucose Value (mg/dL)": "104",
                    "Event Type": "EGV",
                },
            ]
        )

        rows = normalize_dexcom_clarity.parse_clarity_readings(export)

        self.assertEqual(
            [row["timestamp"] for row in rows],
            ["2026-11-01T01:00:00-07:00", "2026-11-01T01:00:00-08:00"],
        )

    def test_dst_fall_back_duplicates_survive_in_either_sort_order(self) -> None:
        run = [
            ("2026-11-01 00:55:00", "90"),
            ("2026-11-01 01:00:00", "101"),
            ("2026-11-01 01:00:00", "104"),
            ("2026-11-01 01:05:00", "110"),
        ]
        for ordered in (run, list(reversed(run))):
            export = clarity_csv(
                [
                    {
                        "Timestamp (YYYY-MM-DDThh:mm:ss)": ts,
                        "Glucose Value (mg/dL)": glucose,
                        "Event Type": "EGV",
                    }
                    for ts, glucose in ordered
                ]
            )

            rows = normalize_dexcom_clarity.parse_clarity_readings(export)

            repeated = sorted(
                row["timestamp"]
                for row in rows
                if row["timestamp"].startswith("2026-11-01T01:00:00")
            )
            self.assertEqual(len(rows), 4)
            self.assertEqual(
                repeated,
                ["2026-11-01T01:00:00-07:00", "2026-11-01T01:00:00-08:00"],
            )

    def test_parse_clarity_readings_rejects_unrecognized_timestamp(self) -> None:
        export = (
            "Timestamp (YYYY-MM-DDThh:mm:ss),Glucose Value (mg/dL),Event Type\n"
            "May 1 2026 8am,101,EGV\n"
        )

        with self.assertRaisesRegex(ValueError, "Unrecognized Clarity timestamp"):
            normalize_dexcom_clarity.parse_clarity_readings(export)

    def test_normalize_readings_is_idempotent_and_sorted(self) -> None:
        with TemporaryDirectory() as tmp:
            table = Path(tmp) / "cgm_readings.csv"
            readings = [
                {"timestamp": "2026-05-01T08:05:00-07:00", "glucose_mg_dl": "104"},
                {"timestamp": "2026-05-01T08:00:00-07:00", "glucose_mg_dl": "101"},
            ]

            self.assertEqual(normalize_dexcom_clarity.normalize_readings(table, readings), (2, 2))
            self.assertEqual(normalize_dexcom_clarity.normalize_readings(table, readings), (0, 2))
            with table.open(newline="") as f:
                rows = list(csv.DictReader(f))

        self.assertEqual(
            [row["timestamp"] for row in rows],
            ["2026-05-01T08:00:00-07:00", "2026-05-01T08:05:00-07:00"],
        )
        self.assertEqual(rows[0]["glucose_mg_dl"], "101")

    def test_naive_legacy_rows_collapse_onto_tz_aware_twins(self) -> None:
        # Rows written by the pre-tz-aware normalizer are naive; a re-pull emits
        # the same instant tz-aware. Both must key to one canonical reading.
        with TemporaryDirectory() as tmp:
            table = Path(tmp) / "cgm_readings.csv"
            with table.open("w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=normalize_dexcom_clarity.TABLE_COLUMNS)
                writer.writeheader()
                writer.writerow({"timestamp": "2026-05-26T07:25:30", "glucose_mg_dl": "117"})

            readings = [{"timestamp": "2026-05-26T07:25:30-07:00", "glucose_mg_dl": "117"}]
            added, total = normalize_dexcom_clarity.normalize_readings(table, readings)

            with table.open(newline="") as f:
                rows = list(csv.DictReader(f))

        self.assertEqual((added, total), (0, 1))
        self.assertEqual([row["timestamp"] for row in rows], ["2026-05-26T07:25:30-07:00"])

    def test_cli_discovers_exports_beside_table_and_prunes(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            table = base / "cgm_readings.csv"
            export = base / "clarity_export.csv"
            export.write_text(
                clarity_csv(
                    [
                        {
                            "Timestamp (YYYY-MM-DDThh:mm:ss)": "2026-05-01 08:00:00",
                            "Glucose Value (mg/dL)": "101",
                            "Event Type": "EGV",
                            "Source Device": "Stelo",
                            "Transmitter ID": "abc",
                        }
                    ]
                ),
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                result = normalize_dexcom_clarity.main(
                    ["--table", str(table), "--prune", "--manifest", str(base / "manifest.json")]
                )
            self.assertEqual(result, 0)
            with table.open(newline="") as f:
                rows = list(csv.DictReader(f))

            self.assertEqual(len(rows), 1)
            self.assertFalse(export.exists())

    def test_cli_prune_refuses_export_with_no_readings(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            table = base / "cgm_readings.csv"
            export = base / "clarity_export.csv"
            export.write_text(
                clarity_csv(
                    [
                        {
                            "Timestamp (YYYY-MM-DDThh:mm:ss)": "2026-05-01 08:05:00",
                            "Glucose Value (mg/dL)": "104",
                            "Event Type": "Calibration",
                            "Source Device": "Stelo",
                            "Transmitter ID": "abc",
                        }
                    ]
                ),
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                result = normalize_dexcom_clarity.main(
                    [str(export), "--table", str(table), "--prune", "--manifest", str(base / "manifest.json")]
                )

            self.assertEqual(result, 1)
            self.assertTrue(export.exists())
            self.assertFalse(table.exists())

    def test_cli_returns_error_for_invalid_existing_table(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            table = base / "cgm_readings.csv"
            export = base / "clarity_export.csv"
            table.write_text("not_timestamp,glucose_mg_dl\nx,101\n", encoding="utf-8")
            export.write_text(
                clarity_csv(
                    [
                        {
                            "Timestamp (YYYY-MM-DDThh:mm:ss)": "2026-05-01 08:00:00",
                            "Glucose Value (mg/dL)": "101",
                            "Event Type": "EGV",
                        }
                    ]
                ),
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                result = normalize_dexcom_clarity.main(
                    [str(export), "--table", str(table), "--manifest", str(base / "manifest.json")]
                )
            self.assertEqual(result, 1)


class DexcomPullTests(unittest.TestCase):
    def settings(self) -> object:
        return auth_dexcom_clarity.Settings(
            username="user@example.com",
            password="password",
            subject_id="sub",
            subject_token="token",
            first_name="Ada",
            last_name="Lovelace",
            date_of_birth="1815-12-10",
            locale="en-US",
            units="mgdl",
            api_base="https://clarity.dexcom.com/api",
            cache_dir=Path("unused"),
        )

    def test_no_login_does_not_attempt_interactive_login_without_cached_token(self) -> None:
        settings = auth_dexcom_clarity.Settings(
            username="",
            password="",
            subject_id="",
            subject_token="",
            first_name="",
            last_name="",
            date_of_birth="",
            locale="en-US",
            units="mgdl",
            api_base="https://clarity.dexcom.com/api",
            cache_dir=Path("unused"),
        )
        original_cached = auth_dexcom_clarity.cached_or_env_session
        original_login = auth_dexcom_clarity.login
        login_calls: list[str] = []

        def fake_cached_or_env_session(settings, force_login):  # noqa: ANN001
            return {}

        def fake_login(*args, **kwargs):  # noqa: ANN002, ANN003
            login_calls.append("login")
            return {}

        try:
            auth_dexcom_clarity.cached_or_env_session = fake_cached_or_env_session
            auth_dexcom_clarity.login = fake_login

            with self.assertRaisesRegex(RuntimeError, "--no-login"):
                auth_dexcom_clarity.resolve_session(settings, object(), force_login=False, no_login=True)
        finally:
            auth_dexcom_clarity.cached_or_env_session = original_cached
            auth_dexcom_clarity.login = original_login

        self.assertEqual(login_calls, [])

    def test_parse_form_action_and_input_value_handle_html_entities(self) -> None:
        html = """
        <form id="ignored" action="/wrong"></form>
        <form id="kc-form-login" action="/login?client=a&amp;tab=b">
          <input name="username" value="user@example.com">
        </form>
        <form id="login_button" action="/handoff">
          <input name="authenticity_token" value="tok&amp;en">
        </form>
        """

        self.assertEqual(
            auth_dexcom_clarity.parse_form_action(html, form_id="kc-form-login"),
            "/login?client=a&tab=b",
        )
        self.assertEqual(
            auth_dexcom_clarity.parse_form_input_value(html, "authenticity_token", form_id="login_button"),
            "tok&en",
        )

    def test_extract_local_storage_session_from_bootstrap_script(self) -> None:
        html = """
        <script>
        const data = {
          ssSubjectId: 'subject-1',
          accessToken: "access-token"
        };
        window.localStorage.setItem('clarity_externalSession', JSON.stringify(data))
        </script>
        """

        self.assertEqual(
            auth_dexcom_clarity.extract_local_storage_session(html),
            {"ssSubjectId": "subject-1", "accessToken": "access-token"},
        )

    def test_extract_session_from_response_uses_query_token(self) -> None:
        response = FakeResponse(
            url="https://clarity.dexcom.com/i/?subjectId=subject-1&accessToken=access-token",
            text="",
        )

        data = auth_dexcom_clarity.extract_session_from_response(response, self.settings())

        self.assertEqual(data["subject_id"], "subject-1")
        self.assertEqual(data["access_token"], "access-token")
        self.assertIn("updated_at", data)

    def test_jwt_helpers_decode_subject_and_freshness(self) -> None:
        now = int(time.time())
        fresh = unsigned_jwt({"exp": now + 900, "subjectId": "subject-1"})
        stale = unsigned_jwt({"exp": now - 1, "subjectId": "subject-1"})

        self.assertEqual(auth_dexcom_clarity.token_subject_id(fresh), "subject-1")
        self.assertTrue(auth_dexcom_clarity.token_is_fresh(fresh))
        self.assertFalse(auth_dexcom_clarity.token_is_fresh(stale))

    def test_post_html_form_refuses_cross_origin_credential_action(self) -> None:
        session = FakeSession(FakeResponse(url="https://uam1.dexcom.com/done", text="ok"))

        with self.assertRaisesRegex(RuntimeError, "cross-origin"):
            auth_dexcom_clarity.post_html_form(
                session,
                "https://example.invalid/steal",
                [("username", "user@example.com"), ("password", "password")],
                referer="https://uam1.dexcom.com/login",
                require_same_origin=True,
            )

        self.assertEqual(session.posts, [])

    def test_summarize_csv_reports_chronological_first_and_last(self) -> None:
        export = clarity_csv(
            [
                {
                    "Timestamp (YYYY-MM-DDThh:mm:ss)": "2026-05-01 08:05:00",
                    "Glucose Value (mg/dL)": "104",
                    "Event Type": "EGV",
                },
                {
                    "Timestamp (YYYY-MM-DDThh:mm:ss)": "2026-05-01 08:00:00",
                    "Glucose Value (mg/dL)": "101",
                    "Event Type": "EGV",
                },
            ]
        )

        summary = pull_dexcom_clarity.summarize_csv(export.encode("utf-8"), normalize_dexcom_clarity.DEFAULT_LOCAL_TZ)

        self.assertEqual(summary["glucose_rows"], 2)
        self.assertEqual(summary["first_timestamp"], "2026-05-01T08:00:00-07:00")
        self.assertEqual(summary["last_timestamp"], "2026-05-01T08:05:00-07:00")

    def test_export_csv_rejects_html_response(self) -> None:
        session = FakeSession(
            FakeResponse(
                url="https://clarity.dexcom.com/login",
                text="<html><body>login</body></html>",
                content=b"<html><body>login</body></html>",
            )
        )

        with self.assertRaisesRegex(RuntimeError, "HTML instead of CSV"):
            pull_dexcom_clarity.export_csv(
                self.settings(),
                session,
                {"access_token": "access-token", "subject_id": "subject-1"},
                dt.date(2026, 5, 1),
                dt.date(2026, 5, 2),
            )


class DexcomAuthTests(unittest.TestCase):
    def settings(self, *, subject_token: str = "", subject_id: str = "") -> object:
        return auth_dexcom_clarity.Settings(
            username="user@example.com",
            password="password",
            subject_id=subject_id,
            subject_token=subject_token,
            first_name="Ada",
            last_name="Lovelace",
            date_of_birth="1815-12-10",
            locale="en-US",
            units="mgdl",
            api_base="https://clarity.dexcom.com/api",
            cache_dir=Path("unused"),
        )

    def test_build_authorize_url_carries_required_oauth_params(self) -> None:
        url = auth_dexcom_clarity.build_authorize_url("user@example.com")
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)

        self.assertTrue(url.startswith(auth_dexcom_clarity.UAM_AUTHORIZE_URL))
        self.assertEqual(query["client_id"], [auth_dexcom_clarity.CLARITY_CLIENT_ID])
        self.assertEqual(query["redirect_uri"], [auth_dexcom_clarity.CLARITY_CALLBACK])
        self.assertEqual(query["login_hint"], ["user@example.com"])
        self.assertEqual(query["response_type"], ["code"])

    def test_has_password_input_detects_password_field(self) -> None:
        self.assertTrue(auth_dexcom_clarity.has_password_input('<input name="password">'))
        self.assertFalse(auth_dexcom_clarity.has_password_input('<input name="username">'))

    def test_parse_login_action_prefers_keycloak_form(self) -> None:
        html = (
            '<form id="kc-form-login" action="/keycloak"></form>'
            '<form action="/fallback"></form>'
        )
        self.assertEqual(auth_dexcom_clarity.parse_login_action(html), "/keycloak")

    def test_normalize_session_data_fills_subject_id_from_token(self) -> None:
        token = unsigned_jwt({"exp": 0, "subjectId": "from-token"})
        data = auth_dexcom_clarity.normalize_session_data({"access_token": token}, self.settings())
        self.assertEqual(data["subject_id"], "from-token")

    def test_cached_or_env_session_uses_fresh_env_token(self) -> None:
        now = int(time.time())
        token = unsigned_jwt({"exp": now + 900, "subjectId": "subject-1"})
        data = auth_dexcom_clarity.cached_or_env_session(
            self.settings(subject_token=token), force_login=False
        )
        self.assertEqual(data["access_token"], token)
        self.assertEqual(data["source"], "env")

    def test_cached_or_env_session_force_login_ignores_stale_token(self) -> None:
        now = int(time.time())
        stale = unsigned_jwt({"exp": now - 1, "subjectId": "subject-1"})
        data = auth_dexcom_clarity.cached_or_env_session(
            self.settings(subject_token=stale), force_login=True
        )
        self.assertEqual(data, {})

    def test_login_error_hint_flags_passkey_otp_form(self) -> None:
        hint = auth_dexcom_clarity.login_error_hint(
            '<form id="kc-form-login"></form><button id="otp-login-button"></button>'
        )
        self.assertIn("passkey", hint)


class DexcomGlucoseUnitTests(unittest.TestCase):
    def test_mmol_export_is_converted_to_mg_dl(self) -> None:
        mmol_csv = (
            "Timestamp (YYYY-MM-DDThh:mm:ss),Glucose Value (mmol/L),Event Type\n"
            "2026-05-01 08:00:00,5.5,EGV\n"
        )
        readings = normalize_dexcom_clarity.parse_clarity_readings(mmol_csv)
        # 5.5 mmol/L * 18.0182 = 99.1 -> 99 mg/dL.
        self.assertEqual(readings[0]["glucose_mg_dl"], "99")

    def test_mg_dl_export_is_passed_through_unconverted(self) -> None:
        readings = normalize_dexcom_clarity.parse_clarity_readings(clarity_csv(
            [
                {
                    "Timestamp (YYYY-MM-DDThh:mm:ss)": "2026-05-01 08:00:00",
                    "Glucose Value (mg/dL)": "101",
                    "Event Type": "EGV",
                }
            ]
        ))
        self.assertEqual(readings[0]["glucose_mg_dl"], "101")

    def test_mg_dl_out_of_range_markers_are_clamped(self) -> None:
        readings = normalize_dexcom_clarity.parse_clarity_readings(clarity_csv(
            [
                {
                    "Timestamp (YYYY-MM-DDThh:mm:ss)": "2026-05-01 08:00:00",
                    "Glucose Value (mg/dL)": "High",
                    "Event Type": "EGV",
                },
                {
                    "Timestamp (YYYY-MM-DDThh:mm:ss)": "2026-05-01 08:05:00",
                    "Glucose Value (mg/dL)": "Low",
                    "Event Type": "EGV",
                },
            ]
        ))
        self.assertEqual([row["glucose_mg_dl"] for row in readings], ["400", "40"])

    def test_mmol_out_of_range_markers_are_clamped(self) -> None:
        mmol_csv = (
            "Timestamp (YYYY-MM-DDThh:mm:ss),Glucose Value (mmol/L),Event Type\n"
            "2026-05-01 08:00:00,High,EGV\n"
            "2026-05-01 08:05:00,Low,EGV\n"
        )
        readings = normalize_dexcom_clarity.parse_clarity_readings(mmol_csv)
        self.assertEqual([row["glucose_mg_dl"] for row in readings], ["400", "40"])

    def test_non_numeric_mmol_export_is_rejected(self) -> None:
        mmol_csv = (
            "Timestamp (YYYY-MM-DDThh:mm:ss),Glucose Value (mmol/L),Event Type\n"
            "2026-05-01 08:00:00,not-a-number,EGV\n"
        )

        with self.assertRaisesRegex(ValueError, "Cannot convert non-numeric mmol/L"):
            normalize_dexcom_clarity.parse_clarity_readings(mmol_csv)


class FakeResponse:
    def __init__(
        self,
        *,
        url: str,
        text: str,
        content: bytes | None = None,
        ok: bool = True,
        json_payload: dict[str, object] | None = None,
    ) -> None:
        self.url = url
        self.text = text
        self.content = content if content is not None else text.encode("utf-8")
        self.ok = ok
        self._json_payload = json_payload or {}

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self._json_payload


class FakeSession:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.posts: list[dict[str, object]] = []

    def post(self, url: str, **kwargs: object) -> FakeResponse:
        self.posts.append({"url": url, **kwargs})
        return self.response


if __name__ == "__main__":
    unittest.main()
