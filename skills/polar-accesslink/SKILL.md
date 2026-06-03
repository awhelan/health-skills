---
name: polar-accesslink
description: Use when authorizing Polar AccessLink, pulling Polar training sessions with stored OAuth tokens, normalizing the saved raw JSON into staged training_sessions/hr_samples/zones/laps tables, or calibrating Fitbit heart rate against Polar runs as ground truth.
---

# Polar AccessLink

Use this skill to authorize Polar AccessLink, pull training data with stored
OAuth tokens, normalize the saved raw JSON into staged CSV tables, and calibrate
Fitbit heart rate against Polar as ground truth.

Install the shared package and provider-script dependencies before running the
helpers directly:

```bash
uv pip install -e ".[scripts]"
```

## Authorize

Polar uses an OAuth2 authorization-code flow, unlike Dexcom (stored
username/password) or Nike (a per-run bearer token). This is a one-time
interactive step that produces a *refreshable* token; the pull phase refreshes
it automatically afterward.

1. Set `POLAR_CLIENT_ID` and `POLAR_CLIENT_SECRET` in
   `.local/secrets/polar.env` (see `.env.example`). Register the redirect URI
   `http://localhost:8001/auth/polar/callback` on your Polar developer app, or
   override it with `POLAR_REDIRECT_URI` / `--redirect-uri`.
2. Run the auth helper. It opens the Polar consent page, runs a localhost
   callback server, and saves tokens to `.local/state/polar/tokens.json`:

```bash
python skills/polar-accesslink/scripts/auth_polar_accesslink.py
```

3. Use `--no-browser` to print the URL instead of opening it; `--scope` to
   change requested scopes. Re-run only when the refresh token is revoked.

## Pull

The pull refreshes the stored token, downloads training sessions over the whole
date range plus feature-rich HR/zone/lap detail for a trailing window, saves the
raw JSON, then normalizes it.

1. Pull recent sessions and normalize them into the staged tables:

```bash
python skills/polar-accesslink/scripts/pull_polar_accesslink.py
```

2. Set the window with `--start-date`/`--end-date` (YYYY-MM-DD) or `--days N`.
   Default start: 7 days before the latest staged session (or 2019-01-01 on a
   first run) through today.
3. `--detail-days N` (default 45) controls how far back the per-second HR,
   zones, and laps are fetched; the lighter session summary always covers the
   full range.
4. Use `--force-refresh` to force a token refresh, and `--no-prompt` to fail
   instead of prompting for the client secret (used by the wearables
   orchestrator).
5. Verify the command output reports the expected session and HR-sample totals.

Raw payloads are saved per run under
`data/raw/polar_accesslink/run=<start>_<end>/` as `training_summary.json` and
`training_details.json`.

## Normalize

Normalize is the deterministic second phase: it reads the saved raw JSON and
merges it into the staged tables without calling Polar. Re-normalize either
through the pull wrapper or by running the normalizer directly:

```bash
python skills/polar-accesslink/scripts/pull_polar_accesslink.py --normalize-only
# or, equivalently:
python skills/polar-accesslink/scripts/normalize_polar_accesslink.py
```

Pointed at the raw root (the default), the normalizer reads every `run=*`
directory for a full rebuild; the pull phase points it at just the run it
fetched. Merges dedupe on `exercise_id` (plus offset/zone/lap keys), so
re-running over an overlapping window is idempotent.

## Calibrate (Fitbit vs Polar)

Polar's chest-strap HR is treated as ground truth for calibrating Fitbit's
wrist HR. These cross-provider scripts read both the Polar staged tables and the
Fitbit staged/Takeout data, so run a Fitbit pull first.

```bash
# Whole-run average-HR bias (matches runs by start/duration/distance):
python skills/polar-accesslink/scripts/calibrate_fitbit_polar.py

# 1-second sample-level bias (lag-aligns per-second HR curves):
python skills/polar-accesslink/scripts/calibrate_fitbit_polar_samples.py
```

Outputs land in `data/derived/polar_fitbit_calibration/` (match CSVs and JSON)
and `reports/wearables/calibration/` (Markdown summaries).

## Output Contract

The pull/normalize phases write four staged CSVs under `data/staged/polar/`:

```text
training_sessions.csv   one row per exercise (date, times, duration, distance,
                        pace, calories, HR avg/max, training load, etc.)
hr_samples.csv          per-second heart-rate samples with offset + timestamp
zones.csv               HR/power zone time and limits per exercise
laps.csv                manual and auto lap splits per exercise
```

Distances are miles, durations are minutes. `date` is the local
(`America/Los_Angeles`) calendar date; `hr_samples.timestamp` is naive local
clock time. Re-pulling an overlapping window or re-normalizing saved raw is
idempotent — rows replace by `exercise_id` (and offset/zone/lap index).

Provenance is written under `data/manifests/ingestions/`:

```text
data/manifests/ingestions/polar-accesslink-last-pull.json
data/manifests/ingestions/polar-accesslink-last-normalize.json
```

## Notes

- The auth helper only binds `http://localhost`/`127.0.0.1` redirect URIs and
  verifies the OAuth `state` parameter.
- Do not commit `.local/secrets/polar.env`, the cached token under
  `.local/state/polar/`, raw payloads, staged tables, or manifests.
- Sample-level calibration (`calibrate_fitbit_polar_samples.py`) reuses the
  Fitbit skill's `pull_fitbit` for token refresh and 1-second HR fetches,
  importing it from `skills/fitbit/scripts/` via a `sys.path` bridge. Run the
  Fitbit skill's auth/pull first so a Fitbit token is available.
- Do not treat this output as medical advice; it is a normalized data table for
  downstream analysis.
