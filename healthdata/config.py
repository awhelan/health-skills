"""Central configuration: paths, source resolution, and layout constants."""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

# Repo root = parent of the healthdata package.
WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOCAL_TIMEZONE = "America/Los_Angeles"


def workspace_path(path: Path) -> str:
    """Render a path relative to the workspace root for logs and manifests."""
    resolved = Path(path).resolve()
    try:
        return str(resolved.relative_to(WORKSPACE_ROOT))
    except ValueError:
        return str(resolved)


DATA_DIR = WORKSPACE_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
STAGED_DIR = DATA_DIR / "staged"
DERIVED_DIR = DATA_DIR / "derived"
MANIFESTS_DIR = DATA_DIR / "manifests"

# Original external source dumps (Google Takeout, genome export, etc.) live
# outside the code tree. Override with HEALTH_SOURCES_DIR; default below.
SOURCES_DIR = Path(os.environ.get("HEALTH_SOURCES_DIR") or (Path.home() / "Documents" / "health" / "sources"))
SOURCES = SOURCES_DIR  # alias used by the genetics scripts

FITBIT_TAKEOUT_DIR = SOURCES_DIR / "fitbit_takeout" / "export=2026-05-23"
FITBIT_API_RAW_DIR = RAW_DIR / "fitbit_api"
POLAR_API_RAW_DIR = RAW_DIR / "polar_accesslink"
DEXCOM_CLARITY_RAW_DIR = RAW_DIR / "dexcom_clarity"
NIKE_RUN_CLUB_RAW_ROOT = RAW_DIR / "nike_run_club_api"
NIKE_RUN_CLUB_EXPORT_DATE_ENV = "NIKE_RUN_CLUB_EXPORT_DATE"
NIKE_RUN_CLUB_EXPORT_DATE = os.environ.get(NIKE_RUN_CLUB_EXPORT_DATE_ENV) or dt.date.today().isoformat()
# Today's dated export subdir under the raw root. The skill's pull script recomputes
# this at call time; this lets the wearables orchestrator target the same directory.
NIKE_RUN_CLUB_EXPORT_DIR = NIKE_RUN_CLUB_RAW_ROOT / f"export={NIKE_RUN_CLUB_EXPORT_DATE}"
NIKE_RUN_CLUB_TIMEZONE_ENV = "NIKE_RUN_CLUB_TIMEZONE"
DEXCOM_CLARITY_TIMEZONE_ENV = "DEXCOM_CLARITY_TIMEZONE"

FITBIT_STAGED_DIR = STAGED_DIR / "fitbit"
POLAR_STAGED_DIR = STAGED_DIR / "polar"
DEXCOM_CLARITY_STAGED_DIR = STAGED_DIR / "dexcom_clarity"
NIKE_RUN_CLUB_STAGED_DIR = STAGED_DIR / "nike_run_club"

FITBIT_TIGHTER_DERIVED_DIR = DERIVED_DIR / "fitbit_tighter_report"
POLAR_FITBIT_CALIBRATION_DERIVED_DIR = DERIVED_DIR / "polar_fitbit_calibration"
INGESTION_MANIFESTS_DIR = MANIFESTS_DIR / "ingestions"
FITBIT_LAST_PULL_FILE = INGESTION_MANIFESTS_DIR / "fitbit-api-last-pull.json"
FITBIT_LAST_NORMALIZE_FILE = INGESTION_MANIFESTS_DIR / "fitbit-last-normalize.json"
POLAR_LAST_PULL_FILE = INGESTION_MANIFESTS_DIR / "polar-accesslink-last-pull.json"
POLAR_LAST_NORMALIZE_FILE = INGESTION_MANIFESTS_DIR / "polar-accesslink-last-normalize.json"
NIKE_LAST_PULL_FILE = INGESTION_MANIFESTS_DIR / "nike-run-club-last-pull.json"
NIKE_LAST_NORMALIZE_FILE = INGESTION_MANIFESTS_DIR / "nike-run-club-last-normalize.json"
DEXCOM_LAST_PULL_FILE = INGESTION_MANIFESTS_DIR / "dexcom-clarity-last-pull.json"
DEXCOM_LAST_NORMALIZE_FILE = INGESTION_MANIFESTS_DIR / "dexcom-clarity-last-normalize.json"
WEARABLES_LAST_PULL_FILE = INGESTION_MANIFESTS_DIR / "wearables-last-pull.json"

WEARABLE_REPORTS_DIR = WORKSPACE_ROOT / "reports" / "wearables"
FITBIT_TIGHTER_REPORT_DIR = WEARABLE_REPORTS_DIR / "fitbit_tighter_report"
CALIBRATION_REPORT_DIR = WEARABLE_REPORTS_DIR / "calibration"

LOCAL_DIR = WORKSPACE_ROOT / ".local"
SECRETS_DIR = LOCAL_DIR / "secrets"
STATE_DIR = LOCAL_DIR / "state"

FITBIT_TOKEN_FILE = STATE_DIR / "fitbit" / "tokens.json"
FITBIT_ENV_FILE = SECRETS_DIR / "fitbit.env"
POLAR_TOKEN_FILE = STATE_DIR / "polar" / "tokens.json"
POLAR_ENV_FILE = SECRETS_DIR / "polar.env"
DEXCOM_ENV_FILE = SECRETS_DIR / "dexcom_clarity.env"
DEXCOM_CACHE_DIR = STATE_DIR / "dexcom_clarity"


# --- 23andMe genome source resolution -------------------------------------
# The export filename embeds the account name, so it is never committed. Set
# HEALTH_GENOME_FILE (absolute path, or filename relative to the 23andMe dir),
# else auto-discover the newest export=*/genome*.txt.

def genome_dir(sources_dir: Path | None = None) -> Path:
    """Directory holding dated 23andMe export folders."""
    return (sources_dir or SOURCES_DIR) / "genetics" / "23andme"


def resolve_genome_file(sources_dir: Path | None = None) -> Path:
    """Locate the 23andMe raw genome .txt export (see module note for order)."""
    base = genome_dir(sources_dir)
    env = os.environ.get("HEALTH_GENOME_FILE")
    if env:
        candidate = Path(env).expanduser()
        return candidate if candidate.is_absolute() else base / env
    matches = sorted(base.glob("export=*/genome*.txt")) or sorted(base.glob("export=*/*.txt"))
    if matches:
        return matches[-1]
    return base / "export=YYYY-MM-DD" / "genome.txt"
