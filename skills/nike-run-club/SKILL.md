---
name: nike-run-club
description: Use when pulling Nike Run Club activities with a user-provided bearer token, saving raw API pages, and normalizing them into a staged activities.csv table for wearable, fitness, or health analysis.
---

# Nike Run Club

Use this skill to pull Nike Run Club activity history with a user-provided
bearer token, save raw API pages, then normalize them into one staged activity
table.

## Pull

Nike access tokens are short-lived (they expire after a few hours), so this
skill never stores them or reads them from the environment. Get a fresh token
each run and pass it directly.

1. Ask the user for a fresh token from a logged-in browser. Tell them to:
   - Log into Nike Run Club / nike.com.
   - Open DevTools (F12) and select the **Network** tab, then reload the page or
     open an activity so requests appear.
   - Filter for `api.nike.com`, click one of those requests, then right-click →
     **Copy** → **Copy as cURL** (or just copy the `authorization` request
     header).
   - Paste it back here.
2. Fish the `Authorization: Bearer ...` token out of the paste. The pull script
   accepts a pasted header or whole cURL blob via stdin and extracts the token
   itself, so you can hand it the paste verbatim without putting the token in
   process argv.
3. Pull and normalize, passing the token for this run only. Run the command,
   paste the header/cURL blob into stdin, then send EOF:

```bash
python skills/nike-run-club/scripts/pull_nike_run_club.py --force --bearer-token-stdin
```

   The pull requires the `requests` package.

4. Use `--start-date`/`--end-date` (YYYY-MM-DD) to limit normalized output by
   activity date.
5. Verify the command output and inspect
   `data/staged/nike_run_club/activities.csv`.

Do not store the token or print it back. If the pull fails with HTTP 401, the
token has expired — ask for a fresh one and re-run.

## Normalize

Pull writes raw API pages under
`data/raw/nike_run_club_api/export=<date>/api_pages/`;
`normalize_nike_run_club.py` is the deterministic second phase. Re-normalize
existing raw pages without calling Nike, either through the pull wrapper or by
running the normalizer directly:

```bash
python skills/nike-run-club/scripts/pull_nike_run_club.py --normalize-only
# or, equivalently:
python skills/nike-run-club/scripts/normalize_nike_run_club.py
```

Both pull and normalize accept `--timezone` (or `NIKE_RUN_CLUB_TIMEZONE`).

## Output Contract

The primary output is:

```text
data/staged/nike_run_club/activities.csv
```

The table has one row per deduplicated activity and includes dates, source
file, source format, activity type, start/end times, duration, distance (miles
plus the raw Nike value and its unit guess), calories, steps, pace/speed/cadence
means, heart-rate summaries, location and start-location tags, weather, terrain,
title, temperature, and a reserved notes column.

Activity dates and naive timestamps are normalized to tz-aware ISO 8601 in
`America/Los_Angeles` by default (override with `--timezone IANA/Name` or
`NIKE_RUN_CLUB_TIMEZONE`), matching the dexcom-clarity-cgm staged table so the
two join cleanly.

Pull and normalize provenance are written under `data/manifests/ingestions/`:

```text
data/manifests/ingestions/nike-run-club-last-pull.json
data/manifests/ingestions/nike-run-club-last-normalize.json
```

## Notes

- Nike does not provide a stable public export API; treat this as a
  user-authorized historical backfill, not a scheduled sync.
- The bearer token is passed per run via `--bearer-token-stdin` and never stored;
  do not write it to a file or commit it.
- Do not commit raw pages, staged tables, or manifests containing personal
  activity metadata.
