#!/usr/bin/env python3
"""Run the Polar AccessLink OAuth2 flow and save tokens to .local/state."""

from __future__ import annotations

import argparse
import base64
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
from typing import Any

from healthdata.auth import run_oauth_callback, stamp_oauth_token
from healthdata.config import POLAR_ENV_FILE, POLAR_TOKEN_FILE
from healthdata.io import load_env_file as _load_env_file, write_json_file


AUTH_URL = "https://auth.polar.com/oauth/authorize"
TOKEN_URL = "https://auth.polar.com/oauth/token"
DEFAULT_REDIRECT_URI = "http://localhost:8001/auth/polar/callback"
DEFAULT_SCOPES = ("training_sessions:read", "sports:read", "routes:read")
DEFAULT_TOKEN_FILE = POLAR_TOKEN_FILE
LOCAL_ENV_FILE = POLAR_ENV_FILE


def load_local_env(path: str | Path = LOCAL_ENV_FILE) -> None:
    _load_env_file(path)


def _json_request(url: str, body: dict[str, str], headers: dict[str, str]) -> dict[str, Any]:
    encoded = urllib.parse.urlencode(body).encode("utf-8")
    request = urllib.request.Request(url, data=encoded, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Polar token request failed: HTTP {exc.code}: {detail}") from exc


def _write_token(path: Path, token: dict[str, Any], client_id: str, redirect_uri: str) -> None:
    stamp_oauth_token(token, client_id=client_id, redirect_uri=redirect_uri)
    write_json_file(path, token, private=True)


def _client_secret() -> str:
    load_local_env()
    value = os.environ.get("POLAR_CLIENT_SECRET")
    if value:
        return value
    return getpass("Polar client secret: ")


def _authorization_url(client_id: str, redirect_uri: str, scopes: list[str], state: str) -> str:
    query = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(scopes),
        "state": state,
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(query)}"


def _exchange_code(code: str, client_id: str, client_secret: str, redirect_uri: str) -> dict[str, Any]:
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
    body = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
    }
    headers = {
        "Accept": "application/json",
        "Authorization": f"Basic {basic}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    return _json_request(TOKEN_URL, body, headers)


def parse_args() -> argparse.Namespace:
    load_local_env()
    parser = argparse.ArgumentParser(
        description="Authorize Polar AccessLink API access and save local OAuth tokens."
    )
    parser.add_argument("--client-id", default=os.environ.get("POLAR_CLIENT_ID"))
    parser.add_argument("--redirect-uri", default=os.environ.get("POLAR_REDIRECT_URI", DEFAULT_REDIRECT_URI))
    parser.add_argument("--token-file", default=DEFAULT_TOKEN_FILE)
    parser.add_argument("--scope", nargs="+", default=list(DEFAULT_SCOPES))
    parser.add_argument("--timeout-seconds", type=int, default=600)
    parser.add_argument("--no-browser", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if not args.client_id:
            raise ValueError("POLAR_CLIENT_ID is required.")
        client_secret = _client_secret()
        token_path = Path(args.token_file)

        state = secrets.token_urlsafe(32)
        authorization_url = _authorization_url(args.client_id, args.redirect_uri, args.scope, state)
        print("Open this Polar authorization URL:")
        print(authorization_url)
        if not args.no_browser:
            webbrowser.open(authorization_url)

        code = run_oauth_callback(args.redirect_uri, state, args.timeout_seconds, provider="Polar")
        token = _exchange_code(code, args.client_id, client_secret, args.redirect_uri)
        _write_token(token_path, token, args.client_id, args.redirect_uri)
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Saved Polar tokens to {token_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
