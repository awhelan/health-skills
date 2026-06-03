"""Shared authentication helpers for provider pull/auth scripts.

Three provider families lean on this module:

- OAuth2 refresh-token pullers (Fitbit, Polar) share ``token_needs_refresh`` and
  ``refresh_oauth_token``; their interactive auth scripts share the localhost
  ``run_oauth_callback`` server and ``stamp_oauth_token``.
- The Dexcom Clarity session reuses the JWT helpers ``decode_jwt_payload`` /
  ``token_is_fresh``.

Token transport uses the stdlib ``urllib`` so this stays dependency-light (no
``requests`` needed just to refresh a token).
"""

from __future__ import annotations

import base64
import html
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from healthdata.io import read_json_file, write_json_file

DEFAULT_REFRESH_MARGIN_SECONDS = 300


# --- OAuth2 refresh-token flow (Fitbit, Polar) ----------------------------


def token_needs_refresh(
    token: dict[str, Any],
    force: bool = False,
    *,
    margin_seconds: int = DEFAULT_REFRESH_MARGIN_SECONDS,
) -> bool:
    """True when the access token is missing, expiring within the margin, or forced."""
    try:
        expires_at = int(token.get("expires_at", 0))
    except (TypeError, ValueError):
        return True
    return force or expires_at <= int(time.time()) + margin_seconds


def _post_token(token_url: str, body: dict[str, str], headers: dict[str, str], label: str) -> dict[str, Any]:
    encoded = urllib.parse.urlencode(body).encode("utf-8")
    request = urllib.request.Request(token_url, data=encoded, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{label} token request failed: HTTP {exc.code}: {detail}") from exc


def stamp_oauth_token(
    token: dict[str, Any],
    *,
    client_id: str,
    redirect_uri: str | None,
) -> dict[str, Any]:
    """Annotate a fresh token payload with obtained/expiry time and client metadata."""
    now = int(time.time())
    token["obtained_at"] = now
    if "expires_in" in token:
        token["expires_at"] = now + int(token["expires_in"])
    token["client_id"] = client_id
    token["redirect_uri"] = redirect_uri
    return token


def refresh_oauth_token(
    token_path: str | Path,
    *,
    token_url: str,
    client_id: str,
    client_secret: str | None = None,
    use_basic_auth: bool = True,
    force: bool = False,
    label: str = "OAuth",
) -> dict[str, Any]:
    """Refresh and persist an OAuth2 access token if it is expiring.

    ``use_basic_auth`` selects between confidential clients (HTTP Basic with the
    client secret, e.g. Polar and Fitbit "server" apps) and public clients
    (``client_id`` in the request body, e.g. Fitbit "client" apps).
    """
    path = Path(token_path)
    token = read_json_file(path)
    if not token_needs_refresh(token, force):
        return token

    refresh = token.get("refresh_token")
    if not refresh:
        raise RuntimeError(f"{path} does not contain a refresh token. Run the auth flow first.")

    body = {"grant_type": "refresh_token", "refresh_token": str(refresh)}
    headers = {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"}
    if use_basic_auth:
        if not client_secret:
            raise ValueError(f"{label} token refresh requires a client secret for basic auth.")
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {basic}"
    else:
        body["client_id"] = client_id

    updated = _post_token(token_url, body, headers, label)
    if not updated.get("refresh_token"):
        updated["refresh_token"] = refresh
    stamp_oauth_token(updated, client_id=client_id, redirect_uri=token.get("redirect_uri"))
    write_json_file(path, updated, private=True)
    return updated


# --- Interactive authorization-code callback (Fitbit, Polar) --------------


def run_oauth_callback(
    redirect_uri: str,
    expected_state: str,
    timeout_seconds: int,
    *,
    provider: str = "OAuth",
) -> str:
    """Serve the localhost redirect URI once and return the authorization code.

    Only binds http://localhost (or 127.0.0.1) and verifies the OAuth ``state``.
    """
    parsed = urllib.parse.urlparse(redirect_uri)
    if parsed.scheme != "http":
        raise ValueError("This local helper only supports an http:// localhost redirect URI.")
    if parsed.hostname not in {"localhost", "127.0.0.1"}:
        raise ValueError("Refusing to bind a non-local redirect URI.")
    if parsed.port is None:
        raise ValueError("Redirect URI must include a local port.")

    result: dict[str, str] = {}
    callback_path = parsed.path or "/"

    class CallbackHandler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: object) -> None:
            return

        def do_GET(self) -> None:
            request = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(request.query)
            if request.path != callback_path:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(f"Unknown {provider} callback path.".encode("utf-8"))
                return

            state = params.get("state", [""])[0]
            code = params.get("code", [""])[0]
            error = params.get("error", [""])[0]
            if error:
                result["error"] = error
            elif state != expected_state:
                result["error"] = "OAuth state mismatch."
            elif not code:
                result["error"] = f"{provider} callback did not include an authorization code."
            else:
                result["code"] = code

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            message = result.get("error") or f"{provider} authorization received. You can close this tab."
            self.wfile.write(f"<html><body><p>{html.escape(message)}</p></body></html>".encode("utf-8"))

    server = HTTPServer((parsed.hostname, parsed.port), CallbackHandler)
    server.timeout = 1
    deadline = time.time() + timeout_seconds
    print(f"Waiting for {provider} callback on {redirect_uri}")
    while time.time() < deadline and not result:
        server.handle_request()
    server.server_close()

    if "error" in result:
        raise RuntimeError(result["error"])
    if "code" not in result:
        raise TimeoutError(f"Timed out waiting for {provider} authorization callback.")
    return result["code"]


# --- JWT helpers (Dexcom Clarity subject token) ---------------------------


def decode_jwt_payload(token: str) -> dict[str, Any]:
    """Decode a JWT payload segment without verifying the signature."""
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
    except Exception:
        return {}


def token_is_fresh(token: str, *, margin_seconds: int = DEFAULT_REFRESH_MARGIN_SECONDS) -> bool:
    """True when the JWT carries an ``exp`` that is more than the margin away."""
    payload = decode_jwt_payload(token)
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)):
        return False
    return exp - time.time() > margin_seconds
