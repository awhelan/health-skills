---
name: fitbit
description: Use when authorizing the Fitbit Web API, pulling recent Fitbit daily metrics / activity logs / TCX with stored OAuth tokens, or normalizing the saved raw payloads into the staged daily_metrics, activity_logs, and activity_tcx tables for fitness, sleep, or health analysis.
---

# Fitbit

Use this skill to authorize the Fitbit Web API, pull recent metrics with stored
OAuth tokens, and normalize the saved raw payloads into staged CSV tables.

Fitbit has two data sources that share one staged schema: the **Web API** (this
skill's recent delta) and a periodic **Google Takeout** export (the more
complete historical bulk, under `~/Documents/health/sources/fitbit_takeout/`).
The API pull is stitched to Takeout by date boundary — its default start date
picks up the day after the latest Takeout step date. The `takeout_fitbit.py`
helper field-unions the Takeout *daily* history into the same `daily_metrics.csv`
the API writes (see **Backfill from Takeout** below); extending that union to
activities and per-second `hr_samples` (Takeout supplying richer fields such as
HR PPG confidence) is the next step.

Fitbit's legacy Web API is scheduled for deprecation in September 2026. Use this
skill for current Fitbit Web API pulls, and plan future public integrations
around the Google Health API migration path.

Install the shared package and provider-script dependencies before running the
helpers directly:

```bash
uv pip install -e ".[scripts]"
```

## Authorize

Fitbit uses an OAuth2 PKCE flow. This is a one-time interactive step that
produces a refreshable token; the pull refreshes it automatically afterward.

1. Set `FITBIT_CLIENT_ID`/`FITBIT_CLIENT_TYPE` (and `FITBIT_CLIENT_SECRET` for a
   `server` app) in `.local/secrets/fitbit.env` (see `.env.example`). Register
   the redirect URI `http://localhost:8000/auth/fitbit/callback`.
2. Run the auth helper; it opens the consent page, serves a localhost callback,
   and saves tokens to `.local/state/fitbit/tokens.json`:

```bash
python skills/fitbit/scripts/auth_fitbit.py
```

3. Use `--no-browser` to print the URL, `--force-consent` to re-prompt scopes,
   `--client-type client` for a public (PKCE-only) app.

## Pull

1. Pull recent data and normalize it into the staged tables:

```bash
python skills/fitbit/scripts/pull_fitbit.py
```

2. Set the window with `--start-date`/`--end-date` (YYYY-MM-DD) or `--days N`.
   Default start: 2 days before the latest staged date (to re-settle late
   summaries), else the day after the latest Takeout step date, else 30 days.
3. `--skip-health-metrics` and `--skip-tcx` drop the optional SpO2/temp/cardio
   pulls and the per-activity TCX downloads. `--force-refresh` forces a token
   refresh; `--no-prompt` fails instead of prompting for the client secret.
4. Verify the command output reports the expected daily-row totals.

Raw payloads are saved per run under `data/raw/fitbit_api/run=<start>_<end>/`.
Per-activity TCX XML downloads are saved under that run's `tcx/` subdirectory.

## Normalize

Normalize is the deterministic second phase: it reads the saved raw JSON and
merges it into the staged tables without calling Fitbit. Re-normalize every
saved run directly:

```bash
python skills/fitbit/scripts/normalize_fitbit.py
```

Pointed at the raw root (the default), it reads every `run=*` directory and
merges; the pull phase normalizes just the run it fetched. Merges dedupe on
`date` (daily) and `log_id` (activities), so re-running is idempotent.

## Backfill from Takeout

The Web API only covers a recent window. `takeout_fitbit.py` ingests the deeper
Google Takeout export — years of daily history — into the same staged
`daily_metrics.csv`, so historical dates are populated alongside the API delta:

```bash
python skills/fitbit/scripts/takeout_fitbit.py
```

1. Reads the Takeout root (default `FITBIT_TAKEOUT_DIR`; override with
   `--takeout-root`) and writes `--staged-dir` (default `data/staged/fitbit/`).
2. The merge is a **field-union keyed on `date`, Takeout-preferred**: Takeout
   values win on overlapping dates, the API fills recent dates Takeout lacks, and
   Takeout-only columns (stress, sleep score, HRV, …) arrive as new fields. A
   `source` column records each row's origin (`api`, `takeout`, or `api+takeout`).
3. Re-running is idempotent — re-ingesting reproduces the same values and `source`
   labels. Run it before or after a Web API pull; order does not change the result.
4. Scope today is `daily_metrics` only; activity logs and per-second `hr_samples`
   are not yet unioned from Takeout.

## Output Contract

Staged CSVs under `data/staged/fitbit/`:

```text
daily_metrics.csv   one row per date: steps, distance, AZM, resting HR + zones,
                    HRV, sleep stages, SpO2/temp/VO2max, exercise rollups
activity_logs.csv   one row per logged exercise (key: log_id)
activity_tcx.csv    per-activity TCX manifest (key: log_id, paths to raw TCX)
```

Re-pulling an overlapping window or re-normalizing saved raw is idempotent —
rows replace by `date` / `log_id`.

Provenance is written under `data/manifests/ingestions/`:

```text
data/manifests/ingestions/fitbit-api-last-pull.json
data/manifests/ingestions/fitbit-last-normalize.json
data/manifests/ingestions/fitbit-takeout-last-normalize.json   (Takeout backfill)
```

## Notes

- OAuth refresh and the localhost authorization callback are shared via
  `healthdata.auth`; the JSON/env/private-file helpers via `healthdata.io`.
- Do not commit `.local/secrets/fitbit.env`, the cached token under
  `.local/state/fitbit/`, raw payloads, staged tables, or manifests.
- The dashboard and reports under `scripts/wearables/` are consumers of these
  staged tables, not part of this skill.
