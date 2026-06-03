#!/usr/bin/env python3
"""Authenticate to Dexcom Clarity and resolve a Clarity subject session.

This module owns everything needed to turn stored credentials into a usable
Clarity subject token: the local credential/session settings, the cookie jar and
session cache under `.local/state/dexcom_clarity/`, JWT freshness checks, and the
UAM/Keycloak login flow (which changes per session, so the form actions are
discovered from the returned HTML rather than replayed from captured URLs).

`pull_dexcom_clarity.py` imports `resolve_session` (and friends) from here. It is
also runnable on its own to refresh/validate the cached session without pulling:

    python auth_dexcom_clarity.py            # log in if needed, cache the session
    python auth_dexcom_clarity.py --no-login # validate cached/env token only
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import html
import json
import os
import re
import secrets
import stat
import sys
import time
import urllib.parse
from dataclasses import dataclass
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from typing import Any

try:
    import requests
except ImportError as exc:  # pragma: no cover - exercised by missing local dep
    raise SystemExit("This script requires `requests`: python3 -m pip install requests") from exc


WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ENV_FILE = WORKSPACE_ROOT / ".local/secrets/dexcom_clarity.env"
DEFAULT_CACHE_DIR = WORKSPACE_ROOT / ".local/state/dexcom_clarity"

CLARITY_CLIENT_ID = "DAEC20AC-9626-4B0E-94B5-B674E298F51E"
CLARITY_CALLBACK = "https://clarity.dexcom.com/users/auth/dexcom_sts/callback"
UAM_AUTHORIZE_URL = "https://uam1.dexcom.com/identity/connect/authorize"
DEFAULT_API_BASE = "https://clarity.dexcom.com/api"
TOKEN_REFRESH_MARGIN_SECONDS = 300

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:151.0) "
    "Gecko/20100101 Firefox/151.0"
)


@dataclass(frozen=True)
class Settings:
    username: str
    password: str
    subject_id: str
    subject_token: str
    first_name: str
    last_name: str
    date_of_birth: str
    locale: str
    units: str
    api_base: str
    cache_dir: Path


def load_env_file(path: Path) -> None:
    path = path.expanduser()
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or key in os.environ:
            continue
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {"'", '"'}
        ):
            value = value[1:-1]
        os.environ[key] = value


def getenv(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def load_settings(env_file: Path, cache_dir: Path) -> Settings:
    load_env_file(env_file)
    return Settings(
        username=getenv("DEXCOM_CLARITY_USERNAME"),
        password=getenv("DEXCOM_CLARITY_PASSWORD"),
        subject_id=getenv("DEXCOM_CLARITY_SUBJECT_ID"),
        subject_token=getenv("DEXCOM_CLARITY_SUBJECT_TOKEN"),
        first_name=getenv("DEXCOM_CLARITY_FIRST_NAME"),
        last_name=getenv("DEXCOM_CLARITY_LAST_NAME"),
        date_of_birth=getenv("DEXCOM_CLARITY_DATE_OF_BIRTH"),
        locale=getenv("DEXCOM_CLARITY_LOCALE", "en-US"),
        units=getenv("DEXCOM_CLARITY_UNITS", "mgdl"),
        api_base=getenv("DEXCOM_CLARITY_API_BASE", DEFAULT_API_BASE).rstrip("/"),
        cache_dir=cache_dir.expanduser(),
    )


def require_settings(settings: Settings, no_login: bool) -> None:
    missing: list[str] = []
    if not settings.username and not no_login:
        missing.append("DEXCOM_CLARITY_USERNAME")
    if not settings.password and not no_login:
        missing.append("DEXCOM_CLARITY_PASSWORD")
    if missing:
        raise SystemExit(f"Missing required Dexcom env settings: {', '.join(missing)}")


def strict_private_file(path: Path) -> None:
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except FileNotFoundError:
        pass


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(stat.S_IRWXU)


def write_json_file(path: Path, payload: object, *, private: bool = False) -> Path:
    """Write pretty JSON with a trailing newline using an atomic replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if private:
        strict_private_file(tmp)
    tmp.replace(path)
    if private:
        strict_private_file(path)
    return path


def session_cache_path(settings: Settings) -> Path:
    return settings.cache_dir / "session.json"


def cookie_jar_path(settings: Settings) -> Path:
    return settings.cache_dir / "cookies.txt"


def load_session_cache(settings: Settings) -> dict[str, Any]:
    path = session_cache_path(settings)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_session_cache(settings: Settings, data: dict[str, Any]) -> None:
    ensure_private_dir(settings.cache_dir)
    path = session_cache_path(settings)
    write_json_file(path, data, private=True)


def make_http_session(settings: Settings) -> requests.Session:
    ensure_private_dir(settings.cache_dir)
    jar = MozillaCookieJar(str(cookie_jar_path(settings)))
    if cookie_jar_path(settings).exists():
        try:
            jar.load(ignore_discard=True, ignore_expires=True)
        except Exception:
            jar.clear()

    session = requests.Session()
    session.cookies = jar
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    return session


def save_cookies(session: requests.Session, settings: Settings) -> None:
    if isinstance(session.cookies, MozillaCookieJar):
        session.cookies.save(ignore_discard=True, ignore_expires=True)
        strict_private_file(cookie_jar_path(settings))


def decode_jwt_payload(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
    except Exception:
        return {}


def token_is_fresh(token: str) -> bool:
    payload = decode_jwt_payload(token)
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)):
        return False
    return exp - time.time() > TOKEN_REFRESH_MARGIN_SECONDS


def token_subject_id(token: str) -> str:
    payload = decode_jwt_payload(token)
    subject_id = payload.get("subjectId")
    return subject_id if isinstance(subject_id, str) else ""


def normalize_session_data(data: dict[str, Any], settings: Settings) -> dict[str, Any]:
    token = data.get("access_token") or data.get("accessToken") or settings.subject_token
    if token:
        data["access_token"] = token

    subject_id = (
        data.get("subject_id")
        or data.get("subjectId")
        or data.get("ssSubjectId")
        or settings.subject_id
        or token_subject_id(token or "")
    )
    if subject_id:
        data["subject_id"] = str(subject_id)

    return data


def cached_or_env_session(settings: Settings, force_login: bool) -> dict[str, Any]:
    env_data = normalize_session_data(
        {
            "access_token": settings.subject_token,
            "subject_id": settings.subject_id,
            "source": "env",
        },
        settings,
    )
    if env_data.get("access_token") and token_is_fresh(env_data["access_token"]):
        return env_data

    if force_login:
        return {}

    cached = normalize_session_data(load_session_cache(settings), settings)
    if cached.get("access_token") and token_is_fresh(cached["access_token"]):
        cached["source"] = cached.get("source") or "cache"
        return cached
    return {}


def build_authorize_url(username: str) -> str:
    state = secrets.token_hex(24)
    nonce = secrets.token_hex(16)
    params = {
        "client_id": CLARITY_CLIENT_ID,
        "redirect_uri": CLARITY_CALLBACK,
        "scope": "openid offline_access AccountManagement",
        "response_type": "code",
        "state": state,
        "nonce": nonce,
        "ui_locales": "en-US",
        "login_hint": username,
        "acr_values": "login_only:true",
    }
    return f"{UAM_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


def parse_login_action(text: str) -> str:
    return parse_form_action(text, form_id="kc-form-login") or parse_form_action(text)


def parse_form_action(
    text: str,
    form_id: str = "",
    input_name: str = "",
    input_value: str = "",
) -> str:
    forms = re.findall(r"<form\b[^>]*>.*?</form>", text, flags=re.DOTALL | re.IGNORECASE)
    for form in forms:
        if form_id and not re.search(
            rf'id=["\']{re.escape(form_id)}["\']',
            form,
            flags=re.IGNORECASE,
        ):
            continue
        if input_name and not re.search(
            rf'name=["\']{re.escape(input_name)}["\']',
            form,
            flags=re.IGNORECASE,
        ):
            continue
        if input_value and not re.search(
            rf'value=["\']{re.escape(input_value)}["\']',
            form,
            flags=re.IGNORECASE,
        ):
            continue

        match = re.search(r'action=["\']([^"\']+)["\']', form, flags=re.IGNORECASE)
        if match:
            return html.unescape(match.group(1))
    return ""


def parse_form_input_value(text: str, name: str, form_id: str = "") -> str:
    forms = re.findall(r"<form\b[^>]*>.*?</form>", text, flags=re.DOTALL | re.IGNORECASE)
    for form in forms:
        if form_id and not re.search(
            rf'id=["\']{re.escape(form_id)}["\']',
            form,
            flags=re.IGNORECASE,
        ):
            continue
        pattern = rf'<input\b[^>]*name=["\']{re.escape(name)}["\'][^>]*>'
        input_match = re.search(pattern, form, flags=re.IGNORECASE)
        if not input_match:
            continue
        value_match = re.search(
            r'value=["\']([^"\']*)["\']',
            input_match.group(0),
            flags=re.IGNORECASE,
        )
        if value_match:
            return html.unescape(value_match.group(1))
    return ""


def has_password_input(text: str) -> bool:
    return bool(re.search(r'name=["\']password["\']', text))


def extract_local_storage_session(text: str) -> dict[str, Any]:
    bootstrap_match = re.search(
        r"const\s+data\s*=\s*\{(?P<body>.*?)\}\s*;\s*"
        r"window\.localStorage\.setItem\(\s*['\"]clarity_externalSession['\"]\s*,\s*"
        r"JSON\.stringify\(\s*data\s*\)\s*\)",
        text,
        re.DOTALL,
    )
    if bootstrap_match:
        data: dict[str, Any] = {}
        body = bootstrap_match.group("body")
        for key in ("ssSubjectId", "subjectId", "accessToken"):
            value_match = re.search(
                rf"\b{key}\s*:\s*(['\"])(.*?)\1",
                body,
                re.DOTALL,
            )
            if value_match:
                data[key] = html.unescape(value_match.group(2))
        if data:
            return data

    patterns = [
        r"localStorage\.setItem\(\s*['\"]clarity_externalSession['\"]\s*,\s*JSON\.stringify\((\{.*?\})\)\s*\)",
        r"localStorage\.setItem\(\s*['\"]clarity_externalSession['\"]\s*,\s*(['\"])(.*?)\1\s*\)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.DOTALL)
        if not match:
            continue
        raw = match.group(1)
        if raw in {"'", '"'} and len(match.groups()) > 1:
            raw = html.unescape(match.group(2))
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            continue
    return {}


def extract_session_from_response(response: requests.Response, settings: Settings) -> dict[str, Any]:
    parsed = urllib.parse.urlparse(response.url)
    query = urllib.parse.parse_qs(parsed.query)
    data: dict[str, Any] = {}
    key_map = {
        "subjectId": "subject_id",
        "accessToken": "access_token",
        "sessionExpiration": "session_expiration",
        "inactiveDuration": "inactive_duration",
    }
    for source_key, dest_key in key_map.items():
        values = query.get(source_key)
        if values:
            data[dest_key] = values[0]

    if not data.get("access_token") and response.text:
        data.update(extract_local_storage_session(response.text))

    data = normalize_session_data(data, settings)
    if data.get("access_token"):
        data["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    return data


def login_error_hint(text: str) -> str:
    if "kc-form-login" in text and "otp-login-button" in text:
        return (
            "Password-only login did not advance; Dexcom may require a passkey, "
            "one-time code, or a corrected stored password."
        )

    stripped = re.sub(r"<(script|style)\b.*?</\1>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    stripped = re.sub(r"<[^>]+>", " ", stripped)
    stripped = re.sub(r"\s+", " ", html.unescape(stripped)).strip()
    candidates = [
        "Invalid username or password",
        "Invalid password",
        "Try again",
        "passkey",
        "verification",
        "temporarily disabled",
    ]
    lower = stripped.lower()
    for candidate in candidates:
        index = lower.find(candidate.lower())
        if index >= 0:
            return stripped[max(0, index - 80) : index + 220]
    return "Dexcom returned the login form again."


def post_html_form(
    session: requests.Session,
    action: str,
    form_data: list[tuple[str, str]],
    referer: str,
    timeout: int = 75,
    require_same_origin: bool = False,
) -> requests.Response:
    url = urllib.parse.urljoin(referer, action)
    if require_same_origin:
        referer_parts = urllib.parse.urlparse(referer)
        url_parts = urllib.parse.urlparse(url)
        if (url_parts.scheme, url_parts.netloc) != (referer_parts.scheme, referer_parts.netloc):
            raise RuntimeError(f"Refusing to post Dexcom credentials to cross-origin form action: {url}")

    response = session.post(
        url,
        data=form_data,
        headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "null",
            "Referer": referer,
        },
        allow_redirects=True,
        timeout=timeout,
    )
    response.raise_for_status()
    return response


def skip_optional_passkey_setup(
    settings: Settings,
    session: requests.Session,
    response: requests.Response,
) -> requests.Response:
    if "required-action-register-passkey" not in response.url and "notNowButton" not in response.text:
        return response

    action = parse_form_action(response.text, form_id="configCredentials")
    if not action:
        action = parse_form_action(
            response.text,
            input_name="requiredActionName",
            input_value="Not now",
        )
    if not action:
        return response

    return post_html_form(
        session,
        action,
        [("requiredActionName", "Not now")],
        referer=response.url,
        timeout=75,
    )


def submit_clarity_handoff(
    session: requests.Session,
    response: requests.Response,
) -> requests.Response:
    if "clarity.dexcom.com/sts_redirect/login" not in response.url and 'id="login_button"' not in response.text:
        return response

    action = parse_form_action(response.text, form_id="login_button")
    token = parse_form_input_value(response.text, "authenticity_token", form_id="login_button")
    if not action or not token:
        return response

    handoff_response = session.post(
        urllib.parse.urljoin(response.url, action),
        data=[("authenticity_token", token)],
        headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://clarity.dexcom.com",
            "Referer": response.url,
        },
        allow_redirects=True,
        timeout=75,
    )
    handoff_response.raise_for_status()
    return handoff_response


def login(settings: Settings, session: requests.Session, no_login: bool) -> dict[str, Any]:
    response = session.get(build_authorize_url(settings.username), allow_redirects=True, timeout=45)
    response.raise_for_status()
    response = submit_clarity_handoff(session, response)

    data = extract_session_from_response(response, settings)
    if data.get("access_token"):
        return data

    action = parse_login_action(response.text)
    if not action:
        raise RuntimeError(
            "Dexcom auth did not return a Clarity session or a recognizable login form."
        )
    if no_login:
        raise RuntimeError("Cached/env token is expired and --no-login was requested.")

    if not has_password_input(response.text):
        username_response = post_html_form(
            session,
            action,
            [
                ("user-type", "email"),
                ("username", settings.username),
                ("credentialId", ""),
                ("login", "Next"),
            ],
            referer=response.url,
            timeout=45,
            require_same_origin=True,
        )
        username_response = submit_clarity_handoff(session, username_response)
        data = extract_session_from_response(username_response, settings)
        if data.get("access_token"):
            return data

        action = parse_login_action(username_response.text)
        if not action:
            raise RuntimeError(
                "Dexcom username step did not return a Clarity session or password form. "
                f"{login_error_hint(username_response.text)}"
            )
        response = username_response

    post_response = post_html_form(
        session,
        action,
        [
            ("username", settings.username),
            ("password", settings.password),
            ("credentialId", ""),
            ("login", ""),
        ],
        referer=response.url,
        timeout=75,
        require_same_origin=True,
    )
    post_response = skip_optional_passkey_setup(settings, session, post_response)
    post_response = submit_clarity_handoff(session, post_response)
    data = extract_session_from_response(post_response, settings)
    if not data.get("access_token"):
        raise RuntimeError(
            "Dexcom login completed without a Clarity subject token. "
            f"{login_error_hint(post_response.text)}"
        )
    return data


def resolve_session(
    settings: Settings,
    http: requests.Session,
    force_login: bool,
    no_login: bool,
) -> dict[str, Any]:
    data = cached_or_env_session(settings, force_login)
    if data:
        return data

    if no_login:
        raise RuntimeError("Cached/env token is expired or missing and --no-login was requested.")

    data = login(settings, http, no_login)
    save_session_cache(settings, data)
    save_cookies(http, settings)
    return data


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Authenticate to Dexcom Clarity and cache the subject session, without pulling data."
    )
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--force-login", action="store_true", help="Ignore cached subject token.")
    parser.add_argument(
        "--no-login",
        action="store_true",
        help="Use only cached/env token; fail instead of posting the password.",
    )
    args = parser.parse_args()
    if args.force_login and args.no_login:
        parser.error("--force-login and --no-login are mutually exclusive")
    return args


def main() -> int:
    args = parse_args()
    settings = load_settings(args.env_file, args.cache_dir)
    require_settings(settings, args.no_login)
    http = make_http_session(settings)
    try:
        session_data = resolve_session(settings, http, args.force_login, args.no_login)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except requests.RequestException as exc:
        print(f"network error: {exc}", file=sys.stderr)
        return 1
    finally:
        save_cookies(http, settings)
    print(
        "Cached Dexcom Clarity session for subject "
        f"{session_data.get('subject_id', '<unknown>')}."
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
