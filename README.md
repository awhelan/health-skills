# Health Workspace

Local, agent-driven skills for personal health data. 
Provider **skills** pull from each source and normalize the results into staged 
tables; a shared `healthdata/` package holds the common paths, auth, and I/O. 
Everything personal or regenerable stays local — 
the `data/` tree and `.local/` secrets are gitignored.

## Skills

Agent workflow tools live in `skills/`. Each owns a `SKILL.md`, an
`agents/openai.yaml`, and bundled deterministic scripts, and follows a
**pull → normalize** flow (some add **calibrate**), writing staged tables under
`data/staged/<provider>/`.

- **fitbit** — Authorize the Fitbit Web API and pull recent daily metrics,
  activity logs, and TCX with stored OAuth tokens; normalize into staged
  `daily_metrics`/`activity_logs`/`activity_tcx`, and backfill the deeper Google
  Takeout history into the same daily table.
- **polar-accesslink** — Authorize Polar AccessLink and pull training sessions
  with stored OAuth tokens; normalize into `training_sessions`/`hr_samples`/
  `zones`/`laps`, and calibrate Fitbit wrist HR against Polar chest-strap HR as
  ground truth.
- **dexcom-clarity-cgm** — Pull Dexcom Clarity/Stelo CGM readings with stored
  credentials (or normalize manual Clarity/Stelo CSV exports) into one
  deduplicated staged `cgm_readings` table.
- **nike-run-club** — Pull Nike Run Club activity history with a user-provided
  bearer token, save the raw API pages, and normalize them into a staged
  `activities` table.

**Setup:** `uv pip install -e ".[scripts]"`, then follow the auth + pull steps in
each skill's `SKILL.md`. Provider secrets go in `.local/secrets/*.env`; OAuth
tokens and session state in `.local/state/<provider>/` (both gitignored). 

## Repository layout

- `healthdata/` — shared package: path constants, source resolution, and
  OAuth/IO helpers (`healthdata.config` is the single source of paths).
- `skills/` — the provider skills above.
- `scripts/` — workspace orchestration and analysis (`wearables/`, `genetics/`,
  `nutrition/`). (To be added later)
- `docs/` — domain notes (`fitness.md`, `nutrition.md`, …).
- `annotations/` — human notes; `annotations/history/personal.md` holds personal
  and family history to weigh alongside labs and genetics.
- `reports/` — generated summaries and analysis.
- `config/sources/*.example.toml` — tracked, non-secret source-config examples.
- `DATA_LAYOUT.md` — full data layout, the two storage roots, and conventions.


## Data

The `data/` tree is **gitignored and does not exist in a fresh checkout** — it is
created the first time you run a skill's pull. After pulls (and normalizes) you
will have:

- `data/raw/` — immutable per-pull API caches: `fitbit_api/run=<range>/`,
  `polar_accesslink/run=<range>/`, `nike_run_club_api/export=<date>/api_pages/`.
- `data/staged/` — deduplicated provider tables: `fitbit/`, `polar/`,
  `dexcom_clarity/`, `nike_run_club/`.
- `data/derived/` — regenerable reports and intermediates (e.g.
  `polar_fitbit_calibration/`).
- `data/manifests/ingestions/` — per-pull/normalize provenance and
  `*-last-pull.json` pointers.

Batch exported reference sources (Fitbit Takeout, genetics, labs, …) are **not**
under `data/`; they can live in an external sources root of your choice,
`~/Documents/health/sources/` (override with `HEALTH_SOURCES_DIR`). See
`DATA_LAYOUT.md` for the two-root model, dedup/format policy, and the planned
`canonical/` and `marts/` layers.
