# Changelog

## Unreleased

### Breaking

- **Isolated run folders (platform mode).** Every run of a recorded command writes its
  artifacts and log to its own folder,
  `<LOG_ROOT_LOCATION>/<project>/<YYYY-MM-DD>/<HH>/<job id>/` (UTC), instead of the
  project's `target/`:
  - `LOG_ROOT_LOCATION` (`--log-root-location`, `LOG_ROOT_LOCATION=` in `dbtx.conf`,
    `DBTX_LOG_ROOT_LOCATION`) is **required** in platform mode (exit 2 without it). The
    example and template configs set `LOG_ROOT_LOCATION=.dbtx/logs`
  - `--target-path` and `--log-path` on a platform command line → exit 2
  - every run has a job id (`--job-id ID`, or a generated one), handed to dbt as
    `DBT_ENV_CUSTOM_ENV_DBTX_JOB_ID`; `DBTX_JOB_ID` in `--vars` → exit 2 in platform mode.
    A job id whose run folder already exists (within the hour) → exit 2
  - `--log-root-location` and `--job-id` with `--standalone-mode` → exit 2 (standalone
    runs keep dbt's `target/` and `logs/`)

- **Platform mode and the database are now required by default.** dbtx records every
  run of a platform project (see "Run history store" in the README), so two problems
  that used to print a warning now stop the command with exit code 2 before dbt
  starts:
  - **No platform found.** Running from inside a single project folder
    (`cd jaffle-shop && dbtx build`) without a `dbtx.conf` platform root. To keep
    running a project on its own, add `--standalone-mode` (no config file, no
    database). To record its runs, set up a platform root (see "Configuration").
  - **No `DB_URI`.** A platform whose commands record runs (`build`, `compile`, `run`,
    `test`, `seed`, `snapshot`, `parse`, `run-operation`, `source freshness`,
    `docs generate`) needs a database URI. Set `DB_URI` (`--db-uri`, `DB_URI=` in
    `dbtx.conf`, or `DBTX_DB_URI`), or pass `--db-backend disabled` (or
    `DB_BACKEND=disabled`) to run without recording.
- Two folders on a platform declaring the same dbt project `name:` now stop a recorded
  command, unless the folder already owns the name in the database (it runs, with a
  warning).
- dbt commands in platform mode now connect to the database before dbt starts: an
  unreachable server, a database without dbtx's tables (`dbtx db init`) or behind this
  version (`dbtx db upgrade`) exits 2.
- **Schema changes are deliberate, SQLite included.** Only `dbtx db init` (first use)
  and `dbtx db upgrade` (after upgrading dbtx) create or migrate dbtx's tables; no run
  does it implicitly. Run `dbtx db init` once on a new platform. The adapter attribute
  `upgrades_automatically` is gone (a plugin that still sets it is not migrated).
- **`dbtx docs serve` uses the database in platform mode** (backend enabled): it needs
  `DB_URI` and an initialised, current database, checked before the port is bound.
  `--db-backend disabled` or `--standalone-mode` serve the sidecar file as before.
- The docs overlay no longer shows "Last compiled" (every run compiles, and so does
  `dbtx compile` on its own).

### Added

- `dbtx docs serve` reads the run history from the database in platform mode: the
  overlay's `dbtx_runtime.json` is built from the stored runs (same shape as the
  sidecar), cached, and checked for new runs at most every 5 seconds, so a running
  server shows new runs without a restart. A database failure keeps the last document
  (one warning); before any, a 503. History that exists only in a sidecar is not shown.
- The overlay shows **Updated** when served from the database: when the version of the
  node a run used first appeared.

- `dbtx warm-cache`: refreshes a project's partial-parse cache in its own `target/`
  without running anything; every platform run copies it into its run folder.
- The project's `target/` is its docs site in platform mode: runs that execute nodes
  copy their rendered reports there and patch the site; `dbtx docs generate` copies the
  new site there (then run `dbtx docs install`); `dbtx docs install`, `status` and
  `serve` default `--docs-loc` to it.
- Run history: each node result stores the compiled SQL dbt ran and the relation it
  built or tested; each invocation stores its run folder (schema revision `0002`;
  SQLite stores migrate automatically, Postgres needs `dbtx db upgrade`).
- `read_job_id` (docs overlay, `dbtx db load`) reads `DBTX_JOB_ID` first, from dbt's
  recorded environment or `--vars`, then the legacy `job_id` var.

- Run history store: each recorded run stores its invocation (dbt's invocation id,
  command, times, result, target, args, job id, user, host, manifest hash), a result
  per node, the project's nodes, versions and edges over time, and the reports and
  their renders. Recording never changes dbt's output or exit code; a failed write is
  one warning.
- Bare `dbtx docs` prints dbtx's docs help anywhere, like `dbtx docs --help`.
