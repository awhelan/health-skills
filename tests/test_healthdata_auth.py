from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
import io
import json
import socket
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from healthdata import auth
from healthdata.io import read_json_file, write_json_file


def unsigned_jwt(payload: dict[str, object]) -> str:
    def encode(data: dict[str, object]) -> str:
        raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode(payload)}."


class TokenRefreshHelperTests(unittest.TestCase):
    def test_token_needs_refresh_expired_fresh_and_forced(self) -> None:
        self.assertTrue(auth.token_needs_refresh({"expires_at": 0}))
        self.assertFalse(auth.token_needs_refresh({"expires_at": int(time.time()) + 9999}))
        self.assertTrue(auth.token_needs_refresh({"expires_at": int(time.time()) + 9999}, force=True))
        self.assertTrue(auth.token_needs_refresh({"expires_at": "not-a-timestamp"}))

    def test_stamp_oauth_token_records_expiry_and_client(self) -> None:
        token = auth.stamp_oauth_token(
            {"access_token": "a", "expires_in": 3600},
            client_id="cid",
            redirect_uri="http://localhost/cb",
        )
        self.assertEqual(token["client_id"], "cid")
        self.assertEqual(token["redirect_uri"], "http://localhost/cb")
        self.assertIn("obtained_at", token)
        self.assertEqual(token["expires_at"], token["obtained_at"] + 3600)

    def test_refresh_oauth_token_returns_fresh_token_without_network(self) -> None:
        with TemporaryDirectory() as tmp:
            token_path = Path(tmp) / "tokens.json"
            fresh = {
                "access_token": "still-good",
                "refresh_token": "r",
                "expires_at": int(time.time()) + 9999,
            }
            write_json_file(token_path, fresh)

            result = auth.refresh_oauth_token(
                token_path,
                token_url="https://example.invalid/token",
                client_id="cid",
                client_secret="secret",
                use_basic_auth=True,
            )

        self.assertEqual(result["access_token"], "still-good")

    def test_refresh_oauth_token_requires_refresh_token_when_stale(self) -> None:
        with TemporaryDirectory() as tmp:
            token_path = Path(tmp) / "tokens.json"
            write_json_file(token_path, {"access_token": "old", "expires_at": 0})

            with self.assertRaisesRegex(RuntimeError, "refresh token"):
                auth.refresh_oauth_token(
                    token_path,
                    token_url="https://example.invalid/token",
                    client_id="cid",
                    client_secret="secret",
                    use_basic_auth=True,
                )

    def test_refresh_oauth_token_preserves_refresh_token_when_response_omits_it(self) -> None:
        with TemporaryDirectory() as tmp:
            token_path = Path(tmp) / "tokens.json"
            write_json_file(
                token_path,
                {
                    "access_token": "old",
                    "refresh_token": "keep-me",
                    "expires_at": 0,
                    "redirect_uri": "http://localhost/cb",
                },
            )

            with patch.object(
                auth,
                "_post_token",
                return_value={"access_token": "new", "expires_in": 3600},
            ):
                result = auth.refresh_oauth_token(
                    token_path,
                    token_url="https://example.invalid/token",
                    client_id="cid",
                    client_secret="secret",
                    use_basic_auth=True,
                )

            persisted = read_json_file(token_path)

        self.assertEqual(result["refresh_token"], "keep-me")
        self.assertEqual(persisted["refresh_token"], "keep-me")
        self.assertEqual(persisted["access_token"], "new")


def free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def request_until_ready(url: str, *, timeout_seconds: float = 3.0) -> None:
    deadline = time.time() + timeout_seconds
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=0.2) as response:
                response.read()
            return
        except urllib.error.URLError as exc:
            last_error = exc
            time.sleep(0.02)
    raise AssertionError(f"Callback server did not respond: {last_error}")


def run_oauth_callback_quiet(redirect_uri: str, expected_state: str, timeout_seconds: int) -> str:
    with redirect_stdout(io.StringIO()):
        return auth.run_oauth_callback(redirect_uri, expected_state, timeout_seconds)


class OAuthCallbackTests(unittest.TestCase):
    def test_run_oauth_callback_rejects_non_http_redirect_uri(self) -> None:
        with self.assertRaisesRegex(ValueError, "http:// localhost"):
            auth.run_oauth_callback("https://localhost:8000/callback", "state", 1)

    def test_run_oauth_callback_rejects_non_localhost_redirect_uri(self) -> None:
        with self.assertRaisesRegex(ValueError, "non-local"):
            auth.run_oauth_callback("http://example.com:8000/callback", "state", 1)

    def test_run_oauth_callback_requires_explicit_port(self) -> None:
        with self.assertRaisesRegex(ValueError, "local port"):
            auth.run_oauth_callback("http://localhost/callback", "state", 1)

    def test_run_oauth_callback_rejects_state_mismatch(self) -> None:
        port = free_tcp_port()
        redirect_uri = f"http://127.0.0.1:{port}/callback"

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(run_oauth_callback_quiet, redirect_uri, "expected", 3)
            request_until_ready(f"{redirect_uri}?state=wrong&code=abc")

            with self.assertRaisesRegex(RuntimeError, "state mismatch"):
                future.result(timeout=5)


class JwtHelperTests(unittest.TestCase):
    def test_decode_jwt_payload_reads_claims(self) -> None:
        token = unsigned_jwt({"subjectId": "s-1", "exp": 123})
        self.assertEqual(auth.decode_jwt_payload(token), {"subjectId": "s-1", "exp": 123})

    def test_decode_jwt_payload_tolerates_garbage(self) -> None:
        self.assertEqual(auth.decode_jwt_payload("not-a-jwt"), {})

    def test_token_is_fresh_uses_exp_and_margin(self) -> None:
        now = int(time.time())
        self.assertTrue(auth.token_is_fresh(unsigned_jwt({"exp": now + 900})))
        self.assertFalse(auth.token_is_fresh(unsigned_jwt({"exp": now - 1})))
        self.assertFalse(auth.token_is_fresh(unsigned_jwt({"no_exp": True})))


if __name__ == "__main__":
    unittest.main()
