"""Runtime I/O helpers shared by local scripts and agent skill scripts."""

from __future__ import annotations

import datetime as dt
import json
import os
import stat
from pathlib import Path
from typing import Any


def utc_now_iso(*, timespec: str = "seconds") -> str:
    """Return the current UTC time as a timezone-aware ISO 8601 string."""
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec=timespec)


def load_env_file(path: str | Path, *, override: bool = False) -> None:
    """Load KEY=VALUE pairs from a local env file without overwriting by default."""
    env_path = Path(path).expanduser()
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
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
        if not key or (not override and key in os.environ):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ[key] = value


def getenv_stripped(name: str, default: str = "") -> str:
    """Read an environment variable and strip surrounding whitespace."""
    return os.environ.get(name, default).strip()


def read_json_file(path: str | Path) -> Any:
    """Read JSON from a UTF-8 file."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json_file(
    path: str | Path,
    payload: Any,
    *,
    private: bool = False,
    sort_keys: bool = True,
) -> Path:
    """Write pretty JSON with a trailing newline using an atomic replace."""
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_suffix(output.suffix + ".tmp")
    text = json.dumps(payload, indent=2, sort_keys=sort_keys) + "\n"
    if private:
        # Create the temp file 0600 before writing, so secret payloads (tokens)
        # are never briefly readable at the default umask. fchmod also normalizes
        # the mode of any leftover temp from a crashed prior run.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), stat.S_IRUSR | stat.S_IWUSR)
            handle.write(text)
    else:
        tmp.write_text(text, encoding="utf-8")
    tmp.replace(output)  # atomic; preserves the temp file's mode
    return output


def ensure_private_dir(path: str | Path) -> Path:
    """Create a local-only state/secrets directory."""
    output = Path(path)
    output.mkdir(parents=True, exist_ok=True)
    output.chmod(stat.S_IRWXU)
    return output
