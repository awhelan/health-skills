#!/usr/bin/env python3
"""Run the Fitbit OAuth2 PKCE flow and save tokens to .local/state."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from getpass import getpass
from pathlib import Path

from healthdata.auth import run_oauth_callback, stamp_oauth_token
from healthdata.config import FITBIT_ENV_FILE, FITBIT_TOKEN_FILE
from healthdata.io import load_env_file as _load_env_file, write_json_file


AUTH_URL = "https://www.fitbit.com/oauth2/authorize"
TOKEN_URL = "https://api.fitbit.com/oauth2/token"
DEFAULT_REDIRECT_URI = "http://localhost:8000/auth/fitbit/callback"
DEFAULT_SCOPES = (
    "activity",
    "heartrate",
    "sleep",
    "profile",
    "location",
    "oxygen_saturation",
    "temperature",
    "respiratory_rate",
    "cardio_fitness",
)
DEFAULT_TOKEN_FILE = FITBIT_TOKEN_FILE
LOCAL_ENV_FILE = FITBIT_ENV_FILE


def load_local_env(path: str | Path = LOCAL_ENV_FILE) -> None:
    _load_env_file(path)


def _base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _json_request(url: str, body: dict[str, str], headers: dict[str, str]) -> dict[str, object]:
    encoded = urllib.parse.urlencode(body).encode("utf-8")
    request = urllib.request.Request(url, data=encoded, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Fitbit token request failed: HTTP {exc.code}: {detail}") from exc


def _write_token(path: Path, token: dict[str, object], client_id: str, redirect_uri: str) -> None:
    stamp_oauth_token(token, client_id=client_id, redirect_uri=redirect_uri)
    write_json_file(path, token, private=True)


def _client_secret(client_type: str) -> str | None:
    if client_type != "server":
        return None
    load_local_env()
    return os.environ.get("FITBIT_CLIENT_SECRET") or getpass("Fitbit client secret: ")


def require_client_id(value: str | None) -> str:
    if value:
        return value
    raise ValueError(
        "Fitbit client id is required. Set FITBIT_CLIENT_ID in "
        ".local/secrets/fitbit.env or pass --client-id."
    )


def _authorization_url(
    client_id: str,
    redirect_uri: str,
    scopes: list[str],
    state: str,
    code_challenge: str,
    force_consent: bool,
) -> str:
    query = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(scopes),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    if force_consent:
        query["prompt"] = "consent"
    return f"{AUTH_URL}?{urllib.parse.urlencode(query)}"


def _exchange_code(
    code: str,
    code_verifier: str,
    client_id: str,
    client_secret: str | None,
    client_type: str,
    redirect_uri: str,
) -> dict[str, object]:
    body = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    if client_type == "server":
        if not client_secret:
            raise ValueError("Server app type requires FITBIT_CLIENT_SECRET.")
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {basic}"
    else:
        body["client_id"] = client_id

    return _json_request(TOKEN_URL, body, headers)


def parse_args() -> argparse.Namespace:
    load_local_env()
    parser = argparse.ArgumentParser(
        description="Authorize Fitbit Web API access and save local OAuth tokens."
    )
    parser.add_argument("--client-id", default=os.environ.get("FITBIT_CLIENT_ID"))
    parser.add_argument(
        "--client-type",
        choices=["server", "client"],
        default=os.environ.get("FITBIT_CLIENT_TYPE", "server"),
        help="Use 'server' for apps with a client secret, or 'client' for public apps.",
    )
    parser.add_argument(
        "--redirect-uri",
        default=os.environ.get("FITBIT_REDIRECT_URI", DEFAULT_REDIRECT_URI),
    )
    parser.add_argument("--token-file", default=DEFAULT_TOKEN_FILE)
    parser.add_argument("--scope", nargs="+", default=list(DEFAULT_SCOPES))
    parser.add_argument("--timeout-seconds", type=int, default=600)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--force-consent", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        token_path = Path(args.token_file)
        client_id = require_client_id(args.client_id)
        client_secret = _client_secret(args.client_type)

        code_verifier = _base64url(secrets.token_bytes(64))
        code_challenge = _base64url(hashlib.sha256(code_verifier.encode("ascii")).digest())
        state = secrets.token_urlsafe(32)
        authorization_url = _authorization_url(
            client_id,
            args.redirect_uri,
            args.scope,
            state,
            code_challenge,
            args.force_consent,
        )

        print("Open this Fitbit authorization URL:")
        print(authorization_url)
        if not args.no_browser:
            webbrowser.open(authorization_url)

        code = run_oauth_callback(args.redirect_uri, state, args.timeout_seconds, provider="Fitbit")
        token = _exchange_code(
            code,
            code_verifier,
            client_id,
            client_secret,
            args.client_type,
            args.redirect_uri,
        )
        _write_token(token_path, token, client_id, args.redirect_uri)
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Saved Fitbit tokens to {token_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
