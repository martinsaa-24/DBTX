# Changelog

## Unreleased

### Breaking

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
  unreachable server, a Postgres database without dbtx's tables (`dbtx db init`) or
  behind this version (`dbtx db upgrade`) exits 2. A SQLite database is created and
  migrated automatically.

### Added

- Run history store: each recorded run stores its invocation (dbt's invocation id,
  command, times, result, target, args, job id, user, host, manifest hash), a result
  per node, the project's nodes, versions and edges over time, and the reports and
  their renders. Recording never changes dbt's output or exit code; a failed write is
  one warning.
- Bare `dbtx docs` prints dbtx's docs help anywhere, like `dbtx docs --help`.
