---
name: dexcom-clarity-cgm
description: Use when pulling Dexcom Clarity or Stelo CGM readings with stored credentials, or normalizing Clarity/Stelo CSV exports into a deduplicated staged CGM readings table, cleaning overlapping exports, or preparing glucose readings for food, sleep, wearable, or health analyses.
---

# Dexcom Clarity CGM

Use this skill to pull Dexcom Clarity/Stelo CGM readings with stored
credentials, or to deterministically normalize Clarity/Stelo CSV exports into
one deduplicated staged table.

## Pull

Dexcom Clarity sessions are reusable across pulls, so this skill reads
credentials from a local env file and caches cookies/session state under
`.local/state/`. That differs from Nike Run Club, where bearer tokens are
short-lived and must be passed per run.

Authentication — the UAM/Keycloak login flow, cookie jar, session cache, and
token freshness — lives in `auth_dexcom_clarity.py`. The pull resolves a session
through it automatically; you can also refresh or validate the cached session on
its own without pulling data:

```bash
python skills/dexcom-clarity-cgm/scripts/auth_dexcom_clarity.py            # log in if needed
python skills/dexcom-clarity-cgm/scripts/auth_dexcom_clarity.py --no-login # validate cached/env token only
```

1. Check for credentials in `.local/secrets/dexcom_clarity.env` (or pass
   `--env-file`). At minimum set `DEXCOM_CLARITY_USERNAME` and
   `DEXCOM_CLARITY_PASSWORD`; see `.env.example` for the optional fields.
2. Pull recent readings and normalize them into the deduplicated table:

```bash
python skills/dexcom-clarity-cgm/scripts/pull_dexcom_clarity.py
```

3. Set the export window with `--start-date`/`--end-date` (YYYY-MM-DD) or
   `--days N`. Default: the last 15 days through tomorrow.
4. Naive Clarity timestamps are tagged as `America/Los_Angeles` by default;
   override with `--timezone IANA/Name` or `DEXCOM_CLARITY_TIMEZONE`.
5. Use `--no-normalize` (or `--output PATH`) to save a dated raw CSV export
   instead of merging into the deduplicated table.
6. Verify the command output reports the expected glucose-row count and the
   first/last timestamps.

The pull requires the `requests` package. It caches the Clarity session and
cookies under `.local/state/dexcom_clarity/`, so repeat pulls skip re-login
until the token expires.

## Normalize

Use the normalizer directly when you already have Clarity/Stelo CSV exports
(e.g. manual downloads) and just need them deduplicated into the table.

1. Locate the target table. Default: `data/staged/dexcom_clarity/cgm_readings.csv`.
2. Locate export CSVs. If no exports are provided, the script reads every `*.csv`
   beside the target table except the table itself.
3. Run:

```bash
python skills/dexcom-clarity-cgm/scripts/normalize_dexcom_clarity.py [exports ...] --table data/staged/dexcom_clarity/cgm_readings.csv
```

4. Use `--prune` only after confirming the exports should be deleted after
   successful normalization.
5. Verify the command output reports the expected number of new and total
   readings.

## Output Contract

The normalized table is a CSV with one row per reading timestamp:

```text
timestamp,glucose_mg_dl,source_device,transmitter_id
```

Timestamps are emitted as tz-aware ISO 8601 strings in the local account
timezone (`America/Los_Angeles` by default), matching the Nike staged table.
Re-running the same export, or re-pulling an overlapping window, is idempotent.
Overlapping readings replace the row with the same timestamp.

Pull and normalize provenance are written to:

```text
data/manifests/ingestions/dexcom-clarity-last-pull.json
data/manifests/ingestions/dexcom-clarity-last-normalize.json
```

## Notes

- The parser extracts `EGV` rows and ignores calibration or non-glucose events.
- Header matching is case-insensitive and works with standard Clarity labels
  such as `Timestamp`, `Glucose Value`, `Event Type`, `Source Device`, and
  `Transmitter ID`.
- Do not commit `.local/secrets/dexcom_clarity.env`, the cached session under
  `.local/state/`, staged tables, or manifests.
- Do not treat this output as medical advice; it is a normalized data table for
  downstream analysis.
