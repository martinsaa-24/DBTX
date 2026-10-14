# Using dbtx: platform mode and standalone mode

dbtx runs in one of two modes. **Platform mode** is the default: several dbt projects
under one platform root, sharing one configuration and one run-history database.
**Standalone mode** (`--standalone-mode`) runs a single project the way plain dbt
would, with dbtx's reports and docs features but no platform and no database.

What happens step by step when a command runs is in [execution.md](execution.md);
what each command does and does not do (run folder, recording, docs patching) is in
[commands.md](commands.md).

## Choosing the mode

The first rule that matches decides:

| # | When | Mode |
|---|---|---|
| 1 | `--standalone-mode` is on the command line | standalone |
| 2 | `--platform-dir DIR` is on the command line | platform, root `DIR` |
| 3 | the current directory holds `dbtx.conf` and is not itself a dbt project | platform, root = current directory |
| — | none of these | **exit 2**: "no dbtx platform found" |

`--help`, `--version`, bare `dbtx` and bare `dbtx docs` work anywhere.

## At a glance

| | Platform mode | Standalone mode |
|---|---|---|
| Typical invocation | `cd /srv/dbtx && dbtx build --project-dir jaffle-shop`, or `dbtx build --platform-dir /srv/dbtx --project-dir jaffle-shop` | `cd jaffle-shop && dbtx build --standalone-mode` |
| Config file | `<platform root>/dbtx.conf` (or `--config-file` / `DBTX_CONFIG_FILE`) | none |
| Options | CLI flag > `dbtx.conf` > `DBTX_*` environment variable | CLI flags only; `DBTX_*` variables are ignored |
| Project dir | a direct child folder of the platform root; a relative `--project-dir` is relative to the root | dbt's own lookup (`--project-dir`, `DBT_PROJECT_DIR`, then the current directory upwards) |
| Profiles | always the project folder's own `profiles.yml` (dbtx passes `--profiles-dir`); any other `--profiles-dir` / `DBT_PROFILES_DIR` → exit 2 | dbt's own lookup (`~/.dbt`, ...) |
| Run history database | required: `DB_URI` (or `--db-backend disabled`) | none |
| Project-name guard | one dbt `name:` per project folder, on this platform and in the database | none |
| Run outputs | an isolated folder per run under `LOG_ROOT_LOCATION` (required) | the project's `target/` and `logs/`, as plain dbt (`--log-root-location` → exit 2) |
| Job id | every run has one: `--job-id ID`, or dbtx generates it | none, unless you pass `--vars '{DBTX_JOB_ID: ...}'` |
| Docs site | the project's target folder (`<project>/target/` by default), kept current after every run | wherever you point `--docs-loc` |
| `dbtx db ...` | available | exit 2 (needs platform mode) |

## Options

| Option | CLI | `dbtx.conf` | Environment | Platform | Standalone |
|---|---|---|---|---|---|
| Database backend | `--db-backend enabled\|disabled` | `DB_BACKEND=` | `DBTX_DB_BACKEND` | default `enabled` | CLI flag → exit 2 |
| Database URI | `--db-uri URI` | `DB_URI=` | `DBTX_DB_URI` | required while the backend is enabled | CLI flag → exit 2 |
| Postgres schema | `--db-schema NAME` | `DB_SCHEMA=` | `DBTX_DB_SCHEMA` | default `dbtx` | CLI flag → exit 2 |
| Store adapter | `--db-adapter NAME` | `DB_ADAPTER=` | `DBTX_DB_ADAPTER` | default: the one for `DB_URI` | CLI flag → exit 2 |
| Run folder root | `--log-root-location DIR` | `LOG_ROOT_LOCATION=` | `DBTX_LOG_ROOT_LOCATION` | **required** (exit 2 without it); relative paths are relative to the layer that set it (the config file's folder, else the current directory) | CLI flag → exit 2 |
| Job id | `--job-id ID` | — | — | optional; generated when absent | CLI flag → exit 2 |
| Config file | `--config-file PATH` | — | `DBTX_CONFIG_FILE` | default `<root>/dbtx.conf` | CLI flag → exit 2 |
| Platform root | `--platform-dir DIR` | — | — | | contradicts `--standalone-mode` |

A platform's `dbtx.conf` should set at least:

```ini
DB_URI=sqlite:///.dbtx/dbtx.db
LOG_ROOT_LOCATION=.dbtx/logs
```

Then create dbtx's tables once with `dbtx db init`, and after upgrading dbtx run
`dbtx db upgrade`. No other command changes the database's schema, on any backend.

## Commands by mode

| Command | Platform mode | Standalone mode |
|---|---|---|
| `dbtx build`, `compile` | recorded; runs in its run folder; renders reports | not recorded; `target/` |
| `dbtx run`, `test`, `seed`, `snapshot`, `run-operation`, `source freshness`, `docs generate` (passed to dbt) | recorded; run folder | plain dbt |
| `dbtx parse` | recorded | plain dbt |
| `dbtx warm-cache` | refreshes the project's partial-parse cache in its target folder, without running anything | not available |
| `dbtx ls`, `debug`, `deps`, `show`, `clean`, ... | plain dbt, not recorded | plain dbt |
| `dbtx docs patch` | explicit `--docs-loc` and `--run-loc`; also run automatically after each run | explicit `--docs-loc` and `--run-loc` |
| `dbtx docs install`, `status`, `serve` | `--docs-loc` defaults to the project's target folder | `--docs-loc` required |
| `dbtx templates` | the project's templates | the project's templates |
| `dbtx config` | mode, root, project, config file, every option and its source | mode and the ignored `DBTX_*` variables |
| `dbtx db init`, `upgrade`, `status`, `load`, `projects` | available | exit 2 |

## Job ids

- **Platform mode:** every invocation has a unique job id, retries included. Pass
  `--job-id ID`, or dbtx generates one. dbtx hands it to dbt as the environment variable
  `DBT_ENV_CUSTOM_ENV_DBTX_JOB_ID`, so dbt records it in `run_results.json` (as
  `DBTX_JOB_ID`) without invalidating the partial-parse cache. `DBTX_JOB_ID` in `--vars`
  is an error (exit 2). A run whose folder already exists is refused (exit 2): this
  catches a reused job id within the same hour.
- **Standalone mode:** dbtx sets no job id. You may pass one as
  `--vars '{DBTX_JOB_ID: ...}'`; `dbtx docs patch` reads it from the run's artifacts.
- Keep `env_var('DBT_ENV_CUSTOM_ENV_DBTX_JOB_ID')` out of `dbt_project.yml` and
  `profiles.yml`: there, dbt discards its whole partial-parse cache whenever the value
  changes, so every run would parse from scratch. In models and YAML files it is fine
  (dbt re-parses only the files that use it).

## Where things are written

| | Platform mode | Standalone mode |
|---|---|---|
| A run's artifacts (`manifest.json`, `run_results.json`, `compiled/`, `run/`, `reports/`) and dbt's log | `<LOG_ROOT_LOCATION>/<project name>/<date>/<hour>/<job id>/` (date and hour in UTC) | `<project>/target/` and `<project>/logs/` |
| Partial-parse cache | `<project>/target/partial_parse.msgpack`, refreshed by `dbtx warm-cache`, copied into each run folder | `<project>/target/` |
| Docs site and its run-history sidecar | `<project>/target/` | `--docs-loc` |
| Run history | the database (`DB_URI`), including each run's folder | none |

**Run folders are never deleted** (a design impact, accepted): every run keeps its own
artifacts and log, so disk use under `LOG_ROOT_LOCATION` grows with every run.
`dbtx clean` / `dbt clean` remove only the project's target folder (the docs site and
the warm cache), never a run folder. Prune old ones yourself, for example by date
folder.
